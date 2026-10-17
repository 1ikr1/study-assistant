"""大模型客户端与配置的测试。

重点：**全程不打网络**。ChatClient 留了一个可注入的 transport，
测试时换成假的 —— 不这么做的话，要么测不了错误分支，
要么每跑一次测试就烧一次 token。
"""

from __future__ import annotations

import json
import os
import shutil
import unittest
from pathlib import Path

from study_coach import config
from study_coach.llm import (
    ChatClient,
    LLMError,
    LLMHTTPError,
    LLMNotConfigured,
    Message,
    extract_content,
)

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


# --------------------------------------------------------------------------
# 假传输层
# --------------------------------------------------------------------------
class Recorder:
    """记下发出去的东西，返回一个预置响应。"""

    def __init__(self, status: int = 200, body: bytes | None = None, reply: str = "好的"):
        self.status = status
        self.calls: list[dict] = []
        self.body = body if body is not None else json.dumps(
            {"choices": [{"message": {"role": "assistant", "content": reply}}]}
        ).encode("utf-8")

    def __call__(self, url, headers, body, timeout):
        self.calls.append({"url": url, "headers": headers, "body": body, "timeout": timeout})
        return self.status, self.body


def make_client(transport) -> ChatClient:
    return ChatClient(
        api_key="sk-test",
        base_url="https://example.invalid/api",
        model="deepseek-chat",
        timeout=12.0,
        transport=transport,
    )


# --------------------------------------------------------------------------
class TestEnvParsing(unittest.TestCase):
    def test_parses_the_common_shapes(self):
        parsed = config.parse_env(
            "A=1\n"
            "# 这是注释\n"
            "\n"
            "export B=2\n"
            'C = "有空格的值"\n'
            "D='单引号也行'\n"
            "E=带#号的?值\n"
            "这一行没有等号\n"
            "=没有键\n"
        )
        self.assertEqual(parsed["A"], "1")
        self.assertEqual(parsed["B"], "2")
        self.assertEqual(parsed["C"], "有空格的值")
        self.assertEqual(parsed["D"], "单引号也行")
        self.assertEqual(parsed["E"], "带#号的?值")
        self.assertNotIn("", parsed)

    def test_only_strips_matched_quotes(self):
        """只有一个引号时不能乱削 —— 那是值的一部分。"""
        self.assertEqual(config.parse_env('A="只有开头')[ 'A'], '"只有开头')
        self.assertEqual(config.parse_env("A=结尾有\"")["A"], '结尾有"')

    def test_empty_and_missing_file_are_not_errors(self):
        tmp = _make_tmpdir()
        try:
            self.assertEqual(config.read_env_file(tmp / "不存在"), {})
            (tmp / ".env").write_text("", encoding="utf-8")
            self.assertEqual(config.read_env_file(tmp / ".env"), {})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_write_env_template_never_overwrites(self):
        """模板只在没有 .env 时生成 —— 绝不能把用户填好的 key 冲掉。"""
        tmp = _make_tmpdir()
        try:
            target = tmp / ".env"
            config.write_env_template(target)
            self.assertIn("DEEPSEEK_API_KEY=", target.read_text(encoding="utf-8"))

            target.write_text("DEEPSEEK_API_KEY=我的密钥\n", encoding="utf-8")
            config.write_env_template(target)
            self.assertEqual(target.read_text(encoding="utf-8"),
                             "DEEPSEEK_API_KEY=我的密钥\n", "不能覆盖已有的 .env")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_environment_variable_wins_over_file(self):
        """临时换个 key 试一下，不该还要去改文件。"""
        tmp = _make_tmpdir()
        try:
            env_file = tmp / ".env"
            env_file.write_text("KEY=来自文件\n", encoding="utf-8")
            os.environ["STUDY_COACH_TEST_KEY"] = "来自环境变量"
            try:
                got = config.get_setting("STUDY_COACH_TEST_KEY", "默认", env_file)
                self.assertEqual(got, "来自环境变量")
            finally:
                os.environ.pop("STUDY_COACH_TEST_KEY", None)
            self.assertEqual(config.get_setting("KEY", "", env_file), "来自文件")
            self.assertEqual(config.get_setting("没有的", "默认", env_file), "默认")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestRequestBuilding(unittest.TestCase):
    def test_endpoint_and_headers(self):
        client = make_client(Recorder())
        self.assertEqual(client.endpoint(), "https://example.invalid/api/chat/completions")
        self.assertEqual(client.build_headers()["Authorization"], "Bearer sk-test")
        self.assertEqual(client.build_headers()["Content-Type"], "application/json")

    def test_base_url_trailing_slash_is_tolerated(self):
        client = ChatClient(api_key="k", base_url="https://x/y/", model="m")
        self.assertEqual(client.endpoint(), "https://x/y/chat/completions")

    def test_payload_shape(self):
        client = make_client(Recorder())
        payload = client.build_payload(
            [Message("system", "你是老师"), Message("user", "讲讲这题")],
            temperature=0.3, max_tokens=123)

        self.assertEqual(payload["model"], "deepseek-chat")
        self.assertEqual(payload["temperature"], 0.3)
        self.assertEqual(payload["max_tokens"], 123)
        self.assertFalse(payload["stream"])
        self.assertEqual(payload["messages"][0], {"role": "system", "content": "你是老师"})


class TestChat(unittest.TestCase):
    def test_success(self):
        rec = Recorder(reply="  这就是答案  ")
        self.assertEqual(make_client(rec).chat([Message("user", "hi")]), "这就是答案")
        self.assertEqual(len(rec.calls), 1)
        self.assertEqual(rec.calls[0]["timeout"], 12.0)

    def test_body_is_real_json_utf8(self):
        """中文必须以 UTF-8 发出去，而且不能变成 \\uXXXX 转义 ——
        虽然两者都合法，但转义会让日志和抓包完全看不懂。"""
        rec = Recorder()
        make_client(rec).chat([Message("user", "讲一下二叉树")])
        raw = rec.calls[0]["body"]
        self.assertIn("讲一下二叉树".encode("utf-8"), raw)
        self.assertEqual(json.loads(raw.decode("utf-8"))["messages"][0]["content"],
                         "讲一下二叉树")

    def test_missing_api_key_is_its_own_error(self):
        """没配 key 不是"故障"，是"功能没开" —— 这两种情况界面上的说法不一样。"""
        client = ChatClient(api_key="", base_url="https://x", model="m",
                            transport=Recorder())
        with self.assertRaises(LLMNotConfigured):
            client.chat([Message("user", "hi")])

    def test_http_error_carries_the_message(self):
        body = json.dumps({"error": {"message": "Invalid API key"}}).encode()
        client = make_client(Recorder(status=401, body=body))
        with self.assertRaises(LLMHTTPError) as ctx:
            client.chat([Message("user", "hi")])
        self.assertEqual(ctx.exception.status, 401)
        self.assertIn("Invalid API key", str(ctx.exception))

    def test_http_error_without_a_known_shape_still_reports(self):
        body = json.dumps({"message": "余额不足"}).encode()
        client = make_client(Recorder(status=402, body=body))
        with self.assertRaises(LLMHTTPError) as ctx:
            client.chat([Message("user", "hi")])
        self.assertIn("余额不足", str(ctx.exception))

    def test_non_json_body(self):
        client = make_client(Recorder(status=200, body=b"<html>502</html>"))
        with self.assertRaises(LLMError):
            client.chat([Message("user", "hi")])


class TestExtractContent(unittest.TestCase):
    def test_normal(self):
        self.assertEqual(
            extract_content({"choices": [{"message": {"content": " 答案 "}}]}), "答案")

    def test_all_the_ways_it_can_be_empty(self):
        """这几种"看起来像响应"的东西，都必须当错误抛出来。
        否则上层会拿到空字符串，当作模型说了句"什么都没有"。"""
        for bad in (
            {},                                   # 什么都没有
            {"choices": []},                      # 空数组
            {"choices": None},                    # 不是数组
            {"choices": [{}]},                    # 少了 message
            {"choices": [{"message": {}}]},       # 少了 content
            {"choices": [{"message": {"content": None}}]},
            {"choices": [{"message": {"content": "   "}}]},
        ):
            with self.assertRaises(LLMError, msg=str(bad)):
                extract_content(bad)


if __name__ == "__main__":
    unittest.main()
