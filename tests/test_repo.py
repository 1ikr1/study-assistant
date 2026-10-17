"""数据层与人在回路的集成测试（用临时数据库，不碰真实数据）。

运行：python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sqlite3
import os
import shutil
import unittest
from pathlib import Path

from study_coach import db, repo, seed

# 测试临时库统一放在项目内的 .tmp-tests 下。
# 好处：1) 沙箱/CI 里系统临时目录不一定可写；2) 测试产物位置可预期，方便排查。
# 注意这里自己建目录而不用 tempfile.TemporaryDirectory：后者会强制把目录权限设成
# 0o700，在受限环境里会直接被拒绝写入。普通 mkdir 用默认权限，到处都能跑。
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


class RepoTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = _make_tmpdir()
        self.db_path = self._tmp / "test.db"
        self.conn = db.connect(self.db_path)
        db.init_db(self.conn)
        seed.load_sample(self.conn)
        seed.default_profile(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        shutil.rmtree(self._tmp, ignore_errors=True)


class TestIngest(RepoTestCase):
    def test_sample_cards_loaded(self):
        self.assertEqual(repo.count_cards(self.conn), 20)

    def test_loading_twice_does_not_duplicate(self):
        added, _ = seed.load_sample(self.conn)
        self.assertEqual(added, 0)
        self.assertEqual(repo.count_cards(self.conn), 20)

    def test_new_cards_are_due_immediately(self):
        self.assertEqual(len(repo.due_cards(self.conn)), 20)


class TestReviewFlow(RepoTestCase):
    def _first_card_id(self) -> int:
        return int(repo.due_cards(self.conn)[0]["id"])

    def test_review_pushes_due_date_forward(self):
        card_id = self._first_card_id()
        result = repo.apply_review(self.conn, card_id=card_id, rating=5)
        self.assertEqual(result["after"].interval_days, 1)
        self.assertNotEqual(result["next_due"], repo.today())

    def test_reviewed_card_leaves_todays_queue(self):
        card_id = self._first_card_id()
        repo.apply_review(self.conn, card_id=card_id, rating=5)
        remaining = {int(r["id"]) for r in repo.due_cards(self.conn)}
        self.assertNotIn(card_id, remaining)

    def test_wrong_answer_increments_lapses(self):
        card_id = self._first_card_id()
        result = repo.apply_review(self.conn, card_id=card_id, rating=0)
        self.assertEqual(result["lapses"], 1)
        # 答错后间隔为 1 天，所以今天不再到期
        remaining = {int(r["id"]) for r in repo.due_cards(self.conn)}
        self.assertNotIn(card_id, remaining)

    def test_log_records_algorithm_state(self):
        """日志里必须留下"当时的算法状态"，否则算法升级后无法重放。"""
        card_id = self._first_card_id()
        repo.apply_review(self.conn, card_id=card_id, rating=4)
        log = self.conn.execute(
            "SELECT * FROM review_logs WHERE card_id = ?", (card_id,)
        ).fetchone()
        self.assertIsNotNone(log)
        self.assertEqual(log["rating"], 4)
        self.assertEqual(log["algorithm"], "sm2")
        self.assertAlmostEqual(float(log["ef_before"]), 2.5, places=6)
        self.assertEqual(int(log["interval_before"]), 0)

    def test_review_unknown_card_raises(self):
        with self.assertRaises(LookupError):
            repo.apply_review(self.conn, card_id=99999, rating=3)


class TestStaging(RepoTestCase):
    """人在回路：先暂存，确认后才落库。"""

    def _first_card_id(self) -> int:
        return int(repo.due_cards(self.conn)[0]["id"])

    def test_staged_review_does_not_change_state_until_confirmed(self):
        card_id = self._first_card_id()
        staging_id = repo.stage_review(self.conn, card_id=card_id, rating=5)

        still_due = {int(r["id"]) for r in repo.due_cards(self.conn)}
        self.assertIn(card_id, still_due)  # 还没落库

        repo.confirm_staged(self.conn, staging_id)
        after = {int(r["id"]) for r in repo.due_cards(self.conn)}
        self.assertNotIn(card_id, after)  # 确认后才生效

    def test_double_confirm_is_rejected(self):
        """原子抢占：同一条暂存记录只能确认一次。"""
        card_id = self._first_card_id()
        staging_id = repo.stage_review(self.conn, card_id=card_id, rating=4)
        repo.confirm_staged(self.conn, staging_id)
        with self.assertRaises(LookupError):
            repo.confirm_staged(self.conn, staging_id)

    def test_pending_list_shrinks_after_confirm(self):
        card_id = self._first_card_id()
        staging_id = repo.stage_review(self.conn, card_id=card_id, rating=3)
        self.assertEqual(len(repo.pending_staged(self.conn)), 1)
        repo.confirm_staged(self.conn, staging_id)
        self.assertEqual(len(repo.pending_staged(self.conn)), 0)

    def test_drop_leaves_state_untouched(self):
        """丢弃＝这次打分不算数，记忆状态必须原封不动。"""
        card_id = self._first_card_id()
        staging_id = repo.stage_review(self.conn, card_id=card_id, rating=5)
        repo.drop_staged(self.conn, staging_id)

        still_due = {int(r["id"]) for r in repo.due_cards(self.conn)}
        self.assertIn(card_id, still_due)
        self.assertEqual(repo.pending_staged(self.conn), [])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS n FROM review_logs").fetchone()["n"], 0
        )

    def test_dropped_record_cannot_be_confirmed(self):
        card_id = self._first_card_id()
        staging_id = repo.stage_review(self.conn, card_id=card_id, rating=5)
        repo.drop_staged(self.conn, staging_id)
        with self.assertRaises(LookupError):
            repo.confirm_staged(self.conn, staging_id)


class TestProfileAndStats(RepoTestCase):
    def test_daily_minutes_available(self):
        self.assertEqual(repo.get_profile(self.conn).get("daily_minutes"), "60")

    def test_profile_upsert(self):
        repo.set_profile(self.conn, "daily_minutes", "90")
        self.assertEqual(repo.get_profile(self.conn)["daily_minutes"], "90")

    def test_stats_reports_subject(self):
        stats = repo.subject_stats(self.conn)
        self.assertEqual(len(stats), 1)
        self.assertEqual(stats[0]["subject"], "数据结构")
        self.assertEqual(stats[0]["total"], 20)

    def test_forecast_has_requested_days(self):
        self.assertEqual(len(repo.forecast(self.conn, days=7)), 7)

    def test_weak_points_empty_before_reviews(self):
        self.assertEqual(repo.weak_points(self.conn), [])

    def test_weak_points_after_failure(self):
        card_id = int(repo.due_cards(self.conn)[0]["id"])
        repo.apply_review(self.conn, card_id=card_id, rating=0)
        weak = repo.weak_points(self.conn)
        self.assertEqual(len(weak), 1)
        self.assertEqual(weak[0]["wrongs"], 1)


if __name__ == "__main__":
    unittest.main()
