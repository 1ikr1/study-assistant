"""讲题功能的测试。

这里测的重点是**界线**，不是文字好不好看：
  · 什么时候该花一次模型调用 —— 这是算法在做判断
  · 什么时候不该调 —— 答对了就不该调，连一次请求都不该发出去
  · prompt 里有没有把该给的上下文给全
"""

from __future__ import annotations

import json
import os
import shutil
import unittest
from pathlib import Path

from study_assistant import coach, db, repo, seed
from study_assistant.llm import ChatClient, LLMNotConfigured

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


class FakeModel:
    """假装成 ChatClient。记录每次调用，返回一句固定的话。"""

    def __init__(self, reply: str = "换个说法讲讲", fail: Exception | None = None):
        self.reply = reply
        self.fail = fail
        self.calls: list[list] = []
        self.model = "fake-model"

    def chat(self, messages, **kwargs):
        self.calls.append(list(messages))
        if self.fail:
            raise self.fail
        return self.reply


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

    def card(self) -> dict:
        return dict(repo.get_card(self.conn, self.card_id))


class TestShouldExplain(Base):
    def test_only_wrong_answers_are_worth_a_model_call(self):
        """这是**算法**做的判断，不是模型 —— 而且它决定了要不要花钱。

        答对了还去调模型，除了浪费，模型还会"硬找问题"，
        反而给已经答对的学生添乱。
        """
        for rating in (0, 1, 2):
            self.assertTrue(coach.should_explain(rating), f"{rating} 分应该讲")
        for rating in (3, 4, 5):
            self.assertFalse(coach.should_explain(rating), f"{rating} 分不该讲")

    def test_out_of_range_ratings_do_not_crash(self):
        self.assertFalse(coach.should_explain(9))
        self.assertFalse(coach.should_explain(-1))


class TestPrompt(Base):
    def test_prompt_carries_everything_the_model_needs(self):
        card = self.card()
        messages = coach.build_explain_messages(
            card, rating=0, style="喜欢看例题，不爱看大段文字")

        self.assertEqual(messages[0].role, "system")
        self.assertEqual(messages[1].role, "user")
        body = messages[1].content
        for must in (card["subject"], card["knowledge_point"],
                     card["question"], card["answer"]):
            self.assertIn(must, body)
        self.assertIn("完全不会", body)                    # 评分说成人话
        self.assertIn("喜欢看例题", body)                  # 画像进了 prompt

    def test_prompt_is_deterministic(self):
        """同样的输入必须得到同样的 prompt —— 不然没法测，也没法复现问题。"""
        card = self.card()
        a = coach.build_explain_messages(card, rating=1)
        b = coach.build_explain_messages(card, rating=1)
        self.assertEqual([m.content for m in a], [m.content for m in b])

    def test_style_is_optional(self):
        card = self.card()
        body = coach.build_explain_messages(card, rating=2)[1].content
        self.assertNotIn("学习偏好", body)

    def test_previous_wrong_answers_are_mentioned(self):
        """答错过 3 次 和 第一次答错，给的讲解该不一样。"""
        self.conn.execute("UPDATE card_state SET lapses = 3 WHERE card_id = ?",
                          (self.card_id,))
        self.conn.commit()
        body = coach.build_explain_messages(self.card(), rating=0)[1].content
        self.assertIn("累计答错过 3 次", body)

    def test_system_prompt_forbids_copying_and_making_things_up(self):
        self.assertIn("不要照抄", coach.SYSTEM_PROMPT)
        self.assertIn("不要编", coach.SYSTEM_PROMPT)


class TestExplain(Base):
    def test_explain_returns_model_text(self):
        model = FakeModel(reply="B 树是多路平衡树……")
        result = coach.explain(self.conn, model, self.card_id, rating=0)
        self.assertEqual(result.text, "B 树是多路平衡树……")
        self.assertEqual(result.card_id, self.card_id)
        self.assertEqual(len(model.calls), 1)

    def test_unknown_card_raises(self):
        with self.assertRaises(LookupError):
            coach.explain(self.conn, FakeModel(), 999999, rating=0)


class TestExplainOrHint(Base):
    """给界面用的版本：永远返回能显示的东西，不抛异常。"""

    def test_correct_answer_does_not_call_the_model(self):
        model = FakeModel()
        out = coach.explain_or_hint(self.conn, model, self.card_id, rating=5)
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "not_needed")
        self.assertEqual(model.calls, [], "答对了还发请求 = 白花钱")

    def test_missing_key_is_reported_not_raised(self):
        model = FakeModel(fail=LLMNotConfigured("还没配置 API key"))
        out = coach.explain_or_hint(self.conn, model, self.card_id, rating=0)
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "llm_error")
        self.assertIn("还没配置", out["text"])

    def test_success_shape(self):
        out = coach.explain_or_hint(self.conn, FakeModel(reply="讲解正文"),
                                    self.card_id, rating=2)
        self.assertTrue(out["ok"])
        self.assertEqual(out["text"], "讲解正文")
        self.assertEqual(out["model"], "fake-model")

    def test_unknown_card_is_reported_not_raised(self):
        out = coach.explain_or_hint(self.conn, FakeModel(), 999999, rating=0)
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "not_found")


class TestAgainstRealClient(Base):
    """再用真的 ChatClient 走一遍，但传输层是假的 —— 验证两层接得上。"""

    def test_full_stack_with_a_fake_transport(self):
        sent: list[dict] = []

        def transport(url, headers, body, timeout):
            sent.append(json.loads(body.decode("utf-8")))
            return 200, json.dumps(
                {"choices": [{"message": {"content": "讲完了"}}]}
            ).encode("utf-8")

        client = ChatClient(api_key="sk-x", base_url="https://x", model="deepseek-chat",
                            transport=transport)
        out = coach.explain_or_hint(self.conn, client, self.card_id, rating=1,
                                    style="喜欢例题")
        self.assertTrue(out["ok"])
        self.assertEqual(out["text"], "讲完了")
        self.assertIn("喜欢例题", sent[0]["messages"][1]["content"])


if __name__ == "__main__":
    unittest.main()
