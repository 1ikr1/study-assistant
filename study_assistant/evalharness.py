"""评测：量化"分级路由对不对、工具选得对不对、回答过不过自检"。

为什么非要有这个：面试官一定会问"多 Agent 比单 Agent 好在哪里"。
没有数字，这句话只能说得很虚。有了这组指标，答案就变成
"在金标准集上分级准确率 X%、工具选择 Y%，多出来的成本是 Z 秒"。

跑法：
    python -m study_assistant.evalharness            # 调真模型，需要 key
    python -m study_assistant.evalharness --dry-run  # 只校验数据集，不花钱
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from study_assistant import agent, db

DATASET = Path(__file__).resolve().parent.parent / "data" / "eval_dataset.json"
REPORT_DIR = Path(__file__).resolve().parent.parent / "eval" / "reports"


@dataclass
class CaseResult:
    case_id: str
    question: str
    expect_mode: str
    actual_mode: str
    expect_agents: list[str]
    actual_agents: list[str]
    gate_verdict: str
    elapsed_ms: int
    calls: int

    @property
    def mode_ok(self) -> bool:
        return self.actual_mode == self.expect_mode

    @property
    def agents_ok(self) -> bool:
        return set(self.expect_agents) <= set(self.actual_agents)

    def to_dict(self) -> dict:
        return {
            "id": self.case_id, "question": self.question,
            "expect_mode": self.expect_mode, "actual_mode": self.actual_mode,
            "mode_ok": self.mode_ok,
            "expect_agents": self.expect_agents, "actual_agents": self.actual_agents,
            "agents_ok": self.agents_ok,
            "gate": self.gate_verdict,
            "elapsed_ms": self.elapsed_ms, "calls": self.calls,
        }


class CountingClient:
    """包一层计数：这次问答一共花了多少次模型调用。"""

    def __init__(self, inner):
        self.inner = inner
        self.model = getattr(inner, "model", "")
        self.calls = 0

    def chat(self, messages, **kwargs):
        self.calls += 1
        return self.inner.chat(messages, **kwargs)


def load_dataset(path: Path | None = None) -> list[dict]:
    data = json.loads((path or DATASET).read_text(encoding="utf-8"))
    return data["cases"]


def build_default_client():
    """默认客户端：按 .env 里的配置建。"""
    from study_assistant import config
    from study_assistant.llm import ChatClient

    s = config.llm_settings()
    return ChatClient(api_key=s["api_key"], base_url=s["base_url"], model=s["model"])


def summarise(results: list[CaseResult]) -> dict:
    total = len(results)
    mode_ok = sum(r.mode_ok for r in results)
    multi = [r for r in results if r.expect_mode == "multi"]
    agents_ok = sum(r.agents_ok for r in multi)
    gate_pass = sum(r.gate_verdict == "pass" for r in results if r.gate_verdict)
    reviewed = sum(1 for r in results if r.gate_verdict)

    return {
        "cases": total,
        "mode_accuracy": round(mode_ok / total, 4) if total else 0,
        "agent_accuracy": round(agents_ok / len(multi), 4) if multi else 0,
        "gate_pass_rate": round(gate_pass / reviewed, 4) if reviewed else 0,
        "avg_ms": round(sum(r.elapsed_ms for r in results) / total, 1) if total else 0,
        "avg_calls": round(sum(r.calls for r in results) / total, 2) if total else 0,
        "avg_calls_direct": _avg_calls(results, "direct"),
        "avg_calls_multi": _avg_calls(results, "multi"),
    }


def _avg_calls(results: list[CaseResult], mode: str) -> float:
    subset = [r for r in results if r.expect_mode == mode]
    return round(sum(r.calls for r in subset) / len(subset), 2) if subset else 0


def run(client_factory, db_path: str, cases: list[dict], *, deep_review: bool = False) -> dict:
    """跑完整评测。deep_review 默认关：评测关心路由，不为质量门控多付一次调用。"""
    results: list[CaseResult] = []

    for case in cases:
        conn = db.connect(db_path)
        counting = CountingClient(client_factory())
        started = time.perf_counter()
        try:
            out = agent.run(conn, counting, case["question"], deep_review=deep_review)
        except Exception as exc:                     # noqa: BLE001
            out = {"mode": "error", "trace": [], "gate": {"verdict": "error"}}
            out["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            conn.close()

        elapsed = int((time.perf_counter() - started) * 1000)
        agents_used = [t["agent"] for t in out.get("trace", [])
                       if t.get("agent") in {"scheduler", "stats", "knowledge", "report"}]
        results.append(CaseResult(
            case_id=case["id"], question=case["question"],
            expect_mode=case["expect_mode"],
            actual_mode=out.get("mode", "?"),
            expect_agents=list(case.get("expect_agents") or []),
            actual_agents=agents_used,
            gate_verdict=(out.get("gate") or {}).get("verdict", ""),
            elapsed_ms=elapsed, calls=counting.calls,
        ))

    report = {"summary": summarise(results),
              "results": [r.to_dict() for r in results],
              "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    return report


def save_report(report: dict) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"eval_{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def format_report(report: dict) -> str:
    s = report["summary"]
    lines = [
        "评测结果",
        "-" * 52,
        f"  用例数        : {s['cases']}",
        f"  分级准确率    : {s['mode_accuracy']:.1%}",
        f"  工具选择准确率: {s['agent_accuracy']:.1%}（只看期望多 Agent 的用例）",
        f"  自检通过率    : {s['gate_pass_rate']:.1%}",
        f"  平均耗时      : {s['avg_ms']:.0f} ms",
        f"  平均调用次数  : {s['avg_calls']}（直答 {s['avg_calls_direct']} / 编排 {s['avg_calls_multi']}）",
        "-" * 52,
    ]
    for r in report["results"]:
        mark = "✓" if r["mode_ok"] and (r["actual_mode"] != "multi" or r["agents_ok"]) else "✗"
        lines.append(
            f"  {mark} {r['id']:<10} 期望 {r['expect_mode']:<7} 实得 {r['actual_mode']:<8}"
            f" {r['elapsed_ms']:>5}ms {r['calls']} 次")
        if r["actual_mode"] == "multi":
            lines.append(f"      子 Agent: {r['actual_agents']}（期望含 {r['expect_agents']}）")
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse

    from study_assistant import config, web

    parser = argparse.ArgumentParser(description="个人学习助手 评测")
    parser.add_argument("--dry-run", action="store_true", help="只校验数据集")
    parser.add_argument("--db", default=str(db.DEFAULT_DB))
    args = parser.parse_args()

    cases = load_dataset()
    print(f"数据集：{len(cases)} 条")
    if args.dry_run:
        for c in cases:
            assert c["expect_mode"] in ("direct", "multi"), c
            assert isinstance(c.get("expect_agents"), list), c
        print("数据集格式 OK（没花钱，也没调模型）")
        raise SystemExit(0)

    if not config.llm_configured():
        print("还没配置 DEEPSEEK_API_KEY，跑不了真评测。")
        raise SystemExit(1)

    report = run(web.build_client, args.db, cases)
    print(format_report(report))
    print(f"\n报告已保存：{save_report(report)}")
