"""全量审核账号与科室审核账号的可见范围回归。"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402
from services.auth import AuthService, ROLE_DOCTOR, hash_password  # noqa: E402
from services.case_flow import CaseFlow  # noqa: E402
from services.store import Store  # noqa: E402


class GlobalDoctorAccessTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.store.create_user(
            "chief_doctor", hash_password("chief-password"),
            role=ROLE_DOCTOR, label="总审医生", department_code="*",
        )
        self.store.create_user(
            "doctor_eye", hash_password("eye-password"),
            role=ROLE_DOCTOR, label="眼科医生", department_code="ophthalmology",
        )
        self.auth = AuthService(accounts={}, store=self.store)
        self.flow = CaseFlow(store=self.store, use_llm=False)
        self.old_store = app_module._shared_store
        self.old_flow = app_module.case_flow
        self.old_auth = app_module.auth_service
        app_module._shared_store = self.store
        app_module.case_flow = self.flow
        app_module.auth_service = self.auth
        app_module.app.config.update(TESTING=True)
        self.client = app_module.app.test_client()

    def tearDown(self):
        app_module._shared_store = self.old_store
        app_module.case_flow = self.old_flow
        app_module.auth_service = self.old_auth
        self.store.close()

    def _login(self, username, password):
        response = self.client.post(
            "/api/login", json={"username": username, "password": password}
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        return {"Authorization": "Bearer " + response.get_json()["session"]["token"]}

    def test_global_account_is_returned_for_each_department_and_session_marks_scope(self):
        session = self.auth.login(username="chief_doctor", password="chief-password")
        self.assertEqual(session.access_scope, "all")
        self.assertEqual(session.to_dict(include_token=False)["access_scope"], "all")
        reviewers = self.auth.list_reviewer_accounts("ophthalmology")
        self.assertEqual(
            {item["username"] for item in reviewers},
            {"chief_doctor", "doctor_eye"},
        )

    def test_global_account_can_list_and_open_existing_case_without_case_access_row(self):
        created = self.flow.create_case(
            medical_history="视物模糊 3 个月。",
            main_issue="该看哪个科",
            owner_id="patient-1",
        )
        self.assertTrue(created.ok, created.errors)
        headers = self._login("chief_doctor", "chief-password")

        queue = self.client.get("/api/v2/cases", headers=headers)
        self.assertEqual(queue.status_code, 200, queue.get_json())
        self.assertEqual(queue.get_json()["total"], 1)
        detail = self.client.get(
            f"/api/v2/case/{created.case_id}/review", headers=headers
        )
        self.assertEqual(detail.status_code, 200, detail.get_json())

    def test_department_account_still_requires_case_access(self):
        created = self.flow.create_case(
            medical_history="视物模糊 3 个月。",
            main_issue="该看哪个科",
            owner_id="patient-1",
        )
        self.assertTrue(created.ok, created.errors)
        headers = self._login("doctor_eye", "eye-password")
        response = self.client.get(
            f"/api/v2/case/{created.case_id}/review", headers=headers
        )
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
