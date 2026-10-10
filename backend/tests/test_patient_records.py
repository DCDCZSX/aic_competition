"""患者端病例记录中心与病例列表接口。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402
from services.auth import AuthService, ROLE_PATIENT  # noqa: E402
from services.case_flow import CaseFlow  # noqa: E402
from services.store import Store  # noqa: E402


class PatientRecordsHttpTest(unittest.TestCase):
    def setUp(self):
        self.old_store = app_module._shared_store
        self.old_flow = app_module.case_flow
        self.old_auth = app_module.auth_service
        self.store = Store(":memory:")
        self.auth = AuthService(accounts={}, store=self.store)
        self.auth.register_patient("alice", "alice-password")
        self.auth.register_patient("bob", "bob-password")
        app_module._shared_store = self.store
        app_module.case_flow = CaseFlow(store=self.store, use_llm=False)
        app_module.auth_service = self.auth
        app_module.app.config.update(TESTING=True)
        self.client = app_module.app.test_client()
        self.store.save_case("C-ALICE-1", owner_id="alice", main_issue="",
                             case_state="awaiting_doctor", payload={
                                 "triage": {"primary": {"department": "内分泌科"}},
                                 "masked_main_issue": "血糖控制不佳",
                                 "masked_history": "首次提交：近两周口渴、夜尿增多"})
        self.store.save_case("C-ALICE-2", owner_id="alice", main_issue="视物模糊",
                             case_state="doctor_confirmed", report_version=2, payload={
                                 "routing": {"status": "submitted", "requested_department": {
                                     "name": "眼科"}},
                                 "triage": {"primary": {"department": "眼科"}}})
        self.store.save_case("C-BOB", owner_id="bob", main_issue="胸闷",
                             case_state="awaiting_doctor", payload={})
        for index in range(12):
            self.store.save_case(f"C-ALICE-P{index:02d}", owner_id="alice",
                                 main_issue=f"历史问题 {index}", case_state="awaiting_doctor",
                                 payload={"masked_history": f"历史病历内容 {index}"})

    def tearDown(self):
        app_module._shared_store = self.old_store
        app_module.case_flow = self.old_flow
        app_module.auth_service = self.old_auth
        self.store.close()

    def _token(self, username):
        response = self.client.post("/api/login", json={
            "username": username, "password": f"{username}-password",
        })
        self.assertEqual(response.status_code, 200, response.get_json())
        return response.get_json()["session"]["token"]

    def test_patient_case_list_is_owner_scoped_and_contains_safe_status_summary(self):
        token = self._token("alice")
        response = self.client.get("/api/v2/my-cases?page_size=50",
                                   headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 200, response.get_json())
        body = response.get_json()
        rows = {row["case_id"]: row for row in body["cases"]}
        self.assertEqual(len(rows), 14)
        self.assertIn("C-ALICE-1", rows)
        self.assertIn("C-ALICE-2", rows)
        self.assertEqual(rows["C-ALICE-2"]["report_version"], 2)
        self.assertEqual(rows["C-ALICE-2"]["primary_department"], "眼科")
        self.assertNotIn("C-BOB", str(body))
        self.assertNotIn("payload", rows["C-ALICE-2"])

    def test_patient_case_list_requires_patient_session(self):
        self.assertEqual(self.client.get("/api/v2/my-cases").status_code, 401)

    def test_patient_case_list_supports_page_size_and_total(self):
        token = self._token("alice")
        response = self.client.get("/api/v2/my-cases?page=2&page_size=5",
                                   headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 200, response.get_json())
        body = response.get_json()
        self.assertEqual(body["page"], 2)
        self.assertEqual(body["page_size"], 5)
        self.assertEqual(body["total"], 14)
        self.assertEqual(len(body["cases"]), 5)

    def test_patient_case_list_falls_back_to_masked_main_issue(self):
        token = self._token("alice")
        response = self.client.get("/api/v2/my-cases?page_size=50",
                                   headers={"Authorization": f"Bearer {token}"})
        rows = {row["case_id"]: row for row in response.get_json()["cases"]}
        self.assertEqual(rows["C-ALICE-1"]["main_issue"], "血糖控制不佳")

    def test_patient_can_archive_and_restore_own_case(self):
        token = self._token("alice")
        headers = {"Authorization": f"Bearer {token}"}
        archive = self.client.post("/api/v2/case/C-ALICE-1/archive", headers=headers)
        self.assertEqual(archive.status_code, 200, archive.get_json())
        listed = self.client.get("/api/v2/my-cases?page_size=50", headers=headers).get_json()
        self.assertNotIn("C-ALICE-1", {row["case_id"] for row in listed["cases"]})
        restore = self.client.delete("/api/v2/case/C-ALICE-1/archive", headers=headers)
        self.assertEqual(restore.status_code, 200, restore.get_json())
        listed = self.client.get("/api/v2/my-cases?page_size=50", headers=headers).get_json()
        self.assertIn("C-ALICE-1", {row["case_id"] for row in listed["cases"]})

    def test_patient_record_detail_contains_submitted_material_without_internal_review(self):
        token = self._token("alice")
        response = self.client.get(
            "/api/v2/case/C-ALICE-1/patient-record",
            headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 200, response.get_json())
        record = response.get_json()["record"]
        self.assertIn("submitted_material", record)
        self.assertEqual(record["submitted_material"]["medical_history"],
                         "首次提交：近两周口渴、夜尿增多")
        self.assertNotIn("reviewer_id", str(record))
        self.assertNotIn("decisions", record)

    def _save_report_snapshot(self):
        self.store.save_report_version({
            "case_id": "C-ALICE-2", "version": 2,
            "published_by": "审核医生", "published_at": "2026-10-06 10:00",
            "review_summary": {"internal_reviewer": "internal-account"},
            "report_json": {
                "summary": {"doctor_confirmed": True, "report_version": 2,
                            "sections": {"主要问题": "当时的视物模糊"}},
                "full": {"doctor_confirmed": True, "sections": {
                    "病例画像": {"主诉": "当时的视物模糊", "资料完整度": 0.8},
                    "医生最终意见": [{"内容": "当时的医生建议", "审核人": "internal-account"}],
                    "审核记录": [{"审核人": "internal-account"}]}},
                "doctor_result": {"published": True, "final_advice": "当时的复诊安排"}},
            "summary_markdown": "旧摘要", "full_markdown": "医生内部完整版",
        })

    def test_patient_reads_saved_report_version_without_current_content_or_internal_review(self):
        self._save_report_snapshot()
        self.store.save_case("C-ALICE-2", owner_id="alice", main_issue="后来修改的问题",
                             case_state="awaiting_recheck", report_version=3,
                             payload={"profile": {"chief_complaint": "后来修改的病情"},
                                      "review": {"final_advice": "尚未发布的新建议"}})
        response = self.client.get("/api/v2/case/C-ALICE-2/versions/2/patient-report",
                                   headers={"Authorization": f"Bearer {self._token('alice')}"})
        self.assertEqual(response.status_code, 200, response.get_json())
        body = response.get_json()
        self.assertEqual(body["version"], 2)
        self.assertEqual(body["report"]["summary"]["sections"]["主要问题"], "当时的视物模糊")
        self.assertEqual(body["report"]["doctor_result"]["final_advice"], "当时的复诊安排")
        self.assertNotIn("后来修改", str(body))
        self.assertNotIn("尚未发布", str(body))
        self.assertNotIn("internal-account", str(body))
        self.assertNotIn("资料完整度", str(body))
        self.assertNotIn("审核记录", str(body))
        self.assertNotIn("full_markdown", body)

    def test_patient_report_version_requires_owner_and_login(self):
        self._save_report_snapshot()
        url = "/api/v2/case/C-ALICE-2/versions/2/patient-report"
        self.assertEqual(self.client.get(url).status_code, 401)
        response = self.client.get(url, headers={"Authorization": f"Bearer {self._token('bob')}"})
        self.assertEqual(response.status_code, 404)

    def test_missing_report_snapshot_does_not_fabricate_history_from_current_case(self):
        response = self.client.get("/api/v2/case/C-ALICE-2/versions/2/patient-report",
                                   headers={"Authorization": f"Bearer {self._token('alice')}"})
        self.assertEqual(response.status_code, 404)
        self.assertIn("未保存", response.get_json()["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
