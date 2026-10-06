"""后台分析任务去重与 MDT 分诊一致性回归测试。"""

import os
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from schemas import SpecialistOpinionCard  # noqa: E402
from services.case_flow import CaseFlow  # noqa: E402
from services.mdt_analyst import METHOD_LLM, SpecialtyAnalysis  # noqa: E402
from services.store import Store  # noqa: E402


def wait_until_finished(flow, case_id, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        thread = flow._active_jobs.get(case_id)
        if thread is None or not thread.is_alive():
            return
        time.sleep(0.01)
    raise AssertionError("后台分析任务未在限定时间内结束")


class AnalysisJobTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.flow = CaseFlow(store=self.store, use_llm=False)

    def tearDown(self):
        self.store.close()

    def test_duplicate_live_job_is_rejected(self):
        case_id = "C-DUPLICATE"
        self.store.save_case(case_id, case_state="pending_materials", payload={
            "analysis": CaseFlow._new_analysis_status(),
        })
        entered = threading.Event()
        release = threading.Event()

        def blocking_worker(_case_id, _phases):
            entered.set()
            release.wait(2)

        self.flow._analysis_worker = blocking_worker
        self.assertTrue(self.flow.start_analysis_job(case_id, phases=["profile"]))
        self.assertTrue(entered.wait(1), "首个后台任务未启动")
        self.assertFalse(self.flow.start_analysis_job(case_id, phases=["profile"]))
        release.set()
        wait_until_finished(self.flow, case_id)

    def test_mdt_uses_persisted_selected_departments(self):
        case_id = "C-PERSISTED-TRIAGE"
        selected = ["眼科", "肾内科"]
        analysis = CaseFlow._new_analysis_status(mdt_total=len(selected))
        analysis["phases"]["profile"] = {"state": "done"}
        analysis["phases"]["triage"] = {"state": "done"}
        self.store.save_case(case_id, case_state="analyzing", main_issue="该看哪个科", payload={
            "masked_history": "糖尿病5年，视物模糊并有蛋白尿。",
            "triage": {"selected": selected, "behavior_type": "B"},
            "profile": {},
            "analysis": analysis,
        })
        seen = []

        def fake_analysis(**kwargs):
            department = kwargs["department"]
            seen.append(department)
            card = SpecialistOpinionCard(
                case_id=kwargs["case_id"], specialty_code=department,
                specialty_name=department, focus=f"{department}关注点",
            )
            return SpecialtyAnalysis(department=department, card=card, method=METHOD_LLM)

        with mock.patch("services.case_flow.analyze_specialty", side_effect=fake_analysis):
            self.assertTrue(self.flow.start_analysis_job(case_id, phases=["mdt"]))
            wait_until_finished(self.flow, case_id)

        self.assertEqual(seen, selected)
        payload = self.store.load_case(case_id)["payload"]
        self.assertEqual(payload["mdt"]["departments"], selected)

    def test_retry_is_rejected_for_confirmed_case_until_new_materials_reopen_review(self):
        case_id = "C-CONFIRMED-RETRY"
        self.store.save_case(case_id, case_state="doctor_confirmed", payload={
            "analysis": CaseFlow._new_analysis_status(),
        })

        result = self.flow.retry_analysis(case_id)

        self.assertFalse(result["ok"])
        self.assertIn("新资料", result["error"])
        self.assertEqual(self.store.load_case(case_id)["case_state"], "doctor_confirmed")

    def test_failed_analysis_does_not_promote_case_to_awaiting_doctor(self):
        case_id = "C-FAILED-ANALYSIS"
        analysis = CaseFlow._new_analysis_status()
        self.store.save_case(case_id, case_state="analyzing", payload={
            "masked_history": "需要分析",
            "triage": {"selected": ["内分泌科"]},
            "analysis": analysis,
        })

        with mock.patch("services.case_flow.extract", side_effect=RuntimeError("模型不可用")):
            self.flow._analysis_worker(case_id, ["profile"])

        case = self.store.load_case(case_id)
        self.assertNotEqual(case["case_state"], "awaiting_doctor")
        self.assertEqual(case["payload"]["analysis"]["state"], "failed")
        self.assertEqual(case["payload"]["analysis"]["phases"]["profile"]["state"], "failed")

    def test_analysis_start_is_rejected_for_doctor_reviewed_case(self):
        case_id = "C-REVIEWED-NO-RETRY"
        self.store.save_case(case_id, case_state="doctor_reviewed", payload={
            "analysis": CaseFlow._new_analysis_status(),
        })

        self.assertFalse(self.flow.start_analysis_job(case_id, phases=["profile"]))
        result = self.flow.retry_analysis(case_id, phases=["profile"])

        self.assertFalse(result["ok"])
        self.assertIn("审核", result["error"])
        self.assertEqual(self.store.load_case(case_id)["case_state"], "doctor_reviewed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
