"""把示例卡片导入数据库。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from study_coach import repo

SAMPLE = Path(__file__).resolve().parent.parent / "data" / "cards_sample.json"


def load_sample(conn: sqlite3.Connection, path: Path = SAMPLE) -> tuple[int, int]:
    """导入示例卡片。

    Returns:
        (新增科目数, 新增卡片数)。重复执行不会重复插入。
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    subject = payload["subject"]

    before = repo.count_cards(conn)
    subject_id = repo.add_subject(conn, subject["name"], subject.get("exam_date"))

    existing = {
        r["question"]
        for r in conn.execute("SELECT question FROM cards WHERE subject_id = ?", (subject_id,))
    }
    added = 0
    for card in payload["cards"]:
        if card["question"] in existing:
            continue
        repo.add_card(
            conn,
            subject_id=subject_id,
            knowledge_point=card["knowledge_point"],
            question=card["question"],
            answer=card["answer"],
            source=card.get("source", ""),
        )
        added += 1

    return (repo.count_cards(conn) - before, added)


def default_profile(conn: sqlite3.Connection) -> None:
    """写入一份默认画像。画像字段有限且确定，所以直接查、不检索。"""
    for key, value in (
        ("daily_minutes", "60"),
        ("study_time", "20:00-22:00"),
        ("style", "喜欢看例题，不爱看大段文字"),
    ):
        repo.set_profile(conn, key, value)


# 示例任务。只为让「今日行动」第一天不是空的。
SAMPLE_TASKS = (
    ("二叉树遍历与递归基础", "数据结构", 40, 1),
    ("排序算法复杂度手推一遍", "数据结构", 30, 2),
    ("错题回顾：图的最短路径", "数据结构", 25, 2),
)


def default_tasks(conn: sqlite3.Connection) -> int:
    """塞几条今天的示例任务。已经有过任务就不再动，避免重复插入。"""
    row = conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()
    if int(row["n"]):
        return 0
    for title, subject, minutes, priority in SAMPLE_TASKS:
        repo.add_task(conn, title=title, subject=subject,
                      minutes=minutes, priority=priority)
    return len(SAMPLE_TASKS)


def bootstrap(conn: sqlite3.Connection) -> tuple[int, int]:
    """建库后一次性把示例数据铺好。四个壳启动时都调这个，保证看到的东西一致。

    Returns:
        (新增卡片数, 新增任务数)
    """
    _, added_cards = load_sample(conn)
    default_profile(conn)
    added_tasks = default_tasks(conn)
    return added_cards, added_tasks
