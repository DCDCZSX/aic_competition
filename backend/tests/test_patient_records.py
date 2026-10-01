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
        self.store.save_case("C-ALICE-1", owner_id="alice", main_issue="血糖控制不佳",
                             case_state="awaiting_doctor", payload={
                                 "triage": {"primary": {"department": "内分泌科"}},
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
