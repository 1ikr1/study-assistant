"""数据访问层：所有 SQL 都收在这里。

业务逻辑（算法、CLI）不直接写 SQL，只调这里的函数。
这样将来换数据库或换 ORM，只需要改这一个文件。
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta

from study_coach.sm2 import SM2State, update


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def today() -> str:
    return date.today().isoformat()


# --------------------------------------------------------------------------
# 科目与卡片
# --------------------------------------------------------------------------
def add_subject(conn: sqlite3.Connection, name: str, exam_date: str | None = None) -> int:
    """新增科目。已存在则直接返回它的 id。"""
    row = conn.execute("SELECT id FROM subjects WHERE name = ?", (name,)).fetchone()
    if row is not None:
        if exam_date:
            conn.execute("UPDATE subjects SET exam_date = ? WHERE id = ?", (exam_date, row["id"]))
            conn.commit()
        return int(row["id"])
    cur = conn.execute(
        "INSERT INTO subjects (name, exam_date, created_at) VALUES (?, ?, ?)",
        (name, exam_date, _now()),
    )
    conn.commit()
    return int(cur.lastrowid)


def add_card(
    conn: sqlite3.Connection,
    *,
    subject_id: int,
    knowledge_point: str,
    question: str,
    answer: str,
    source: str = "",
) -> int:
    """新增卡片，并同时创建它的记忆状态（due_at = 今天，所以新卡立刻可复习）。"""
    cur = conn.execute(
        """
        INSERT INTO cards (subject_id, knowledge_point, question, answer, source, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (subject_id, knowledge_point, question, answer, source, _now()),
    )
    card_id = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO card_state (card_id, due_at, updated_at) VALUES (?, ?, ?)",
        (card_id, today(), _now()),
    )
    conn.commit()
    return card_id


def count_cards(conn: sqlite3.Connection, subject_id: int | None = None) -> int:
    if subject_id is None:
        row = conn.execute("SELECT COUNT(*) AS n FROM cards").fetchone()
    else:
        row = conn.execute("SELECT COUNT(*) AS n FROM cards WHERE subject_id = ?", (subject_id,)).fetchone()
    return int(row["n"])


def get_card(conn: sqlite3.Connection, card_id: int) -> sqlite3.Row | None:
    """取一张卡，连同它的科目和记忆状态。取不到返回 None。

    给"让模型讲一遍"用的 —— 讲题需要题面、标准答案，还需要
    "这张卡他答错过几次"，否则模型给的建议会很泛。
    """
    return conn.execute(
        """
        SELECT c.id, c.question, c.answer, c.source, c.knowledge_point,
               s.name AS subject,
               st.ef, st.interval_days, st.repetitions, st.lapses
          FROM cards c
          JOIN subjects s     ON s.id = c.subject_id
          JOIN card_state st  ON st.card_id = c.id
         WHERE c.id = ?
        """,
        (card_id,),
    ).fetchone()


# --------------------------------------------------------------------------
# 查询：今天该复习什么
# --------------------------------------------------------------------------
def due_cards(
    conn: sqlite3.Connection,
    *,
    on_date: str | None = None,
    limit: int | None = None,
) -> list[sqlite3.Row]:
    """今天到期的卡片，按"逾期最久 + 科目"排序。

    这是整个系统最高频的查询，依赖 card_state.due_at 上的索引。
    """
    day = on_date or today()
    sql = """
        SELECT c.id, c.knowledge_point, c.question, c.answer, c.source,
               s.name AS subject, s.exam_date,
               st.ef, st.interval_days, st.repetitions, st.due_at, st.lapses
        FROM card_state st
        JOIN cards c    ON c.id = st.card_id
        JOIN subjects s ON s.id = c.subject_id
        WHERE st.due_at <= ?
        ORDER BY st.due_at ASC, s.name ASC, c.knowledge_point ASC
    """
    params: list[object] = [day]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return list(conn.execute(sql, params))


# --------------------------------------------------------------------------
# 核心：应用一次复习
# --------------------------------------------------------------------------
def apply_review(
    conn: sqlite3.Connection,
    *,
    card_id: int,
    rating: int,
    reviewed_on: str | None = None,
) -> dict:
    """把一次复习写进系统：更新状态 + 追加日志。

    这是唯一会改动 card_state 的地方。

    Returns:
        包含 before / after / next_due / elapsed_days 的字典，供 CLI 展示。
    """
    day = reviewed_on or today()
    row = conn.execute(
        "SELECT ef, interval_days, repetitions, due_at, last_review_at, lapses "
        "FROM card_state WHERE card_id = ?",
        (card_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"卡片不存在：{card_id}")

    before = SM2State(
        ef=float(row["ef"]),
        interval_days=int(row["interval_days"]),
        repetitions=int(row["repetitions"]),
    )

    # 距上次复习过了多少天——日志里要记，用于将来重放算法
    if row["last_review_at"]:
        last = date.fromisoformat(str(row["last_review_at"])[:10])
        elapsed = (date.fromisoformat(day) - last).days
    else:
        elapsed = 0

    after = update(before, rating)
    next_due = (date.fromisoformat(day) + timedelta(days=after.interval_days)).isoformat()
    lapses = int(row["lapses"]) + (1 if rating < 3 else 0)

    conn.execute(
        """
        UPDATE card_state
           SET ef = ?, interval_days = ?, repetitions = ?,
               due_at = ?, last_review_at = ?, lapses = ?, updated_at = ?
         WHERE card_id = ?
        """,
        (after.ef, after.interval_days, after.repetitions,
         next_due, day, lapses, _now(), card_id),
    )
    conn.execute(
        """
        INSERT INTO review_logs
            (card_id, reviewed_at, rating, elapsed_days, scheduled_days,
             ef_before, interval_before, algorithm)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'sm2')
        """,
        (card_id, day, rating, elapsed, before.interval_days, before.ef, before.interval_days),
    )
    conn.commit()

    return {
        "card_id": card_id,
        "before": before,
        "after": after,
        "elapsed_days": elapsed,
        "next_due": next_due,
        "lapses": lapses,
    }


# --------------------------------------------------------------------------
# 统计与预测
# --------------------------------------------------------------------------
def subject_stats(conn: sqlite3.Connection) -> list[dict]:
    """每个科目的掌握度与负债。

    掌握度 = 平均 (1 / (1 + 当前间隔))，简单、单调、可解释：
    间隔越长说明记得越牢，掌握度越接近 1。
    """
    rows = conn.execute(
        """
        SELECT s.id, s.name, s.exam_date,
               COUNT(c.id)              AS total,
               AVG(st.interval_days)    AS avg_interval,
               SUM(CASE WHEN st.due_at <= ? THEN 1 ELSE 0 END) AS due_now,
               SUM(st.lapses)           AS total_lapses
        FROM subjects s
        LEFT JOIN cards c      ON c.subject_id = s.id
        LEFT JOIN card_state st ON st.card_id = c.id
        GROUP BY s.id
        ORDER BY s.name
        """,
        (today(),),
    ).fetchall()

    out: list[dict] = []
    for r in rows:
        avg_interval = float(r["avg_interval"] or 0.0)
        mastery = 1.0 / (1.0 + avg_interval) if r["total"] else 0.0
        out.append(
            {
                "subject": r["name"],
                "exam_date": r["exam_date"],
                "total": int(r["total"] or 0),
                "due_now": int(r["due_now"] or 0),
                "avg_interval": round(avg_interval, 1),
                "mastery": round(mastery, 3),
                "lapses": int(r["total_lapses"] or 0),
            }
        )
    return out


def weak_points(conn: sqlite3.Connection, *, min_reviews: int = 1, limit: int = 5) -> list[dict]:
    """薄弱知识点：答错次数最多的那些。"""
    rows = conn.execute(
        """
        SELECT c.knowledge_point,
               COUNT(l.id)                                        AS reviews,
               SUM(CASE WHEN l.rating < 3 THEN 1 ELSE 0 END)      AS wrongs
        FROM review_logs l
        JOIN cards c ON c.id = l.card_id
        GROUP BY c.knowledge_point
        HAVING COUNT(l.id) >= ?
        ORDER BY wrongs DESC, reviews DESC
        LIMIT ?
        """,
        (min_reviews, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def forecast(conn: sqlite3.Connection, *, days: int = 7) -> list[tuple[str, int]]:
    """未来 N 天，每天分别有多少张卡到期。

    用来回答"我接下来几天会不会爆掉"。
    """
    start = date.today()
    out: list[tuple[str, int]] = []
    for offset in range(days):
        day = (start + timedelta(days=offset)).isoformat()
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM card_state WHERE due_at = ?", (day,)
        ).fetchone()
        out.append((day, int(row["n"])))
    return out


def overdue_count(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM card_state WHERE due_at < ?", (today(),)
    ).fetchone()
    return int(row["n"])


# --------------------------------------------------------------------------
# 个人画像（直接查，不走检索）
# --------------------------------------------------------------------------
def set_profile(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO profile (key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, value, _now()),
    )
    conn.commit()


def get_profile(conn: sqlite3.Connection) -> dict[str, str]:
    return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM profile")}


# --------------------------------------------------------------------------
# 人在回路：暂存区
# --------------------------------------------------------------------------
def stage_review(
    conn: sqlite3.Connection, *, card_id: int, rating: int, raw_input: str = ""
) -> int:
    """把一次复习先放进暂存区，等用户确认再落库。"""
    cur = conn.execute(
        """
        INSERT INTO staging_reviews (card_id, rating, raw_input, status, created_at)
        VALUES (?, ?, ?, 'pending', ?)
        """,
        (card_id, rating, raw_input, _now()),
    )
    conn.commit()
    return int(cur.lastrowid)


def confirm_staged(conn: sqlite3.Connection, staging_id: int) -> dict:
    """确认一条暂存记录并落库。

    用条件 UPDATE 做"原子抢占"：只有还处于 pending 的记录能被确认，
    所以同一条即使被并发确认两次，也只会落库一次。
    """
    cur = conn.execute(
        "UPDATE staging_reviews SET status = 'confirmed', confirmed_at = ? "
        "WHERE id = ? AND status = 'pending'",
        (_now(), staging_id),
    )
    conn.commit()
    if cur.rowcount == 0:
        raise LookupError("这条暂存记录不存在，或已经被确认过了")

    row = conn.execute(
        "SELECT card_id, rating FROM staging_reviews WHERE id = ?", (staging_id,)
    ).fetchone()
    return apply_review(conn, card_id=int(row["card_id"]), rating=int(row["rating"]))


def pending_staged(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT st.id, st.card_id, st.rating, st.created_at, c.question
            FROM staging_reviews st
            JOIN cards c ON c.id = st.card_id
            WHERE st.status = 'pending'
            ORDER BY st.id
            """
        )
    )


def drop_staged(conn: sqlite3.Connection, staging_id: int) -> None:
    """丢弃一条暂存记录：什么都不落库，等于"这次打分不算数"。

    和 confirm_staged 一样用条件 UPDATE，保证同一条记录只能被处理一次。
    """
    cur = conn.execute(
        "UPDATE staging_reviews SET status = 'dropped', confirmed_at = ? "
        "WHERE id = ? AND status = 'pending'",
        (_now(), staging_id),
    )
    conn.commit()
    if cur.rowcount == 0:
        raise LookupError("这条暂存记录不存在，或已经被处理过了")


# --------------------------------------------------------------------------
# 计划任务：今天/这周打算做什么
# --------------------------------------------------------------------------
def add_task(
    conn: sqlite3.Connection,
    *,
    title: str,
    subject: str = "",
    due_date: str | None = None,
    minutes: int = 30,
    priority: int = 2,
) -> int:
    """加一条计划任务。due_date 不填就是今天。"""
    cur = conn.execute(
        """
        INSERT INTO tasks (title, subject, due_date, minutes, priority, status, created_at)
        VALUES (?, ?, ?, ?, ?, 'todo', ?)
        """,
        (title, subject, due_date or today(), int(minutes), int(priority), _now()),
    )
    conn.commit()
    return int(cur.lastrowid)


def tasks_on(conn: sqlite3.Connection, day: str | None = None) -> list[sqlite3.Row]:
    """某天的任务。没做完的排前面，然后按优先级、再按创建时间。"""
    return list(
        conn.execute(
            """
            SELECT id, title, subject, due_date, minutes, priority, status, done_at
              FROM tasks
             WHERE due_date = ?
             ORDER BY (status = 'done'), priority ASC, id ASC
            """,
            (day or today(),),
        )
    )


def set_task_status(conn: sqlite3.Connection, task_id: int, status: str) -> None:
    """改任务状态（todo / done）。"""
    if status not in ("todo", "done"):
        raise ValueError(f"状态只能是 todo 或 done，收到 {status!r}")
    cur = conn.execute(
        "UPDATE tasks SET status = ?, done_at = ? WHERE id = ?",
        (status, _now() if status == "done" else None, task_id),
    )
    conn.commit()
    if cur.rowcount == 0:
        raise LookupError(f"任务不存在：{task_id}")


def task_progress(conn: sqlite3.Connection, day: str | None = None) -> tuple[int, int]:
    """某天的 (已完成数, 总数)。"""
    row = conn.execute(
        "SELECT COUNT(*) AS total, SUM(status = 'done') AS done "
        "FROM tasks WHERE due_date = ?",
        (day or today(),),
    ).fetchone()
    return int(row["done"] or 0), int(row["total"] or 0)


# --------------------------------------------------------------------------
# 专注记录：一次计时 = 一条
# --------------------------------------------------------------------------
def log_focus(
    conn: sqlite3.Connection,
    *,
    planned_min: int,
    actual_sec: int,
    finished: bool = False,
    task_id: int | None = None,
    started_at: str | None = None,
) -> int:
    """记一次专注。

    注意这里**不要求必须完整跑完**——中途被打断也要记，
    因为"今天到底专注了多久"才是学习洞察要用的数据，
    "完整跑完了几轮"是另一个指标。两件事分开存，将来怎么切都行。
    """
    start = started_at or _now()
    cur = conn.execute(
        """
        INSERT INTO focus_sessions
            (task_id, planned_min, actual_sec, finished, started_at, ended_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (task_id, int(planned_min), int(actual_sec), 1 if finished else 0,
         start, _now()),
    )
    conn.commit()
    return int(cur.lastrowid)


def focus_seconds(conn: sqlite3.Connection, day: str | None = None) -> int:
    """某天累计专注了多少秒。"""
    target = day or today()
    row = conn.execute(
        "SELECT COALESCE(SUM(actual_sec), 0) AS s FROM focus_sessions "
        "WHERE substr(started_at, 1, 10) = ?",
        (target,),
    ).fetchone()
    return int(row["s"])


def focus_by_day(conn: sqlite3.Connection, *, days: int = 7) -> list[tuple[str, int]]:
    """最近 N 天每天专注了多少秒（用来画节奏图）。"""
    start = date.today() - timedelta(days=days - 1)
    out: list[tuple[str, int]] = []
    for offset in range(days):
        day = (start + timedelta(days=offset)).isoformat()
        out.append((day, focus_seconds(conn, day)))
    return out
