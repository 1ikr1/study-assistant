"""计划任务、专注记录、今日看板的测试。

这三块是「工作台」那层新加的东西，和复习调度是两条独立的线：
一条管"什么时候复习哪个知识点"，一条管"今天打算干什么、实际干了多久"。
所以它们的测试也分开写。
"""

from __future__ import annotations

import os
import shutil
import unittest
from datetime import date, timedelta
from pathlib import Path

from study_assistant import db, repo, seed, view_model as vm

_TMP_ROOT = Path(__file__).resolve().parent.parent / ".tmp-tests"
_TMP_ROOT.mkdir(parents=True, exist_ok=True)


def _make_tmpdir() -> Path:
    while True:
        path = _TMP_ROOT / f"run-{os.urandom(6).hex()}"
        try:
            path.mkdir(parents=False, exist_ok=False)
            return path
        except FileExistsError:
            continue


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = _make_tmpdir()
        self.conn = db.connect(self._tmp / "test.db")
        db.init_db(self.conn)
        seed.bootstrap(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        shutil.rmtree(self._tmp, ignore_errors=True)


class TestTasks(Base):
    def test_bootstrap_creates_sample_tasks_for_today(self):
        rows = repo.tasks_on(self.conn)
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(r["due_date"] == repo.today() for r in rows))

    def test_bootstrap_is_idempotent(self):
        seed.bootstrap(self.conn)
        seed.bootstrap(self.conn)
        self.assertEqual(len(repo.tasks_on(self.conn)), 3)

    def test_add_and_toggle(self):
        task_id = repo.add_task(self.conn, title="背单词 100 个", subject="英语",
                                minutes=20, priority=1)
        repo.set_task_status(self.conn, task_id, "done")

        row = self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        self.assertEqual(row["status"], "done")
        self.assertIsNotNone(row["done_at"])

        repo.set_task_status(self.conn, task_id, "todo")
        row = self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        self.assertIsNone(row["done_at"], "改回未完成要顺手把完成时间清掉")

    def test_unknown_task_raises(self):
        with self.assertRaises(LookupError):
            repo.set_task_status(self.conn, 99999, "done")

    def test_bad_status_is_rejected(self):
        task_id = repo.add_task(self.conn, title="x")
        with self.assertRaises(ValueError):
            repo.set_task_status(self.conn, task_id, "whatever")

    def test_tasks_are_sorted(self):
        """没做完的排前面，然后按优先级。"""
        repo.add_task(self.conn, title="次要的事", priority=3)
        repo.add_task(self.conn, title="重要的事", priority=1)
        rows = repo.tasks_on(self.conn)
        titles = [r["title"] for r in rows]
        self.assertLess(titles.index("重要的事"), titles.index("次要的事"))

        priorities = [r["priority"] for r in rows]
        self.assertEqual(priorities, sorted(priorities), "未完成的要按优先级排")

        # 把排在第一条的标记完成，它应该掉到最后
        first = rows[0]
        repo.set_task_status(self.conn, first["id"], "done")
        rows = repo.tasks_on(self.conn)
        self.assertEqual(rows[-1]["id"], first["id"], "做完的排到列表末尾")

    def test_progress_counts(self):
        done, total = repo.task_progress(self.conn)
        self.assertEqual((done, total), (0, 3))

        repo.set_task_status(self.conn, repo.tasks_on(self.conn)[0]["id"], "done")
        done, total = repo.task_progress(self.conn)
        self.assertEqual((done, total), (1, 3))

    def test_tasks_of_another_day_are_not_mixed_in(self):
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        repo.add_task(self.conn, title="明天的任务", due_date=tomorrow)
        self.assertEqual(len(repo.tasks_on(self.conn)), 3)
        self.assertEqual(len(repo.tasks_on(self.conn, tomorrow)), 1)


class TestFocus(Base):
    def test_log_and_accumulate(self):
        repo.log_focus(self.conn, planned_min=25, actual_sec=1500, finished=True)
        repo.log_focus(self.conn, planned_min=25, actual_sec=600, finished=False)
        self.assertEqual(repo.focus_seconds(self.conn), 2100)

    def test_interrupted_session_still_counts(self):
        """中途被打断也要记 —— "今天专注了多久"和"完整跑完几轮"是两件事。"""
        repo.log_focus(self.conn, planned_min=50, actual_sec=7, finished=False)
        self.assertEqual(repo.focus_seconds(self.conn), 7)
        row = self.conn.execute("SELECT finished FROM focus_sessions").fetchone()
        self.assertEqual(int(row["finished"]), 0)

    def test_focus_by_day_returns_seven_buckets(self):
        repo.log_focus(self.conn, planned_min=25, actual_sec=900)
        rows = repo.focus_by_day(self.conn, days=7)
        self.assertEqual(len(rows), 7)
        self.assertEqual(rows[-1][0], repo.today(), "最后一个应该是今天")
        self.assertEqual(rows[-1][1], 900)
        self.assertEqual(sum(s for _, s in rows[:6]), 0)

    def test_focus_is_counted_per_day_not_globally(self):
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        repo.log_focus(self.conn, planned_min=25, actual_sec=300,
                       started_at=f"{yesterday}T20:00:00")
        repo.log_focus(self.conn, planned_min=25, actual_sec=600)
        self.assertEqual(repo.focus_seconds(self.conn, yesterday), 300)
        self.assertEqual(repo.focus_seconds(self.conn), 600)

    def test_focus_session_can_point_at_a_task(self):
        task_id = repo.tasks_on(self.conn)[0]["id"]
        repo.log_focus(self.conn, planned_min=25, actual_sec=1500, task_id=task_id)
        row = self.conn.execute("SELECT task_id FROM focus_sessions").fetchone()
        self.assertEqual(int(row["task_id"]), int(task_id))


class TestDashboard(Base):
    def test_countdown_comes_from_the_exam_date(self):
        data = vm.dashboard(self.conn)
        self.assertIsNotNone(data["exam_date"])
        expected = (date.fromisoformat(data["exam_date"]) - date.today()).days
        self.assertEqual(data["countdown_days"], expected)

    def test_dashboard_aggregates_everything(self):
        repo.log_focus(self.conn, planned_min=25, actual_sec=1200)
        data = vm.dashboard(self.conn)

        self.assertEqual(data["today"], repo.today())
        self.assertEqual(data["tasks_total"], 3)
        self.assertEqual(data["tasks_done"], 0)
        self.assertEqual(data["focus_seconds_today"], 1200)
        self.assertEqual(len(data["focus_7d"]), 7)
        self.assertEqual(data["due_total"], 20)
        self.assertEqual(data["total_cards"], 20)

    def test_focus_target_comes_from_the_profile(self):
        """画像里的"每天多少分钟"要同时驱动两件事：复习题量 和 专注目标。"""
        data = vm.dashboard(self.conn)
        self.assertEqual(data["focus_target_seconds"], 60 * 60)

        repo.set_profile(self.conn, "daily_minutes", "30")
        data = vm.dashboard(self.conn)
        self.assertEqual(data["focus_target_seconds"], 30 * 60)
        self.assertEqual(data["due_now"], 20)   # 30 / 1.5 = 20 张，刚好全做

    def test_dashboard_survives_a_broken_profile_value(self):
        """画像里被填了乱七八糟的东西时，看板不能整个崩掉。"""
        repo.set_profile(self.conn, "daily_minutes", "不知道")
        data = vm.dashboard(self.conn)
        self.assertEqual(data["focus_target_seconds"], 0)
        self.assertIsNone(data["limit"] if "limit" in data else vm.limit_from(data["profile"]))


if __name__ == "__main__":
    unittest.main()
