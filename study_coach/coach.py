"""让大模型讲题 —— Phase 2 的第一块，也是最关键的一块。

它划清了整个项目的分界线：

    · 什么时候讲、讲哪道题  →  算法说了算（答错了才值得花一次模型调用）
    · 怎么讲                →  模型说了算

所以这个文件里有两个纯函数：`should_explain()` 和 `build_explain_messages()`。
都没有副作用、都能逐字断言 —— 因为**prompt 是产品逻辑的一部分**，
它不该散落在发请求的地方，改一句话也没人知道。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from study_coach import repo
from study_coach.llm import ChatClient, LLMError, Message

# 评分对应的说法。注意这里的措辞是给模型看的，
# 和界面上按钮的短标签（"完美""很吃力"）故意不一样 ——
# 喂给模型的是句子，不是标签。
RATING_SENTENCE = {
    5: "完全答对，毫不费力",
    4: "答对了，但有点犹豫",
    3: "答对了，但是很吃力",
    2: "答错了，但后来想起来了",
    1: "答错了，只有点印象",
    0: "完全不会，没有任何头绪",
}

# 只有这三种情况才值得打扰模型。答对了就别花这个钱，
# 也免得模型"硬找问题"，反而给学生添乱。
_NEEDS_EXPLAIN = {0, 1, 2}

SYSTEM_PROMPT = """\
你是一位耐心的考研辅导老师。学生刚做错了一道题，你的任务是让他真正弄懂，
而不是把标准答案复述一遍。

要求：
1. 不要照抄标准答案。换一种讲法，最好从"为什么"讲起。
2. 结合他做错这件事，点出最容易错在哪一步。
3. 最后给一个能记住的小抓手：一个对比、一个例子，或者一句话的记法。
4. 中文，口语化，200 字以内。不要用 Markdown 标题，不要加粗。
5. 只讲这道题涉及的知识点。拿不准的就说不确定，不要编。"""


@dataclass(frozen=True)
class ExplainResult:
    """讲题的结果，连同"这次调用了什么"一起返回 —— 方便记录和排查。"""

    text: str
    card_id: int
    knowledge_point: str
    model: str


def should_explain(rating: int) -> bool:
    """这次复习之后，值不值得让模型讲一遍。

    这是**算法做出的判断**，不是模型。判断依据只有一条：
    这次有没有答错。答对了不必讲。
    """
    return rating in _NEEDS_EXPLAIN


def build_explain_messages(
    card: dict,
    *,
    rating: int,
    style: str = "",
) -> list[Message]:
    """组装 prompt。纯函数 —— 给它同样的输入，永远同样的输出。

    Args:
        card: 至少包含 subject / knowledge_point / question / answer / lapses。
        rating: 这次给自己打的分。
        style: 画像里的学习偏好。会作为一句话塞进去，
               让模型按学生习惯的方式讲。
    """
    lapses = int(card.get("lapses") or 0)
    lines = [
        f"科目：{card.get('subject', '')}",
        f"知识点：{card.get('knowledge_point', '')}",
        "",
        f"题目：{card.get('question', '')}",
        "",
        f"标准答案：{card.get('answer', '')}",
        "",
        f"他这次打了 {rating} 分（{RATING_SENTENCE.get(rating, '未知')}）。",
    ]
    if lapses:
        lines.append(f"这张卡他累计答错过 {lapses} 次。")
    if style:
        lines.append(f"他的学习偏好：{style}。")

    return [
        Message(role="system", content=SYSTEM_PROMPT),
        Message(role="user", content="\n".join(lines)),
    ]


def explain(
    conn: sqlite3.Connection,
    client: ChatClient,
    card_id: int,
    *,
    rating: int,
    style: str = "",
    temperature: float = 0.7,
) -> ExplainResult:
    """让模型讲一道题。

    Raises:
        LookupError: 卡片不存在。
        LLMError: 模型那边出问题（没配 key、网络不通、接口报错）。
    """
    row = repo.get_card(conn, card_id)
    if row is None:
        raise LookupError(f"卡片不存在：{card_id}")

    card = dict(row)
    messages = build_explain_messages(card, rating=rating, style=style)
    text = client.chat(messages, temperature=temperature, max_tokens=600)

    return ExplainResult(
        text=text,
        card_id=card_id,
        knowledge_point=str(card.get("knowledge_point", "")),
        model=client.model,
    )


def explain_or_hint(
    conn: sqlite3.Connection,
    client: ChatClient,
    card_id: int,
    *,
    rating: int,
    style: str = "",
) -> dict:
    """给界面用的版本：永远返回一个能显示的结果，不抛异常。

    为什么不让界面自己 catch：界面只需要知道"显示什么"，
    不该去分辨"是没配 key 还是网络超时"。那种判断属于这一层。
    """
    if not should_explain(rating):
        return {
            "ok": False,
            "reason": "not_needed",
            "text": "这题答对了，不用讲。答错的时候再来看。",
        }
    try:
        result = explain(conn, client, card_id, rating=rating, style=style)
    except LookupError as exc:
        return {"ok": False, "reason": "not_found", "text": str(exc)}
    except LLMError as exc:
        return {"ok": False, "reason": "llm_error", "text": f"模型那边出问题了：{exc}"}
    return {
        "ok": True,
        "reason": "",
        "text": result.text,
        "model": result.model,
        "knowledge_point": result.knowledge_point,
    }
