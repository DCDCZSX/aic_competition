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
                             case_state="awaiting_doctor", payload={"triage": {
                                 "primary": {"department": "内分泌科"}}})
        self.store.save_case("C-ALICE-2", owner_id="alice", main_issue="视物模糊",
                             case_state="doctor_confirmed", report_version=2, payload={
                                 "routing": {"status": "submitted", "requested_department": {
                                     "name": "眼科"}},
                                 "triage": {"primary": {"department": "眼科"}}})
        self.store.save_case("C-BOB", owner_id="bob", main_issue="胸闷",
                             case_state="awaiting_doctor", payload={})

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
        response = self.client.get("/api/v2/my-cases",
                                   headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 200, response.get_json())
        body = response.get_json()
        rows = {row["case_id"]: row for row in body["cases"]}
        self.assertEqual(set(rows), {"C-ALICE-1", "C-ALICE-2"})
        self.assertEqual(rows["C-ALICE-2"]["report_version"], 2)
        self.assertEqual(rows["C-ALICE-2"]["primary_department"], "眼科")
        self.assertNotIn("C-BOB", str(body))
        self.assertNotIn("payload", rows["C-ALICE-2"])

    def test_patient_case_list_requires_patient_session(self):
        self.assertEqual(self.client.get("/api/v2/my-cases").status_code, 401)


if __name__ == "__main__":
    unittest.main(verbosity=2)
