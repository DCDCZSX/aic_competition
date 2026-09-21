# -*- coding: utf-8 -*-
"""模型调用错误的稳定分类与脱敏契约。"""

import requests
import sys
import os
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.model_errors import (  # noqa: E402
    classify_model_exception,
    invalid_model_response,
)


class TestModelErrorPolicy(unittest.TestCase):
    def test_timeout_is_safe_and_stable(self):
        error = classify_model_exception(requests.Timeout("secret-timeout-detail"))
        self.assertEqual(error.code, "ai_timeout")
        self.assertNotIn("secret-timeout-detail", error.message)

    def test_http_statuses_are_classified_without_response_text(self):
        cases = {
            401: "ai_auth_failed",
            403: "ai_auth_failed",
            429: "ai_rate_limited",
            503: "ai_upstream_unavailable",
        }
        for status_code, expected_code in cases.items():
            with self.subTest(status_code=status_code):
                response = mock.Mock(status_code=status_code)
                error = classify_model_exception(
                    requests.HTTPError("secret-response-body", response=response),
                    status_code=status_code,
                )
                self.assertEqual(error.code, expected_code)
                self.assertNotIn("secret-response-body", error.message)

    def test_unknown_exception_does_not_echo_detail(self):
        error = classify_model_exception(RuntimeError("secret-internal-path"))
        self.assertEqual(error.code, "ai_request_failed")
        self.assertNotIn("secret-internal-path", error.message)

    def test_invalid_response_has_no_body_detail(self):
        error = invalid_model_response()
        self.assertEqual(error.code, "ai_invalid_response")
        self.assertNotIn("choices", error.message)


if __name__ == "__main__":
    unittest.main(verbosity=2)
