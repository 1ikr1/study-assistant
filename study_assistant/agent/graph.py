"""把计划编译成 LangGraph 图。

手写编排时，"谁依赖谁""哪些能并行""失败了怎么收尾"全靠 for 循环维护；
换成 StateGraph 之后，这些由图的语义表达：

    START -> s1 -> (汇合点) -> report -> END
    START -> s2 ->

图是**每次请求动态构建**的：节点和边来自 Supervisor 给的计划。
这也是它比固定流水线更合适的地方 —— 问题的形状不同，图的形状就不同。

两个工程细节：
1. 每个节点自己开数据库连接（图会在不同线程并行跑无依赖的分支，
   而 sqlite 连接不能跨线程共用）。
2. 节点内部吞掉普通异常、只放行 LLMError（某个子 Agent 崩了，
   应该让 report 说"这部分缺失"，而不是让整张图失败）。
"""

from __future__ import annotations

import time
from typing import Annotated, Callable, TypedDict

from langgraph.graph import END, START, StateGraph

from study_assistant import db
from study_assistant.agent.schemas import Plan, WorkItem
from study_assistant.agent.workers import resolve
from study_assistant.llm import ChatClient, LLMError

# on_event(event) 给 SSE 用；传 None 就是纯执行
EventSink = Callable[[dict], None] | None


def _merge(left: dict, right: dict) -> dict:
    """results 的合并规则：后写的覆盖同名，其余保留。

    LangGraph 默认整体替换，并行分支会互相覆盖 —— 必须显式给 reducer。
    """
    return {**left, **right}


class DispatchState(TypedDict):
    question: str
    results: Annotated[dict, _merge]


def build(
    db_path: str,
    client: ChatClient,
    plan: Plan,
    on_event: EventSink = None,
):
    """按计划把图搭出来并编译。"""
    graph = StateGraph(DispatchState)

    for item in plan.items:
        graph.add_node(item.id, _make_node(db_path, client, item, on_event))

    for item in plan.items:
        if item.depends_on:
            for dep in item.depends_on:
                graph.add_edge(dep, item.id)
        else:
            graph.add_edge(START, item.id)

    graph.add_edge(plan.items[-1].id, END)
    return graph.compile()


def _make_node(
    db_path: str,
    client: ChatClient,
    item: WorkItem,
    on_event: EventSink,
) -> Callable[[DispatchState], dict]:
    worker = resolve(item.agent)

    def node(state: DispatchState) -> dict:
        _emit(on_event, {
            "type": "step_start", "id": item.id,
            "agent": item.agent, "kind": worker.kind,
            "depends_on": list(item.depends_on),
        })

        args = dict(item.args)
        if item.agent == "report":
            # report 必须知道用户问的是什么，否则只能写无头无尾的总结
            args["question"] = state.get("question", "")
        upstream = {d: state.get("results", {}).get(d, "") for d in item.depends_on}

        conn = db.connect(db_path)
        started = time.perf_counter()
        try:
            output = worker.run(conn, client, args, upstream)
            status = "ok"
        except LLMError:
            raise                     # 模型不可用是全局问题，往上抛
        except Exception as exc:      # noqa: BLE001
            output = f"这一步失败了：{type(exc).__name__}: {exc}"
            status = "error"
        finally:
            conn.close()

        elapsed = int((time.perf_counter() - started) * 1000)
        _emit(on_event, {
            "type": "step_end", "id": item.id, "agent": item.agent,
            "kind": worker.kind, "status": status, "ms": elapsed,
            "output": _preview(output),
        })
        return {"results": {item.id: output}}

    return node


def _preview(text: str, limit: int = 160) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


def _emit(sink: EventSink, event: dict) -> None:
    if sink is not None:
        sink(event)
