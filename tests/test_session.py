"""交互式菜单的测试。

交互程序最容易出的问题是"看起来在跑，其实什么都没干"。
所以这里的测试不只看输出文字，还要回头查数据库：**这次复习真的落库了吗**。
做法是把 input() 换成脚本化的假输入（喂一串预定好的按键）。
"""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import unittest
from pathlib import Path
from unittest import mock

from study_coach import db, repo, seed, session

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


class SessionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = _make_tmpdir()
        self.conn = db.connect(self._tmp / "test.db")
        db.init_db(self.conn)
        seed.load_sample(self.conn)
        seed.default_profile(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _feed(self, lines: list[str]) -> str:
        """把一串"按键"喂给菜单，返回它打印出来的所有内容。"""
        pending = iter(lines)

        def fake_input(prompt: str = "") -> str:
            print(prompt, end="")  # 真实的 input 会把提示语打印出来
            try:
                return next(pending)
            except StopIteration:
                raise EOFError from None

        buf = io.StringIO()
        with mock.patch("builtins.input", fake_input), contextlib.redirect_stdout(buf):
            session.main_menu(self.conn)
        return buf.getvalue()


class TestMenuNavigation(SessionTestCase):
    def test_quit_from_menu(self):
        out = self._feed(["0"])
        self.assertIn("下次见", out)

    def test_invalid_choice_is_reported(self):
        out = self._feed(["9", "0"])
        self.assertIn("请输入 0-5 之间的序号", out)

    def test_overview_shows_due_count(self):
        out = self._feed(["0"])
        self.assertIn("今天到期 20 张", out)


class TestReviewSession(SessionTestCase):
    def test_rating_a_card_actually_writes_to_db(self):
        """走一遍"开始复习"，确认第一张卡真的被记下来了。"""
        before = len(repo.due_cards(self.conn, limit=None))

        out = self._feed([
            "1",    # 开始复习
            "",     # 回车开始
            "",     # 想好了，看答案
            "5",    # 打满分
            "",     # 第二张：看答案
            "q",    # 第二张：不想打了，退出
            "",     # 回到菜单
            "0",    # 退出程序
        ])

        self.assertIn("本轮完成 1 张", out)
        self.assertIn("答对 1 张（100%）", out)

        after = len(repo.due_cards(self.conn, limit=None))
        self.assertEqual(after, before - 1, "复习过的卡片应该离开到期队列")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS n FROM review_logs").fetchone()["n"], 1
        )

    def test_bad_rating_is_rejected_then_accepted(self):
        """乱填的评分要被挡回来，并且不能污染数据。"""
        out = self._feed([
            "1", "", "",
            "abc",   # 不是数字
            "7",     # 超出范围
            "3",     # 终于合法
            "", "q", "", "0",
        ])
        self.assertIn("只能填 0 到 5", out)
        self.assertIn("本轮完成 1 张", out)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS n FROM review_logs").fetchone()["n"], 1
        )

    def test_wrong_answer_is_recorded_as_a_lapse(self):
        self._feed(["1", "", "", "0", "", "q", "", "0"])
        row = self.conn.execute("SELECT SUM(lapses) AS s FROM card_state").fetchone()
        self.assertEqual(int(row["s"]), 1)


class TestPreviewScreen(SessionTestCase):
    def test_preview_does_not_change_anything(self):
        """预览是只读的——看一百次也不能改变任何一张卡的状态。"""
        snapshot = [
            tuple(r)
            for r in self.conn.execute(
                "SELECT card_id, ef, interval_days, repetitions, due_at, lapses "
                "FROM card_state ORDER BY card_id"
            )
        ]
        out = self._feed(["5", "1", "q", "0"])
        self.assertIn("如果以后每次都答满分", out)

        after = [
            tuple(r)
            for r in self.conn.execute(
                "SELECT card_id, ef, interval_days, repetitions, due_at, lapses "
                "FROM card_state ORDER BY card_id"
            )
        ]
        self.assertEqual(snapshot, after)


if __name__ == "__main__":
    unittest.main()
