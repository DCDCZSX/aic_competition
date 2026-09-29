"""患者确认主责科室后按科室授权病例的 HTTP 集成测试。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402
from services.auth import AuthService, ROLE_DOCTOR  # noqa: E402
from services.case_flow import CaseFlow  # noqa: E402
from services.preview_tokens import PreviewTokenService  # noqa: E402
from services.store import Store  # noqa: E402
from services.auth import hash_password  # noqa: E402


class DepartmentRoutingHttpTest(unittest.TestCase):
    def setUp(self):
        self.old_store = app_module._shared_store
        self.old_flow = app_module.case_flow
        self.old_auth = app_module.auth_service
        self.old_previews = app_module.preview_tokens
        self.store = Store(":memory:")
        self.auth = AuthService(accounts={}, store=self.store)
        self.auth.register_patient("alice", "alice-password")
        self.auth.register_patient("bob", "bob-password")
        self.store.create_user(
            "doctor_endocrine", hash_password("doctor-password"), role=ROLE_DOCTOR,
            label="内分泌科医生", department_code="endocrinology")
        self.store.create_user(
            "doctor_eye", hash_password("doctor-password"), role=ROLE_DOCTOR,
            label="眼科医生", department_code="ophthalmology")
        app_module._shared_store = self.store
        app_module.case_flow = CaseFlow(store=self.store, use_llm=False)
        app_module.auth_service = self.auth
        app_module.preview_tokens = PreviewTokenService()
        app_module.app.config.update(TESTING=True)
        self.client = app_module.app.test_client()
        self._save_case(
            "C-ENDO",
            owner="alice",
            triage_department="内分泌科",
            main_issue="血糖控制不佳",
        )
        self._save_case(
            "C-ADJUST",
            owner="alice",
            triage_department="内分泌科",
            main_issue="视物模糊",
        )
        self._save_case(
            "C-NO-DOCTOR",
            owner="alice",
            triage_department="心血管内科",
            main_issue="胸闷",
        )

    def tearDown(self):
        app_module._shared_store = self.old_store
        app_module.case_flow = self.old_flow
        app_module.auth_service = self.old_auth
        app_module.preview_tokens = self.old_previews
        self.store.close()

    def _save_case(self, case_id, *, owner, triage_department, main_issue):
        self.store.save_case(
            case_id,
            owner_id=owner,
            main_issue=main_issue,
            case_state="awaiting_doctor",
            payload={
                "profile": {"chief_complaint": main_issue},
                "triage": {
                    "primary": {"department": triage_department},
                    "selected": [triage_department],
                },
            },
        )

    def _token(self, username, password):
        response = self.client.post("/api/login", json={
            "username": username, "password": password,
        })
        self.assertEqual(response.status_code, 200, response.get_json())
        return response.get_json()["session"]["token"]

    @staticmethod
    def _headers(token):
        return {"Authorization": f"Bearer {token}"}

    def test_department_catalog_exposes_only_primary_eligible_departments(self):
        response = self.client.get("/api/departments")
        self.assertEqual(response.status_code, 200, response.get_json())
        departments = response.get_json()["departments"]
        codes = {item["code"] for item in departments}
        self.assertIn("endocrinology", codes)
        self.assertNotIn("radiology", codes)

    def test_default_submission_grants_only_triage_department_doctor(self):
        patient = self._token("alice", "alice-password")
        response = self.client.post(
            "/api/v2/case/C-ENDO/submit-review",
            headers=self._headers(patient), json={},
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        body = response.get_json()
        self.assertEqual(body["routing"]["status"], "submitted")
        self.assertEqual(body["routing"]["requested_department"]["code"], "endocrinology")
        self.assertEqual(body["reviewer_count"], 1)
        self.assertNotIn("reviewer_id", str(body))

        endocrine = self._token("doctor_endocrine", "doctor-password")
        eye = self._token("doctor_eye", "doctor-password")
        self.assertEqual(
            self.client.get("/api/v2/case/C-ENDO", headers=self._headers(endocrine)).status_code,
            200,
        )
        self.assertEqual(
            self.client.get("/api/v2/case/C-ENDO", headers=self._headers(eye)).status_code,
            404,
        )

    def test_patient_can_confirm_a_different_primary_department(self):
        patient = self._token("alice", "alice-password")
        response = self.client.post(
            "/api/v2/case/C-ADJUST/submit-review",
            headers=self._headers(patient), json={"department_code": "ophthalmology"},
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        routing = response.get_json()["routing"]
        self.assertEqual(routing["requested_department"]["code"], "ophthalmology")
        self.assertTrue(routing["patient_adjusted"])
        self.assertEqual(
            self.store.load_case("C-ADJUST")["payload"]["routing"]["requested_department"]["name"],
            "眼科",
        )

    def test_no_matching_doctor_leaves_case_unsubmitted(self):
        patient = self._token("alice", "alice-password")
        response = self.client.post(
            "/api/v2/case/C-NO-DOCTOR/submit-review",
            headers=self._headers(patient), json={},
        )
        self.assertEqual(response.status_code, 409, response.get_json())
        self.assertEqual(response.get_json().get("code"), "no_reviewer_available")
        self.assertNotIn("routing", self.store.load_case("C-NO-DOCTOR")["payload"])

    def test_only_owner_can_submit_and_repeat_submission_is_idempotent(self):
        bob = self._token("bob", "bob-password")
        denied = self.client.post(
            "/api/v2/case/C-ENDO/submit-review",
            headers=self._headers(bob), json={},
        )
        self.assertEqual(denied.status_code, 404)

        alice = self._token("alice", "alice-password")
        first = self.client.post(
            "/api/v2/case/C-ENDO/submit-review",
            headers=self._headers(alice), json={},
        )
        second = self.client.post(
            "/api/v2/case/C-ENDO/submit-review",
            headers=self._headers(alice), json={},
        )
        self.assertEqual(first.status_code, 200, first.get_json())
        self.assertEqual(second.status_code, 200, second.get_json())
        self.assertFalse(second.get_json()["changed"])
        self.assertEqual(self.store.list_authorized_case_ids("doctor_endocrine").count("C-ENDO"), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
