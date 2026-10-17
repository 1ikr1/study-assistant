"""计划的结构定义与校验 —— 这一层是"代码门控"的核心。

为什么不能直接信任模型给的 JSON：

  · 它可能多写一个不存在的子 Agent（"帮我删库"也能写成任务）
  · 它可能引用一个后面才出现的依赖（环）
  · 它可能一口气拆二十步（成本失控）
  · 它可能在 JSON 前后加一段"好的，这是计划："（解析失败）

所以模型的输出只是**建议**，必须先过这里才能被执行。
这里的每个函数都是纯函数，可以逐条测。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

# 允许出现的子 Agent 白名单。模型写出别的名字一律拒绝。
ALLOWED_AGENTS = ("scheduler", "stats", "knowledge", "report")

# 一次协作最多几步。防止模型把"讲个题"拆成二十步烧钱。
MAX_STEPS = 4


class PlanError(ValueError):
    """计划不合法。上层会把它的 message 当作"这次没拆成"处理。"""


@dataclass(frozen=True)
class WorkItem:
    """一个待执行的子任务。"""

    id: str                       # s1 / s2 ...
    agent: str                    # 必须在 ALLOWED_AGENTS 里
    args: dict = field(default_factory=dict)     # 给这个子 Agent 的入参
    depends_on: tuple[str, ...] = ()             # 依赖哪些前面的任务


@dataclass(frozen=True)
class Plan:
    """Supervisor 给出的完整计划。"""

    reason: str
    items: tuple[WorkItem, ...]


def extract_json(text: str) -> dict:
    """从模型回复里抠出 JSON。

    真实模型经常在 JSON 前后加一句话 —— 不能假设它只吐 JSON。
    先整体解析，失败再找第一个平衡的大括号块。
    """
    text = (text or "").strip()
    if not text:
        raise PlanError("模型没有返回内容")

    # 去掉 ```json ``` 围栏
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.S)
    if fence:
        text = fence.group(1).strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = json.loads(_first_json_object(text))

    if not isinstance(data, dict):
        raise PlanError("模型返回的不是一个 JSON 对象")
    return data


def _first_json_object(text: str) -> str:
    """找到第一个括号平衡的 {...} 片段。字符串里的括号不算。"""
    start = text.find("{")
    if start < 0:
        raise PlanError("回复里找不到 JSON")
    depth, in_str, escape = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    raise PlanError("JSON 括号不配对")


def parse_plan(data: dict) -> Plan:
    """把模型给的 dict 变成可信的 Plan。任何不合规都抛 PlanError。"""
    reason = str(data.get("reason") or "").strip()
    raw_tasks = data.get("tasks")

    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise PlanError("计划里没有任务")
    if len(raw_tasks) > MAX_STEPS:
        raise PlanError(f"最多 {MAX_STEPS} 步，模型给了 {len(raw_tasks)} 步")

    items: list[WorkItem] = []
    seen: set[str] = set()

    for raw in raw_tasks:
        if not isinstance(raw, dict):
            raise PlanError("任务必须是对象")

        item_id = str(raw.get("id") or "").strip()
        agent = str(raw.get("agent") or "").strip()
        if not item_id:
            raise PlanError("任务缺少 id")
        if item_id in seen:
            raise PlanError(f"任务 id 重复：{item_id}")
        if agent not in ALLOWED_AGENTS:
            raise PlanError(f"不认识的子 Agent：{agent}")

        args = raw.get("args") or {}
        if not isinstance(args, dict):
            raise PlanError(f"{item_id} 的 args 必须是对象")

        depends = raw.get("depends_on") or []
        if isinstance(depends, str):
            depends = [depends]
        if not isinstance(depends, list):
            raise PlanError(f"{item_id} 的 depends_on 必须是数组")
        depends = tuple(str(d).strip() for d in depends)

        # 只能依赖**已经出现过**的任务 —— 这一条同时排掉了环和"依赖未来"
        for dep in depends:
            if dep not in seen:
                raise PlanError(f"{item_id} 依赖了不存在或还没排到的 {dep}")

        seen.add(item_id)
        items.append(WorkItem(id=item_id, agent=agent, args=args, depends_on=depends))

    plan = Plan(reason=reason, items=tuple(items))
    _check_final_step(plan)
    return plan


def _check_final_step(plan: Plan) -> None:
    """多步骤计划必须以 report 收尾 —— 否则没人负责"出成品"。

    这条不是洁癖：没有 report，前端就得自己拼一堆中间结果，
    而"把数据说成人话"正是这一步存在的意义。
    """
    if len(plan.items) > 1 and plan.items[-1].agent != "report":
        raise PlanError("多步骤计划的最后一步必须是 report")
