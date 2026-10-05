"""后端优化的关键业务回归，不重复覆盖基础模块测试。"""

from __future__ import annotations

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.case_flow import CaseFlow  # noqa: E402
from services.auth import AuthService  # noqa: E402
from services.mdt_analyst import analyze_specialty  # noqa: E402
from services.model_output_guard import (  # noqa: E402
    UnsafeModelOutput,
    assert_safe_generated_text,
    public_text,
)
from services.s2_analyst import ModelPick, S2Result, _parse_picks  # noqa: E402
from services.store import Store  # noqa: E402


CASE = (
    "患者：张*，男，58岁。主诉：视物模糊3月，双下肢浮肿1周。"
    "既往2型糖尿病5年，高血压10年。检查：尿蛋白++，空腹血糖9.2。"
)


class FakeModel:
    api_key = "test"

    def __init__(self, payload):
        self.payload = payload

    def call_ai_api_raw(self, *args, **kwargs):
        return {"ok": True, "content": json.dumps(self.payload, ensure_ascii=False),
                "truncated": False, "finish_reason": "stop"}


class BackendOptimizationTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.flow = CaseFlow(store=self.store, use_llm=False)
        result = self.flow.create_case(medical_history=CASE, main_issue="该看哪个科")
        self.assertTrue(result.ok, result.errors)
        self.case_id = result.case_id

    def tearDown(self):
        self.store.close()

    def test_doctor_modification_changes_effective_report(self):
        review = self.flow.get_review(self.case_id)
        primary = next(item for item in review["items"]
                       if item["target_type"] == "department_selection")
        result = self.flow.submit_decision(
            self.case_id, primary["item_id"], "modify",
            after="牵头建议：眼科", reason="结合症状调整首诊方向",
            reviewer_name="审核医生", reviewer_id="doctor-1",
        )
        self.assertTrue(result["ok"], result)
        report = self.flow.build_reports(self.case_id)["full"]["sections"]
        self.assertEqual(report["分诊结果"]["牵头方向"], "眼科")

    def test_new_material_invalidates_published_review_without_reusing_decisions(self):
        for item in self.flow.get_review(self.case_id)["items"]:
            if item["review_tier"] == "required" and item["state"] == "awaiting":
                decision = self.flow.submit_decision(
                    self.case_id, item["item_id"], "accept", reviewer_name="审核医生")
                self.assertTrue(decision["ok"], decision.get("error"))
        published = self.flow.publish_review(self.case_id, reviewer_name="审核医生")
        self.assertTrue(published["ok"], published)
        old_count = len(self.store.list_decisions(self.case_id))

        updated = self.flow.add_update(
            self.case_id, kind="检查结果", content="复查肌酐正常，尿蛋白仍为阳性"
        )
        self.assertTrue(updated["ok"], updated)
        case = self.flow.get_case(self.case_id)
        self.assertEqual(case["case_state"], "awaiting_recheck")
        self.assertFalse(case["payload"]["review"]["published"])
        self.assertEqual(case["payload"]["review_round"], 2)
        review = self.flow.get_review(self.case_id)
        self.assertTrue(review["stale_decisions"])
        self.assertTrue(
            any(item["state"] == "awaiting" for item in review["items"]),
            "受影响的条目应回到待审状态",
        )
        self.assertEqual(len(self.store.list_decisions(self.case_id)), old_count)

    def test_followup_crisis_is_blocked_before_case_mutation(self):
        before = self.flow.get_case(self.case_id)["payload"]
        result = self.flow.submit_answers(
            self.case_id, {"symptom_trend": "持续胸痛伴大汗，越来越重"}
        )
        self.assertFalse(result["ok"])
        self.assertTrue(result["blocked"])
        after = self.flow.get_case(self.case_id)["payload"]
        self.assertEqual(before.get("answers"), after.get("answers"))

    def test_main_issue_participates_in_rule_triage(self):
        result = self.flow.create_case(
            medical_history=(
                "患者男，58岁，既往2型糖尿病5年，高血压10年。"
                "检查为空腹血糖9.2，尿蛋白阳性。"
            ),
            main_issue="视物模糊三个月，想确认应该先看哪个科",
        )
        self.assertTrue(result.ok, result.errors)
        self.assertIn("眼科", result.triage["selected"])

    def test_recompute_keeps_completed_task_progress(self):
        case = self.flow.get_case(self.case_id)
        task = case["payload"]["tasks"][0]
        done = self.flow.update_task_state(self.case_id, task["task_id"], "done")
        self.assertTrue(done["ok"], done)
        updated = self.flow.add_update(
            self.case_id, kind="症状变化", content="视物模糊程度与上周相同"
        )
        self.assertTrue(updated["ok"], updated)
        matching = [item for item in updated["path"]["tasks"]
                    if item.get("type") == task.get("type")
                    and item.get("source") == task.get("source")
                    and item.get("content") == task.get("content")]
        self.assertTrue(matching)
        self.assertEqual(matching[0]["state"], "done")

    def test_update_never_keeps_stale_mdt_when_model_is_unavailable(self):
        def seed(case):
            payload = dict(case.get("payload") or {})
            payload["mdt"] = {"analyses": [{"department": "眼科", "status": "ok"}]}
            payload["mdt_status"] = {"state": "done"}
            case["payload"] = payload
        self.store.mutate_case(self.case_id, seed)

        result = self.flow.add_update(
            self.case_id, kind="检查结果", content="新增眼底检查结果，需医生复核"
        )
        self.assertTrue(result["ok"], result)
        payload = self.flow.get_case(self.case_id)["payload"]
        self.assertIsNone(payload["mdt"])
        self.assertEqual(payload["analysis"]["mdt"]["state"], "skipped")

    def test_unknown_answer_field_is_rejected_before_mutation(self):
        before = self.flow.get_case(self.case_id)["payload"]
        result = self.flow.submit_answers(self.case_id, {"hidden_prompt_field": "任意内容"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "unknown_answer_fields")
        after = self.flow.get_case(self.case_id)["payload"]
        self.assertEqual(before.get("answers"), after.get("answers"))

    def test_repeated_answer_submissions_keep_previous_effective_answers(self):
        fields = [q["field"] for q in self.flow.get_case(self.case_id)["payload"]["questions"]]
        self.assertGreaterEqual(len(fields), 2)
        first = self.flow.submit_answers(self.case_id, {fields[0]: "持续三个月"})
        second = self.flow.submit_answers(self.case_id, {fields[1]: "近一周加重"})
        self.assertTrue(first["ok"], first)
        self.assertTrue(second["ok"], second)
        answers = self.flow.get_case(self.case_id)["payload"]["answers"]
        self.assertEqual(answers[fields[0]], "持续三个月")
        self.assertEqual(answers[fields[1]], "近一周加重")

    def test_no_key_progressive_request_finishes_in_rules_only_mode(self):
        class NoKeyModel:
            api_key = ""

        flow = CaseFlow(store=self.store, ai_service=NoKeyModel(), use_llm=True)
        result = flow.create_case(
            medical_history=CASE, main_issue="该看哪个科", progressive=True)
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.analysis_mode, "rules_only")
        self.assertFalse(result.analysis_pending)
        status = flow.get_analysis(result.case_id)
        self.assertEqual(status["analysis"]["state"], "done")
        self.assertEqual(status["analysis"]["mdt"]["state"], "skipped")
        self.assertFalse(status["mdt_ready"])


class ModelOutputBoundaryTest(unittest.TestCase):
    def test_probability_variants_are_hidden(self):
        self.assertNotIn("90%", public_text("有90%可能性"))
        self.assertNotIn("0.8", public_text("confidence=0.8"))

    def test_treatment_recommendation_variants_are_rejected(self):
        for text in ("建议进行手术治疗", "可考虑手术治疗", "建议使用某药", "建议规律服药"):
            with self.subTest(text=text), self.assertRaises(UnsafeModelOutput):
                assert_safe_generated_text(text, field_name="模型建议")

    def test_s2_drops_definitive_diagnosis_reason(self):
        raw = json.dumps({"picks": [{
            "department": "眼科", "stance": "supplement", "confidence": 0.8,
            "reason": "明确诊断为青光眼",
        }]}, ensure_ascii=False)
        self.assertEqual(_parse_picks(raw), [])

    def test_s2_public_payload_hides_internal_confidence(self):
        result = S2Result(picks=[ModelPick(
            department="眼科", stance="supplement", confidence=0.91,
            reason="模型置信度 91%，建议参与",
        )], method="llm")
        public = json.dumps(result.to_dict(), ensure_ascii=False)
        self.assertNotIn("confidence", public)
        self.assertNotIn("91%", public)
        self.assertNotIn("置信度", public)
        self.assertNotIn("概率", public)

    def test_unsafe_specialty_card_is_rejected(self):
        model = FakeModel({
            "focus": "明确诊断为糖尿病肾病",
            "facts": ["尿蛋白阳性"],
            "inferences": [{"text": "确诊为糖尿病肾病", "confidence": "high"}],
            "checks": [{"priority": "high", "content": "立即服用某药 10mg"}],
            "information_gaps": [],
            "referral": None,
        })
        result = analyze_specialty(
            ai_service=model, department="肾内科", text=CASE, case_id="C-SAFE"
        )
        self.assertEqual(result.method, "failed")
        self.assertIsNone(result.card)
        self.assertIn("安全限制", result.error)


class BoundedAuthStateTest(unittest.TestCase):
    def test_login_failure_key_count_is_bounded(self):
        with mock.patch.dict(os.environ, {"APP_MAX_LOGIN_FAILURE_KEYS": "100"}):
            auth = AuthService(accounts={})
        for index in range(130):
            try:
                auth.login(username=f"missing-{index}", password="bad")
            except Exception:
                pass
        self.assertLessEqual(len(auth._login_failures), 100)


if __name__ == "__main__":
    unittest.main(verbosity=2)
