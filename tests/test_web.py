"""网页后端测试（FastAPI 版）。

用 TestClient 而不是起真服务器 —— 同样是发真实 HTTP 请求走完整中间件，
但不占端口、不用等、跑得快。

重点仍然不是"能不能通"，而是：
  · 敏感数据（.env / 数据库）不出现在页面里
  · 非法输入被 Pydantic 拦住（422）而不是漏进业务层
  · 大模型相关接口全程不打网络
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from study_assistant import db, seed, web

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


class Fake:
    """假模型：按剧本返回，顺便记账。"""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list = []
        self.model = "fake-model"

    def chat(self, messages, **kwargs):
        self.calls.append(list(messages))
        if not self.replies:
            raise AssertionError("模型被多调用了")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = _make_tmpdir()
        self.db_path = self._tmp / "test.db"
        conn = db.connect(self.db_path)
        db.init_db(conn)
        seed.bootstrap(conn)
        conn.close()

        self.app = web.create_app(str(self.db_path))
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)


# --------------------------------------------------------------------------
class TestPageAndReads(Base):
    def test_page_is_served(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/html", r.headers["content-type"])
        for word in ("个人学习助手", "今日行动", "错题复习", "助手"):
            self.assertIn(word, r.text)

    def test_openapi_docs_available(self):
        """换 FastAPI 顺带拿到的：自动生成的接口文档。"""
        self.assertEqual(self.client.get("/docs").status_code, 200)
        spec = self.client.get("/openapi.json").json()
        self.assertIn("/api/dashboard", spec["paths"])

    def test_ping(self):
        self.assertTrue(self.client.get("/api/ping").json()["ok"])

    def test_dashboard(self):
        d = self.client.get("/api/dashboard").json()
        self.assertEqual(d["tasks_total"], 3)
        self.assertEqual(d["due_total"], 20)
        self.assertIn("llm_ready", d)
        self.assertEqual(len(d["focus_7d"]), 7)

    def test_queue_and_stats(self):
        self.assertEqual(len(self.client.get("/api/queue").json()["cards"]), 20)
        stats = self.client.get("/api/stats").json()
        self.assertEqual(stats["subjects"][0]["subject"], "数据结构")

    def test_forecast_bounds_are_validated(self):
        self.assertEqual(len(self.client.get("/api/forecast?days=7").json()["days"]), 7)
        # Pydantic 的 Query(ge=1, le=60) 直接拦掉越界
        self.assertEqual(self.client.get("/api/forecast?days=999").status_code, 422)

    def test_preview_math(self):
        data = self.client.get("/api/preview?card_id=1").json()
        self.assertEqual([s["days"] for s in data["steps"]],
                         [1, 6, 16, 45, 130, 390, 1209])

    def test_unknown_path_returns_404(self):
        self.assertEqual(self.client.get("/api/nope").status_code, 404)

    def test_unknown_card_returns_error_json(self):
        """前端统一读 {error: ...}，所以 404 也要是这个形状。"""
        r = self.client.get("/api/preview?card_id=999999")
        self.assertEqual(r.status_code, 404)
        self.assertIn("error", r.json())


# --------------------------------------------------------------------------
class TestWrites(Base):
    def test_review_changes_state(self):
        card = self.client.get("/api/queue").json()["cards"][0]
        r = self.client.post("/api/review", json={"card_id": card["id"], "rating": 5}).json()
        self.assertEqual(r["before"]["interval_days"], 0)
        self.assertEqual(r["after"]["interval_days"], 1)
        self.assertEqual(r["remaining"], 19)

    def test_invalid_rating_is_rejected_by_pydantic(self):
        """换框架最直接的收益：越界在进业务层之前就被拦掉。"""
        card = self.client.get("/api/queue").json()["cards"][0]
        r = self.client.post("/api/review", json={"card_id": card["id"], "rating": 99})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(len(self.client.get("/api/queue").json()["cards"]), 20)

    def test_staging_round_trip(self):
        card = self.client.get("/api/queue").json()["cards"][0]
        staged = self.client.post("/api/stage",
                                  json={"card_id": card["id"], "rating": 4}).json()
        self.assertEqual(len(self.client.get("/api/staged").json()["items"]), 1)

        self.client.post("/api/confirm", json={"staging_id": staged["staging_id"]})
        self.assertEqual(len(self.client.get("/api/staged").json()["items"]), 0)

        # 再确认一次要被原子抢占挡住
        again = self.client.post("/api/confirm", json={"staging_id": staged["staging_id"]})
        self.assertEqual(again.status_code, 404)
        self.assertIn("已经", again.json()["error"])

    def test_drop_keeps_state(self):
        card = self.client.get("/api/queue").json()["cards"][0]
        staged = self.client.post("/api/stage",
                                  json={"card_id": card["id"], "rating": 5}).json()
        self.client.post("/api/drop", json={"staging_id": staged["staging_id"]})
        self.assertEqual(len(self.client.get("/api/queue").json()["cards"]), 20)
        self.assertEqual(self.client.get("/api/stats").json()["subjects"][0]["lapses"], 0)

    def test_profile_changes_daily_limit(self):
        self.client.post("/api/profile", json={"key": "daily_minutes", "value": "15"})
        d = self.client.get("/api/dashboard").json()
        self.assertEqual(d["profile"]["daily_minutes"], "15")
        self.assertEqual(len(self.client.get("/api/queue").json()["cards"]), 10)

    def test_task_add_and_toggle(self):
        created = self.client.post("/api/task/add", json={
            "title": "英语真题一套", "subject": "英语", "minutes": 90, "priority": 1,
        }).json()
        before = self.client.get("/api/dashboard").json()["tasks_total"]
        self.assertEqual(before, 4)

        self.client.post("/api/task/status",
                         json={"task_id": created["task_id"], "status": "done"})
        self.assertEqual(self.client.get("/api/dashboard").json()["tasks_done"], 1)

    def test_invalid_task_status_is_rejected(self):
        created = self.client.post("/api/task/add", json={"title": "x"}).json()
        r = self.client.post("/api/task/status",
                             json={"task_id": created["task_id"], "status": "瞎写"})
        self.assertEqual(r.status_code, 422)   # Literal 校验，进不了业务层

    def test_focus_logging(self):
        self.client.post("/api/focus/log",
                         json={"planned_min": 25, "actual_sec": 1500, "finished": True})
        self.assertEqual(
            self.client.get("/api/dashboard").json()["focus_seconds_today"], 1500)

    def test_shutdown_returns_ok(self):
        """TestClient 里没有真 server，但接口本身要能正常返回。"""
        r = self.client.post("/api/shutdown")
        self.assertTrue(r.json()["ok"])


# --------------------------------------------------------------------------
class TestCardLibrary(Base):
    """卡片库：之前只能靠改 JSON 导入，现在有了正经入口。"""

    def test_list_and_search(self):
        self.assertEqual(len(self.client.get("/api/cards").json()["cards"]), 20)

        found = self.client.get("/api/cards?q=哈希").json()["cards"]
        self.assertTrue(found)
        self.assertTrue(all("哈希" in c["question"] + c["knowledge_point"]
                            or "哈希" in c["answer"] for c in found))

        none = self.client.get("/api/cards?q=量子力学").json()["cards"]
        self.assertEqual(none, [])

    def test_add_card(self):
        r = self.client.post("/api/card/add", json={
            "subject": "数据结构",
            "knowledge_point": "红黑树",
            "question": "红黑树的五条性质是什么？",
            "answer": "节点非红即黑；根是黑；叶子是黑；红节点的子节点必须是黑；黑高相等。",
            "source": "教材 Ch7",
        }).json()
        self.assertTrue(r["ok"])
        self.assertEqual(len(self.client.get("/api/cards").json()["cards"]), 21)

        # 新卡立刻到期，可以复习
        self.assertEqual(self.client.get("/api/dashboard").json()["due_total"], 21)

    def test_add_card_creates_a_new_subject(self):
        self.client.post("/api/card/add", json={
            "subject": "英语", "knowledge_point": "虚拟语气",
            "question": "What is subjunctive mood?",
            "answer": "表示假设、愿望等非真实情况。",
        })
        subjects = self.client.get("/api/subjects").json()["subjects"]
        self.assertIn("英语", [s["name"] for s in subjects])

    def test_add_card_requires_content(self):
        r = self.client.post("/api/card/add", json={
            "subject": "数据结构", "knowledge_point": "x",
            "question": "", "answer": "y",
        })
        self.assertEqual(r.status_code, 422)

    def test_update_card(self):
        card = self.client.get("/api/cards").json()["cards"][0]
        self.client.post("/api/card/update", json={
            "card_id": card["id"],
            "question": "改过之后的题目",
        })
        again = next(c for c in self.client.get("/api/cards").json()["cards"]
                     if c["id"] == card["id"])
        self.assertEqual(again["question"], "改过之后的题目")
        self.assertEqual(again["answer"], card["answer"], "没传的字段不该被清空")

    def test_delete_card(self):
        card = self.client.get("/api/cards").json()["cards"][0]
        self.client.post("/api/card/delete", json={"card_id": card["id"]})
        self.assertEqual(len(self.client.get("/api/cards").json()["cards"]), 19)

    def test_delete_unknown_card_returns_404(self):
        r = self.client.post("/api/card/delete", json={"card_id": 999999})
        self.assertEqual(r.status_code, 404)

    def test_update_subject_exam_date(self):
        subject = self.client.get("/api/subjects").json()["subjects"][0]
        self.client.post("/api/subject/update", json={
            "subject_id": subject["id"], "exam_date": "2027-01-15"})
        d = self.client.get("/api/dashboard").json()
        self.assertEqual(d["exam_date"], "2027-01-15")

    def test_clear_exam_date(self):
        subject = self.client.get("/api/subjects").json()["subjects"][0]
        self.client.post("/api/subject/update", json={
            "subject_id": subject["id"], "exam_date": ""})
        d = self.client.get("/api/dashboard").json()
        self.assertIsNone(d["countdown_days"], "没考期时倒计时应为空")


# --------------------------------------------------------------------------
class TestLLMEndpointsNeverTouchTheNetwork(Base):
    def test_explain_with_fake_model(self):
        def transport(url, headers, body, timeout):
            return 200, json.dumps(
                {"choices": [{"message": {"content": "换个讲法"}}]}).encode()

        from study_assistant.llm import ChatClient
        card = self.client.get("/api/queue").json()["cards"][0]
        fake = ChatClient(api_key="k", base_url="https://x", model="m",
                          transport=transport)
        with mock.patch.object(web, "build_client", lambda: fake):
            out = self.client.post("/api/explain",
                                   json={"card_id": card["id"], "rating": 0}).json()
        self.assertTrue(out["ok"])
        self.assertEqual(out["text"], "换个讲法")

    def test_explain_skips_the_call_when_you_got_it_right(self):
        sent: list[str] = []

        def transport(url, headers, body, timeout):
            sent.append(url)
            return 200, b"{}"

        from study_assistant.llm import ChatClient
        card = self.client.get("/api/queue").json()["cards"][0]
        fake = ChatClient(api_key="k", base_url="https://x", model="m", transport=transport)
        with mock.patch.object(web, "build_client", lambda: fake):
            out = self.client.post("/api/explain",
                                   json={"card_id": card["id"], "rating": 5}).json()
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "not_needed")
        self.assertEqual(sent, [])

    def test_agent_chat_direct(self):
        with mock.patch.object(web, "build_client",
                               lambda: Fake('{"mode":"direct","answer":"直接答"}')):
            out = self.client.post("/api/agent/chat", json={"question": "你好"}).json()
        self.assertTrue(out["ok"])
        self.assertEqual(out["answer"], "直接答")
        self.assertEqual(out["mode"], "direct")

    def test_agent_stream_emits_ordered_events(self):
        plan = json.dumps({"mode": "multi", "reason": "算+写", "tasks": [
            {"id": "s1", "agent": "stats", "args": {"days": 7}},
            {"id": "s2", "agent": "report", "args": {}, "depends_on": ["s1"]},
        ]}, ensure_ascii=False)
        long_answer = "根据这一周的数据，你的掌握度还不错，最薄弱的是二叉树遍历。"
        good_review = json.dumps({
            "scores": {"忠实": 18, "切题": 17, "完整": 16, "清晰": 17, "有用": 16},
            "issues": [], "summary": "没问题",
        }, ensure_ascii=False)

        with mock.patch.object(web, "build_client",
                               lambda: Fake(plan, long_answer, good_review)):
            r = self.client.post("/api/agent/stream", json={"question": "我这一周怎么样"})

        self.assertEqual(r.status_code, 200)
        self.assertIn("text/event-stream", r.headers["content-type"])

        events = [json.loads(line[6:]) for line in r.text.splitlines()
                  if line.startswith("data: ")]
        kinds = [e["type"] for e in events]
        self.assertEqual(kinds, ["phase", "mode", "step_start", "step_end",
                                 "step_start", "step_end", "gate", "answer"])
        self.assertEqual(events[-1]["text"], long_answer)
        self.assertEqual(events[-2]["verdict"], "pass")

    def test_agent_stream_reports_model_outage_as_an_event(self):
        """模型不可用不该 500，应该变成一条 error 事件。"""
        from study_assistant.llm import LLMNotConfigured
        with mock.patch.object(web, "build_client",
                               lambda: Fake(LLMNotConfigured("没 key"))):
            r = self.client.post("/api/agent/stream", json={"question": "hi"})
        events = [json.loads(line[6:]) for line in r.text.splitlines()
                  if line.startswith("data: ")]
        self.assertEqual(events[-1]["type"], "error")
        self.assertIn("没 key", events[-1]["message"])


# --------------------------------------------------------------------------
class TestDesktopLaunch(Base):
    def test_port_is_exclusive(self):
        """Windows 的 SO_REUSEADDR 允许两个进程绑同一个端口，必须用独占。"""
        first = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        web._use_exclusive_port(first)
        try:
            first.bind(("127.0.0.1", 0))
            first.listen()
            port = first.getsockname()[1]

            second = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            web._use_exclusive_port(second)
            try:
                with self.assertRaises(OSError):
                    second.bind(("127.0.0.1", port))
            finally:
                second.close()
        finally:
            first.close()

    def test_close_window_is_safe_with_bogus_pid(self):
        self.assertEqual(web.close_app_window(None), 0)
        self.assertEqual(web.close_app_window(0), 0)
        self.assertGreaterEqual(web.close_app_window(999_999_999), 0)

    def test_find_app_browser(self):
        exe = web.find_app_browser()
        if exe is None:
            self.skipTest("没装 Edge / Chrome")
        self.assertTrue(Path(exe).is_file())

    def test_fullscreen_launch_args(self):
        if web.find_app_browser() is None:
            self.skipTest("没装 Edge / Chrome")
        captured: list[list[str]] = []

        def fake_popen(args, **kw):
            import types
            captured.append(list(args))
            return types.SimpleNamespace(pid=4321)

        with mock.patch("subprocess.Popen", fake_popen), \
                mock.patch.object(web, "app_profile_dir", lambda: self._tmp / "profile"):
            how, pid = web.open_window("http://x/", app_window=True, fullscreen=True)

        self.assertIn("全屏", how)
        self.assertIsNotNone(pid)
        args = captured[0]
        self.assertTrue(any("?fullscreen=1" in a for a in args))
        self.assertTrue(any(a.startswith("--user-data-dir=") for a in args))
        self.assertIn("--start-fullscreen", args)


if __name__ == "__main__":
    unittest.main()
