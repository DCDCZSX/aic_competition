"""病例流程编排（接线层）的单元测试。

运行（在 CHAIS/backend 目录下）：
    python -m unittest tests.test_case_flow -v

验收：把"录入 → 校验(G1/G2/G3) → 脱敏 → 画像(G4) → 追问 → 落盘"串成可用链路。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.case_flow import CaseFlow  # noqa: E402
from services.store import Store  # noqa: E402

RICH_CASE = (
    "患者：张*，男，58岁。主诉：视物模糊3月，双下肢浮肿1周。"
    "既往2型糖尿病5年，高血压10年。目前口服二甲双胍0.5g bid。"
    "检查：尿蛋白++，空腹血糖9.2。曾行心电图检查提示异常。"
)


class FlowTestBase(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.flow = CaseFlow(store=self.store)

    def tearDown(self):
        self.store.close()


class TestHappyPath(FlowTestBase):
    def test_create_case_success(self):
        r = self.flow.create_case(medical_history=RICH_CASE,
                                  main_issue="该看哪个科", goals=["multi_disease"], age=58)
        self.assertTrue(r.ok, r.errors)
        self.assertTrue(r.case_id.startswith("C"))
        self.assertTrue(r.passes_gate)
        self.assertIsNotNone(r.profile)

    def test_profile_extracted(self):
        r = self.flow.create_case(medical_history=RICH_CASE, main_issue="该看哪个科")
        self.assertIn("视物模糊", r.profile["chief_complaint"])
        self.assertEqual(r.profile["age"], 58)
        names = [d["name"] for d in r.profile["diseases"]]
        self.assertIn("2型糖尿病", names)

    def test_source_refs_preserved_for_ui(self):
        """画像卡必须带 source_refs（患者端 hover 显示原文出处）。"""
        r = self.flow.create_case(medical_history=RICH_CASE, main_issue="该看哪个科")
        for d in r.profile["diseases"]:
            self.assertTrue(d["source_refs"], f"{d['name']} 缺少 source_refs")

    def test_questions_generated_and_prioritised(self):
        r = self.flow.create_case(medical_history=RICH_CASE, main_issue="该看哪个科")
        self.assertTrue(r.questions)
        self.assertLessEqual(len(r.questions), 3)
        flags = [q["affects_triage"] for q in r.questions]
        self.assertEqual(flags, sorted(flags, reverse=True), "影响分诊的问题应优先")

    def test_masked_preview_returned(self):
        r = self.flow.create_case(
            medical_history="患者：张*，男，58岁。主诉：咳嗽2周。", main_issue="该看哪个科")
        self.assertIn("medical_history", r.masked_preview)

    def test_case_persisted_and_audited(self):
        r = self.flow.create_case(medical_history=RICH_CASE, main_issue="该看哪个科")
        case = self.store.load_case(r.case_id)
        self.assertIsNotNone(case, "病例应落盘")
        events = [a["event"] for a in self.store.list_audit(r.case_id)]
        self.assertIn("case_created", events)
        self.assertTrue(self.store.list_transitions(r.case_id))

    def test_get_case_returns_payload(self):
        r = self.flow.create_case(medical_history=RICH_CASE, main_issue="该看哪个科")
        case = self.flow.get_case(r.case_id)
        self.assertEqual(case["case_id"], r.case_id)


class TestGuardrailBlocking(FlowTestBase):
    def test_crisis_blocks_and_gives_advice(self):
        r = self.flow.create_case(medical_history="患者持续胸痛伴大汗2小时")
        self.assertFalse(r.ok)
        self.assertTrue(r.blocked)
        self.assertEqual(r.blocked_by, "G1")
        self.assertIn("120", r.advice)

    def test_crisis_case_is_audited(self):
        r = self.flow.create_case(medical_history="患者突发昏迷")
        events = [a["event"] for a in self.store.list_audit(r.case_id)]
        self.assertIn("intake_blocked", events, "阻断必须留痕")

    def test_crisis_does_not_create_normal_case(self):
        r = self.flow.create_case(medical_history="大出血不止")
        self.assertIsNone(r.profile, "危象不应产出画像")
        self.assertEqual(r.questions, [], "危象不应进入追问")

    def test_pii_blocks(self):
        r = self.flow.create_case(medical_history=f"患者电话13812345678，{RICH_CASE}")
        self.assertFalse(r.ok)
        self.assertEqual(r.blocked_by, "G3")

    def test_empty_input_blocks_g2(self):
        r = self.flow.create_case(medical_history="", main_issue="")
        self.assertFalse(r.ok)
        self.assertEqual(r.blocked_by, "G2")

    def test_crisis_priority_over_pii(self):
        r = self.flow.create_case(medical_history="持续胸痛伴大汗，电话 13812345678")
        self.assertEqual(r.blocked_by, "G1")


class TestGateAndGaps(FlowTestBase):
    def test_thin_text_fails_gate(self):
        r = self.flow.create_case(medical_history="患者，男，60岁。有点不舒服。")
        if not r.passes_gate:
            self.assertFalse(r.ok, "门禁未过应阻断进入分析")
            self.assertEqual(r.case_state, "need_more_info")

    def test_gaps_use_matrix_labels(self):
        """缺口文案必须能被依赖矩阵匹配（否则追问会漏配）。"""
        from services.dependency_matrix import BY_LABEL

        r = self.flow.create_case(medical_history=RICH_CASE, main_issue="该看哪个科")
        for gap in r.gaps:
            self.assertTrue(any(label in gap for label in BY_LABEL),
                            f"缺口「{gap}」无法被依赖矩阵匹配")

    def test_gaps_non_empty_for_incomplete_case(self):
        r = self.flow.create_case(medical_history=RICH_CASE, main_issue="该看哪个科")
        self.assertTrue(r.gaps)


class TestAnswers(FlowTestBase):
    def setUp(self):
        super().setUp()
        self.case = self.flow.create_case(medical_history=RICH_CASE, main_issue="该看哪个科")

    def test_answer_resolves_gap(self):
        field = self.case.questions[0]["field"] if self.case.questions else "cardiac_exam"
        outcome = self.flow.submit_answers(self.case.case_id, {field: "做过心电图"})
        self.assertTrue(outcome["ok"])
        self.assertIn(field, outcome["resolved_fields"])

    def test_skipped_answer_keeps_gap(self):
        field = self.case.questions[0]["field"] if self.case.questions else "cardiac_exam"
        outcome = self.flow.submit_answers(self.case.case_id, {field: "不清楚 / 跳过"})
        self.assertIn(field, outcome["unresolved_fields"])
        self.assertTrue(outcome["reliability_downgrade"], "跳过关键问题应触发可靠性降级")

    def test_answers_audited(self):
        self.flow.submit_answers(self.case.case_id, {"cardiac_exam": "做过"})
        events = [a["event"] for a in self.store.list_audit(self.case.case_id)]
        self.assertIn("answers_submitted", events)

    def test_answers_after_confirmation_open_new_review_round(self):
        self.flow.store.transition_case(self.case.case_id, "doctor_confirmed", actor="doctor")
        result = self.flow.submit_answers(
            self.case.case_id, {"blood_glucose_control": "最近控制不佳"})
        self.assertTrue(result["ok"], result)
        case = self.flow.store.load_case(self.case.case_id)
        self.assertEqual(case["case_state"], "awaiting_recheck")
        self.assertFalse((case["payload"].get("review") or {}).get("published", False))

    def test_too_many_answers_are_rejected_before_processing(self):
        answers = {f"field-{index}": "回答" for index in range(4)}
        outcome = self.flow.submit_answers(self.case.case_id, answers)
        self.assertFalse(outcome["ok"])
        self.assertIn("最多", outcome["error"])

    def test_overlong_answer_key_is_rejected_before_processing(self):
        answers = {"k" * 129: "回答"}
        outcome = self.flow.submit_answers(self.case.case_id, answers)
        self.assertFalse(outcome["ok"])
        self.assertIn("键", outcome["error"])

    def test_unknown_case(self):
        outcome = self.flow.submit_answers("C-NOT-EXIST", {})
        self.assertFalse(outcome["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
