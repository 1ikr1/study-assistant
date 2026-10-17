"""多 Agent 编排层的测试。

重点不在"能不能跑通"，而在**门控**：
  · 模型给的计划不合规时，代码必须拒绝（而不是照着执行）
  · 模型不可用时，必须降级而不是 500
  · 子 Agent 只拿到自己声明的依赖，不是全部上下文
"""

from __future__ import annotations

import json
import os
import shutil
import unittest
from pathlib import Path
from unittest import mock

from study_assistant import db, repo, seed
from study_assistant.agent import orchestrator, schemas, supervisor, workers
from study_assistant.agent.schemas import PlanError
from study_assistant.agent.workers import Worker
from study_assistant.llm import LLMError, LLMNotConfigured

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


class ScriptedClient:
    """按剧本依次返回。剧本用完还调 = 测试写错了（多花了钱）。"""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[list] = []
        self.model = "scripted"

    def chat(self, messages, **kwargs):
        self.calls.append(list(messages))
        if not self.replies:
            raise AssertionError("模型被多调用了一次")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def _plan_json(*tasks, reason="需要算数据 + 出成品") -> str:
    return json.dumps({"mode": "multi", "reason": reason, "tasks": list(tasks)},
                      ensure_ascii=False)


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = _make_tmpdir()
        self.conn = db.connect(self._tmp / "test.db")
        db.init_db(self.conn)
        seed.bootstrap(self.conn)
        self.card_id = int(repo.due_cards(self.conn)[0]["id"])

    def tearDown(self) -> None:
        self.conn.close()
        shutil.rmtree(self._tmp, ignore_errors=True)


# --------------------------------------------------------------------------
class TestExtractJson(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(schemas.extract_json('{"mode":"direct"}'), {"mode": "direct"})

    def test_fenced(self):
        raw = '```json\n{"mode":"direct"}\n```'
        self.assertEqual(schemas.extract_json(raw), {"mode": "direct"})

    def test_with_chatter_around_it(self):
        """真实模型经常在 JSON 前后加一句话。"""
        raw = '好的，这是计划：\n{"mode":"direct","answer":"x"}\n希望有帮助！'
        self.assertEqual(schemas.extract_json(raw)["answer"], "x")

    def test_nested_braces_inside_strings(self):
        raw = '{"a":"这里有个 } 括号","b":{"c":1}}'
        self.assertEqual(schemas.extract_json(raw)["b"]["c"], 1)

    def test_garbage(self):
        for bad in ("", "完全没有 JSON", "{不配对"):
            with self.assertRaises(PlanError, msg=bad):
                schemas.extract_json(bad)


class TestParsePlan(unittest.TestCase):
    def _valid(self):
        return {
            "reason": "要算 + 要写",
            "tasks": [
                {"id": "s1", "agent": "stats", "args": {"days": 7}},
                {"id": "s2", "agent": "report", "args": {}, "depends_on": ["s1"]},
            ],
        }

    def test_valid(self):
        plan = schemas.parse_plan(self._valid())
        self.assertEqual(len(plan.items), 2)
        self.assertEqual(plan.items[1].depends_on, ("s1",))

    def test_rejects_unknown_agent(self):
        """模型写出不存在的子 Agent 一律拒绝 —— 这是白名单，不是建议。"""
        data = self._valid()
        data["tasks"][0]["agent"] = "shell"
        with self.assertRaises(PlanError):
            schemas.parse_plan(data)

    def test_rejects_duplicate_ids(self):
        data = self._valid()
        data["tasks"][1]["id"] = "s1"
        with self.assertRaises(PlanError):
            schemas.parse_plan(data)

    def test_rejects_dependency_on_the_future(self):
        """依赖后面才出现的任务 = 环的前兆，直接拒绝。"""
        data = self._valid()
        data["tasks"][0]["depends_on"] = ["s2"]
        with self.assertRaises(PlanError):
            schemas.parse_plan(data)

    def test_rejects_too_many_steps(self):
        data = {"reason": "", "tasks": [
            {"id": f"s{i}", "agent": "stats"} for i in range(schemas.MAX_STEPS + 1)
        ]}
        with self.assertRaises(PlanError):
            schemas.parse_plan(data)

    def test_multi_step_must_end_with_report(self):
        """没有 report 就没人负责"把数据说成人话"。"""
        data = {"reason": "", "tasks": [
            {"id": "s1", "agent": "stats"},
            {"id": "s2", "agent": "scheduler"},
        ]}
        with self.assertRaises(PlanError):
            schemas.parse_plan(data)

    def test_empty_tasks(self):
        with self.assertRaises(PlanError):
            schemas.parse_plan({"reason": "x", "tasks": []})


# --------------------------------------------------------------------------
class TestWorkers(Base):
    def test_scheduler_is_pure_code_and_uses_the_budget(self):
        text = workers.run_scheduler(self.conn, None, {"minutes": 15}, {})
        self.assertIn("15 分钟", text)
        self.assertIn("到期卡片", text)

    def test_stats_counts_real_numbers(self):
        text = workers.run_stats(self.conn, None, {"days": 7}, {})
        self.assertIn("20 张卡", text)
        self.assertIn("未来 7 天", text)

    def test_knowledge_rejects_a_missing_card_gracefully(self):
        text = workers.run_knowledge(self.conn, ScriptedClient(), {"card_id": 99999}, {})
        self.assertIn("失败", text)

    def test_catalog_lists_all_workers(self):
        text = workers.catalog()
        for name in ("scheduler", "stats", "knowledge", "report"):
            self.assertIn(name, text)


# --------------------------------------------------------------------------
class TestSupervisor(Base):
    def test_direct_decision(self):
        client = ScriptedClient('{"mode":"direct","answer":"先睡一觉"}')
        d = supervisor.decide(self.conn, client, "我好累")
        self.assertEqual(d.mode, "direct")
        self.assertEqual(d.answer, "先睡一觉")

    def test_multi_decision(self):
        client = ScriptedClient(_plan_json(
            {"id": "s1", "agent": "stats", "args": {"days": 7}},
            {"id": "s2", "agent": "report", "args": {}, "depends_on": ["s1"]}))
        d = supervisor.decide(self.conn, client, "我这一周怎么样")
        self.assertEqual(d.mode, "multi")
        self.assertEqual(len(d.plan.items), 2)

    def test_prompt_injects_the_profile(self):
        """Supervisor 是唯一被要求"全量注入画像"的角色。"""
        client = ScriptedClient('{"mode":"direct","answer":"x"}')
        supervisor.decide(self.conn, client, "hi")
        prompt = client.calls[0][0].content
        self.assertIn("每天可用分钟数", prompt)
        self.assertIn("学习偏好", prompt)

    def test_unknown_mode_raises(self):
        client = ScriptedClient('{"mode":"whatever"}')
        with self.assertRaises(PlanError):
            supervisor.decide(self.conn, client, "hi")


# --------------------------------------------------------------------------
class TestOrchestrator(Base):
    def test_direct_path_makes_one_call(self):
        client = ScriptedClient('{"mode":"direct","answer":"直接回答"}')
        out = orchestrator.run(self.conn, client, "你好")
        self.assertEqual(out["mode"], "direct")
        self.assertEqual(out["answer"], "直接回答")
        self.assertEqual(len(client.calls), 1, "简单问题只该花一次调用")
        self.assertEqual(len(out["trace"]), 1)

    def test_multi_path_runs_workers_in_order(self):
        client = ScriptedClient(
            _plan_json(
                {"id": "s1", "agent": "stats", "args": {"days": 7}},
                {"id": "s2", "agent": "report", "args": {}, "depends_on": ["s1"]}),
            "这是最终回答",
        )
        out = orchestrator.run(self.conn, client, "我这一周学得怎么样")

        self.assertEqual(out["mode"], "multi")
        self.assertEqual(out["answer"], "这是最终回答")
        agents = [t["agent"] for t in out["trace"]]
        self.assertEqual(agents, ["supervisor", "stats", "report"])
        self.assertEqual(out["trace"][1]["kind"], "code", "stats 是纯代码")
        self.assertEqual(out["trace"][2]["kind"], "model")
        self.assertTrue(all(t["ms"] >= 0 for t in out["trace"]))

    def test_report_only_sees_its_dependencies(self):
        """Context Engineering：报告只能看到它声明依赖的那一步。"""
        client = ScriptedClient(
            _plan_json(
                {"id": "s1", "agent": "stats", "args": {"days": 7}},
                {"id": "s2", "agent": "report", "args": {}, "depends_on": ["s1"]}),
            "最终回答",
        )
        orchestrator.run(self.conn, client, "怎么样")

        report_prompt = client.calls[1][1].content      # 第二次调用 = report
        self.assertIn("【s1】", report_prompt)
        self.assertIn("用户的问题：怎么样", report_prompt)

    def test_bad_plan_degrades_instead_of_crashing(self):
        client = ScriptedClient("我不是 JSON", "兜底回答")
        out = orchestrator.run(self.conn, client, "问题")
        self.assertEqual(out["mode"], "degraded")
        self.assertEqual(out["answer"], "兜底回答")
        self.assertEqual(out["trace"][0]["status"], "degraded")

    def test_one_worker_failing_does_not_kill_the_request(self):
        def boom(*_a, **_k):
            raise RuntimeError("模拟子 Agent 崩溃")

        broken = dict(workers.REGISTRY)
        broken["stats"] = Worker("stats", "code", "坏的", boom)

        client = ScriptedClient(
            _plan_json(
                {"id": "s1", "agent": "stats", "args": {}},
                {"id": "s2", "agent": "report", "args": {}, "depends_on": ["s1"]}),
            "带着缺口回答",
        )
        with mock.patch.dict(workers.REGISTRY, broken):
            out = orchestrator.run(self.conn, client, "问题")

        self.assertEqual(out["mode"], "multi")
        self.assertEqual(out["trace"][1]["status"], "error")
        self.assertEqual(out["answer"], "带着缺口回答")

    def test_model_down_propagates(self):
        """模型完全不可用是全局问题，应该往上抛，由 web 层翻译成人话。"""
        client = ScriptedClient(LLMNotConfigured("没 key"))
        with self.assertRaises(LLMError):
            orchestrator.run(self.conn, client, "问题")

    def test_empty_question_short_circuits(self):
        client = ScriptedClient()      # 一次都不该调
        out = orchestrator.run(self.conn, client, "   ")
        self.assertEqual(out["trace"], [])
        self.assertEqual(client.calls, [])


if __name__ == "__main__":
    unittest.main()
