"""编排：校验 → 派发 → 汇总。

这是整个多 Agent 层的骨架，也是最该被测试覆盖的地方。

三个关键点：

1. **派发按顺序、只给该给的**。每个子 Agent 拿到的是它 args 加上它声明
   依赖的那几步的结果，不是全部历史、也不是别人的输出。
2. **单步失败不炸全局**。某一步挂了，把"这一步失败了"作为它的结果传下去，
   让 report 去说明缺了什么 —— 比整个请求 500 好得多。
3. **一定要有出口**。Supervisor 给的 JSON 不合规时，降级成直接回答，
   而不是把解析错误甩给用户。
"""

from __future__ import annotations

import sqlite3
import time

from study_assistant.agent import supervisor, workers
from study_assistant.agent.schemas import PlanError
from study_assistant.llm import ChatClient, LLMError


def _preview(text: str, limit: int = 160) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


def run(conn: sqlite3.Connection, client: ChatClient, question: str) -> dict:
    """跑一次完整的问答。

    Returns:
        {
          "mode": "direct" | "multi" | "degraded",
          "answer": str,
          "reason": str,
          "trace": [ {step...} ],
        }

    Raises:
        LLMError: 模型完全不可用（没配 key / 网络不通）。
                  这种情况由上层转成一句人话，不在这里吞掉。
    """
    question = (question or "").strip()
    if not question:
        return {"mode": "direct", "answer": "你想问什么？", "reason": "", "trace": []}

    trace: list[dict] = []

    # ---- 第 1 步：让 Supervisor 判断 --------------------------------------
    t0 = time.perf_counter()
    try:
        decision = supervisor.decide(conn, client, question)
    except PlanError as exc:
        # 模型给了不合规的计划 —— 降级，而不是报错
        answer = supervisor.fallback_direct(client, question, str(exc))
        trace.append({
            "id": "supervisor", "agent": "supervisor", "kind": "model",
            "status": "degraded", "ms": _ms(t0),
            "output": _preview(f"计划不合规（{exc}），已降级为直接回答"),
        })
        return {"mode": "degraded", "answer": answer,
                "reason": f"计划不合规：{exc}", "trace": trace}

    trace.append({
        "id": "supervisor", "agent": "supervisor", "kind": "model",
        "status": "ok", "ms": _ms(t0),
        "output": _preview(
            "直接回答" if decision.mode == "direct"
            else f"拆成 {len(decision.plan.items)} 步：{decision.reason}"),
    })

    if decision.mode == "direct":
        return {"mode": "direct", "answer": decision.answer,
                "reason": "", "trace": trace}

    # ---- 第 2 步：逐节点派发 ---------------------------------------------
    plan = decision.plan
    results: dict[str, str] = {}

    for item in plan.items:
        worker = workers.resolve(item.agent)
        args = dict(item.args)
        if item.agent == "report":
            # report 必须知道用户问的是什么，否则只能写一段无头无尾的总结
            args["question"] = question
        upstream = {dep: results.get(dep, "") for dep in item.depends_on}

        t1 = time.perf_counter()
        try:
            output = worker.run(conn, client, args, upstream)
            status = "ok"
        except LLMError:
            raise                      # 模型不可用是全局问题，交给上层
        except Exception as exc:        # noqa: BLE001
            output = f"这一步失败了：{type(exc).__name__}: {exc}"
            status = "error"

        results[item.id] = output
        trace.append({
            "id": item.id, "agent": item.agent, "kind": worker.kind,
            "status": status, "ms": _ms(t1), "output": _preview(output),
            "depends_on": list(item.depends_on),
        })

    # ---- 第 3 步：取出成品 -----------------------------------------------
    last = plan.items[-1].id
    answer = results.get(last, "")
    if not answer or trace[-1]["status"] == "error":
        # 收尾那步挂了，别把中间结果当答案给用户
        answer = supervisor.fallback_direct(client, question, "收尾失败")
        return {"mode": "degraded", "answer": answer,
                "reason": "收尾步骤失败", "trace": trace}

    return {"mode": "multi", "answer": answer, "reason": plan.reason, "trace": trace}


def _ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)
