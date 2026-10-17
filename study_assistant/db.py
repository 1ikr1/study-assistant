"""数据库：建表与连接。

Phase 1 刻意只用标准库 sqlite3——零依赖，装好 Python 就能跑。
Phase 2 接入 FastAPI 时会换成 SQLAlchemy + Alembic（需要异步 session 和表结构迁移），
但数据访问都收在 repo.py 里，替换时不影响业务逻辑。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "study.db"

# 7 张表。分三层：
#   · 业务数据：subjects / cards / card_state
#   · 事实日志：review_logs（只增不改，用来重放算法）
#   · 辅助：profile（画像）/ staging_reviews（人在回路）/ memories（对话记忆）
SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS subjects (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    exam_date  TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cards (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_id      INTEGER NOT NULL REFERENCES subjects(id),
    knowledge_point TEXT NOT NULL,
    question        TEXT NOT NULL,
    answer          TEXT NOT NULL,
    source          TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cards_subject ON cards(subject_id, knowledge_point);

-- 记忆状态：算法的输出。每张卡一行，被反复更新。
CREATE TABLE IF NOT EXISTS card_state (
    card_id        INTEGER PRIMARY KEY REFERENCES cards(id) ON DELETE CASCADE,
    -- SM-2 的三个变量
    ef             REAL    NOT NULL DEFAULT 2.5,
    interval_days  INTEGER NOT NULL DEFAULT 0,
    repetitions    INTEGER NOT NULL DEFAULT 0,
    -- FSRS 的两个变量（Phase 3 才会填）
    stability      REAL,
    difficulty     REAL,
    -- 通用
    due_at         TEXT    NOT NULL,
    last_review_at TEXT,
    lapses         INTEGER NOT NULL DEFAULT 0,
    updated_at     TEXT    NOT NULL
);
-- 最高频的查询是「今天该复习什么」，这个索引必须有
CREATE INDEX IF NOT EXISTS idx_state_due ON card_state(due_at);

-- 复习日志：只增不改的事实记录。
-- 关键：连"当时的算法状态"一起存，这样算法升级后可以重放对比。
CREATE TABLE IF NOT EXISTS review_logs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id         INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    reviewed_at     TEXT    NOT NULL,
    rating          INTEGER NOT NULL,
    elapsed_days    INTEGER NOT NULL,
    scheduled_days  INTEGER NOT NULL,
    ef_before       REAL    NOT NULL,
    interval_before INTEGER NOT NULL,
    algorithm       TEXT    NOT NULL DEFAULT 'sm2'
);
CREATE INDEX IF NOT EXISTS idx_logs_card ON review_logs(card_id, reviewed_at);

-- 个人画像：字段有限且确定，所以直接查、不检索。
CREATE TABLE IF NOT EXISTS profile (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- 人在回路：答题记录先进这里，用户确认后才写进 review_logs。
CREATE TABLE IF NOT EXISTS staging_reviews (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id      INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    rating       INTEGER NOT NULL,
    raw_input    TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'pending',
    created_at   TEXT NOT NULL,
    confirmed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_staging_status ON staging_reviews(status);

-- 对话记忆：用户随口说的偏好（"晚上效率高"）。
-- 条目会持续增长，所以按相关性召回，而不是全量注入。
CREATE TABLE IF NOT EXISTS memories (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    content      TEXT NOT NULL,
    kind         TEXT NOT NULL DEFAULT 'fact',
    created_at   TEXT NOT NULL,
    last_used_at TEXT,
    use_count    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_memories_kind ON memories(kind);

-- 计划任务：今天/这周打算做什么。
-- 和 cards 的区别：cards 是"要记住的知识点"，tasks 是"要完成的事"。
CREATE TABLE IF NOT EXISTS tasks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT    NOT NULL,
    subject    TEXT    NOT NULL DEFAULT '',
    due_date   TEXT    NOT NULL,               -- 打算哪天做
    minutes    INTEGER NOT NULL DEFAULT 30,    -- 预计花多久
    priority   INTEGER NOT NULL DEFAULT 2,     -- 1 重要 / 2 一般 / 3 次要
    status     TEXT    NOT NULL DEFAULT 'todo',-- todo / done
    done_at    TEXT,
    created_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_day ON tasks(due_date, status);

-- 专注记录：一次计时 = 一条。
-- 和 review_logs 一样是"事实日志"：只增不改，将来的学习洞察全靠它。
CREATE TABLE IF NOT EXISTS focus_sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
    planned_min INTEGER NOT NULL,              -- 计划专注多少分钟
    actual_sec  INTEGER NOT NULL,              -- 实际专注了多少秒
    finished    INTEGER NOT NULL DEFAULT 0,    -- 1 = 完整跑完，0 = 中途停
    started_at  TEXT    NOT NULL,
    ended_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_focus_day ON focus_sessions(started_at);
"""


def connect(db_path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    """打开数据库连接。如果文件不存在会自动创建。"""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row  # 让查询结果能按列名取值
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """建表（幂等，重复调用不会报错）。"""
    conn.executescript(SCHEMA)
    conn.commit()


def path_of(conn: sqlite3.Connection) -> str:
    """取出这条连接指向的数据库文件路径。

    为什么需要：LangGraph 可能在不同线程里并行跑子 Agent，而 sqlite 的连接
    不能跨线程共用。所以每个节点要按路径自己开一条连接 —— 路径从这里拿。
    """
    row = conn.execute("PRAGMA database_list").fetchone()
    return str(row["file"]) if row and row["file"] else ""
