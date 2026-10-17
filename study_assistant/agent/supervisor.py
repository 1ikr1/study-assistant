"""Supervisor：主 Agent。

它只做一件事 —— **判断这个问题值不值得拆，值的话拆成什么**。
产物是一段 JSON，不是"直接动手"。

这里有两个刻意的设计：

1. **一次调用同时完成"分类"和"拆分"**。
   简历里写的分级响应是"单 Agent 直答 vs 多 Agent 编排"，
   如果先调一次分类、再调一次规划，简单问题也要两次模型调用 ——
   那就违背了"单次调用能解决的绝不进图"。
2. **画像全量注入**。
   别的子 Agent 按需取上下文，唯独 Supervisor 例外：
   "每天只有 60 分钟""喜欢看例题"这些会直接改变它该不该拆、怎么拆，
   交给语义召回反而可能漏掉。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from study_assistant import repo
from study_assistant.agent import schemas, workers
from study_assistant.agent.schemas import MAX_STEPS, Plan, PlanError
from study_assistant.llm import ChatClient, LLMError, Message


@dataclass(frozen=True)
class Decision:
    """Supervisor 的判定结果。"""

    mode: str                 # "direct" | "multi"
    answer: str = ""          # mode=direct 时直接可用的回答
    plan: Plan | None = None  # mode=multi 时的计划
    reason: str = ""


def _profile_lines(conn: sqlite3.Connection) -> str:
    profile = repo.get_profile(conn)
    if not profile:
        return "（还没填画像）"
    key_name = {
        "daily_minutes": "每天可用分钟数",
        "study_time": "学习时段",
        "style": "学习偏好",
    }
    return "\n".join(f"- {key_name.get(k, k)}：{v}" for k, v in profile.items())


def build_messages(conn: sqlite3.Connection, question: str) -> list[Message]:
    """组装 Supervisor 的 prompt。纯函数（除了读画像）。"""
    due = len(repo.due_cards(conn, limit=None))
    system = f"""\
你是一个考研学习助手的主控。你要判断用户的问题需要几个子 Agent，并给出计划。

可用的子 Agent：
{workers.catalog()}

判断规则：
- 一句话能答的（闲聊、简单概念、直接建议）走 direct，你自己回答。
- 只有「查数据 + 算 + 出成品」这类需要两件以上不同能力协作时，才走 multi。
- 单次调用能解决的绝不拆。拆得越多越慢越贵。
- 拆解时最多 {MAX_STEPS} 步，最后一步必须是 report（它负责把材料写成回答）。
- 子 Agent 之间只能依赖前面已经出现过的步骤。

输出格式：**只输出一个 JSON**，不要任何解释文字，不要 Markdown 围栏。

直接回答：
{{"mode":"direct","answer":"给用户的回答"}}

需要拆解：
{{"mode":"multi","reason":"为什么值得拆","tasks":[
  {{"id":"s1","agent":"stats","args":{{"days":7}}}},
  {{"id":"s2","agent":"report","args":{{}},"depends_on":["s1"]}}
]}}

用户画像（每次都要考虑）：
{_profile_lines(conn)}"""

    user = f"今天是 {repo.today()}，当前有 {due} 张卡到期。\n\n用户问题：{question}"
    return [Message("system", system), Message("user", user)]


def decide(conn: sqlite3.Connection, client: ChatClient, question: str) -> Decision:
    """问 Supervisor 要一个决策。

    Raises:
        PlanError: 模型给的 JSON 不合规（上层会降级成直接回答）。
        LLMError: 模型调用本身失败。
    """
    raw = client.chat(build_messages(conn, question), temperature=0.2, max_tokens=700)
    data = schemas.extract_json(raw)

    mode = str(data.get("mode") or "").strip().lower()
    if mode == "direct":
        answer = str(data.get("answer") or "").strip()
        if not answer:
            raise PlanError("direct 模式但没给 answer")
        return Decision(mode="direct", answer=answer)

    if mode == "multi":
        plan = schemas.parse_plan(data)
        return Decision(mode="multi", plan=plan, reason=plan.reason)

    raise PlanError(f"不认识的 mode：{mode!r}")


def fallback_direct(client: ChatClient, question: str, why: str = "") -> str:
    """Supervisor 掉链子时的兜底：直接问一遍，别让用户看到报错。

    注意这里是**降级**不是**重试**——重试同一个坏 prompt 只会再坏一次。
    """
    messages = [
        Message("system",
                "你是考研学习助手。直接回答用户的问题，200 字以内，中文，"
                "不确定就说不确定，不要编。"),
        Message("user", question),
    ]
    text = client.chat(messages, temperature=0.5, max_tokens=500)
    return text.strip()
