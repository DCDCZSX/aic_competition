"""HTTP 鉴权、病例所有权、旧接口下线与 CORS 回归测试。"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402
from services.auth import AuthService, ROLE_DOCTOR, ROLE_PATIENT  # noqa: E402
from services.case_flow import CaseFlow  # noqa: E402
from services.preview_tokens import PreviewTokenService  # noqa: E402
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
        self.old_previews = app_module.preview_tokens
        self.store = Store(":memory:")
        app_module._shared_store = self.store
        app_module.case_flow = CaseFlow(store=self.store, use_llm=False)
        app_module.auth_service = AuthService(accounts=ACCOUNTS)
        app_module.preview_tokens = PreviewTokenService()
        app_module.app.config.update(TESTING=True)
        self.client = app_module.app.test_client()
        self.env = mock.patch.dict(os.environ, {"APP_AUTH_REQUIRED": "true"})
        self.env.start()
        self.store.save_case("C-ALICE", owner_id="alice", case_state="awaiting_doctor",
                             payload={"marker": "alice"})
        self.store.save_case("C-BOB", owner_id="bob", case_state="awaiting_doctor",
                             payload={"marker": "bob"})
        self.store.save_case("C-LEGACY", owner_id="", case_state="awaiting_doctor")
        self.store.grant_case_access("C-BOB", "doctor", granted_by="bob")

    def tearDown(self):
        self.env.stop()
        app_module._shared_store = self.old_store
        app_module.case_flow = self.old_flow
        app_module.auth_service = self.old_auth
        app_module.preview_tokens = self.old_previews
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

    def test_doctor_cannot_read_case_without_explicit_access(self):
        token = self._token("doctor", "doctor-password")
        response = self.client.get("/api/v2/case/C-ALICE", headers=self._headers(token))
        self.assertEqual(response.status_code, 404)

    def test_owner_can_grant_and_revoke_reviewer_access(self):
        alice = self._token("alice", "alice-password")
        doctor = self._token("doctor", "doctor-password")
        grant = self.client.post("/api/v2/case/C-ALICE/access",
                                 headers=self._headers(alice),
                                 json={"reviewer_id": "doctor"})
        self.assertEqual(grant.status_code, 200, grant.get_json())
        self.assertEqual(self.client.get("/api/v2/case/C-ALICE",
                                        headers=self._headers(doctor)).status_code, 200)
        revoke = self.client.delete("/api/v2/case/C-ALICE/access",
                                    headers=self._headers(alice),
                                    json={"reviewer_id": "doctor"})
        self.assertEqual(revoke.status_code, 200, revoke.get_json())
        self.assertEqual(self.client.get("/api/v2/case/C-ALICE",
                                        headers=self._headers(doctor)).status_code, 404)

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
        headers = self._headers(token)
        preview = self.client.post("/api/v2/preview", headers=headers, json={
            "medical_history": "2型糖尿病5年，近3个月视物模糊，血糖控制不佳。",
            "main_issue": "应该先去哪个科室就诊？",
        }).get_json()
        response = self.client.post("/api/v2/case", headers=self._headers(token), json={
            "case_id": "C-ATTACKER-CONTROLLED",
            "preview_token": preview["preview_token"],
        })
        self.assertEqual(response.status_code, 200, response.get_json())
        case_id = response.get_json()["case_id"]
        self.assertNotEqual(case_id, "C-ATTACKER-CONTROLLED")
        self.assertEqual(self.store.load_case(case_id)["owner_id"], "alice")

    def test_preview_token_is_owner_bound_and_one_time(self):
        alice = self._token("alice", "alice-password")
        bob = self._token("bob", "bob-password")
        preview = self.client.post("/api/v2/preview", headers=self._headers(alice), json={
            "medical_history": "2型糖尿病5年，视物模糊3个月。", "main_issue": "该看哪科",
        }).get_json()
        token = preview["preview_token"]
        denied = self.client.post("/api/v2/case", headers=self._headers(bob),
                                  json={"preview_token": token})
        self.assertEqual(denied.status_code, 409)
        created = self.client.post("/api/v2/case", headers=self._headers(alice),
                                   json={"preview_token": token})
        self.assertEqual(created.status_code, 200, created.get_json())
        reused = self.client.post("/api/v2/case", headers=self._headers(alice),
                                  json={"preview_token": token})
        self.assertEqual(reused.status_code, 409)

    def test_guest_is_ephemeral_and_cannot_access_case_api(self):
        response = self.client.post("/api/guest/session")
        self.assertEqual(response.status_code, 201)
        guest_token = response.get_json()["session"]["token"]
        headers = self._headers(guest_token)
        self.assertEqual(self.client.get("/api/v2/case/C-ALICE", headers=headers).status_code, 404)
        triage = self.client.post("/api/guest/triage", headers=headers, json={
            "medical_history": "2型糖尿病5年，视物模糊3个月，尿蛋白++。",
            "main_issue": "该看哪科",
        })
        self.assertEqual(triage.status_code, 200, triage.get_json())
        body = triage.get_json()
        self.assertTrue(body["guest_mode"])
        self.assertFalse(body["persistent"])
        self.assertNotIn("case_id", body)
        case_ids = {item["case_id"] for item in self.store.list_cases(limit=100)}
        self.assertEqual(case_ids, {"C-ALICE", "C-BOB", "C-LEGACY"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
