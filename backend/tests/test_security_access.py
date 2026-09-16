"""HTTP 鉴权、病例所有权、旧接口下线与 CORS 回归测试。"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402
from services.auth import AuthService, ROLE_DOCTOR, ROLE_PATIENT  # noqa: E402
from services.case_flow import CaseFlow  # noqa: E402
from services.store import Store  # noqa: E402


ACCOUNTS = {
    "alice": {"password": "alice-password", "role": ROLE_PATIENT, "label": "患者 A"},
    "bob": {"password": "bob-password", "role": ROLE_PATIENT, "label": "患者 B"},
    "doctor": {"password": "doctor-password", "role": ROLE_DOCTOR, "label": "审核医生"},
}


class SecurityAccessTest(unittest.TestCase):
    def setUp(self):
        self.old_store = app_module._shared_store
        self.old_flow = app_module.case_flow
        self.old_auth = app_module.auth_service
        self.store = Store(":memory:")
        app_module._shared_store = self.store
        app_module.case_flow = CaseFlow(store=self.store, use_llm=False)
        app_module.auth_service = AuthService(accounts=ACCOUNTS)
        app_module.app.config.update(TESTING=True)
        self.client = app_module.app.test_client()
        self.env = mock.patch.dict(os.environ, {"APP_AUTH_REQUIRED": "true"})
        self.env.start()
        self.store.save_case("C-ALICE", owner_id="alice", case_state="awaiting_doctor",
                             payload={"marker": "alice"})
        self.store.save_case("C-BOB", owner_id="bob", case_state="awaiting_doctor",
                             payload={"marker": "bob"})
        self.store.save_case("C-LEGACY", owner_id="", case_state="awaiting_doctor")

    def tearDown(self):
        self.env.stop()
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

    def test_unauthenticated_case_access_is_rejected(self):
        self.assertEqual(self.client.get("/api/v2/case/C-ALICE").status_code, 401)

    def test_patient_cannot_read_another_patients_case(self):
        token = self._token("alice", "alice-password")
        response = self.client.get("/api/v2/case/C-BOB", headers=self._headers(token))
        self.assertEqual(response.status_code, 404)

    def test_patient_cannot_claim_legacy_ownerless_case(self):
        token = self._token("alice", "alice-password")
        response = self.client.get("/api/v2/case/C-LEGACY", headers=self._headers(token))
        self.assertEqual(response.status_code, 404)

    def test_doctor_can_read_authorized_cases(self):
        token = self._token("doctor", "doctor-password")
        response = self.client.get("/api/v2/case/C-BOB", headers=self._headers(token))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["case"]["owner_id"], "bob")

    def test_query_string_token_is_not_authentication(self):
        token = self._token("alice", "alice-password")
        response = self.client.get(f"/api/v2/case/C-ALICE?token={token}")
        self.assertEqual(response.status_code, 401)

    def test_legacy_bypass_routes_are_removed(self):
        self.assertEqual(self.client.post("/api/start_consultation", json={}).status_code, 404)
        self.assertEqual(self.client.get("/api/get_discussion_status/C-ALICE").status_code, 404)
        self.assertEqual(self.client.get("/api/get_all_discussions").status_code, 404)

    def test_arbitrary_origin_is_not_reflected(self):
        response = self.client.get("/api/me", headers={"Origin": "https://evil.example"})
        self.assertNotEqual(response.headers.get("Access-Control-Allow-Origin"),
                            "https://evil.example")

    def test_case_creation_sets_owner_and_ignores_client_case_id(self):
        token = self._token("alice", "alice-password")
        response = self.client.post("/api/v2/case", headers=self._headers(token), json={
            "case_id": "C-ATTACKER-CONTROLLED",
            "medical_history": "2型糖尿病5年，近3个月视物模糊，血糖控制不佳。",
            "main_issue": "应该先去哪个科室就诊？",
        })
        self.assertEqual(response.status_code, 200, response.get_json())
        case_id = response.get_json()["case_id"]
        self.assertNotEqual(case_id, "C-ATTACKER-CONTROLLED")
        self.assertEqual(self.store.load_case(case_id)["owner_id"], "alice")


if __name__ == "__main__":
    unittest.main(verbosity=2)
