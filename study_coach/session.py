"""交互式会话：双击启动之后的那个菜单和复习流程。

cli.py 是"一条命令做一件事"，适合脚本化；这个文件负责"人坐在电脑前用它"，
所以它需要循环、需要提示、需要输入校验。
两者共用同一套 repo 层，逻辑不会跑偏。
"""

from __future__ import annotations

import sqlite3

from study_coach import db, repo
from study_coach.cli import DEFAULT_MINUTES_PER_CARD, card_limit_from_profile, hr
from study_coach.sm2 import SM2State, preview

RATING_HELP = """\
     5 完美满分      4 答对了但犹豫      3 答对了但很吃力
     2 错了但想得起来  1 错了但有印象      0 完全不会"""


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------
def _say(text: str = "", **kwargs) -> None:
    print(text, **kwargs)


def _ask(prompt: str) -> str:
    """读一行输入。遇到 EOF（比如输出被重定向）就当成想退出。"""
    try:
        return input(prompt)
    except EOFError:
        _say()
        raise KeyboardInterrupt from None


def _ask_rating() -> int | None:
    """问一次评分。返回 0-5；用户想退出就返回 None。"""
    while True:
        raw = _ask("  给这次表现打分（0-5，q 退出）：").strip().lower()
        if raw in {"q", "quit", "exit", "退出"}:
            return None
        if raw.isdigit() and 0 <= int(raw) <= 5:
            return int(raw)
        _say("  ↑ 只能填 0 到 5 之间的一个数字。")


def _pause(prompt: str = "  按回车回到菜单...") -> None:
    _ask(prompt)


# --------------------------------------------------------------------------
# 开始复习
# --------------------------------------------------------------------------
def interactive_review(conn: sqlite3.Connection) -> None:
    """逐张过卡：先看题 → 自己回忆 → 看答案 → 打分。"""
    profile = repo.get_profile(conn)
    limit = card_limit_from_profile(profile)
    cards = repo.due_cards(conn, limit=limit)

    hr()
    _say(f"开始复习 · {repo.today()}")
    hr()
    if not cards:
        _say("  今天没有到期的卡片，休息一下吧。")
        _say()
        _pause()
        return

    if limit:
        _say(f"  画像约束：每天 {profile.get('daily_minutes')} 分钟 ≈ {limit} 张")
    _say(f"  这一轮共 {len(cards)} 张。中途想停，打分时输入 q。")
    _say()
    _say("  评分标准：")
    _say(RATING_HELP)
    _pause("\n  准备好了按回车开始...")

    done = 0
    right = 0
    for i, row in enumerate(cards, start=1):
        _say()
        hr()
        _say(f"  第 {i} / {len(cards)} 张   [{row['subject']} · {row['knowledge_point']}]")
        hr()
        _say(f"  {row['question']}")
        _say()
        _ask("  先自己回忆一遍，想好了按回车看答案...")
        _say()
        _say(f"  答案：{row['answer']}")
        if row["source"]:
            _say(f"  来源：{row['source']}")
        _say()

        rating = _ask_rating()
        if rating is None:
            _say("\n  好的，先停在这。已经记录过的不会丢。")
            break

        result = repo.apply_review(conn, card_id=int(row["id"]), rating=rating)
        before, after = result["before"], result["after"]
        done += 1
        right += 1 if rating >= 3 else 0

        if rating >= 3:
            _say(f"  ✓ 间隔 {before.interval_days} 天 → {after.interval_days} 天，"
                 f"下次 {result['next_due']}")
        else:
            _say(f"  ✗ 答错，间隔重置为 {after.interval_days} 天，这张卡会重新来过。")

    _say()
    hr()
    if done:
        _say(f"  本轮完成 {done} 张，答对 {right} 张（{right / done:.0%}）")
    else:
        _say("  这轮一张都没记。")
    _say(f"  今天还剩 {len(repo.due_cards(conn, limit=None))} 张到期。")
    hr()
    _pause()


# --------------------------------------------------------------------------
# 预览某张卡的间隔增长
# --------------------------------------------------------------------------
def interactive_preview(conn: sqlite3.Connection) -> None:
    rows = list(
        conn.execute(
            """
            SELECT c.id, c.question, c.knowledge_point, s.name AS subject,
                   st.ef, st.interval_days, st.repetitions
            FROM cards c
            JOIN subjects s    ON s.id = c.subject_id
            JOIN card_state st ON st.card_id = c.id
            ORDER BY c.id
            """
        )
    )
    if not rows:
        _say("  还没有卡片，先回菜单建库。")
        _pause()
        return

    hr()
    _say(f"  共有 {len(rows)} 张卡。输入编号看它的间隔增长（q 返回）")
    hr()
    for i, r in enumerate(rows, start=1):
        mark = "  " if r["repetitions"] else "· "
        _say(f"  {mark}{i:>3}. [{r['knowledge_point']}] {r['question'][:34]}")
    _say()
    _say("  带 · 的是还没复习过的新卡。")
    _say()

    while True:
        raw = _ask("  编号（q 返回）：").strip().lower()
        if raw in {"q", "quit", "exit", "退出", ""}:
            return
        if not raw.isdigit() or not (1 <= int(raw) <= len(rows)):
            _say(f"  ↑ 请输入 1 到 {len(rows)} 之间的编号。")
            continue

        r = rows[int(raw) - 1]
        state = SM2State(
            ef=float(r["ef"]),
            interval_days=int(r["interval_days"]),
            repetitions=int(r["repetitions"]),
        )
        _say()
        hr()
        _say(f"  [{r['subject']} · {r['knowledge_point']}]")
        _say(f"  {r['question']}")
        hr()
        _say(f"  当前：EF {state.ef:.2f} · 间隔 {state.interval_days} 天 "
             f"· 连对 {state.repetitions} 次")
        _say()
        _say("  如果以后每次都答满分，间隔会这样长：")
        for step, interval in preview(state):
            bar = "█" * min(40, max(1, interval // 5))
            _say(f"    第 {step} 次 → {interval:>5} 天  {bar}")
        _say()


# --------------------------------------------------------------------------
# 主菜单
# --------------------------------------------------------------------------
def _show_due_brief(conn: sqlite3.Connection) -> None:
    profile = repo.get_profile(conn)
    limit = card_limit_from_profile(profile)
    due = repo.due_cards(conn, limit=limit)
    total = len(repo.due_cards(conn, limit=None))
    overdue = repo.overdue_count(conn)

    if not due and not total:
        _say("  今天没有到期的卡片。")
        return
    _say(f"  今天到期 {total} 张", end="")
    if limit and total > limit:
        _say(f"（按每天 {profile.get('daily_minutes')} 分钟，本轮到 {len(due)} 张）")
    else:
        _say()
    if overdue:
        _say(f"  其中有 {overdue} 张已经逾期")
    _say()
    for i, row in enumerate(due[:5], start=1):
        _say(f"    {i}. [{row['knowledge_point']}] {row['question'][:40]}")
    if len(due) > 5:
        _say(f"    ... 还有 {len(due) - 5} 张")


def main_menu(conn: sqlite3.Connection) -> None:
    """主循环。返回即退出程序。"""
    actions = {
        "1": ("开始复习（逐张过卡）", interactive_review),
        "2": ("各科统计与薄弱点", lambda c: _stats_and_pause(c)),
        "3": ("未来 7 天复习负载", lambda c: _forecast_and_pause(c)),
        "4": ("我的个人画像", lambda c: _profile_and_pause(c)),
        "5": ("看某张卡未来的间隔", interactive_preview),
    }

    while True:
        _say()
        hr("═")
        _say("   study-coach · 备考助手")
        hr("═")
        _show_due_brief(conn)
        _say()
        for key, (label, _) in actions.items():
            _say(f"   {key}  {label}")
        _say("   0  退出")
        _say()

        choice = _ask("  请选择：").strip()
        if choice in {"0", "q", "quit", "exit", "退出"}:
            _say("\n  下次见。\n")
            return
        item = actions.get(choice)
        if item is None:
            _say("  ↑ 请输入 0-5 之间的序号。")
            continue
        _say()
        item[1](conn)


# --------------------------------------------------------------------------
# 菜单里的几个"看一眼就走"的功能
# --------------------------------------------------------------------------
def _stats_and_pause(conn: sqlite3.Connection) -> None:
    stats = repo.subject_stats(conn)
    hr()
    _say("  各科统计")
    hr()
    if not stats:
        _say("  还没有数据。")
        _pause()
        return
    _say(f"  {'科目':<10}{'卡片':>5}{'今天到期':>9}{'平均间隔':>9}{'掌握度':>8}{'累计答错':>9}")
    _say("  " + "-" * 54)
    for s in stats:
        _say(f"  {s['subject']:<10}{s['total']:>5}{s['due_now']:>9}"
             f"{s['avg_interval']:>9.1f}{s['mastery']:>8.2f}{s['lapses']:>9}")
    _say()
    _say("  掌握度 = 1 / (1 + 平均间隔)，间隔越长越接近 1。")
    if stats[0]["exam_date"]:
        _say(f"  考试日期：{stats[0]['exam_date']}")

    weak = repo.weak_points(conn)
    if weak:
        _say()
        _say("  薄弱知识点（按答错次数）：")
        for w in weak:
            _say(f"    · {w['knowledge_point']:<14} 复习 {w['reviews']} 次，错 {w['wrongs']} 次")
    else:
        _say()
        _say("  还没有复习记录，看不出薄弱点。")
    _say()
    _pause()


def _forecast_and_pause(conn: sqlite3.Connection) -> None:
    rows = repo.forecast(conn, days=7)
    hr()
    _say("  未来 7 天复习负载")
    hr()
    busiest = max((n for _, n in rows), default=0)
    for day, n in rows:
        bar = "█" * (round(n / busiest * 34) if busiest else 0)
        _say(f"  {day}  {n:>4} 张  {bar}")
    _say()
    total = sum(n for _, n in rows)
    _say(f"  合计 {total} 张。")
    if busiest and busiest > total / len(rows) * 2:
        _say("  中间有一天明显偏多，可以考虑提前分摊。")
    _say()
    _pause()


def _profile_and_pause(conn: sqlite3.Connection) -> None:
    profile = repo.get_profile(conn)
    hr()
    _say("  我的个人画像")
    hr()
    if not profile:
        _say("  （还没有画像）")
    for k, v in profile.items():
        _say(f"  {k:<16} {v}")
    limit = card_limit_from_profile(profile)
    if limit:
        _say()
        _say(f"  由「每天可用 {profile.get('daily_minutes')} 分钟」推出："
             f"本轮最多 {limit} 张")
        _say(f"  （按每张 {DEFAULT_MINUTES_PER_CARD} 分钟估算）")
    _say()
    _say("  画像字段是有限且确定的，所以直接查表，不走向量检索——")
    _say("  这样既快又不会因为相似度差一点就漏掉。")
    _say()
    _pause()


def run(db_path=None) -> None:
    """入口：建好连接，进菜单，退出时关掉。"""
    conn = db.connect(db_path or db.DEFAULT_DB)
    db.init_db(conn)
    try:
        main_menu(conn)
    except KeyboardInterrupt:
        _say("\n\n  已退出。\n")
    finally:
        conn.close()
