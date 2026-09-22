# -*- coding: utf-8 -*-
"""环境变量解析的边界契约。"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.config import ConfigurationError, read_int_env  # noqa: E402
from services.ai_service import AIService  # noqa: E402
from services.auth import AuthService  # noqa: E402


class TestIntegerEnvironment(unittest.TestCase):
    def test_missing_or_empty_value_uses_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TEST_INTEGER_ENV", None)
            self.assertEqual(read_int_env("TEST_INTEGER_ENV", 7), 7)
            os.environ["TEST_INTEGER_ENV"] = "  "
            self.assertEqual(read_int_env("TEST_INTEGER_ENV", 7), 7)

    def test_whitespace_is_accepted_for_valid_integer(self):
        with mock.patch.dict(os.environ, {"TEST_INTEGER_ENV": " 12 "}):
            self.assertEqual(read_int_env("TEST_INTEGER_ENV", 7, minimum=1), 12)

    def test_invalid_text_names_the_variable(self):
        with mock.patch.dict(os.environ, {"TEST_INTEGER_ENV": "not-a-number"}):
            with self.assertRaisesRegex(ConfigurationError, "TEST_INTEGER_ENV"):
                read_int_env("TEST_INTEGER_ENV", 7)

    def test_out_of_range_value_is_rejected(self):
        with mock.patch.dict(os.environ, {"TEST_INTEGER_ENV": "0"}):
            with self.assertRaisesRegex(ConfigurationError, "TEST_INTEGER_ENV"):
                read_int_env("TEST_INTEGER_ENV", 7, minimum=1)
        with mock.patch.dict(os.environ, {"TEST_INTEGER_ENV": "11"}):
            with self.assertRaisesRegex(ConfigurationError, "TEST_INTEGER_ENV"):
                read_int_env("TEST_INTEGER_ENV", 7, maximum=10)

    def test_ai_service_rejects_invalid_retry_configuration(self):
        with mock.patch.dict(os.environ, {"AI_MAX_RETRIES": "99"}):
            with self.assertRaisesRegex(ConfigurationError, "AI_MAX_RETRIES"):
                AIService()

    def test_ai_service_rejects_invalid_timeout_configuration(self):
        with mock.patch.dict(os.environ, {"AI_READ_TIMEOUT": "999"}):
            with self.assertRaisesRegex(ConfigurationError, "AI_READ_TIMEOUT"):
                AIService()

    def test_auth_service_rejects_invalid_login_configuration(self):
        with mock.patch.dict(os.environ, {"APP_LOGIN_MAX_FAILURES": "not-a-number"}):
            with self.assertRaisesRegex(ConfigurationError, "APP_LOGIN_MAX_FAILURES"):
                AuthService()

    def test_guest_session_rejects_invalid_ttl_configuration(self):
        auth = AuthService()
        with mock.patch.dict(os.environ, {"APP_GUEST_TTL_MINUTES": "0"}):
            with self.assertRaisesRegex(ConfigurationError, "APP_GUEST_TTL_MINUTES"):
                auth.create_guest_session()


if __name__ == "__main__":
    unittest.main(verbosity=2)
