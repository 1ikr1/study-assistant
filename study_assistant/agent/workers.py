"""子 Agent 的实现。

分成两类，界线很清楚：

    纯代码（kind="code"）    scheduler / stats   —— 算数的事，模型碰不得
    模型   （kind="model"）   knowledge / report   —— 说话的事，代码干不了

这样分的好处是面试时能讲清楚："为什么这两个用代码？因为它们算的是确定性的量，
模型只会把它算错。" 而不是"因为我只会这么写"。

每个 worker 的签名统一为 run(conn, client, args, upstream) -> str。
upstream 只包含**它声明依赖的那几步**的结果，不是全部历史 —— 这就是
Context Engineering：子 Agent 不该看见跟自己无关的东西。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Callable

from study_assistant import coach, repo
from study_assistant.llm import ChatClient, Message

# 每个 worker 的输出上限。超过就截断 —— 不是怕写不下，
# 是怕后面那个模型被一堆无关细节淹没，还多烧 token。
MAX_OUTPUT_CHARS = 1200


@dataclass(frozen=True)
class Worker:
    name: str
    kind: str            # "code" | "model"
    description: str
    run: Callable[[sqlite3.Connection, ChatClient, dict, dict[str, str]], str]


def _clip(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "……（已截断）"


def _minutes_budget(conn: sqlite3.Connection, args: dict) -> int:
    """这次能花多少分钟。优先用任务里给的，其次用画像。"""
    raw = args.get("minutes")
    if raw is not None:
        try:
            return max(5, int(float(raw)))
        except (TypeError, ValueError):
            pass
    profile = repo.get_profile(conn)
    try:
        return max(5, int(float(profile.get("daily_minutes", "60"))))
    except ValueError:
        return 60


# --------------------------------------------------------------------------
# 纯代码：排复习
# --------------------------------------------------------------------------
def run_scheduler(conn, client, args, upstream) -> str:
    """按时间预算排出"现在先做什么"。全部是确定性计算。"""
    minutes = _minutes_budget(conn, args)
    per_card = 1.5
    limit = max(1, int(minutes / per_card))

    all_due = repo.due_cards(conn, limit=None)
    picked = repo.due_cards(conn, limit=limit)
    tasks = repo.tasks_on(conn)
    done, total = repo.task_progress(conn)

    lines = [f"时间预算：{minutes} 分钟（约 {limit} 张卡）",
             f"到期卡片：{len(all_due)} 张，本次建议做 {len(picked)} 张"]
    if all_due:
        overdue = repo.overdue_count(conn)
        if overdue:
            lines.append(f"其中逾期 {overdue} 张，优先")
        lines.append("建议顺序（逾期最久的在前）：")
        for i, row in enumerate(picked[:8], 1):
            lines.append(f"  {i}. [{row['knowledge_point']}] {row['question']}"
                         f"（间隔 {row['interval_days']} 天，EF {float(row['ef']):.2f}）")
    else:
        lines.append("今天没有到期卡片，可以专注别的事情。")

    pending = [t for t in tasks if t["status"] != "done"]
    lines.append(f"今日任务：{done}/{total} 完成，还剩 {len(pending)} 条")
    for t in pending[:5]:
        lines.append(f"  · {t['title']}（{t['minutes']} 分钟）")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 纯代码：统计
# --------------------------------------------------------------------------
def run_stats(conn, client, args, upstream) -> str:
    """掌握度、薄弱点、未来负载。全是 SQL 聚合。"""
    try:
        days = max(1, min(30, int(args.get("days") or 7)))
    except (TypeError, ValueError):
        days = 7

    subjects = repo.subject_stats(conn)
    weak = repo.weak_points(conn)
    forecast = repo.forecast(conn, days=days)
    reviewed = conn.execute(
        "SELECT COUNT(*) AS n FROM review_logs WHERE reviewed_at = ?",
        (repo.today(),)).fetchone()["n"]

    lines = [f"今日已复习：{reviewed} 次"]
    for s in subjects:
        lines.append(
            f"科目 {s['subject']}：{s['total']} 张卡，今天到期 {s['due_now']}，"
            f"平均间隔 {s['avg_interval']} 天，掌握度 {s['mastery']}，累计答错 {s['lapses']}")

    if weak:
        lines.append("薄弱知识点（按答错次数）：")
        for w in weak[:5]:
            lines.append(f"  · {w['knowledge_point']}：复习 {w['reviews']} 次，错 {w['wrongs']} 次")
    else:
        lines.append("还没有复习记录，看不出薄弱点。")

    lines.append(f"未来 {days} 天到期分布：")
    lines.append("  " + "，".join(f"{d[5:]} {n} 张" for d, n in forecast))
    peak = max((n for _, n in forecast), default=0)
    if peak:
        lines.append(f"峰值 {peak} 张/天。")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 模型：讲题
# --------------------------------------------------------------------------
def run_knowledge(conn, client, args, upstream) -> str:
    """讲一个知识点。指定了 card_id 就走已有的 coach，否则自由提问。"""
    card_id = args.get("card_id")
    if card_id is not None:
        try:
            result = coach.explain(
                conn, client, int(card_id), rating=int(args.get("rating", 0)))
            return _clip(result.text)
        except (LookupError, TypeError, ValueError) as exc:
            return f"讲题失败：{exc}"

    question = str(args.get("question") or "").strip()
    if not question:
        return "没有指定要讲的知识点。"
    messages = [
        Message("system",
                "你是一位考研辅导老师。用 200 字以内、口语化地讲清楚这个概念，"
                "最好给一个例子。只讲你确定的内容，不确定就说不确定，不要编。"),
        Message("user", question),
    ]
    return _clip(client.chat(messages, temperature=0.6, max_tokens=500))


# --------------------------------------------------------------------------
# 模型：把材料写成话
# --------------------------------------------------------------------------
def run_report(conn, client, args, upstream) -> str:
    """把前面几步的结果组织成一段能直接看的回答。"""
    question = str(args.get("question") or "").strip()
    if not upstream:
        return "没有可汇总的材料。"

    blocks = []
    for key, value in upstream.items():
        blocks.append(f"【{key}】\n{_clip(value, 800)}")

    messages = [
        Message("system",
                "你是学习助手。根据给你的材料回答用户的问题。\n"
                "要求：\n"
                "1. 只用材料里的数字和事实，不要自己编新的数据。\n"
                "2. 如果材料不足以回答，直接说缺什么。\n"
                "3. 中文，条理清楚，300 字以内。不要用 Markdown 标题。"),
        Message("user", f"用户的问题：{question}\n\n材料：\n" + "\n\n".join(blocks)),
    ]
    return _clip(client.chat(messages, temperature=0.5, max_tokens=700), 2000)


# --------------------------------------------------------------------------
REGISTRY: dict[str, Worker] = {
    w.name: w for w in (
        Worker("scheduler", "code", "按时间预算排今天该复习哪些卡、还剩哪些任务", run_scheduler),
        Worker("stats", "code", "各科掌握度、薄弱知识点、未来几天到期分布", run_stats),
        Worker("knowledge", "model", "讲清楚一个知识点或一道错题", run_knowledge),
        Worker("report", "model", "把前面的材料组织成最终回答", run_report),
    )
}


def resolve(name: str) -> Worker:
    if name not in REGISTRY:
        raise KeyError(f"不认识的子 Agent：{name}")
    return REGISTRY[name]


def catalog() -> str:
    """给 Supervisor 看的工具清单。它靠这段文字决定拆成什么。"""
    return "\n".join(
        f"- {w.name}（{w.kind}）：{w.description}" for w in REGISTRY.values())
