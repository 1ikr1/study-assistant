"""编排入口：Supervisor 判断 -> 编译图 -> 执行 -> 汇总。

分成三段，是因为三段都会出错，而且**出错的处理方式不同**：

  1. Supervisor 的 JSON 不合规  ->  降级成直接回答（用户不该看到"解析失败"）
  2. 某个子 Agent 崩了          ->  带着缺口继续，让 report 说明缺什么
  3. 模型完全不可用             ->  往上抛，由 API 层翻译成人话

编排本身（谁依赖谁、怎么并行）交给 LangGraph，这里只管首尾。
"""

from __future__ import annotations

import sqlite3
import time
from typing import Callable

from study_assistant import db
from study_assistant.agent import graph as graph_mod
from study_assistant.agent import quality, supervisor
from study_assistant.agent.schemas import PlanError
from study_assistant.llm import ChatClient

EventSink = Callable[[dict], None] | None


def _preview(text: str, limit: int = 160) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


def run(
    conn: sqlite3.Connection,
    client: ChatClient,
    question: str,
    on_event: EventSink = None,
    deep_review: bool = True,
) -> dict:
    """跑一次完整的问答。

    Args:
        on_event: 每发生一件事就回调一次（给 SSE 用）。传 None 就只返回结果。

    Returns:
        {"mode": "direct"|"multi"|"degraded", "answer": str, "reason": str,
         "trace": [{...}]}

    Raises:
        LLMError: 模型完全不可用。
    """
    emit = on_event or (lambda _e: None)
    question = (question or "").strip()
    if not question:
        return {"mode": "direct", "answer": "你想问什么？", "reason": "", "trace": []}

    trace: list[dict] = []

    # ---- 1. Supervisor 判断 ----------------------------------------------
    emit({"type": "phase", "phase": "supervisor"})
    t0 = time.perf_counter()
    try:
        decision = supervisor.decide(conn, client, question)
    except PlanError as exc:
        answer = supervisor.fallback_direct(client, question, str(exc))
        entry = {
            "id": "supervisor", "agent": "supervisor", "kind": "model",
            "status": "degraded", "ms": _ms(t0),
            "output": _preview(f"计划不合规（{exc}），已降级为直接回答"),
        }
        trace.append(entry)
        emit({"type": "step_end", **entry})
        emit({"type": "answer", "text": answer, "mode": "degraded"})
        return {"mode": "degraded", "answer": answer,
                "reason": f"计划不合规：{exc}", "trace": trace}

    trace.append({
        "id": "supervisor", "agent": "supervisor", "kind": "model",
        "status": "ok", "ms": _ms(t0),
        "output": _preview(
            "直接回答" if decision.mode == "direct"
            else f"拆成 {len(decision.plan.items)} 步：{decision.reason}"),
    })
    emit({"type": "mode", "mode": decision.mode,
          "reason": decision.reason,
          "steps": [i.id for i in decision.plan.items] if decision.plan else []})

    if decision.mode == "direct":
        emit({"type": "answer", "text": decision.answer, "mode": "direct"})
        return {"mode": "direct", "answer": decision.answer,
                "reason": "", "trace": trace}

    # ---- 2. 编译并执行图 --------------------------------------------------
    plan = decision.plan

    def sink(event: dict) -> None:
        if event.get("type") == "step_end":
            trace.append({k: event.get(k) for k in
                          ("id", "agent", "kind", "status", "ms", "output")})
        emit(event)

    compiled = graph_mod.build(db.path_of(conn), client, plan, sink)
    final = compiled.invoke({"question": question, "results": {}})
    results: dict[str, str] = final.get("results", {})

    # ---- 3. 取成品 --------------------------------------------------------
    last = plan.items[-1].id
    answer = results.get(last, "")
    last_entry = next((t for t in trace if t.get("id") == last), {})

    if not answer or last_entry.get("status") == "error":
        answer = supervisor.fallback_direct(client, question, "收尾失败")
        emit({"type": "answer", "text": answer, "mode": "degraded"})
        return {"mode": "degraded", "answer": answer,
                "reason": "收尾步骤失败", "trace": trace}

    # ---- 4. 质量门控 ------------------------------------------------------
    materials = "\n\n".join(
        f"【{k}】{v}" for k, v in results.items() if k != last)
    gate = quality.evaluate(client, question, answer, materials, deep=deep_review)
    emit({"type": "gate", **gate.to_dict()})

    if gate.verdict == "fail":
        answer = f"⚠️ 这段回答没过自检（{gate.note or '质量分偏低'}），请谨慎参考。\n\n{answer}"

    emit({"type": "answer", "text": answer, "mode": "multi"})
    return {"mode": "multi", "answer": answer, "reason": plan.reason,
            "trace": trace, "gate": gate.to_dict()}


def _ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)
