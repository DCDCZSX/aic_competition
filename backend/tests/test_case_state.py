"""病例/报告状态机的单元测试。

运行（在 CHAIS/backend 目录下）：
    python -m unittest tests.test_case_state -v

验收对应《产品开发规格书-比赛版v-competition.md》§7.1：
- 状态可查可流转（非法转移被拒）
- 异常时保留原版本 + 转人工复核，不产空白报告
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from schemas import CASE_STATES  # noqa: E402
from services.case_state import (  # noqa: E402
    ANOMALY_KINDS,
    STATE_ANALYZING,
    STATE_AWAITING_DOCTOR,
    STATE_AWAITING_RECHECK,
    STATE_DOCTOR_CONFIRMED,
    STATE_DOCTOR_REVIEWED,
    STATE_NEED_MORE_INFO,
    STATE_PENDING,
    TRANSITIONS,
    CaseStateMachine,
    IllegalTransition,
    UnknownState,
)


def _fixed_clock():
    """确定性时钟，便于断言时间线。"""
    ticks = {"n": 0}

    def _now() -> str:
        ticks["n"] += 1
        return f"2026-09-11T10:00:{ticks['n']:02d}"

    return _now


class TestTransitions(unittest.TestCase):
    def test_happy_path(self):
        """正常链路：资料待确认 → 分析中 → 待审核 → 已审阅 → 已确认 → 待复核 → 再分析。"""
        sm = CaseStateMachine("C001", clock=_fixed_clock())
        sm.transition(STATE_ANALYZING, actor="patient", reason="画像确认通过")
        sm.transition(STATE_AWAITING_DOCTOR, actor="system", reason="AI 分析完成")
        sm.transition(STATE_DOCTOR_REVIEWED, actor="doctor", reason="审阅完成")
        sm.transition(STATE_DOCTOR_CONFIRMED, actor="doctor", reason="确认发布审核版")
        sm.transition(STATE_AWAITING_RECHECK, actor="patient", reason="上传新检查结果")
        sm.transition(STATE_ANALYZING, actor="system", reason="受影响模块重算")

        self.assertEqual(sm.state, STATE_ANALYZING)
        self.assertEqual(len(sm.history), 6)
        self.assertEqual(sm.version, 1, "流转本身不应改动报告版本")

    def test_illegal_transition_rejected(self):
        sm = CaseStateMachine("C001")
        with self.assertRaises(IllegalTransition):
            sm.transition(STATE_DOCTOR_CONFIRMED, actor="doctor")  # 未审核就确认

    def test_illegal_transition_keeps_state(self):
        sm = CaseStateMachine("C001")
        try:
            sm.transition(STATE_DOCTOR_CONFIRMED, actor="doctor")
        except IllegalTransition:
            pass
        self.assertEqual(sm.state, STATE_PENDING)
        self.assertEqual(len(sm.history), 0)

    def test_unknown_state_rejected(self):
        with self.assertRaises(UnknownState):
            CaseStateMachine("C001", state="flying")

    def test_all_states_covered_by_transition_table(self):
        for state in CASE_STATES:
            self.assertIn(state, TRANSITIONS, f"状态 {state} 缺少转移规则")

    def test_can_transition_helper(self):
        sm = CaseStateMachine("C001")
        self.assertTrue(sm.can_transition(STATE_ANALYZING))
        self.assertFalse(sm.can_transition(STATE_DOCTOR_CONFIRMED))

    def test_is_confirmed_only_after_doctor_confirmation(self):
        sm = CaseStateMachine("C001")
        sm.transition(STATE_ANALYZING)
        sm.transition(STATE_AWAITING_DOCTOR)
        sm.transition(STATE_DOCTOR_REVIEWED)
        self.assertFalse(sm.is_confirmed(), "未确认前不得视为审核版")
        sm.transition(STATE_DOCTOR_CONFIRMED)
        self.assertTrue(sm.is_confirmed())


class TestAnomalyHandling(unittest.TestCase):
    """规格书 §7.1：显示明确原因 + 保留原版本 + 转人工复核，不得静默。"""

    def test_anomaly_records_reason_and_switches_to_need_more_info(self):
        sm = CaseStateMachine("C001", clock=_fixed_clock())
        sm.transition(STATE_ANALYZING)
        rec = sm.record_anomaly("timeout", "内分泌科分析超时 120s", actor="system")

        self.assertTrue(rec.is_anomaly)
        self.assertEqual(rec.anomaly_kind, "timeout")
        self.assertIn("分析超时", rec.reason)
        self.assertEqual(rec.to_state, STATE_NEED_MORE_INFO)
        self.assertEqual(sm.state, STATE_NEED_MORE_INFO)
        self.assertEqual(len(sm.anomalies), 1)

    def test_anomaly_preserves_version(self):
        sm = CaseStateMachine("C001")
        sm.transition(STATE_ANALYZING)
        before = sm.version
        sm.record_anomaly("json_parse_failed", "专科意见卡 JSON 解析失败")
        self.assertEqual(sm.version, before, "异常必须保留原版本，不得产出空白新版本")

    def test_unknown_anomaly_kind_still_recorded(self):
        sm = CaseStateMachine("C001")
        rec = sm.record_anomaly("外星人入侵", "未预期异常")
        self.assertEqual(rec.anomaly_kind, "unknown", "未知异常也要留痕，不能丢")
        self.assertEqual(len(sm.anomalies), 1)

    def test_all_declared_kinds_accepted(self):
        for kind in ANOMALY_KINDS:
            with self.subTest(kind=kind):
                sm = CaseStateMachine("C001")
                sm.transition(STATE_ANALYZING)
                rec = sm.record_anomaly(kind, f"测试 {kind}")
                self.assertEqual(rec.anomaly_kind, kind)

    def test_anomaly_from_unswitchable_state_keeps_state_but_logs(self):
        """医生已确认态无法直接转补资料时：状态不变，但异常必须留痕。"""
        sm = CaseStateMachine("C001")
        sm.transition(STATE_ANALYZING)
        sm.transition(STATE_AWAITING_DOCTOR)
        sm.transition(STATE_DOCTOR_REVIEWED)
        sm.transition(STATE_DOCTOR_CONFIRMED)

        rec = sm.record_anomaly("rule_model_conflict", "与强规则冲突")
        self.assertEqual(sm.state, STATE_DOCTOR_CONFIRMED, "无法转移时保持原状态")
        self.assertEqual(rec.to_state, STATE_DOCTOR_CONFIRMED)
        self.assertEqual(len(sm.anomalies), 1, "异常仍必须记录")


class TestAuditTimeline(unittest.TestCase):
    def test_timeline_has_labels_and_order(self):
        sm = CaseStateMachine("C001", clock=_fixed_clock())
        sm.transition(STATE_ANALYZING, actor="patient", reason="画像确认通过")
        sm.record_anomaly("evidence_unverifiable", "缺少眼底检查依据")

        tl = sm.timeline()
        self.assertEqual(len(tl), 2)
        self.assertEqual(tl[0]["to"], "AI 分析中")
        self.assertEqual(tl[0]["actor"], "patient")
        self.assertTrue(tl[1]["is_anomaly"])
        self.assertEqual(tl[1]["to"], "退回补资料")
        self.assertLess(tl[0]["at"], tl[1]["at"], "时间线应按时间递增")

    def test_bump_version(self):
        sm = CaseStateMachine("C001")
        self.assertEqual(sm.bump_version(), 2)
        self.assertEqual(sm.version, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
