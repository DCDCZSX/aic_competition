"""业务入口回归：病例依据更新、当前用药和异步分析的一致性。"""
import threading
import unittest
from unittest import mock

from services import doctor_mdt, portrait_extractor, rule_engine
from services.case_flow import CaseFlow
from services.store import Store


class ContextScopeTest(unittest.TestCase):
    def departments(self, text):
        return {hit.department for hit in rule_engine.evaluate(text).hits}

    def test_negated_normal_result_keeps_abnormal_signal(self):
        self.assertIn("内分泌科", self.departments("主诉：咳嗽3天。血糖不正常。"))
        self.assertNotIn("内分泌科", self.departments("主诉：咳嗽3天。血糖正常。"))

    def test_resolved_symptom_does_not_remove_chronic_disease(self):
        text = "主诉：咳嗽3天。糖尿病伴视物模糊已缓解。"
        self.assertIn("糖尿病", [d.name for d in portrait_extractor._extract_diseases(text)])
        self.assertIn("内分泌科", self.departments(text))
        self.assertNotIn("眼科", self.departments(text))

    def test_abnormal_other_indicator_does_not_cancel_normal_result(self):
        for connector in ("但", "，", "、", "同时"):
            with self.subTest(connector=connector):
                text = "血糖正常" + connector + "尿蛋白升高"
                self.assertNotIn("内分泌科", self.departments(text))
                self.assertIn("肾内科", self.departments(text))


class CurrentMedicationTest(unittest.TestCase):
    def test_latest_dose_replaces_earlier_dose(self):
        meds = portrait_extractor._extract_medications("二甲双胍 500mg。最新用药：二甲双胍 1000mg。")
        self.assertEqual([(m.name, m.dose) for m in meds], [("二甲双胍", "1000mg")])

    def test_administration_and_change_words_are_not_drug_names(self):
        meds = portrait_extractor._extract_medications("长期口服二甲双胍500mg。二甲双胍调整为1000mg。")
        self.assertEqual([(m.name, m.dose) for m in meds], [("二甲双胍", "1000mg")])

    def test_stopped_medication_is_not_current_and_restart_is_current(self):
        stopped = portrait_extractor._extract_medications("二甲双胍500mg。现停用二甲双胍。")
        self.assertEqual(stopped, [])
        restarted = portrait_extractor._extract_medications("二甲双胍500mg。停用二甲双胍。恢复口服二甲双胍250mg。")
        self.assertEqual([(m.name, m.dose) for m in restarted], [("二甲双胍", "250mg")])

    def test_update_refreshes_current_medication_and_retains_original_text(self):
        store = Store(":memory:")
        flow = CaseFlow(store=store, use_llm=False)
        try:
            case = flow.create_case(medical_history="主诉：咳嗽3天。糖尿病5年。二甲双胍500mg。")
            result = flow.add_update(case.case_id, kind="用药方案", content="最新用药：二甲双胍1000mg。")
            self.assertTrue(result["ok"], result)
            payload = store.load_case(case.case_id)["payload"]
            self.assertEqual([(m["name"], m["dose"]) for m in payload["profile"]["medications"]],
                             [("二甲双胍", "1000mg")])
            self.assertIn("二甲双胍500mg", payload["masked_history"])
            self.assertIn("二甲双胍1000mg", payload["updates"][-1]["content"])
        finally:
            store.close()


class NonClinicalUpdateTest(unittest.TestCase):
    def test_schedule_update_preserves_simulation_and_clinical_projection(self):
        store = Store(":memory:")
        flow = CaseFlow(store=store, use_llm=False)
        try:
            case = flow.create_case(medical_history="主诉：糖尿病5年，视物模糊3个月，尿蛋白++。")
            cid = case.case_id
            routed = flow.submit_doctor_routing(cid, primary_department="内分泌科", secondary_departments=["眼科"],
                decision="recommend_mdt", purpose="共同评估", reason="协作", reviewer_id="合成医生")
            self.assertTrue(routed["ok"], routed)
            payload = store.load_case(cid)["payload"]
            flow._patch_payload(cid, doctor_mdt_simulation={"state": "done",
                "input_fingerprint": doctor_mdt.input_fingerprint(payload),
                "result": {"analyses": [{"department": "眼科", "status": "ok"}], "aggregation": {}}})
            before = store.load_case(cid)["payload"]
            result = flow.add_update(cid, kind="复查安排", content="下周二门诊复诊")
            self.assertTrue(result["ok"], result)
            after = store.load_case(cid)["payload"]
            self.assertEqual(doctor_mdt.simulation_view(after)["state"], "done")
            for key in ("masked_history", "profile", "triage", "path", "gaps"):
                self.assertEqual(after[key], before[key], key)
            self.assertEqual(after["updates"][-1]["content"], "下周二门诊复诊")
        finally:
            store.close()


class PartialAnalysisConcurrencyTest(unittest.TestCase):
    def test_live_slow_worker_is_not_reported_as_orphaned(self):
        store = Store(":memory:")
        flow = CaseFlow(store=store, use_llm=False)
        try:
            status = flow._new_analysis_status()
            status["state"] = "running"
            status["started_at"] = "2000-01-01T00:00:00"
            status["phases"]["mdt"]["state"] = "running"
            store.save_case("SLOW-LIVE", case_state="analyzing", payload={
                "analysis": status, "triage": {"selected": ["眼科", "肾内科"]}})
            flow._active_jobs["SLOW-LIVE"] = mock.Mock(is_alive=mock.Mock(return_value=True))
            result = flow.get_analysis("SLOW-LIVE")
            self.assertEqual(result["analysis"]["state"], "running")
            self.assertEqual(result["analysis"]["phases"]["mdt"]["state"], "running")
            self.assertFalse(result["analysis"]["failures"])
        finally:
            store.close()

    def test_late_answer_analysis_cannot_overwrite_new_update_analysis(self):
        store = Store(":memory:")
        flow = CaseFlow(store=store, use_llm=False)
        started, release = threading.Event(), threading.Event()
        jobs = []
        try:
            case = flow.create_case(medical_history="主诉：2型糖尿病5年，视物模糊3个月，双下肢浮肿1周。尿蛋白++，血糖9.2。")
            cid = case.case_id
            depts = store.load_case(cid)["payload"]["triage"]["selected"]
            flow._patch_payload(cid, mdt={"analyses": [{"department": d, "status": "ok", "focus": "初始",
                "facts": [], "inferences": [], "checks": [], "information_gaps": []} for d in depts],
                "departments": depts, "aggregation": {}})
            flow.use_llm = True
            flow.ai_service = mock.Mock(api_key="synthetic")

            def delayed_specialty(**kwargs):
                if threading.current_thread().name.startswith("partial-mdt-"):
                    started.set()
                    if not release.wait(5):
                        raise RuntimeError("等待释放模型替身超时")
                    tag = "旧回答分析"
                else:
                    tag = "新检查分析"
                result = mock.Mock()
                result.to_dict.return_value = {"department": kwargs["department"], "status": "ok", "focus": tag,
                    "facts": [tag], "inferences": [], "checks": [], "information_gaps": []}
                return result

            with mock.patch.object(flow, "_model_available", return_value=True), \
                 mock.patch("services.case_flow.analyze_specialty", side_effect=delayed_specialty):
                answer = flow.submit_answers(cid, {"fundus_exam": "眼底检查未见异常"}, defer_mdt=True)
                self.assertTrue(answer["ok"], answer)
                self.assertTrue(started.wait(2), "应从追问入口启动局部任务")
                with flow._jobs_lock:
                    jobs = list(flow._active_jobs.values())
                update = flow.add_update(cid, kind="检查结果", content="眼底复查提示视网膜出血")
                self.assertTrue(update["ok"], update)
                release.set()
                for job in jobs:
                    job.join(3)
                    self.assertFalse(job.is_alive())
            payload = store.load_case(cid)["payload"]
            self.assertEqual(payload["mdt"]["analyses"][0]["focus"], "新检查分析")
            self.assertIn("眼底复查提示视网膜出血", payload["masked_history"])
        finally:
            release.set()
            for job in jobs:
                job.join(3)
            store.close()


if __name__ == "__main__":
    unittest.main()
