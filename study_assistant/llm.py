"""调用大模型。零依赖。

这一层的意义不只是"能发请求"，而是把**不可控的东西关在一个盒子里**：
网络会超时、接口会报错、返回的 JSON 可能缺字段、模型可能不听话。
上层（coach.py）只想要一句话，不该关心这些。

顺便说明一件事：**调大模型没有什么魔法**，它就是一个带 Authorization 头的
HTTP POST，返回的 JSON 里把 `choices[0].message.content` 取出来。
所以标准库的 urllib.request 完全够用，不需要装 SDK。

设计上留了一个可注入的 transport —— 测试时不打网络，直接喂假响应。
不这么做的话，要么测不了，要么每次跑测试都烧 token。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Iterable

# transport(url, headers, body, timeout) -> (状态码, 响应体)
Transport = Callable[[str, dict[str, str], bytes, float], "tuple[int, bytes]"]


class LLMError(RuntimeError):
    """所有大模型相关的错误。上层只需要 catch 这一个。"""


class LLMNotConfigured(LLMError):
    """还没配 API key。这不是故障，是"功能没开"。"""


class LLMHTTPError(LLMError):
    """接口返回了错误状态码。"""

    def __init__(self, status: int, message: str):
        super().__init__(f"接口返回 {status}：{message}")
        self.status = status
        self.message = message


@dataclass(frozen=True)
class Message:
    """一条对话消息。frozen 是为了防止被各处顺手改掉。"""

    role: str          # system / user / assistant
    content: str

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


def _urllib_transport(url: str, headers: dict[str, str], body: bytes,
                      timeout: float) -> tuple[int, bytes]:
    """默认的发送方式：标准库。"""
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        # 4xx/5xx 也是"收到了响应"，把响应体带回去，调用方才知道原因
        return exc.code, exc.read()
    except urllib.error.URLError as exc:
        raise LLMError(f"网络不通：{exc.reason}") from exc
    except TimeoutError as exc:
        raise LLMError(f"请求超时（{timeout:.0f} 秒）") from exc


class ChatClient:
    """OpenAI 兼容的对话接口客户端。DeepSeek 就是这一套。"""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        timeout: float = 60.0,
        transport: Transport | None = None,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self._transport = transport or _urllib_transport

    # ---- 纯函数部分：拼请求（可以单独断言） ---------------------------
    def endpoint(self) -> str:
        return f"{self.base_url}/chat/completions"

    def build_payload(
        self,
        messages: Iterable[Message],
        *,
        temperature: float = 0.7,
        max_tokens: int = 800,
    ) -> dict:
        return {
            "model": self.model,
            "messages": [m.to_dict() for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }

    def build_headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

    # ---- 真正发请求 ---------------------------------------------------
    def chat(
        self,
        messages: Iterable[Message],
        *,
        temperature: float = 0.7,
        max_tokens: int = 800,
    ) -> str:
        """发一轮对话，返回模型说的话。失败一律抛 LLMError。"""
        if not self.api_key:
            raise LLMNotConfigured("还没配置 API key")

        payload = self.build_payload(
            messages, temperature=temperature, max_tokens=max_tokens)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        status, raw = self._transport(
            self.endpoint(), self.build_headers(), body, self.timeout)

        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LLMError(f"接口返回的不是合法 JSON（{status}）") from exc

        if status >= 400:
            # 各家错误体格式不完全一样，尽力把能看的挑出来
            detail = ""
            if isinstance(data, dict):
                err = data.get("error")
                if isinstance(err, dict):
                    detail = str(err.get("message") or err)
                elif err:
                    detail = str(err)
                else:
                    detail = str(data.get("message") or data)[:200]
            raise LLMHTTPError(status, detail or "（没有更多信息）")

        return extract_content(data)


def extract_content(data: dict) -> str:
    """从响应里取正文。

    单独抽出来是因为这里最容易被"看起来像"的响应骗到 ——
    缺字段、choices 是空数组、content 是 None，都得当错误处理，
    不然上层会拿到一个空字符串当作模型的回答。
    """
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LLMError("响应里没有 choices")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = (message or {}).get("content")
    if not isinstance(content, str) or not content.strip():
        raise LLMError("响应里没有正文")
    return content.strip()
