"""SM-2 间隔重复算法。

这是整个项目的基石：根据"这次答得怎么样"，算出"下次什么时候复习"。

为什么用算法而不是大模型：
  · 同样输入必须得到同样的输出（可复现）
  · 必须能写单元测试（可验证）
  · 算法升级后要能用历史日志重放（可对比）
  这三件事大模型都做不到。
"""

from __future__ import annotations

from dataclasses import dataclass

# EF（难度系数）的下限。防止一张卡被惩罚到"每天都要复习"。
EF_MIN = 1.3
# EF 的初始值。2.5 是 SM-2 论文里的经验值。
EF_INIT = 2.5
# 评分 >= 这个值算"答对了"。
PASS_THRESHOLD = 3
# 前两次的固定间隔（天）。刚开始没有历史数据，不能靠乘法，只能用经验值。
FIRST_INTERVAL = 1
SECOND_INTERVAL = 6


@dataclass(frozen=True)
class SM2State:
    """一张卡的记忆状态。frozen 表示"不可变"——每次更新都产生新对象。"""

    ef: float = EF_INIT
    interval_days: int = 0
    repetitions: int = 0


def ef_delta(rating: int) -> float:
    """评分对 EF 的影响。

    这个公式是"不对称"的：答错扣的比答对加的多得多。

        rating=5 → +0.10
        rating=4 →  0.00
        rating=3 → -0.14
        rating=2 → -0.32
        rating=0 → -0.80

    刻意的保守设计：宁可复习得频繁一点，也不要漏掉一张快要忘的卡。
    """
    return 0.1 - (5 - rating) * (0.08 + (5 - rating) * 0.02)


def update(state: SM2State, rating: int) -> SM2State:
    """给定本次评分，返回新的记忆状态。

    Args:
        state: 当前状态。
        rating: 0-5 的评分。3 及以上算答对。

    Returns:
        新的 SM2State。原对象不变。

    Raises:
        ValueError: 评分不在 0-5 之间。
    """
    if not 0 <= rating <= 5:
        raise ValueError(f"rating 必须是 0-5 的整数，收到 {rating!r}")

    if rating >= PASS_THRESHOLD:
        # 答对了：间隔按 1 天 → 6 天 → 乘 EF 递增
        if state.repetitions == 0:
            interval = FIRST_INTERVAL
        elif state.repetitions == 1:
            interval = SECOND_INTERVAL
        else:
            interval = round(state.interval_days * state.ef)
        repetitions = state.repetitions + 1
    else:
        # 答错了：说明没记住，间隔重置
        interval = FIRST_INTERVAL
        repetitions = 0

    # EF 无论答对答错都会更新，且有下限
    ef = max(EF_MIN, state.ef + ef_delta(rating))

    return SM2State(ef=round(ef, 4), interval_days=interval, repetitions=repetitions)


def preview(state: SM2State) -> list[tuple[int, int]]:
    """预览：从现在起，如果每次都答"完美"（5 分），接下来几次的间隔。

    用来回答"这张卡以后会多久复习一次"——纯计算，不碰数据库。
    """
    out: list[tuple[int, int]] = []
    cursor = state
    for step in range(1, 8):
        cursor = update(cursor, 5)
        out.append((step, cursor.interval_days))
    return out
