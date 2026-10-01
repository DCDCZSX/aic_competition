"""任务 2：医生负责的 MDT 初筛与病例分流。"""

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.case_flow import CaseFlow  # noqa: E402
from services.auth import AuthService, ROLE_DOCTOR, ROLE_PATIENT, hash_password  # noqa: E402
from services.store import Store  # noqa: E402
import app as app_module  # noqa: E402


CASE_TEXT = (
    "患者：李*，男，58岁。主诉：视物模糊3月，双下肢浮肿1周。"
    "既往2型糖尿病5年，高血压10年。目前口服二甲双胍0.5g bid。"
    "检查：尿蛋白++，空腹血糖9.2。曾行心电图检查提示异常。"
)


class MdtScreeningFlowTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.flow = CaseFlow(store=self.store)
        created = self.flow.create_case(medical_history=CASE_TEXT, main_issue="该看哪个科")
        self.assertTrue(created.ok, created.errors)
        self.case_id = created.case_id
        self.before_state = self.store.load_case(self.case_id)["case_state"]

    def tearDown(self):
        self.store.close()

    def submit(self, **kwargs):
        if kwargs.get("decision") == "recommend_mdt":
            existing = self.store.load_case(self.case_id)["payload"].get("triage_doctor_confirmation")
            if not existing:
                self.flow.submit_triage_confirmation(
                    self.case_id, primary_department="内分泌科",
                    secondary_departments=["眼科"], reason="测试前置分流确认", reviewer_id="doctor")
        return self.flow.submit_mdt_screening(
            self.case_id,
            reviewer_id="doctor",
            **kwargs,
        )

    def test_all_decisions_persist_as_substate_without_changing_case_state(self):
        for decision, extra in (
            ("single_specialty", {}),
            ("recommend_mdt", {"purpose": "明确眼底和代谢问题的共同就医重点",
                                "departments": ["内分泌科", "眼科"]}),
            ("need_more_info", {}),
            ("urgent_care", {}),
        ):
            if decision == "recommend_mdt":
                confirmed = self.flow.submit_triage_confirmation(
                    self.case_id, primary_department="内分泌科",
                    secondary_departments=["眼科"], reason="先确认分流方向", reviewer_id="doctor")
                self.assertTrue(confirmed["ok"], confirmed)
            result = self.submit(decision=decision, reason=f"经过资料核对后选择{decision}", **extra)
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["mdt_screening"]["decision"], decision)
            self.assertEqual(self.store.load_case(self.case_id)["case_state"], self.before_state)

    def test_reason_is_required_for_every_decision(self):
        result = self.submit(decision="single_specialty", reason="")
        self.assertFalse(result["ok"])
        self.assertIn("原因", result["error"])

    def test_recommend_mdt_requires_purpose_and_two_distinct_domain_departments(self):
        missing_purpose = self.submit(
            decision="recommend_mdt", reason="需要多科共同判断", departments=["内分泌科", "眼科"])
        self.assertFalse(missing_purpose["ok"])
        self.assertIn("目的", missing_purpose["error"])

        too_few = self.submit(
            decision="recommend_mdt", reason="需要多科共同判断", purpose="明确就医重点",
            departments=["眼科"])
        self.assertFalse(too_few["ok"])
        self.assertIn("至少两个", too_few["error"])

        duplicate = self.submit(
            decision="recommend_mdt", reason="需要多科共同判断", purpose="明确就医重点",
            departments=["眼科", "眼科"])
        self.assertFalse(duplicate["ok"])
        self.assertIn("不同", duplicate["error"])

    def test_recommend_mdt_uses_confirmed_triage_departments_when_scope_is_omitted(self):
        result = self.submit(
            decision="recommend_mdt", reason="需要多科共同判断",
            purpose="明确共同处理重点")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["mdt_screening"]["departments"], ["内分泌科", "眼科"])
        self.assertEqual(result["mdt_screening"]["departments_source"], "triage_confirmation")

    def test_invalid_decision_and_out_of_domain_department_are_rejected(self):
        invalid_decision = self.submit(decision="auto_publish", reason="不支持的决定")
        self.assertFalse(invalid_decision["ok"])
        self.assertIn("决定", invalid_decision["error"])

        invalid_department = self.submit(
            decision="recommend_mdt", reason="需要多科共同判断", purpose="明确就医重点",
            departments=["内分泌科", "火星医学科"])
        self.assertFalse(invalid_department["ok"])
        self.assertIn("科室", invalid_department["error"])

    def test_record_is_patient_safe_and_audited_without_raw_reason(self):
        result = self.submit(
            decision="recommend_mdt", reason="依据脱敏资料，需要多科共同判断",
            purpose="明确就医重点", departments=["内分泌科", "眼科"])
        self.assertTrue(result["ok"], result)
        screening = self.store.load_case(self.case_id)["payload"]["mdt_screening"]
        self.assertEqual(screening["departments"], ["内分泌科", "眼科"])
        self.assertEqual(screening["reviewer_id"], "doctor")
        self.assertNotIn("原始", json.dumps(screening, ensure_ascii=False))
        events = self.store.list_audit(self.case_id)
        record = next(item for item in events if item["event"] == "mdt_screening_recorded")
        self.assertIn("recommend_mdt", record["detail"])
        self.assertNotIn("原始", record["detail"])

    def test_published_case_cannot_be_rescreened(self):
        self.store.update_case(self.case_id, case_state="doctor_confirmed")
        result = self.submit(decision="single_specialty", reason="不应覆盖已发布审核版")
        self.assertFalse(result["ok"])
        self.assertIn("已发布", result["error"])

    def test_urgent_care_is_marked_outside_normal_mdt_route(self):
        result = self.submit(decision="urgent_care", reason="存在需要立即线下处理的风险")
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["mdt_screening"]["mdt_queue_eligible"])
        self.assertIn("急诊", result["mdt_screening"]["patient_next_step"])
        self.assertEqual(self.flow.list_review_cases()["rows"], [])

    def test_recommend_mdt_grants_selected_department_collaborators(self):
        self.store.create_user(
            "doctor_endo", hash_password("doctor-password"), role=ROLE_DOCTOR,
            label="内分泌协作者", department_code="endocrinology",
        )
        self.store.create_user(
            "doctor_eye", hash_password("doctor-password"), role=ROLE_DOCTOR,
            label="眼科协作者", department_code="ophthalmology",
        )
        result = self.submit(
            decision="recommend_mdt", reason="需要共同判断", purpose="明确会诊重点",
            departments=["眼科", "内分泌科"],
            department_reviewer_map={
                "眼科": ["doctor_eye"],
                "内分泌科": ["doctor_endo"],
            },
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(set(result["authorized_departments"]), {"眼科", "内分泌科"})
        self.assertEqual(result["authorized_reviewer_count"], 2)
        self.assertEqual(result["pending_departments"], [])
        self.assertTrue(self.store.has_case_access(self.case_id, "doctor_eye"))
        self.assertTrue(self.store.has_case_access(self.case_id, "doctor_endo"))
        events = self.store.list_audit(self.case_id)
        self.assertTrue(any(item["event"] == "mdt_access_granted" for item in events))

    def test_mdt_without_a_department_reviewer_is_explicitly_pending(self):
        self.store.create_user(
            "doctor_endo", hash_password("doctor-password"), role=ROLE_DOCTOR,
            label="内分泌协作者", department_code="endocrinology",
        )
        result = self.submit(
            decision="recommend_mdt", reason="需要共同判断", purpose="明确会诊重点",
            departments=["眼科", "内分泌科"],
            department_reviewer_map={"眼科": [], "内分泌科": ["doctor_endo"]},
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["authorized_departments"], ["内分泌科"])
        self.assertEqual(result["pending_departments"], ["眼科"])
        self.assertEqual(result["authorized_reviewer_count"], 1)
        self.assertFalse(self.store.has_case_access(self.case_id, "doctor_eye"))
        events = self.store.list_audit(self.case_id)
        self.assertTrue(any(item["event"] == "mdt_access_pending" for item in events))

    def test_formal_mdt_record_requires_recommendation_and_persists_latest_record(self):
        before = self.flow.submit_mdt_formal_record(
            self.case_id, status="completed", participants=["内分泌科医生"],
            conclusion="不应在未启动正式 MDT 时记录", unresolved="", next_step="",
            reviewer_id="doctor")
        self.assertFalse(before["ok"])
        self.assertIn("启动正式 MDT", before["error"])

        screening = self.submit(
            decision="recommend_mdt", reason="需要多科共同判断", purpose="明确共同处理重点",
            departments=["内分泌科", "眼科"])
        self.assertTrue(screening["ok"], screening)
        for status in ("planned", "in_progress", "completed"):
            result = self.flow.submit_mdt_formal_record(
                self.case_id, status=status, participants=["内分泌科医生", "眼科医生"],
                conclusion="共同确认后续检查" if status == "completed" else "",
                unresolved="待补充检查" if status != "completed" else "",
                next_step="完成检查后复评", reviewer_id="doctor")
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["mdt_formal_record"]["status"], status)
        payload = self.store.load_case(self.case_id)["payload"]
        self.assertEqual(payload["mdt_formal_record"]["status"], "completed")
        self.assertEqual(payload["mdt_formal_record"]["participants"], ["内分泌科医生", "眼科医生"])
        report = self.flow.build_reports(self.case_id)["full"]
        self.assertEqual(report["sections"]["分诊结果"]["牵头方向"], "内分泌科")
        self.assertTrue(any(item.get("来源") == "正式 MDT 会诊记录" for item in report["sections"]["医生最终意见"]))

    def test_formal_mdt_completed_requires_conclusion(self):
        screening = self.submit(
            decision="recommend_mdt", reason="需要多科共同判断", purpose="明确共同处理重点",
            departments=["内分泌科", "眼科"])
        self.assertTrue(screening["ok"], screening)
        result = self.flow.submit_mdt_formal_record(
            self.case_id, status="completed", participants=[], conclusion="",
            unresolved="", next_step="", reviewer_id="doctor")
        self.assertFalse(result["ok"])
        self.assertIn("结论", result["error"])

    def test_triage_confirmation_persists_before_mdt(self):
        result = self.flow.submit_triage_confirmation(
            self.case_id, primary_department="内分泌科",
            secondary_departments=["眼科"], reason="医生核对分诊依据后确认",
            reviewer_id="doctor")
        self.assertTrue(result["ok"], result)
        confirmation = self.store.load_case(self.case_id)["payload"]["triage_doctor_confirmation"]
        self.assertEqual(confirmation["primary_department"], "内分泌科")
        self.assertEqual(confirmation["secondary_departments"], ["眼科"])
        self.assertEqual(confirmation["selected"], ["内分泌科", "眼科"])
        review = self.flow.get_review(self.case_id)
        self.assertFalse(any(item["target_type"] == "department_selection" for item in review["items"]))

    def test_recommend_mdt_requires_confirmed_triage_departments(self):
        result = self.flow.submit_mdt_screening(
            self.case_id, decision="recommend_mdt", reason="需要多科共同判断",
            purpose="明确共同处理重点", departments=["内分泌科", "眼科"], reviewer_id="doctor")
        self.assertFalse(result["ok"])
        self.assertIn("分流确认", result["error"])


class MdtScreeningHttpTest(unittest.TestCase):
    def setUp(self):
        self.old_store = app_module._shared_store
        self.old_flow = app_module.case_flow
        self.old_auth = app_module.auth_service
        self.store = Store(":memory:")
        self.flow = CaseFlow(store=self.store, use_llm=False)
        app_module._shared_store = self.store
        app_module.case_flow = self.flow
        app_module.auth_service = AuthService(accounts={
            "alice": {"password": "alice-password", "role": ROLE_PATIENT, "label": "患者 A"},
            "doctor": {"password": "doctor-password", "role": ROLE_DOCTOR,
                       "label": "审核医生", "department_code": "endocrinology"},
            "doctor_eye": {"password": "doctor-password", "role": ROLE_DOCTOR,
                            "label": "眼科协作者", "department_code": "ophthalmology"},
        }, store=self.store)
        app_module.app.config.update(TESTING=True)
        self.client = app_module.app.test_client()
        self.store.save_case("C-MDT", owner_id="alice", case_state="awaiting_doctor",
                             payload={"profile": {"chief_complaint": "视物模糊"}})
        self.store.grant_case_access("C-MDT", "doctor", granted_by="alice")

    def tearDown(self):
        app_module._shared_store = self.old_store
        app_module.case_flow = self.old_flow
        app_module.auth_service = self.old_auth
        self.store.close()

    def _token(self, username, password):
        response = self.client.post("/api/login", json={
            "username": username, "password": password,
        })
        self.assertEqual(response.status_code, 200, response.get_json())
        return response.get_json()["session"]["token"]

    @staticmethod
    def _headers(token):
        return {"Authorization": f"Bearer {token}"}

    def _confirm_triage(self, token):
        response = self.client.post(
            "/api/v2/case/C-MDT/triage-confirmation", headers=self._headers(token),
            json={"primary_department": "内分泌科", "secondary_departments": ["眼科"],
                  "reason": "先确认分流方向"})
        self.assertEqual(response.status_code, 200, response.get_json())

    def test_screening_requires_authenticated_authorized_doctor(self):
        body = {"decision": "single_specialty", "reason": "先由眼科评估"}
        self.assertEqual(self.client.post("/api/v2/case/C-MDT/mdt-screening", json=body).status_code, 401)

        patient = self._token("alice", "alice-password")
        self.assertEqual(
            self.client.post("/api/v2/case/C-MDT/mdt-screening",
                             headers=self._headers(patient), json=body).status_code,
            403,
        )

        other = self._token("doctor", "doctor-password")
        self.store.revoke_case_access("C-MDT", "doctor", revoked_by="alice")
        response = self.client.post("/api/v2/case/C-MDT/mdt-screening",
                                    headers=self._headers(other), json=body)
        self.assertEqual(response.status_code, 404)

    def test_authorized_doctor_can_submit_and_patient_cannot_see_reviewer_id(self):
        doctor = self._token("doctor", "doctor-password")
        self._confirm_triage(doctor)
        response = self.client.post(
            "/api/v2/case/C-MDT/mdt-screening",
            headers=self._headers(doctor),
            json={
                "decision": "recommend_mdt",
                "reason": "资料提示需要共同评估",
                "purpose": "明确眼底和代谢问题的就医重点",
                "departments": ["眼科", "内分泌科"],
            },
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertTrue(response.get_json()["ok"])
        self.assertEqual(response.get_json()["authorized_reviewer_count"], 2)
        eye = self._token("doctor_eye", "doctor-password")
        self.assertEqual(
            self.client.get("/api/v2/case/C-MDT", headers=self._headers(eye)).status_code,
            200,
        )
        review = self.client.get("/api/v2/case/C-MDT/review", headers=self._headers(doctor))
        self.assertEqual(review.status_code, 200)
        self.assertEqual(review.get_json()["mdt_screening"]["decision"], "recommend_mdt")
        packet = review.get_json()["mdt_preparation_packet"]
        self.assertEqual(packet["screening"]["purpose"], "明确眼底和代谢问题的就医重点")
        self.assertNotIn("masked_history", json.dumps(packet, ensure_ascii=False))

        patient = self._token("alice", "alice-password")
        case = self.client.get("/api/v2/case/C-MDT", headers=self._headers(patient))
        self.assertEqual(case.status_code, 200)
        screening = case.get_json()["case"]["payload"]["mdt_screening"]
        self.assertEqual(screening["decision"], "recommend_mdt")
        self.assertNotIn("reviewer_id", screening)
        report = self.client.get("/api/v2/case/C-MDT/report", headers=self._headers(patient))
        self.assertEqual(report.status_code, 200)
        self.assertEqual(report.get_json()["mdt_screening"]["decision"], "recommend_mdt")
        self.assertNotIn("reviewer_id", json.dumps(report.get_json(), ensure_ascii=False))

    def test_invalid_screening_payload_is_json_400(self):
        doctor = self._token("doctor", "doctor-password")
        response = self.client.post(
            "/api/v2/case/C-MDT/mdt-screening",
            headers=self._headers(doctor),
            json={"decision": "recommend_mdt", "reason": "需要共同评估"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json().get("code"), "invalid_request")

    def test_authorized_doctor_can_record_formal_mdt_after_screening(self):
        doctor = self._token("doctor", "doctor-password")
        self._confirm_triage(doctor)
        screening = self.client.post(
            "/api/v2/case/C-MDT/mdt-screening", headers=self._headers(doctor),
            json={"decision": "recommend_mdt", "reason": "需要共同评估",
                  "purpose": "明确共同处理重点", "departments": ["眼科", "内分泌科"]})
        self.assertEqual(screening.status_code, 200, screening.get_json())
        response = self.client.post(
            "/api/v2/case/C-MDT/mdt-formal-record", headers=self._headers(doctor),
            json={"status": "completed", "participants": ["眼科医生", "内分泌科医生"],
                  "conclusion": "共同确认后续检查", "unresolved": "", "next_step": "复查后复评"})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()["mdt_formal_record"]["status"], "completed")
        review = self.client.get("/api/v2/case/C-MDT/review", headers=self._headers(doctor))
        self.assertEqual(review.get_json()["mdt_formal_record"]["conclusion"], "共同确认后续检查")
        patient = self._token("alice", "alice-password")
        patient_case = self.client.get("/api/v2/case/C-MDT", headers=self._headers(patient))
        self.assertNotIn("mdt_formal_record", patient_case.get_json()["case"]["payload"])
        self.assertNotIn("triage_doctor_confirmation", patient_case.get_json()["case"]["payload"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
