"""数据结构 schema 的单元测试。

运行（在 CHAIS/backend 目录下）：
    python -m unittest tests.test_schemas -v

验收对应《产品开发规格书-比赛版v-competition.md》：
- §5 schema：类型/枚举/样例；"schema 校验失败能被捕获"
- §5.7 可靠性等级：必须带原因，且模型自报置信度不参与
- §7.1 病例/报告状态枚举齐全
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from schemas import (  # noqa: E402
    CASE_STATES,
    CASE_STATE_LABELS,
    ChallengeRound,
    CheckItem,
    DepartmentPick,
    Disagreement,
    DoctorDecision,
    Inference,
    PatientProfile,
    Reliability,
    Reviewer,
    SchemaError,
    SpecialistOpinionCard,
    TaskItem,
    TriageResult,
    reliability_level,
)

# 规格书 §5.1 的样例载荷（原文照抄，作为回归基准）
SPEC_PROFILE_SAMPLE = {
    "case_id": "C001",
    "behavior_type": "B",
    "age": 58,
    "sex": "女",
    "chief_complaint": "视物模糊3月，下肢浮肿1周",
    "diseases": [
        {"name": "2型糖尿病", "duration": "5年", "control": "不详",
         "confidence": "medium", "source_refs": ["S-2:12"]}
    ],
    "symptoms_timeline": [
        {"symptom": "视物模糊", "onset": "3月前", "trend": "渐重",
         "confidence": "high", "source_refs": ["S-2:12"]}
    ],
    "exams": [
        {"item": "尿蛋白", "value": "++", "abnormal": True, "source_refs": ["S-1:34"]}
    ],
    "medications": [{"name": "二甲双胍", "dose": "0.5g bid"}],
    "doctor_opinions": [],
    "information_gaps": ["血糖控制水平不详", "未提供眼底检查"],
    "completeness": 0.72,
    "direct_quote_coverage": 0.65,
}


class TestPatientProfile(unittest.TestCase):
    def test_spec_sample_is_valid(self):
        p = PatientProfile.from_dict(SPEC_PROFILE_SAMPLE)
        p.validate()
        self.assertEqual(p.case_id, "C001")
        self.assertEqual(p.behavior_type, "B")
        self.assertEqual(len(p.diseases), 1)

    def test_disease_requires_source_refs(self):
        payload = dict(SPEC_PROFILE_SAMPLE)
        payload["diseases"] = [{"name": "2型糖尿病", "confidence": "medium"}]
        p = PatientProfile.from_dict(payload)
        with self.assertRaises(SchemaError):
            p.validate()

    def test_gate_blocks_when_quote_coverage_low(self):
        payload = dict(SPEC_PROFILE_SAMPLE)
        payload["direct_quote_coverage"] = 0.45
        p = PatientProfile.from_dict(payload)
        p.validate()
        self.assertFalse(p.passes_gate, "直引覆盖率 <0.60 应阻断（G4）")

    def test_gate_passes_at_threshold(self):
        payload = dict(SPEC_PROFILE_SAMPLE)
        payload["direct_quote_coverage"] = 0.60
        self.assertTrue(PatientProfile.from_dict(payload).passes_gate)

    def test_invalid_behavior_type(self):
        payload = dict(SPEC_PROFILE_SAMPLE)
        payload["behavior_type"] = "Z"
        with self.assertRaises(SchemaError):
            PatientProfile.from_dict(payload).validate()

    def test_invalid_confidence(self):
        payload = dict(SPEC_PROFILE_SAMPLE)
        payload["diseases"] = [{"name": "高血压", "confidence": "very-high",
                                "source_refs": ["S-1:1"]}]
        with self.assertRaises(SchemaError):
            PatientProfile.from_dict(payload).validate()


class TestTriageResult(unittest.TestCase):
    def _pick(self, name, role="协同"):
        return DepartmentPick(code="x", name=name, role=role, reason="测试")

    def test_valid_triage(self):
        t = TriageResult(
            case_id="C001",
            behavior_type="B",
            primary_department=self._pick("内分泌科", "牵头"),
            secondary_departments=[self._pick("肾内科"), self._pick("眼科")],
            reliability=Reliability(level="中", reasons=["血糖控制情况缺失"]),
            decision_path="rule-dominant",
        )
        t.validate()
        self.assertEqual(t.total_departments, 3)

    def test_max_departments_enforced(self):
        t = TriageResult(
            case_id="C001",
            behavior_type="B",
            primary_department=self._pick("内分泌科", "牵头"),
            secondary_departments=[self._pick(f"科{i}") for i in range(6)],  # 共 7 科
        )
        with self.assertRaises(SchemaError):
            t.validate()

    def test_primary_role_must_be_lead(self):
        t = TriageResult(
            case_id="C001",
            behavior_type="B",
            primary_department=self._pick("内分泌科", "协同"),  # 错误：应为「牵头」
        )
        with self.assertRaises(SchemaError):
            t.validate()

    def test_reliability_requires_reasons(self):
        with self.assertRaises(SchemaError):
            Reliability(level="高", reasons=[]).validate()

    def test_invalid_decision_path(self):
        t = TriageResult(case_id="C001", behavior_type="B", decision_path="magic")
        with self.assertRaises(SchemaError):
            t.validate()


class TestOpinionCard(unittest.TestCase):
    def test_valid_card(self):
        card = SpecialistOpinionCard(
            case_id="C001",
            specialty_code="ophthalmology",
            specialty_name="眼科",
            focus="糖尿病视网膜病变排查",
            facts=["视物模糊3月、渐进加重"],
            inferences=[Inference(text="需排查糖尿病视网膜病变", confidence="medium",
                                  evidence_refs=["exam:眼底检查-缺失"])],
            checks=[CheckItem(priority="high", content="行眼底照相/散瞳眼底检查")],
            information_gaps=["无既往眼底检查记录"],
        )
        card.validate()

    def test_empty_referral_rejected(self):
        card = SpecialistOpinionCard(
            case_id="C001", specialty_code="cardiology", specialty_name="心血管内科",
            referral="   ",
        )
        with self.assertRaises(SchemaError):
            card.validate()

    def test_bad_check_priority(self):
        card = SpecialistOpinionCard(
            case_id="C001", specialty_code="cardiology", specialty_name="心血管内科",
            checks=[CheckItem(priority="urgent", content="查心电图")],
        )
        with self.assertRaises(SchemaError):
            card.validate()


class TestAggregation(unittest.TestCase):
    def test_disagreement_needs_two_positions(self):
        d = Disagreement(topic="是否首选有创评估", positions={"心血管内科": "建议冠脉CTA"})
        with self.assertRaises(SchemaError):
            d.validate()

    def test_valid_disagreement(self):
        d = Disagreement(
            topic="是否首选有创性血管评估",
            positions={"心血管内科": "建议冠脉CTA", "肾内科": "先控制蛋白尿再评估"},
            reason="专科优先级差异",
            resolving_info="血糖控制水平与肾功结果",
            evidence_state="conflict",
        )
        d.validate()

    def test_challenge_outcome_must_be_structured(self):
        """质询产出只能是结构化结论，不得是概率。"""
        c = ChallengeRound(
            question="眼底缺失时「眼科优先」依据是否充分？",
            raised_by="内分泌科",
            responses=[{"specialty": "眼科", "stance": "补充证据", "reason": "缺少眼底结果"}],
            outcome="需补充证据",
            follow_up_evidence=["眼底照相结果"],
        )
        c.validate()

        bad = ChallengeRound(question="?", outcome="概率 0.64")
        with self.assertRaises(SchemaError):
            bad.validate()

    def test_bad_response_stance(self):
        c = ChallengeRound(
            question="?", outcome="维持原判断",
            responses=[{"specialty": "眼科", "stance": "同意一半", "reason": "x"}],
        )
        with self.assertRaises(SchemaError):
            c.validate()


class TestTaskItem(unittest.TestCase):
    def test_valid_task(self):
        TaskItem(task_id="T-001", case_id="C001", type="就医咨询",
                 content="向内分泌科医生确认血糖控制目标", priority="high").validate()

    def test_imperative_medical_action_rejected(self):
        """医疗行为类任务不得是指令式（规格书 §5.5）。"""
        t = TaskItem(task_id="T-002", case_id="C001", type="就医咨询",
                     content="立即停用二甲双胍")
        with self.assertRaises(SchemaError):
            t.validate()

    def test_bad_task_type(self):
        t = TaskItem(task_id="T-003", case_id="C001", type="随便", content="x")
        with self.assertRaises(SchemaError):
            t.validate()


class TestDoctorDecision(unittest.TestCase):
    def test_modify_requires_reason_and_before_after(self):
        d = DoctorDecision(decision_id="DD-1", case_id="C001",
                           target_type="department_selection", target_id="oph",
                           action="modify", reason="", before=None, after=None)
        with self.assertRaises(SchemaError):
            d.validate()

    def test_modify_ok_with_full_fields(self):
        d = DoctorDecision(decision_id="DD-1", case_id="C001",
                           target_type="department_selection", target_id="oph",
                           action="modify", before="先看眼科", after="先查眼底再看眼科",
                           reason="补充检查优先级", reviewer=Reviewer(role="审核医生", dept="眼科"),
                           version=3)
        d.validate()

    def test_reject_requires_reason(self):
        d = DoctorDecision(decision_id="DD-2", case_id="C001",
                           target_type="patient_advice", target_id="adv-2",
                           action="reject")
        with self.assertRaises(SchemaError):
            d.validate()

    def test_accept_needs_no_reason(self):
        DoctorDecision(decision_id="DD-3", case_id="C001",
                       target_type="path_step", target_id="step-1",
                       action="accept").validate()

    def test_bad_action(self):
        d = DoctorDecision(decision_id="DD-4", case_id="C001",
                           target_type="path_step", target_id="s", action="approve")
        with self.assertRaises(SchemaError):
            d.validate()


class TestReliabilityRule(unittest.TestCase):
    """主文档 §6.5 / 规格书 §5.7：等级必须带原因，模型自报置信度不参与。"""

    def test_doctor_confirmed_is_high(self):
        r = reliability_level(completeness=0.5, doctor_confirmed=True)
        self.assertEqual(r.level, "高")
        self.assertTrue(r.reasons)

    def test_model_inference_only_is_low(self):
        r = reliability_level(completeness=0.9, only_model_inference=True)
        self.assertEqual(r.level, "低")

    def test_conflict_is_low(self):
        r = reliability_level(completeness=0.9, has_conflict=True)
        self.assertEqual(r.level, "低")

    def test_low_completeness_is_medium(self):
        r = reliability_level(completeness=0.4)
        self.assertEqual(r.level, "中")

    def test_inconsistent_modules_is_medium(self):
        r = reliability_level(completeness=0.9, multi_module_consistent=False)
        self.assertEqual(r.level, "中")

    def test_clean_case_is_high(self):
        r = reliability_level(completeness=0.9)
        self.assertEqual(r.level, "高")

    def test_every_level_has_reasons(self):
        cases = [
            reliability_level(completeness=0.9),
            reliability_level(completeness=0.4),
            reliability_level(completeness=0.9, has_conflict=True),
        ]
        for r in cases:
            r.validate()  # 无原因会抛 SchemaError


class TestStateEnums(unittest.TestCase):
    """规格书 §7.1：病例/报告状态枚举必须齐全（含中文标签）。"""

    EXPECTED = {
        "pending_materials", "analyzing", "awaiting_doctor", "need_more_info",
        "doctor_reviewed", "doctor_confirmed", "awaiting_recheck",
    }

    def test_case_states_complete(self):
        self.assertEqual(set(CASE_STATES), self.EXPECTED)

    def test_every_state_has_label(self):
        for state in CASE_STATES:
            self.assertIn(state, CASE_STATE_LABELS)
            self.assertTrue(CASE_STATE_LABELS[state].strip())


if __name__ == "__main__":
    unittest.main(verbosity=2)
