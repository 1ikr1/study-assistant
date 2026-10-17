"""界面要用的数据，统一在这里组装。

为什么单独一个文件：个人学习助手 现在有四个壳——

    cli.py      一条命令做一件事
    session.py  终端里跑菜单
    web.py      网页服务器 + 浏览器
    desktop.py  原生窗口（tkinter）

如果每个壳都自己写一遍"今天该复习什么""统计怎么算"，迟早会算得不一样。
所以凡是"取数据"的活都放这儿，壳只负责把拿到的数据画出来。

注意这里**不包含任何业务规则**——真正的规则（间隔怎么算、什么算答对）
在 sm2.py 和 repo.py 里。这一层只做"翻译"：把数据库的行整理成界面好用的形状。
"""

from __future__ import annotations

import sqlite3
from datetime import date

from study_assistant import repo
from study_assistant.sm2 import SM2State, preview

# 每张卡平均花多少分钟（回忆 + 看答案 + 判断）。
# 用来把画像里的"每天多少分钟"换算成"今天最多几张"。
MINUTES_PER_CARD = 1.5


def limit_from(profile: dict[str, str]) -> int | None:
    """画像里的"每天可用时间" → 今天最多复习几张。

    这就是"画像直接参与计算"的落地——不走检索，直接读字段。
    """
    raw = profile.get("daily_minutes")
    if not raw:
        return None
    try:
        minutes = float(raw)
    except ValueError:
        return None
    return max(1, int(minutes / MINUTES_PER_CARD))


def reviewed_today(conn: sqlite3.Connection) -> int:
    """今天已经复习过多少次。"""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM review_logs WHERE reviewed_at = ?", (repo.today(),)
    ).fetchone()
    return int(row["n"])


def state(conn: sqlite3.Connection) -> dict:
    """顶栏要用的东西：今天几号、到期多少张、画像约束是什么。"""
    profile = repo.get_profile(conn)
    return {
        "today": repo.today(),
        "due_total": len(repo.due_cards(conn, limit=None)),
        "overdue": repo.overdue_count(conn),
        "limit": limit_from(profile),
        "profile": profile,
        "reviewed_today": reviewed_today(conn),
        "total_cards": repo.count_cards(conn),
    }


def queue(conn: sqlite3.Connection) -> list[dict]:
    """今天该复习的卡片列表（已经按画像的时间预算截断过）。"""
    rows = repo.due_cards(conn, limit=limit_from(repo.get_profile(conn)))
    return [
        {
            "id": int(r["id"]),
            "subject": r["subject"],
            "knowledge_point": r["knowledge_point"],
            "question": r["question"],
            "answer": r["answer"],
            "source": r["source"],
            "ef": round(float(r["ef"]), 2),
            "interval_days": int(r["interval_days"]),
            "repetitions": int(r["repetitions"]),
            "lapses": int(r["lapses"]),
            "due_at": r["due_at"],
        }
        for r in rows
    ]


def stats(conn: sqlite3.Connection) -> dict:
    return {"subjects": repo.subject_stats(conn), "weak": repo.weak_points(conn)}


def forecast(conn: sqlite3.Connection, days: int) -> list[dict]:
    return [{"date": d, "count": n} for d, n in repo.forecast(conn, days=days)]


def card_preview(conn: sqlite3.Connection, card_id: int) -> dict:
    """某张卡未来的间隔增长。纯计算，不写数据库。"""
    row = conn.execute(
        "SELECT st.ef, st.interval_days, st.repetitions, c.question, c.knowledge_point "
        "FROM card_state st JOIN cards c ON c.id = st.card_id WHERE st.card_id = ?",
        (card_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"卡片不存在：{card_id}")
    st = SM2State(
        ef=float(row["ef"]),
        interval_days=int(row["interval_days"]),
        repetitions=int(row["repetitions"]),
    )
    return {
        "knowledge_point": row["knowledge_point"],
        "question": row["question"],
        "current": {"ef": round(st.ef, 2), "interval_days": st.interval_days,
                    "repetitions": st.repetitions},
        "steps": [{"round": s, "days": d} for s, d in preview(st)],
    }


def staged(conn: sqlite3.Connection) -> list[dict]:
    return [
        {
            "id": int(r["id"]),
            "card_id": int(r["card_id"]),
            "rating": int(r["rating"]),
            "created_at": r["created_at"],
            "question": r["question"],
        }
        for r in repo.pending_staged(conn)
    ]


def card_rows(
    conn: sqlite3.Connection,
    *,
    subject_id: int | None = None,
    query: str = "",
) -> list[dict]:
    """卡片库列表（已经整理成界面好用的形状）。"""
    out = []
    for r in repo.list_cards(conn, subject_id=subject_id, query=query):
        item = dict(r)
        item["ef"] = round(float(item["ef"]), 2)
        out.append(item)
    return out


def subject_rows(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in repo.list_subjects(conn)]


def review_result(result: dict) -> dict:
    """把 repo.apply_review 的返回值整理成界面好显示的形状。"""
    before: SM2State = result["before"]
    after: SM2State = result["after"]
    return {
        "before": {"ef": round(before.ef, 2), "interval_days": before.interval_days,
                   "repetitions": before.repetitions},
        "after": {"ef": round(after.ef, 2), "interval_days": after.interval_days,
                  "repetitions": after.repetitions},
        "next_due": result["next_due"],
        "elapsed_days": result["elapsed_days"],
        "lapses": result["lapses"],
        "note": note_for(after.interval_days),
    }


def note_for(days: int) -> str:
    """给用户看的一句话反馈。"""
    if days <= 1:
        return "这张卡明天还会出现"
    if days < 30:
        return f"{days} 天后再见"
    return f"记得很牢，{days} 天后再见"


# --------------------------------------------------------------------------
# 今日行动看板
# --------------------------------------------------------------------------
def tasks(conn: sqlite3.Connection, day: str | None = None) -> list[dict]:
    return [dict(r) for r in repo.tasks_on(conn, day)]


def dashboard(conn: sqlite3.Connection) -> dict:
    """「今日行动」要显示的所有东西，一次查完。

    为什么不拆成五个接口让前端分别请求：这些数字之间是有关系的
    （比如"专注目标"来自画像、"剩余题量"来自复习队列），
    分五次拿会出现"上半屏是旧数据、下半屏是新数据"的错位。
    """
    profile = repo.get_profile(conn)
    subjects = repo.subject_stats(conn)
    exam = subjects[0]["exam_date"] if subjects else None

    countdown: int | None = None
    if exam:
        try:
            countdown = (date.fromisoformat(exam) - date.today()).days
        except ValueError:
            countdown = None

    done, total = repo.task_progress(conn)
    focus_today = repo.focus_seconds(conn)
    try:
        focus_target = int(float(profile.get("daily_minutes", "0")) * 60)
    except ValueError:
        focus_target = 0

    due = len(repo.due_cards(conn, limit=None))
    mastery = subjects[0]["mastery"] if subjects else 0.0

    return {
        "today": repo.today(),
        "exam_date": exam,
        "countdown_days": countdown,
        "tasks": tasks(conn),
        "tasks_done": done,
        "tasks_total": total,
        "focus_seconds_today": focus_today,
        "focus_target_seconds": focus_target,
        "focus_7d": [{"date": d, "seconds": s}
                     for d, s in repo.focus_by_day(conn, days=7)],
        "due_total": due,
        "due_now": len(repo.due_cards(conn, limit=limit_from(profile))),
        "reviewed_today": reviewed_today(conn),
        "mastery": mastery,
        "total_cards": repo.count_cards(conn),
        "overdue": repo.overdue_count(conn),
        "profile": profile,
    }
