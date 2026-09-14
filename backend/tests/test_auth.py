"""登录、角色与会话守卫的单元测试（规格书 §3.1 / 主文档 §11）。

运行（在 CHAIS/backend 目录下）：
    python -m unittest tests.test_auth -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.auth import (  # noqa: E402
    ACCOUNTS,
    ROLE_DOCTOR,
    ROLE_PATIENT,
    AuthError,
    AuthService,
    auth_required,
    extract_token,
)


class TestLogin(unittest.TestCase):
    def setUp(self):
        self.auth = AuthService()

    def test_account_login_success(self):
        session = self.auth.login(username="patient", password="patient")
        self.assertEqual(session.role, ROLE_PATIENT)
        self.assertTrue(session.token)
        self.assertTrue(session.label)

    def test_wrong_password_rejected(self):
        with self.assertRaises(AuthError):
            self.auth.login(username="doctor", password="wrong")

    def test_unknown_account_rejected(self):
        with self.assertRaises(AuthError):
            self.auth.login(username="admin", password="admin")

    def test_role_shortcut_login(self):
        patient = self.auth.login(role=ROLE_PATIENT)
        doctor = self.auth.login(role=ROLE_DOCTOR)
        self.assertEqual(patient.role, ROLE_PATIENT)
        self.assertEqual(doctor.role, ROLE_DOCTOR)
        self.assertNotEqual(patient.token, doctor.token)

    def test_unknown_role_rejected(self):
        with self.assertRaises(AuthError):
            self.auth.login(role="admin")

    def test_no_credentials_rejected(self):
        with self.assertRaises(AuthError):
            self.auth.login()

    def test_accounts_are_the_only_ones(self):
        self.assertEqual(set(ACCOUNTS), {"patient", "doctor"})

    def test_real_auth_not_implemented(self):
        """比赛版明确不做真实认证：账号表里没有实名/短信相关字段。"""
        for account in ACCOUNTS.values():
            self.assertNotIn("phone", account)
            self.assertNotIn("id_card", account)


class TestSession(unittest.TestCase):
    def setUp(self):
        self.auth = AuthService()
        self.session = self.auth.login(role=ROLE_PATIENT)

    def test_verify_valid_token(self):
        self.assertEqual(self.auth.verify(self.session.token).username, "patient")

    def test_verify_unknown_token(self):
        self.assertIsNone(self.auth.verify("not-a-token"))

    def test_verify_empty_token(self):
        self.assertIsNone(self.auth.verify(None))
        self.assertIsNone(self.auth.verify(""))

    def test_logout_invalidates(self):
        self.assertTrue(self.auth.logout(self.session.token))
        self.assertIsNone(self.auth.verify(self.session.token))

    def test_logout_twice_is_false(self):
        self.auth.logout(self.session.token)
        self.assertFalse(self.auth.logout(self.session.token))

    def test_expired_session_is_rejected(self):
        auth = AuthService(ttl_hours=0)
        session = auth.login(role=ROLE_PATIENT)
        self.assertIsNone(auth.verify(session.token), "0 小时 TTL 应立即过期")

    def test_to_dict_hides_token_when_asked(self):
        self.assertNotIn("token", self.session.to_dict(include_token=False))
        self.assertIn("token", self.session.to_dict())


class TestScopes(unittest.TestCase):
    """后台按角色鉴权，而不是靠前端跳转（规格书 §3.1）。"""

    def setUp(self):
        self.auth = AuthService()

    def test_patient_scopes(self):
        s = self.auth.login(role=ROLE_PATIENT)
        self.assertTrue(self.auth.has_scope(s, "intake"))
        self.assertTrue(self.auth.has_scope(s, "case:own"))
        self.assertFalse(self.auth.has_scope(s, "decision"), "患者不能做医生审核决策")

    def test_doctor_scopes(self):
        s = self.auth.login(role=ROLE_DOCTOR)
        self.assertTrue(self.auth.has_scope(s, "review"))
        self.assertTrue(self.auth.has_scope(s, "decision"))
        self.assertFalse(self.auth.has_scope(s, "intake"), "医生端不承担患者录入")

    def test_none_session_has_no_scope(self):
        self.assertFalse(self.auth.has_scope(None, "intake"))


class TestTokenExtraction(unittest.TestCase):
    def test_bearer_header(self):
        self.assertEqual(extract_token("Bearer abc123"), "abc123")

    def test_raw_header(self):
        self.assertEqual(extract_token("abc123"), "abc123")

    def test_fallback(self):
        self.assertEqual(extract_token(None, "from-query"), "from-query")

    def test_empty(self):
        self.assertIsNone(extract_token(None))


class TestAuthRequiredFlag(unittest.TestCase):
    def test_default_is_false(self):
        old = os.environ.pop("APP_AUTH_REQUIRED", None)
        try:
            self.assertFalse(auth_required())
        finally:
            if old is not None:
                os.environ["APP_AUTH_REQUIRED"] = old

    def test_env_can_enable(self):
        old = os.environ.get("APP_AUTH_REQUIRED")
        try:
            os.environ["APP_AUTH_REQUIRED"] = "true"
            self.assertTrue(auth_required())
        finally:
            if old is None:
                os.environ.pop("APP_AUTH_REQUIRED", None)
            else:
                os.environ["APP_AUTH_REQUIRED"] = old


if __name__ == "__main__":
    unittest.main(verbosity=2)
