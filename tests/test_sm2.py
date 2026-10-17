"""SM-2 算法的单元测试。

这一份测试是这个项目最重要的资产之一：
它证明了"调度是确定的、可验证的"——而这正是用算法而不是大模型的理由。

运行：python -m unittest discover -s tests -v
"""

from __future__ import annotations

import unittest

from study_coach.sm2 import EF_INIT, EF_MIN, SM2State, ef_delta, preview, update


class TestIntervals(unittest.TestCase):
    """间隔的推进规则。"""

    def test_first_pass_jumps_to_one_day(self):
        s = update(SM2State(), 5)
        self.assertEqual(s.interval_days, 1)
        self.assertEqual(s.repetitions, 1)

    def test_second_pass_jumps_to_six_days(self):
        s = update(update(SM2State(), 5), 5)
        self.assertEqual(s.interval_days, 6)
        self.assertEqual(s.repetitions, 2)

    def test_third_pass_multiplies_by_ef(self):
        s = SM2State()
        for _ in range(3):
            s = update(s, 5)
        # 第 3 次：上一次间隔 6 天 × 当时 EF（第 2 次后为 2.7）= 16.2 → 16
        self.assertEqual(s.interval_days, 16)

    def test_interval_grows_monotonically(self):
        s = SM2State()
        intervals = []
        for _ in range(4):
            s = update(s, 5)
            intervals.append(s.interval_days)
        self.assertEqual(intervals, sorted(intervals))
        self.assertEqual(intervals[:3], [1, 6, 16])

    def test_wrong_answer_resets_interval(self):
        s = SM2State()
        for _ in range(3):
            s = update(s, 5)
        self.assertGreater(s.interval_days, 10)

        s = update(s, 1)  # 答错
        self.assertEqual(s.interval_days, 1)
        self.assertEqual(s.repetitions, 0)

    def test_after_reset_it_restarts_from_one(self):
        s = update(SM2State(), 5)      # 1 天
        s = update(s, 0)               # 答错，重置
        s = update(s, 5)               # 重新开始 → 1 天
        self.assertEqual(s.interval_days, 1)
        self.assertEqual(s.repetitions, 1)


class TestEasinessFactor(unittest.TestCase):
    """EF（难度系数）的调整。"""

    def test_perfect_rating_increases_ef(self):
        self.assertAlmostEqual(ef_delta(5), 0.1, places=6)

    def test_hesitant_rating_keeps_ef(self):
        self.assertAlmostEqual(ef_delta(4), 0.0, places=6)

    def test_hard_rating_decreases_ef(self):
        self.assertAlmostEqual(ef_delta(3), -0.14, places=6)

    def test_penalty_is_asymmetric(self):
        """答错扣的比答对加的多得多——这是刻意的保守设计。"""
        self.assertLess(ef_delta(2), -ef_delta(5) * 3)

    def test_ef_never_drops_below_floor(self):
        s = SM2State()
        for _ in range(50):
            s = update(s, 0)
        self.assertAlmostEqual(s.ef, EF_MIN, places=6)

    def test_ef_updates_even_on_failure(self):
        """SM-2 里 EF 无论答对答错都会更新。"""
        before = SM2State()
        after = update(before, 2)
        self.assertLess(after.ef, before.ef)


class TestDeterminism(unittest.TestCase):
    """可复现性——这正是"不用大模型算"的核心理由。"""

    def test_same_input_same_output(self):
        a = update(SM2State(ef=2.35, interval_days=15, repetitions=3), 4)
        b = update(SM2State(ef=2.35, interval_days=15, repetitions=3), 4)
        self.assertEqual(a, b)

    def test_repeated_runs_are_identical(self):
        runs = []
        for _ in range(10):
            s = SM2State()
            for rating in (5, 4, 3, 2, 5):
                s = update(s, rating)
            runs.append(s)
        self.assertEqual(len(set(runs)), 1)

    def test_state_is_immutable(self):
        original = SM2State()
        update(original, 5)
        self.assertEqual(original, SM2State())  # 原对象没被改动

    def test_preview_does_not_mutate(self):
        s = SM2State()
        preview(s)
        self.assertEqual(s, SM2State())


class TestValidation(unittest.TestCase):
    def test_rating_above_range_rejected(self):
        with self.assertRaises(ValueError):
            update(SM2State(), 6)

    def test_rating_below_range_rejected(self):
        with self.assertRaises(ValueError):
            update(SM2State(), -1)

    def test_ef_init_value(self):
        self.assertEqual(EF_INIT, 2.5)
        self.assertEqual(SM2State().ef, EF_INIT)


if __name__ == "__main__":
    unittest.main()
