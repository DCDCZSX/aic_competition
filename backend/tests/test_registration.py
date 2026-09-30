"""患者自助注册 HTTP 接口测试。"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402
from services.auth import AuthService  # noqa: E402
from services.store import Store  # noqa: E402


class RegistrationHttpTest(unittest.TestCase):
    def setUp(self):
        self.old_store = app_module._shared_store
        self.old_auth = app_module.auth_service
        self.store = Store(":memory:")
        app_module._shared_store = self.store
        app_module.auth_service = AuthService(accounts={}, store=self.store)
        app_module.app.config.update(TESTING=True)
        self.client = app_module.app.test_client()
        attempts = getattr(app_module, "_registration_attempts", None)
        if attempts is not None:
            attempts.clear()

    def tearDown(self):
        app_module._shared_store = self.old_store
        app_module.auth_service = self.old_auth
        self.store.close()

    def test_registers_patient_and_returns_authenticated_session(self):
        response = self.client.post("/api/register", json={
            "username": "alice_01",
            "password": "a-strong-password",
            "password_confirm": "a-strong-password",
            "label": "患者 A",
        })
        self.assertEqual(response.status_code, 201, response.get_json())
        body = response.get_json()
        self.assertEqual(body["status"], "registered")
        self.assertEqual(body["session"]["role"], "patient")
        self.assertNotIn("password", body)
        self.assertNotIn("password_hash", body)
        stored = self.store.load_user("alice_01")
        self.assertIsNotNone(stored)
        self.assertNotEqual(stored["password_hash"], "a-strong-password")

    def test_duplicate_username_returns_stable_conflict_code(self):
        first = self.client.post("/api/register", json={
            "username": "alice_01", "password": "a-strong-password",
        })
        self.assertEqual(first.status_code, 201, first.get_json())
        duplicate = self.client.post("/api/register", json={
            "username": "alice_01", "password": "another-password",
        })
        self.assertEqual(duplicate.status_code, 409, duplicate.get_json())
        self.assertEqual(duplicate.get_json().get("code"), "username_taken")

    def test_invalid_fields_and_role_escalation_are_rejected(self):
        for payload in (
            {"username": "ab", "password": "a-strong-password"},
            {"username": "alice_01", "password": "short"},
            {"username": "alice_01", "password": "a-strong-password",
             "password_confirm": "different-password"},
            {"username": "doctor_01", "password": "a-strong-password", "role": "doctor"},
        ):
            response = self.client.post("/api/register", json=payload)
            self.assertEqual(response.status_code, 400, response.get_json())
            self.assertEqual(response.get_json().get("code"), "invalid_request")

    def test_registration_is_rate_limited_by_source_address(self):
        with mock.patch.object(app_module, "REGISTRATION_MAX_PER_HOUR", 1, create=True):
            first = self.client.post("/api/register", json={
                "username": "alice_01", "password": "a-strong-password",
            })
            self.assertEqual(first.status_code, 201, first.get_json())
            second = self.client.post("/api/register", json={
                "username": "alice_02", "password": "a-strong-password",
            })
            self.assertEqual(second.status_code, 429, second.get_json())
            self.assertEqual(second.get_json().get("code"), "rate_limited")


if __name__ == "__main__":
    unittest.main(verbosity=2)
