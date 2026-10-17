"""原生窗口界面的测试。

窗口不 mainloop，而是建出来直接调用方法（这正好是 tkinter 的好处：
界面操作就是普通的方法调用，可以像测函数一样测）。

没有窗口显示环境时整组跳过，免得在别的机器上跑测试时一片红。
"""

from __future__ import annotations

import os
import shutil
import struct
import unittest
from pathlib import Path
from unittest import mock

try:
    import tkinter as tk

    _probe = tk.Tk()
    _probe.withdraw()
    _probe.destroy()
    HAS_TK = True
except Exception:  # pragma: no cover - 没装 tkinter 或者没有显示环境
    HAS_TK = False

from study_coach import db, repo, seed

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


@unittest.skipUnless(HAS_TK, "这台机器上没有可用的 tkinter")
class DesktopTestCase(unittest.TestCase):
    def setUp(self) -> None:
        from study_coach import desktop  # 延迟导入：tkinter 不能用时别在收集阶段就炸

        self.desktop = desktop
        self._tmp = _make_tmpdir()
        self.processed: list[Path] = []

        self.conn = db.connect(self._tmp / "test.db")
        db.init_db(self.conn)
        seed.load_sample(self.conn)
        seed.default_profile(self.conn)

        self.app = desktop.StudyApp(self.conn, str(self._tmp / "test.db"), visible=False)
        self.app.root.update_idletasks()

    def tearDown(self) -> None:
        try:
            self.app.root.destroy()
        except Exception:
            pass
        try:
            self.conn.close()
        except Exception:
            pass
        shutil.rmtree(self._tmp, ignore_errors=True)

    # ---- 启动与切换 ---------------------------------------------------
    def test_window_opens_with_correct_title_and_state(self):
        self.assertEqual(self.app.root.title(), "study-coach · 备考助手")
        self.assertEqual(self.app.state["due_total"], 20)
        self.assertEqual(self.app.state["limit"], 40)      # 60 分钟 / 1.5
        self.assertEqual(len(self.app.queue), 20)

    def test_icon_is_a_multiresolution_ico(self):
        """图标必须是多分辨率 ico —— 只有一张 256 的话，任务栏里会糊成一团。"""
        self.assertTrue(self.desktop.ICON_ICO.is_file(), "icon.ico 应该跟着包一起在")

        raw = self.desktop.ICON_ICO.read_bytes()
        reserved, kind, count = struct.unpack("<HHH", raw[:6])
        self.assertEqual((reserved, kind), (0, 1), "ICO 文件头不对")
        self.assertEqual(count, 6, "应该含 6 种尺寸")

        sizes = set()
        for i in range(count):
            entry = raw[6 + 16 * i: 22 + 16 * i]
            w, h, _, _, _, _, length, offset = struct.unpack("<BBBBHHII", entry)
            sizes.add(256 if w == 0 else w)
            # 每一项都应指向一段真的 PNG
            self.assertEqual(raw[offset:offset + 8], b"\x89PNG\r\n\x1a\n")
            self.assertGreater(length, 0)

        for want in (16, 32, 48, 64, 128, 256):
            self.assertIn(want, sizes, f"缺少 {want}×{want} 这一档")

    def test_all_tabs_render_without_error(self):
        for tab in ("review", "stats", "load", "profile"):
            self.app.show_tab(tab)
            self.app.root.update_idletasks()
            self.assertEqual(self.app.tab, tab)

    def test_nav_marks_active_tab(self):
        self.app.show_tab("stats")
        self.assertEqual(self.app.nav_buttons["stats"].cget("fg"),
                         self.desktop.FG)
        self.assertEqual(self.app.nav_buttons["review"].cget("fg"),
                         self.desktop.DIM)

    # ---- 复习流程 -----------------------------------------------------
    def test_reveal_then_rate_writes_to_db(self):
        self.assertFalse(self.app.revealed)
        self.app._reveal()
        self.assertTrue(self.app.revealed)

        first = self.app.queue[0]
        with mock.patch.object(self.app, "reload", wraps=self.app.reload):
            pass
        self.app.rate(5)

        after = [c["id"] for c in repo.due_cards(self.conn, limit=None)]
        self.assertNotIn(first["id"], after, "复习过的卡片应该离开今天的队列")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS n FROM review_logs").fetchone()["n"], 1
        )
        self.assertEqual(self.app.index, 1)
        self.assertFalse(self.app.revealed, "打分之后要收起答案，准备下一张")
        self.assertFalse(self.app.toast[1], "答对了不该是红色提示")

    def test_wrong_answer_shows_red_toast_and_counts_a_lapse(self):
        self.app._reveal()
        self.app.rate(0)
        self.assertTrue(self.app.toast[1], "答错要用红色提示")
        row = self.conn.execute("SELECT SUM(lapses) AS s FROM card_state").fetchone()
        self.assertEqual(int(row["s"]), 1)

    def test_space_key_reveals_and_digits_rate(self):
        """快捷键要和网页版一致：空格看答案，0-5 打分。"""
        self.app._on_space(None)
        self.assertTrue(self.app.revealed)
        self.app._on_digit(4)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS n FROM review_logs").fetchone()["n"], 1
        )

    def test_space_does_nothing_before_answer_is_shown_twice(self):
        """已经露出答案之后再按空格，不能把状态搞乱。"""
        self.app._on_space(None)
        self.app._on_space(None)
        self.assertTrue(self.app.revealed)
        self.assertEqual(self.app.index, 0)

    # ---- 逐步确认模式 -------------------------------------------------
    def test_staged_mode_does_not_touch_state(self):
        self.app.staged_mode = True
        self.app._reveal()
        self.app.rate(3)

        self.assertEqual(len(self.app.staged), 1, "打分应该进了暂存区")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS n FROM review_logs").fetchone()["n"], 0,
            "暂存阶段绝不能写进记忆日志",
        )

    def test_confirm_and_drop_from_the_ui(self):
        self.app.staged_mode = True
        self.app._reveal()
        self.app.rate(3)
        staging_id = self.app.staged[0]["id"]

        before = len(repo.due_cards(self.conn, limit=None))
        self.app.confirm_staged(staging_id)
        self.assertEqual(len(repo.due_cards(self.conn, limit=None)), before - 1)
        self.assertEqual(len(self.app.staged), 0)

        # 同一条再确认一次要被挡住
        self.app.confirm_staged(staging_id)
        self.assertTrue(self.app.toast[1])

    def test_drop_leaves_everything_alone(self):
        self.app.staged_mode = True
        self.app._reveal()
        self.app.rate(5)
        self.app.drop_staged(self.app.staged[0]["id"])

        self.assertEqual(len(self.app.staged), 0)
        self.assertEqual(len(repo.due_cards(self.conn, limit=None)), 20)
        self.assertEqual(self.app.state["reviewed_today"], 0)

    # ---- 画像 ---------------------------------------------------------
    def test_profile_save_changes_the_daily_limit(self):
        self.app.show_tab("profile")
        self.assertIsNotNone(self.app.daily_entry)
        self.app.daily_entry.delete(0, "end")
        self.app.daily_entry.insert(0, "15")
        self.app._save_daily()

        self.assertEqual(self.app.state["profile"]["daily_minutes"], "15")
        self.assertEqual(self.app.state["limit"], 10)          # 15 / 1.5
        self.assertEqual(len(self.app.queue), 10, "队列应该被新预算截断")

    def test_profile_rejects_garbage(self):
        self.app.show_tab("profile")
        self.app.daily_entry.delete(0, "end")
        self.app.daily_entry.insert(0, "abc")
        self.app._save_daily()

        self.assertTrue(self.app.toast[1])
        self.assertEqual(self.app.state["profile"]["daily_minutes"], "60")

    # ---- 和别的壳保持一致 ---------------------------------------------
    def test_desktop_and_web_report_the_same_numbers(self):
        """四个壳共用 view_model，算出来的数必须一样。不然就是分层没做干净。"""
        from study_coach import cli, session, view_model

        state = view_model.state(self.conn)
        self.assertEqual(self.app.state["due_total"], state["due_total"])
        self.assertEqual(
            len(self.app.queue),
            len(view_model.queue(self.conn)),
        )


if __name__ == "__main__":
    unittest.main()
