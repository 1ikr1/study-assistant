"""网页后端的接口测试。

真的起一个服务器、真的发 HTTP 请求——不 mock。
因为这一层最容易出问题的地方恰恰是"路由写错""JSON 编码错"这种
只有真发一次请求才会暴露的东西。
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
import types
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from study_coach import db, seed, web

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


class WebTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = _make_tmpdir()
        self.db_path = self._tmp / "test.db"

        conn = db.connect(self.db_path)
        db.init_db(conn)
        seed.load_sample(conn)
        seed.default_profile(conn)
        conn.close()

        self.httpd = web.make_server(self.db_path, port=0)  # 0 = 随便挑个空闲端口
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        shutil.rmtree(self._tmp, ignore_errors=True)

    # ---- 两个小助手 ---------------------------------------------------
    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def get(self, path: str):
        with urllib.request.urlopen(self.url(path), timeout=5) as res:
            return json.loads(res.read().decode("utf-8"))

    def post(self, path: str, payload: dict, expect_error: bool = False):
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.url(path), data=data, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as res:
                return json.loads(res.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = json.loads(exc.read().decode("utf-8"))
            if expect_error:
                return body
            raise AssertionError(f"接口报错了：{body}") from None

    # ---- 测试 ----------------------------------------------------------
    def test_page_is_served(self):
        with urllib.request.urlopen(self.url("/"), timeout=5) as res:
            self.assertEqual(res.status, 200)
            self.assertIn("text/html", res.headers["Content-Type"])
            body = res.read().decode("utf-8")
        self.assertIn("study-coach", body)
        # 工作台骨架：侧边栏 + 五个页面里的几个关键词
        for word in ("今日行动", "开始专注", "错题复习", "学习洞察", "countdown"):
            self.assertIn(word, body, f"页面里应该有「{word}」")

    def test_dashboard_endpoint(self):
        data = self.get("/api/dashboard")
        self.assertEqual(data["tasks_total"], 3)
        self.assertEqual(data["focus_seconds_today"], 0)
        self.assertEqual(data["focus_target_seconds"], 3600)
        self.assertEqual(len(data["focus_7d"]), 7)
        self.assertIsNotNone(data["exam_date"])

    def test_add_task_and_toggle_it(self):
        before = self.get("/api/dashboard")["tasks_total"]
        created = self.post("/api/task/add", {
            "title": "英语真题一套", "subject": "英语", "minutes": 90, "priority": 1,
        })
        self.assertTrue(created["ok"])

        data = self.get("/api/dashboard")
        self.assertEqual(data["tasks_total"], before + 1)
        new_task = [t for t in data["tasks"] if t["title"] == "英语真题一套"][0]
        self.assertEqual(new_task["minutes"], 90)

        self.post("/api/task/status", {"task_id": created["task_id"], "status": "done"})
        data = self.get("/api/dashboard")
        self.assertEqual(data["tasks_done"], 1)

    def test_focus_logging_shows_up_on_the_dashboard(self):
        self.post("/api/focus/log", {"planned_min": 25, "actual_sec": 1500, "finished": True})
        data = self.get("/api/dashboard")
        self.assertEqual(data["focus_seconds_today"], 1500)
        self.assertEqual(data["focus_7d"][-1]["seconds"], 1500)

    def test_bad_payload_is_reported_not_crashed(self):
        body = self.post("/api/task/status", {}, expect_error=True)
        self.assertIn("error", body)

    def test_dashboard_tells_the_ui_whether_ai_is_available(self):
        data = self.get("/api/dashboard")
        self.assertIn("llm_ready", data)
        self.assertIsInstance(data["llm_ready"], bool)
        self.assertIn("llm_model", data)

    def test_explain_endpoint_never_touches_the_network(self):
        """整条链路走通，但 ChatClient 的传输层是假的 ——
        跑测试不该烧 token，也不该依赖有没有网。"""
        from study_coach.llm import ChatClient

        def transport(url, headers, body, timeout):
            return 200, json.dumps(
                {"choices": [{"message": {"content": "换个讲法：先看为什么"}}]}
            ).encode("utf-8")

        card = self.get("/api/queue")["cards"][0]
        fake = ChatClient(api_key="sk-x", base_url="https://x", model="test-model",
                          transport=transport)
        with mock.patch.object(web, "build_client", lambda: fake):
            out = self.post("/api/explain", {"card_id": card["id"], "rating": 0})
        self.assertTrue(out["ok"])
        self.assertEqual(out["text"], "换个讲法：先看为什么")
        self.assertEqual(out["model"], "test-model")

    def test_explain_endpoint_says_no_thanks_when_you_got_it_right(self):
        """答对了就不该发请求 —— 构造客户端本身不花钱，发出去才花钱。"""
        from study_coach.llm import ChatClient

        sent: list[str] = []

        def transport(url, headers, body, timeout):
            sent.append(url)
            return 200, b"{}"

        card = self.get("/api/queue")["cards"][0]
        fake = ChatClient(api_key="sk-x", base_url="https://x", model="m",
                          transport=transport)
        with mock.patch.object(web, "build_client", lambda: fake):
            out = self.post("/api/explain", {"card_id": card["id"], "rating": 5})

        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "not_needed")
        self.assertEqual(sent, [], "答对了还发请求 = 白花钱")

    def test_state_and_queue(self):
        state = self.get("/api/state")
        self.assertEqual(state["due_total"], 20)
        self.assertEqual(state["total_cards"], 20)
        self.assertEqual(state["limit"], 40)  # 60 分钟 / 1.5 分钟

        queue = self.get("/api/queue")["cards"]
        self.assertEqual(len(queue), 20)
        self.assertIn("question", queue[0])

    def test_unknown_path_returns_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(self.url("/api/nope"), timeout=5)
        self.assertEqual(ctx.exception.code, 404)

    def test_ping(self):
        self.assertTrue(self.get("/api/ping")["ok"])

    def test_close_app_window_is_safe_with_a_bogus_pid(self):
        """找不到窗口时只能返回 0，绝不能抛异常 ——
        关闭流程里任何一步炸掉，用户就会卡在一个关不掉的窗口里。"""
        self.assertEqual(web.close_app_window(None), 0)
        self.assertEqual(web.close_app_window(0), 0)
        self.assertGreaterEqual(web.close_app_window(999_999_999), 0)

    def test_find_app_browser(self):
        """应用模式靠浏览器实现，所以要能可靠地找到它。

        没装 Edge / Chrome 的机器上返回 None 也算正常，这时会退回普通标签页。
        """
        exe = web.find_app_browser()
        if exe is None:
            self.skipTest("这台机器上没找到 Edge / Chrome")
        self.assertTrue(Path(exe).is_file())
        self.assertIn(Path(exe).stem.lower(), ("msedge", "chrome"))

    def test_fullscreen_launch_args(self):
        """「默认全屏」能不能成立，全看这几个参数拼得对不对。

        最关键的是 --user-data-dir：Edge 已经在后台跑着的时候，新启动的
        msedge.exe 只是给它发个消息，命令行参数会被直接丢掉，
        --start-fullscreen 就白写了。给一个独占的配置目录才能逼它开新进程。
        """
        if web.find_app_browser() is None:
            self.skipTest("这台机器上没找到 Edge / Chrome")

        captured: list[list[str]] = []

        def fake_popen(args, **kw):
            captured.append(list(args))
            return types.SimpleNamespace(pid=4321)

        with mock.patch("subprocess.Popen", fake_popen), \
                mock.patch.object(web, "app_profile_dir", lambda: self._tmp / "profile"):
            how, pid = web.open_window(
                "http://127.0.0.1:8965/", app_window=True, fullscreen=True)

        self.assertIn("全屏", how)
        self.assertIsNotNone(pid, "要拿得到进程号，「关闭应用」才能把窗口也关掉")
        args = captured[0]
        self.assertTrue(any(a.startswith("--app=http://127.0.0.1:8965/?fullscreen=1")
                            for a in args), "--app 要带上 ?fullscreen=1")
        self.assertTrue(any(a.startswith("--user-data-dir=") for a in args),
                        "必须有独占配置目录，否则参数会被已有 Edge 进程吞掉")
        self.assertIn("--start-fullscreen", args)

    def test_missing_profile_dir_degrades_to_windowed_app(self):
        """配置目录建不出来时，只退化成"带标题栏的应用窗口"，
        不能连应用窗口都不开、掉到普通浏览器标签页。"""
        if web.find_app_browser() is None:
            self.skipTest("这台机器上没找到 Edge / Chrome")

        captured: list[list[str]] = []

        def fake_popen(args, **kw):
            captured.append(list(args))
            return types.SimpleNamespace(pid=4321)

        def boom():
            raise OSError("建不出来")

        with mock.patch("subprocess.Popen", fake_popen), \
                mock.patch.object(web, "app_profile_dir", boom):
            how, pid = web.open_window(
                "http://127.0.0.1:8966/", app_window=True, fullscreen=True)

        self.assertIn("应用模式", how, "仍然是应用窗口")
        self.assertIsNotNone(pid)
        self.assertFalse(any(a.startswith("--user-data-dir=") for a in captured[0]))

    def test_port_is_exclusive(self):
        """Windows 的 SO_REUSEADDR 允许两个进程绑同一个端口，会静默出错。

        真出过这个 bug：端口探测永远说"空闲"，最后两个服务抢一个端口，
        请求随机落到其中一个，表现成"数据时好时坏"。所以这里钉死它。
        """
        with self.assertRaises(OSError):
            extra = web.make_server(self.db_path, port=self.port)
            extra.server_close()

    def test_stats_and_forecast(self):
        stats = self.get("/api/stats")
        self.assertEqual(stats["subjects"][0]["subject"], "数据结构")
        self.assertEqual(len(self.get("/api/forecast?days=7")["days"]), 7)

    def test_review_changes_state_and_leaves_the_queue(self):
        card = self.get("/api/queue")["cards"][0]
        result = self.post("/api/review", {"card_id": card["id"], "rating": 5})

        self.assertEqual(result["before"]["interval_days"], 0)
        self.assertEqual(result["after"]["interval_days"], 1)
        self.assertEqual(result["remaining"], 19)

        remaining = [c["id"] for c in self.get("/api/queue")["cards"]]
        self.assertNotIn(card["id"], remaining)

    def test_bad_rating_is_rejected(self):
        card = self.get("/api/queue")["cards"][0]
        body = self.post("/api/review", {"card_id": card["id"], "rating": 9},
                         expect_error=True)
        self.assertIn("error", body)
        # 被拒绝之后，队列不能有任何变化
        self.assertEqual(len(self.get("/api/queue")["cards"]), 20)

    def test_preview_math(self):
        data = self.get("/api/preview?card_id=1")
        self.assertEqual([s["days"] for s in data["steps"]], [1, 6, 16, 45, 130, 390, 1209])

    def test_staging_round_trip_and_atomic_confirm(self):
        cards = self.get("/api/queue")["cards"]

        staged = self.post("/api/stage", {"card_id": cards[0]["id"], "rating": 4})
        self.assertEqual(len(self.get("/api/staged")["items"]), 1)

        # 还没确认，卡片必须还在队列里
        self.assertIn(cards[0]["id"], [c["id"] for c in self.get("/api/queue")["cards"]])

        self.post("/api/confirm", {"staging_id": staged["staging_id"]})
        self.assertEqual(len(self.get("/api/staged")["items"]), 0)
        self.assertNotIn(cards[0]["id"], [c["id"] for c in self.get("/api/queue")["cards"]])

        # 再确认一次要被挡住
        again = self.post("/api/confirm", {"staging_id": staged["staging_id"]},
                          expect_error=True)
        self.assertIn("已经", again["error"])

    def test_drop_keeps_state_untouched(self):
        cards = self.get("/api/queue")["cards"]
        staged = self.post("/api/stage", {"card_id": cards[0]["id"], "rating": 5})
        self.post("/api/drop", {"staging_id": staged["staging_id"]})

        self.assertEqual(len(self.get("/api/staged")["items"]), 0)
        self.assertEqual(len(self.get("/api/queue")["cards"]), 20)
        self.assertEqual(self.get("/api/state")["reviewed_today"], 0)

    def test_profile_update_changes_the_daily_limit(self):
        self.post("/api/profile", {"key": "daily_minutes", "value": "15"})
        state = self.get("/api/state")
        self.assertEqual(state["profile"]["daily_minutes"], "15")
        self.assertEqual(state["limit"], 10)  # 15 / 1.5
        self.assertEqual(len(self.get("/api/queue")["cards"]), 10)

    def test_shutdown_endpoint_really_stops_the_server(self):
        self.assertTrue(self.post("/api/shutdown", {})["ok"])

        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                urllib.request.urlopen(self.url("/api/ping"), timeout=1).close()
            except OSError:
                return  # 连不上了 —— 这正是我们想要的
            time.sleep(0.1)
        self.fail("调了 /api/shutdown 之后，服务器还在响应")


if __name__ == "__main__":
    unittest.main()
