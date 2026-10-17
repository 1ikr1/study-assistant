"""多 Agent 编排层。

结构照搬「Supervisor-Worker」：

    supervisor.py   主 Agent：判断该不该拆、拆成什么（模型的活）
    schemas.py      计划的结构与校验（纯函数，代码的门控）
    workers.py      子 Agent：两个纯代码 + 两个模型
    orchestrator.py 校验 → 派发 → 汇总

一条铁律：**拆解是模型的判断，派发与验收是代码的门控。**
模型输出的是"建议"，不是"指令"—— 计划必须先过 schemas 的校验才能执行。
"""

from study_assistant.agent.orchestrator import run  # noqa: F401
