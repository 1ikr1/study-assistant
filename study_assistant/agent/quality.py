"""回答质量门控。三层，照搬简历项目 1 的思路：

    1. 规则层（零成本）  空回答 / 过短 / 复读 / 编造数字
    2. 模型层（一次调用）五维清单逐项判定
    3. 结论            pass / warn / fail / unknown

最重要的一条：**评审不可用时必须单独置位，绝不静默当作通过。**
"没检查"和"检查通过"是两件完全不同的事，混起来就等于给用户一个假的安心。

为什么这里死盯数字：这个项目的主线是"算数交给代码，模型只管说话"。
所以回答里出现的数字，**必须能在材料里找到**。找不到就是模型自己算的，
而它算的数是不可信的。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from study_assistant.llm import ChatClient, LLMError, Message

MIN_LENGTH = 20
MAX_LENGTH = 2000


@dataclass
class GateResult:
    verdict: str                      # pass | warn | fail | unknown
    score: int                        # 0-100
    checks: list[dict] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict:
        return {"verdict": self.verdict, "score": self.score,
                "checks": self.checks, "note": self.note}


_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def extract_numbers(text: str) -> set[str]:
    """抠出文本里的数字。整数和小数都算，日期也能被这一步识别。"""
    return set(_NUMBER.findall(text or ""))


def rule_checks(answer: str, materials: str) -> list[dict]:
    """规则层：不花 token，先把明显的毛病拦下来。"""
    answer = (answer or "").strip()
    checks: list[dict] = []

    checks.append({
        "name": "非空", "ok": bool(answer),
        "detail": "" if answer else "回答是空的",
    })

    length_ok = MIN_LENGTH <= len(answer) <= MAX_LENGTH
    checks.append({
        "name": "长度合理", "ok": length_ok,
        "detail": f"{len(answer)} 字"
                  + ("" if length_ok else f"（应在 {MIN_LENGTH}-{MAX_LENGTH}）"),
    })

    lines = [ln.strip() for ln in answer.splitlines() if ln.strip()]
    repeated = len(lines) != len(set(lines))
    checks.append({
        "name": "没有复读", "ok": not repeated,
        "detail": "有重复行" if repeated else "",
    })

    invented = sorted(extract_numbers(answer) - extract_numbers(materials))
    checks.append({
        "name": "数字可溯源", "ok": not invented,
        "detail": f"这些数字材料里没有：{invented[:5]}" if invented else "",
    })

    return checks


REVIEW_PROMPT = """\
你是学习助手的质量审核。给你用户的问题、参考资料、助手的回答，
请按五个维度逐项判定，然后只输出一个 JSON，不要任何解释文字。

五个维度（每项 0-20 分）：
1. 忠实：回答里的事实和数字是否都来自参考资料，没有编造
2. 切题：是否回答了用户真正问的问题
3. 完整：材料里能支撑的要点是否都讲到了
4. 清晰：结构清楚、不啰嗦、不含糊
5. 有用：读完能否指导下一步行动

输出格式：
{"scores":{"忠实":18,"切题":17,"完整":15,"清晰":16,"有用":14},
 "issues":["有问题写这里，没有就空数组"],
 "summary":"一句话结论"}
"""


def model_review(
    client: ChatClient, question: str, answer: str, materials: str
) -> dict | None:
    """模型层评审。返回 None 表示**评审本身不可用**（不是"通过"）。"""
    messages = [
        Message("system", REVIEW_PROMPT),
        Message("user",
                f"用户的问题：{question}\n\n参考资料：\n{materials[:3000]}\n\n"
                f"助手的回答：\n{answer}"),
    ]
    try:
        raw = client.chat(messages, temperature=0.0, max_tokens=400)
    except LLMError:
        return None

    from study_assistant.agent.schemas import PlanError, extract_json
    try:
        data = extract_json(raw)
    except PlanError:
        return None

    scores = data.get("scores")
    if not isinstance(scores, dict) or not scores:
        return None
    values = [int(v) for v in scores.values() if isinstance(v, (int, float))]
    if not values:
        return None
    return {
        "scores": scores,
        "total": sum(values),
        "issues": [str(x) for x in (data.get("issues") or [])][:5],
        "summary": str(data.get("summary") or ""),
    }


def evaluate(
    client: ChatClient,
    question: str,
    answer: str,
    materials: str,
    *,
    deep: bool = True,
) -> GateResult:
    """跑完整的三层检查。

    Args:
        deep: 是否跑模型层。规则层发现硬伤时直接跳过，省一次调用。
    """
    checks = rule_checks(answer, materials)

    hard_fail = any(
        (not c["ok"]) and c["name"] in ("非空", "长度合理", "没有复读")
        for c in checks
    )
    if hard_fail:
        return GateResult(verdict="fail", score=0, checks=checks,
                          note="规则层就不过，没必要再花一次模型调用")

    if not deep:
        soft_ok = all(c["ok"] for c in checks)
        return GateResult(verdict="pass" if soft_ok else "warn",
                          score=100 if soft_ok else 60, checks=checks,
                          note="" if soft_ok else "只跑了规则层")

    review = model_review(client, question, answer, materials)
    if review is None:
        return GateResult(
            verdict="unknown", score=0, checks=checks,
            note="模型评审不可用（没 key / 网络问题），这次回答未经复核")

    traceable = any(c["name"] == "数字可溯源" and not c["ok"] for c in checks)
    score = max(0, min(100, review["total"] - (10 if traceable else 0)))

    if score >= 80:
        verdict = "pass"
    elif score >= 60:
        verdict = "warn"
    else:
        verdict = "fail"

    checks = checks + [{
        "name": "模型评审", "ok": verdict != "fail",
        "detail": review["summary"] or "；".join(review["issues"]),
    }]
    return GateResult(verdict=verdict, score=score, checks=checks,
                      note=review["summary"])
