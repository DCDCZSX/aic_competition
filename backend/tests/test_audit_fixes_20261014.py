"""2026-10-14 材料审计反例的回归测试。

逐条覆盖审计清单 F02–F06、F09、F13 中能用代码复现的反例，防止修复回退。
"""
import time
import unittest

from services import doctor_mdt, portrait_extractor, rule_engine, s2_analyst, update_service
from services.case_flow import CaseFlow
from services.prompt_text import MODEL_CASE_TEXT_LIMIT, case_text_for_prompt
from services.store import Store
from tests.test_create_progress import CASE_B, FakeAI, wait_settled


def hit_departments(text):
    return {hit.department for hit in rule_engine.evaluate(text).hits}


class ContextRecognitionTest(unittest.TestCase):
    """F02：否定病史、正常结果、既往已愈症状。"""

    def test_denied_disease_history_not_in_profile_or_rules(self):
        text = "患者否认糖尿病病史，咳嗽3天。"
        self.assertEqual(portrait_extractor._extract_diseases(text), [])
        self.assertNotIn("内分泌科", hit_departments(text))

    def test_denied_list_with_turn_keeps_later_fact(self):
        diseases = [d.name for d in portrait_extractor._extract_diseases("否认高血压、糖尿病病史，但有冠心病")]
        self.assertEqual(diseases, ["冠心病"])

    def test_resolved_past_fever_not_current_symptom(self):
        text = "既往发热已治愈，现腹痛2天。"
        self.assertNotIn("感染科", hit_departments(text))
        symptoms = [e.symptom for e in portrait_extractor._extract_symptoms(text)]
        self.assertEqual(symptoms, ["腹痛"])

    def test_normal_glucose_is_not_endocrine_signal(self):
        self.assertNotIn("内分泌科", hit_departments("空腹血糖6.0正常，头晕2天。"))

    def test_abnormal_values_and_chronic_history_still_hit(self):
        self.assertIn("内分泌科", hit_departments("糖尿病5年，空腹血糖9.8mmol/L"))
        self.assertIn("内分泌科", hit_departments("既往糖尿病10年，血糖控制不佳"))
        self.assertIn("感染科", hit_departments("无明显诱因出现发热3天"))
        self.assertIn("感染科", hit_departments("既往反复发热，现再次发热2天"))

    def test_disease_duration_not_borrowed_from_next_clause(self):
        diseases = portrait_extractor._extract_diseases("高血压，咳嗽3天")
        self.assertEqual([(d.name, d.duration) for d in diseases], [("高血压", "")])


class TimelineOrderTest(unittest.TestCase):
    """F13：时间线按起病先后排序。"""

    def test_earlier_onset_first(self):
        events = portrait_extractor._extract_symptoms("发热1天，咳嗽5天。")
        self.assertEqual([e.symptom for e in events], ["咳嗽", "发热"])


class FullTextPromptTest(unittest.TestCase):
    """F03：模型提示词包含完整正文（含追加在末尾的资料）。"""

    def test_long_case_tail_reaches_s2_prompt(self):
        text = "患者咳嗽3天。" * 1000 + "【尾部标记】眼底检查示出血。"
        self.assertGreater(len(text), 4000)
        prompt = s2_analyst._build_prompt(text, rule_engine.evaluate(text), "咳嗽", {})
        self.assertIn("【尾部标记】", prompt)

    def test_max_merged_length_fits_without_clipping(self):
        self.assertGreaterEqual(MODEL_CASE_TEXT_LIMIT, update_service.MAX_MERGED_HISTORY_LENGTH)
        text = "甲" * update_service.MAX_MERGED_HISTORY_LENGTH
        self.assertEqual(case_text_for_prompt(text), text)

    def test_overlong_text_keeps_tail_and_marks_omission(self):
        text = "头" * 10000 + "中" * 10000 + "【最新资料】"
        clipped = case_text_for_prompt(text)
        self.assertLessEqual(len(clipped), MODEL_CASE_TEXT_LIMIT)
        self.assertTrue(clipped.endswith("【最新资料】"))
        self.assertIn("中间省略", clipped)


class ProfileRefreshTest(unittest.TestCase):
    """F04：追问回答与补充资料进入后，结构化画像同步刷新。"""

    def setUp(self):
        self.store = Store(":memory:")
        self.flow = CaseFlow(store=self.store)
        result = self.flow.create_case(medical_history="咳嗽5天，发热2天，体温38.5℃。", owner_id="p")
        self.assertTrue(result.ok, result.errors)
        self.cid = result.case_id

    def tearDown(self):
        self.store.close()

    def profile(self):
        return self.store.load_case(self.cid)["payload"]["profile"]

    def test_answer_with_exam_result_updates_profile_exams(self):
        before = self.profile()
        self.assertFalse(any(e.get("item") == "胸部影像" for e in before.get("exams") or []))
        out = self.flow.submit_answers(self.cid, {"chest_imaging": "胸部CT示右下肺实变"})
        self.assertTrue(out["ok"], out)
        exams = {e["item"]: e for e in self.profile().get("exams") or []}
        self.assertIn("胸部影像", exams)
        self.assertIn("右下肺实变", exams["胸部影像"]["value"])
        self.assertTrue(exams["胸部影像"]["abnormal"])
        self.assertEqual(exams["胸部影像"]["source_refs"], ["追问回答"])

    def test_answer_with_new_disease_enters_profile(self):
        out = self.flow.submit_answers(self.cid, {"medication_list": "患有高血压3年，口服氨氯地平5mg qd"})
        self.assertTrue(out["ok"], out)
        diseases = {d["name"] for d in self.profile().get("diseases") or []}
        self.assertIn("高血压", diseases)

    def test_update_with_exam_result_updates_profile(self):
        out = self.flow.add_update(self.cid, kind="检查结果", content="眼底检查示视网膜出血")
        self.assertTrue(out["ok"], out)
        exams = {e["item"]: e for e in self.profile().get("exams") or []}
        self.assertIn("眼底检查", exams)
        self.assertEqual(exams["眼底检查"]["source_refs"], ["补充资料"])

    def test_non_clinical_update_keeps_profile(self):
        before = self.profile()
        out = self.flow.add_update(self.cid, kind="复查安排", content="下周三复查血常规")
        self.assertTrue(out["ok"], out)
        self.assertEqual(self.profile(), before)


class DegradedModelPhaseTest(unittest.TestCase):
    """F05：模型全部超时时，画像与选科相位不能显示"完成"。"""

    def test_timeouts_mark_phases_failed_and_retryable(self):
        class TimeoutAI(FakeAI):
            def call_ai_api(self, prompt, role_description=None):
                raise TimeoutError("read timeout")

        flow = CaseFlow(store=Store(":memory:"), ai_service=TimeoutAI(), use_llm=True)
        try:
            result = flow.create_case(medical_history=CASE_B, main_issue="该看哪个科",
                                      age=58, progressive=True)
            data = wait_settled(flow, result.case_id, 25.0)
            analysis = data["analysis"]
            self.assertNotEqual(analysis["state"], "done", analysis)
            self.assertEqual(analysis["phases"]["profile"]["state"], "failed", analysis)
            self.assertEqual(analysis["phases"]["triage"]["state"], "failed", analysis)
            self.assertIn("已保留规则结果", analysis["phases"]["triage"].get("note", ""))
            self.assertIn("triage", analysis.get("failed_phases") or [])
            payload = flow.get_case(result.case_id)["payload"]
            self.assertTrue(payload.get("triage"), "规则分诊结果必须保留")
            retry = flow.retry_analysis(result.case_id)
            self.assertTrue(retry["ok"], retry)
            self.assertIn("triage", retry["retried"])
            deadline = time.time() + 20
            while time.time() < deadline:
                if flow.get_analysis(result.case_id)["analysis"]["state"] != "running":
                    break
                time.sleep(0.05)
        finally:
            flow.store.close()


class UnknownExamRecheckTest(unittest.TestCase):
    """F06：识别不出的检查资料必须触发全部参与科室复核。"""

    def test_partial_overlap_now_covers_all_departments(self):
        current = ["内分泌科", "风湿免疫科", "呼吸内科"]
        result = update_service.determine_affected(
            "检查结果", "外院某项特殊检查报告已出，详见附件。", current)
        self.assertTrue(result["unclassified"])
        self.assertEqual(result["departments"], current)

    def test_recognized_exam_stays_targeted(self):
        result = update_service.determine_affected(
            "检查结果", "眼底检查示视网膜出血", ["内分泌科", "风湿免疫科", "呼吸内科"])
        self.assertFalse(result["unclassified"])
        self.assertEqual(result["departments"], ["内分泌科"])


class SimulationFingerprintTest(unittest.TestCase):
    """F09：复查安排等非临床资料不使医生端会诊模拟整轮失效。"""

    def base(self):
        return {"masked_history": "咳嗽5天", "profile": {"diseases": []}, "answers": {},
                "updates": [{"kind": "检查结果", "content": "胸片正常"}]}

    def test_follow_up_schedule_keeps_fingerprint(self):
        payload = self.base()
        before = doctor_mdt.input_fingerprint(payload)
        payload["updates"] = payload["updates"] + [{"kind": "复查安排", "content": "下周复查"}]
        payload["profile"] = dict(payload["profile"], refreshed_at="2026-10-14 20:00:00")
        self.assertEqual(doctor_mdt.input_fingerprint(payload), before)

    def test_clinical_update_changes_fingerprint(self):
        payload = self.base()
        before = doctor_mdt.input_fingerprint(payload)
        payload["updates"] = payload["updates"] + [{"kind": "检查结果", "content": "CT示结节"}]
        self.assertNotEqual(doctor_mdt.input_fingerprint(payload), before)


if __name__ == "__main__":
    unittest.main()
