"""命令行入口。

Phase 1 的"成品"就是这个 CLI —— 它已经是一个能用的备考工具了，
只是还没有大模型和界面。

用法：
    python -m study_assistant.cli menu          打开交互式菜单（双击启动用这条）
    python -m study_assistant.cli init          建库并导入示例卡片
    python -m study_assistant.cli due           今天该复习什么
    python -m study_assistant.cli answer 3      看第 3 张的答案
    python -m study_assistant.cli review 3 4    给第 3 张打 4 分
    python -m study_assistant.cli preview 3     看这张卡未来的复习间隔
    python -m study_assistant.cli stage 3 4     先暂存打分（不落库），等人确认
    python -m study_assistant.cli staged        看暂存区里有什么
    python -m study_assistant.cli confirm 1     确认第 1 条暂存，真正生效
    python -m study_assistant.cli stats         各科统计
    python -m study_assistant.cli forecast      未来 7 天负载
    python -m study_assistant.cli profile       查看画像
"""

from __future__ import annotations

import argparse
import sqlite3
import sys

from study_assistant import config, db, repo, seed
from study_assistant.sm2 import SM2State, preview

# Windows 终端默认可能是 GBK，强制 UTF-8 避免中文报错
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 每张卡平均花多少分钟（回忆 + 看答案 + 判断）。用来把画像里的时间换算成题量。
DEFAULT_MINUTES_PER_CARD = 1.5


def hr(char: str = "─", width: int = 72) -> None:
    print(char * width)


def card_limit_from_profile(profile: dict[str, str]) -> int | None:
    """根据画像里的"每天可用时间"，算出今天最多复习多少张。

    这就是"画像直接参与计算"的落地——不走检索，直接读字段。
    """
    raw = profile.get("daily_minutes")
    if not raw:
        return None
    try:
        minutes = float(raw)
    except ValueError:
        return None
    return max(1, int(minutes / DEFAULT_MINUTES_PER_CARD))


# --------------------------------------------------------------------------
# init
# --------------------------------------------------------------------------
def cmd_init(args) -> None:
    conn = db.connect(args.db)
    db.init_db(conn)
    added, added_tasks = seed.bootstrap(conn)
    env_path = config.write_env_template()

    hr()
    print("初始化完成")
    hr()
    print(f"  数据库    : {args.db}")
    print(f"  新增卡片  : {added}")
    print(f"  卡片总数  : {repo.count_cards(conn)}")
    print(f"  今日任务  : {added_tasks} 条")
    print(f"  配置文件  : {env_path}")
    if not config.llm_configured():
        print()
        print("  提醒：「让 AI 讲一遍」需要 API key。")
        print(f"        打开 {env_path}，把 DEEPSEEK_API_KEY= 后面填上就行。")
        print("        这个文件不会被提交（.gitignore 里排除了）。")
    print()
    print("下一步：")
    print("  python -m study_assistant.cli due      # 看看今天该复习什么")
    conn.close()


# --------------------------------------------------------------------------
# due
# --------------------------------------------------------------------------
def cmd_due(args) -> None:
    conn = db.connect(args.db)
    db.init_db(conn)
    profile = repo.get_profile(conn)
    limit = card_limit_from_profile(profile)

    all_due = repo.due_cards(conn, limit=None)
    shown = all_due[:limit] if limit else all_due

    hr()
    print(f"今天该复习 · {repo.today()}")
    hr()
    if limit:
        minutes = profile.get("daily_minutes", "?")
        print(f"  画像约束：每天可用 {minutes} 分钟")
        print(f"            ≈ {limit} 张（按每张 {DEFAULT_MINUTES_PER_CARD} 分钟估算）")
    print(f"  到期卡片：{len(all_due)} 张", end="")
    if limit and len(all_due) > limit:
        print(f"  → 按优先级取前 {len(shown)} 张")
    else:
        print()
    overdue = repo.overdue_count(conn)
    if overdue:
        print(f"  其中逾期：{overdue} 张")
    print()

    if not shown:
        print("  （没有到期的卡片。休息一下吧。）")
        conn.close()
        return

    for i, row in enumerate(shown, start=1):
        print(f"  {i:>3}. [{row['subject']} · {row['knowledge_point']}]")
        print(f"       {row['question']}")
        print(f"       间隔 {row['interval_days']} 天 · EF {row['ef']:.2f} · 答错 {row['lapses']} 次")
        print()

    print("给某张打分的命令：")
    print("  python -m study_assistant.cli review <序号> <评分0-5>")
    conn.close()


# --------------------------------------------------------------------------
# answer
# --------------------------------------------------------------------------
def cmd_answer(args) -> None:
    conn = db.connect(args.db)
    rows = _due_with_index(conn)
    row = rows.get(args.index)
    if row is None:
        print(f"今天没有第 {args.index} 张到期的卡片。先跑 `due` 看看。")
        conn.close()
        return
    hr()
    print(f"[{row['subject']} · {row['knowledge_point']}]")
    hr()
    print(f"问题：{row['question']}")
    print()
    print(f"答案：{row['answer']}")
    print()
    print(f"来源：{row['source'] or '（未标注）'}")
    hr()
    print("现在给自己打个分，然后记录：")
    print(f"  python -m study_assistant.cli review {args.index} <评分0-5>")
    print()
    print("  5 完美   4 正确但犹豫   3 正确但吃力")
    print("  2 错了但想起来   1 错了但记得   0 完全不会")
    conn.close()


# --------------------------------------------------------------------------
# review
# --------------------------------------------------------------------------
def cmd_review(args) -> None:
    if not 0 <= args.rating <= 5:
        print("评分必须是 0-5 的整数。")
        return

    conn = db.connect(args.db)
    rows = _due_with_index(conn)
    row = rows.get(args.index)
    if row is None:
        print(f"今天没有第 {args.index} 张到期的卡片。")
        conn.close()
        return

    result = repo.apply_review(conn, card_id=int(row["id"]), rating=args.rating)
    after: SM2State = result["after"]

    hr()
    print(f"已记录 · [{row['knowledge_point']}]  评分 {args.rating}")
    hr()
    print(f"  距上次复习   : {result['elapsed_days']} 天")
    print(f"  间隔         : {result['before'].interval_days} 天 → {after.interval_days} 天")
    print(f"  EF（难度）   : {result['before'].ef:.2f} → {after.ef:.2f}")
    print(f"  下次复习     : {result['next_due']}")
    print()
    if args.rating < 3:
        print("  答错了，间隔已重置为 1 天。这张卡会重新来过。")
    elif after.interval_days >= 30:
        print("  记得很牢，间隔已经拉到 30 天以上。")
    conn.close()


# --------------------------------------------------------------------------
# preview
# --------------------------------------------------------------------------
def cmd_preview(args) -> None:
    conn = db.connect(args.db)
    rows = _all_with_index(conn)
    row = rows.get(args.index)
    if row is None:
        print(f"没有第 {args.index} 张卡片。")
        conn.close()
        return

    state = SM2State(
        ef=float(row["ef"]),
        interval_days=int(row["interval_days"]),
        repetitions=int(row["repetitions"]),
    )

    hr()
    print(f"[{row['subject']} · {row['knowledge_point']}]")
    print(f"{row['question']}")
    hr()
    print(f"  当前状态：EF {state.ef:.2f} · 间隔 {state.interval_days} 天 · 连对 {state.repetitions} 次")
    print()
    print("  如果接下来每次都得满分（5 分），间隔会这样增长：")
    for step, interval in preview(state):
        bar = "█" * min(40, max(1, interval // 5))
        print(f"    第 {step} 次 → {interval:>4} 天  {bar}")
    print()
    print("  （这是纯算法算出来的——同样输入永远得到同样结果）")
    conn.close()


# --------------------------------------------------------------------------
# 人在回路：暂存 → 确认
# --------------------------------------------------------------------------
def cmd_stage(args) -> None:
    """把打分先记进暂存区。这一步不改变记忆状态。"""
    if not 0 <= args.rating <= 5:
        print("评分必须是 0-5 的整数。")
        return

    conn = db.connect(args.db)
    rows = _due_with_index(conn)
    row = rows.get(args.index)
    if row is None:
        print(f"今天没有第 {args.index} 张到期的卡片。")
        conn.close()
        return

    staging_id = repo.stage_review(conn, card_id=int(row["id"]), rating=args.rating)

    hr()
    print(f"已暂存（还没生效）· [{row['knowledge_point']}]  评分 {args.rating}")
    hr()
    print(f"  暂存编号 : {staging_id}")
    print("  这张卡   : 仍然在今天的队列里——因为还没写进记忆状态")
    print()
    print("  确认后才真正生效：")
    print(f"    python -m study_assistant.cli confirm {staging_id}")
    print("  觉得这次打分不算数，就丢掉它：")
    print(f"    python -m study_assistant.cli drop {staging_id}")
    conn.close()


def cmd_staged(args) -> None:
    conn = db.connect(args.db)
    rows = repo.pending_staged(conn)

    hr()
    print("暂存区（等待你确认的复习记录）")
    hr()
    if not rows:
        print("  （空的。）")
        conn.close()
        return

    for r in rows:
        print(f"  #{r['id']:<4} 评分 {r['rating']}  {r['question']}")
        print(f"        暂存于 {r['created_at']}")
    print()
    print("  confirm <编号> 让它生效，drop <编号> 丢掉它")
    conn.close()


def cmd_confirm(args) -> None:
    conn = db.connect(args.db)
    try:
        result = repo.confirm_staged(conn, args.staging_id)
    except LookupError as exc:
        print(f"确认失败：{exc}")
        conn.close()
        return

    after: SM2State = result["after"]
    hr()
    print(f"已确认并落库 · 暂存 #{args.staging_id}")
    hr()
    print(f"  间隔         : {result['before'].interval_days} 天 → {after.interval_days} 天")
    print(f"  EF（难度）   : {result['before'].ef:.2f} → {after.ef:.2f}")
    print(f"  下次复习     : {result['next_due']}")
    conn.close()


def cmd_drop(args) -> None:
    conn = db.connect(args.db)
    try:
        repo.drop_staged(conn, args.staging_id)
    except LookupError as exc:
        print(f"丢弃失败：{exc}")
        conn.close()
        return
    print(f"已丢弃暂存 #{args.staging_id}。记忆状态没有任何变化。")
    conn.close()


# --------------------------------------------------------------------------
# menu：交互式会话（双击启动时走这条）
# --------------------------------------------------------------------------
def cmd_menu(args) -> None:
    # 延迟导入：session.py 需要复用本文件里的 hr / card_limit_from_profile，
    # 在模块顶层互相 import 会成环，所以放到函数里。
    from study_assistant import session

    session.run(args.db)


def cmd_web(args) -> None:
    """启动网页界面。web.py 里也不想反向 import cli，所以同样延迟导入。"""
    from study_assistant import web as web_module

    web_module.serve(
        db_path=args.db,
        port=args.port or None,
        open_browser=not args.no_browser,
        fullscreen=args.fullscreen,
    )


def cmd_desktop(args) -> None:
    """启动原生窗口界面（tkinter，零下载）。"""
    from study_assistant import desktop

    desktop.run(args.db)


def cmd_eval(args) -> None:
    """跑评测：分级路由 / 工具选择 / 自检通过率。"""
    from study_assistant import config, evalharness

    cases = evalharness.load_dataset()
    hr()
    print(f"评测集：{len(cases)} 条")
    hr()

    if args.dry_run:
        for case in cases:
            if case["expect_mode"] not in ("direct", "multi"):
                raise SystemExit(f"用例 {case['id']} 的 expect_mode 不合法")
        print("数据集格式 OK —— 没调模型，也没花钱。")
        return

    if not config.llm_configured():
        print("还没配置 DEEPSEEK_API_KEY，跑不了真评测。")
        print("（想先验证数据集，加 --dry-run）")
        return

    report = evalharness.run(evalharness.build_default_client, args.db, cases)
    print(evalharness.format_report(report))
    print()
    print(f"报告已保存：{evalharness.save_report(report)}")


# --------------------------------------------------------------------------
# stats / forecast / profile
# --------------------------------------------------------------------------
def cmd_stats(args) -> None:
    conn = db.connect(args.db)
    stats = repo.subject_stats(conn)
    hr()
    print("各科统计")
    hr()
    if not stats:
        print("  还没有数据。先跑 `init`。")
        conn.close()
        return
    print(f"  {'科目':<12}{'卡片':>6}{'今天到期':>10}{'平均间隔':>10}{'掌握度':>9}{'累计答错':>10}")
    print("  " + "-" * 66)
    for s in stats:
        print(
            f"  {s['subject']:<12}{s['total']:>6}{s['due_now']:>10}"
            f"{s['avg_interval']:>10.1f}{s['mastery']:>9.2f}{s['lapses']:>10}"
        )
    if stats and stats[0]["exam_date"]:
        print()
        print(f"  考试日期：{stats[0]['exam_date']}")

    weak = repo.weak_points(conn)
    if weak:
        print()
        print("  薄弱知识点（按答错次数）：")
        for w in weak:
            print(f"    · {w['knowledge_point']:<16} 复习 {w['reviews']} 次，错 {w['wrongs']} 次")
    conn.close()


def cmd_forecast(args) -> None:
    conn = db.connect(args.db)
    rows = repo.forecast(conn, days=args.days)
    hr()
    print(f"未来 {args.days} 天复习负载")
    hr()
    busiest = max((n for _, n in rows), default=0)
    for day, n in rows:
        bar = "█" * (round(n / busiest * 40) if busiest else 0)
        print(f"  {day}  {n:>4} 张  {bar}")
    print()
    total = sum(n for _, n in rows)
    print(f"  合计 {total} 张")
    if busiest and busiest > total / max(1, len(rows)) * 2:
        print("  注意：中间有一天明显偏多，可能要提前分摊。")
    conn.close()


def cmd_profile(args) -> None:
    conn = db.connect(args.db)
    profile = repo.get_profile(conn)
    hr()
    print("个人画像")
    hr()
    if not profile:
        print("  （还没有画像。先跑 `init`。）")
    for k, v in profile.items():
        print(f"  {k:<16} {v}")
    limit = card_limit_from_profile(profile)
    if limit:
        print()
        print(f"  由「每天可用 {profile.get('daily_minutes')} 分钟」推出：今天最多复习 {limit} 张")
    conn.close()


# --------------------------------------------------------------------------
# 辅助
# --------------------------------------------------------------------------
def _due_with_index(conn: sqlite3.Connection) -> dict[int, sqlite3.Row]:
    """把"今天该复习"的列表按 1 开始的序号索引，方便用序号操作。"""
    limit = card_limit_from_profile(repo.get_profile(conn))
    rows = repo.due_cards(conn, limit=limit)
    return {i: row for i, row in enumerate(rows, start=1)}


def _all_with_index(conn: sqlite3.Connection) -> dict[int, sqlite3.Row]:
    sql = """
        SELECT c.id, c.question, c.knowledge_point, s.name AS subject,
               st.ef, st.interval_days, st.repetitions
        FROM cards c
        JOIN subjects s ON s.id = c.subject_id
        JOIN card_state st ON st.card_id = c.id
        ORDER BY c.id
    """
    return {i: row for i, row in enumerate(conn.execute(sql), start=1)}


def main() -> None:
    # 公共参数：让每个子命令都能用 --db 指定数据库（测试时会用到）
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=str(db.DEFAULT_DB), help="数据库文件路径")

    parser = argparse.ArgumentParser(
        prog="study",
        description="个人学习助手 —— 模型负责理解，算法负责调度",
        epilog="每个子命令都支持 --db 指定数据库文件，例如：study due --db my.db",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("menu", parents=[common], help="打开交互式菜单（双击启动用）").set_defaults(func=cmd_menu)

    p_web = sub.add_parser("web", parents=[common], help="启动网页界面")
    p_web.add_argument("--port", type=int, default=0, help="不填就自动挑一个空闲端口")
    p_web.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    p_web.add_argument("--fullscreen", action="store_true",
                       help="全屏打开（退出要按 F11，默认是最大化窗口）")
    p_web.set_defaults(func=cmd_web)

    sub.add_parser(
        "desktop", parents=[common], help="启动原生窗口界面（tkinter，零下载）"
    ).set_defaults(func=cmd_desktop)

    p_eval = sub.add_parser("eval", parents=[common], help="跑评测（分级/工具选择/自检）")
    p_eval.add_argument("--dry-run", action="store_true", help="只校验数据集，不调模型")
    p_eval.set_defaults(func=cmd_eval)

    sub.add_parser("init", parents=[common], help="建库并导入示例卡片").set_defaults(func=cmd_init)
    sub.add_parser("due", parents=[common], help="今天该复习什么").set_defaults(func=cmd_due)

    p_ans = sub.add_parser("answer", parents=[common], help="看某张卡的答案")
    p_ans.add_argument("index", type=int, help="due 列表里的序号")
    p_ans.set_defaults(func=cmd_answer)

    p_rev = sub.add_parser("review", parents=[common], help="记录一次复习")
    p_rev.add_argument("index", type=int, help="due 列表里的序号")
    p_rev.add_argument("rating", type=int, help="评分 0-5")
    p_rev.set_defaults(func=cmd_review)

    p_prev = sub.add_parser("preview", parents=[common], help="看某张卡未来的复习间隔")
    p_prev.add_argument("index", type=int, help="卡片序号（按创建顺序）")
    p_prev.set_defaults(func=cmd_preview)

    p_stage = sub.add_parser("stage", parents=[common], help="先暂存一次打分，不落库")
    p_stage.add_argument("index", type=int, help="due 列表里的序号")
    p_stage.add_argument("rating", type=int, help="评分 0-5")
    p_stage.set_defaults(func=cmd_stage)

    sub.add_parser("staged", parents=[common], help="查看暂存区").set_defaults(func=cmd_staged)

    p_conf = sub.add_parser("confirm", parents=[common], help="确认一条暂存记录，让它生效")
    p_conf.add_argument("staging_id", type=int, help="暂存编号")
    p_conf.set_defaults(func=cmd_confirm)

    p_drop = sub.add_parser("drop", parents=[common], help="丢弃一条暂存记录")
    p_drop.add_argument("staging_id", type=int, help="暂存编号")
    p_drop.set_defaults(func=cmd_drop)

    sub.add_parser("stats", parents=[common], help="各科统计").set_defaults(func=cmd_stats)

    p_fc = sub.add_parser("forecast", parents=[common], help="未来负载预测")
    p_fc.add_argument("--days", type=int, default=7)
    p_fc.set_defaults(func=cmd_forecast)

    sub.add_parser("profile", parents=[common], help="查看个人画像").set_defaults(func=cmd_profile)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
