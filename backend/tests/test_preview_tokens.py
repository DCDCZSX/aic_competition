"""Focused tests for preview-token retention and consumption semantics."""

import os
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.preview_tokens import PreviewTokenService  # noqa: E402


class PreviewTokenServiceTest(unittest.TestCase):
    def test_invalid_or_non_positive_limits_use_safe_defaults(self):
        invalid_values = (None, "", "not-a-number", 0, -3)

        for value in invalid_values:
            with self.subTest(value=value):
                service = PreviewTokenService(per_owner_limit=value, global_limit=value)
                self.assertEqual(service.per_owner_limit, 5)
                self.assertEqual(service.global_limit, 5000)

    def test_positive_limits_are_preserved(self):
        service = PreviewTokenService(per_owner_limit="7", global_limit=13)

        self.assertEqual(service.per_owner_limit, 7)
        self.assertEqual(service.global_limit, 13)

    def test_app_wiring_uses_safe_defaults_for_invalid_limits(self):
        backend_dir = Path(__file__).resolve().parents[1]
        environment = os.environ.copy()
        environment.update({
            "APP_PREVIEW_MAX_PER_USER": "",
            "APP_PREVIEW_MAX_TOTAL": "not-a-number",
            "APP_DB_PATH": ":memory:",
        })
        result = subprocess.run(
            [sys.executable, "-c",
             "import app; print(app.preview_tokens.per_owner_limit, "
             "app.preview_tokens.global_limit)"],
            cwd=backend_dir,
            env=environment,
            capture_output=True,
            text=True,
            errors="replace",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "5 5000")

    def test_app_wiring_preserves_positive_limits(self):
        backend_dir = Path(__file__).resolve().parents[1]
        environment = os.environ.copy()
        environment.update({
            "APP_PREVIEW_MAX_PER_USER": "7",
            "APP_PREVIEW_MAX_TOTAL": "13",
            "APP_DB_PATH": ":memory:",
        })
        result = subprocess.run(
            [sys.executable, "-c",
             "import app; print(app.preview_tokens.per_owner_limit, "
             "app.preview_tokens.global_limit)"],
            cwd=backend_dir,
            env=environment,
            capture_output=True,
            text=True,
            errors="replace",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "7 13")

    def test_per_owner_limit_evicts_oldest_token(self):
        service = PreviewTokenService(per_owner_limit=5, global_limit=20)
        tokens = [service.issue("alice", "history", f"issue-{index}")
                  for index in range(6)]

        self.assertIsNone(service.consume(tokens[0], "alice"))
        for token in tokens[1:]:
            self.assertIsNotNone(service.consume(token, "alice"))

    def test_global_limit_evicts_only_globally_oldest_token(self):
        service = PreviewTokenService(per_owner_limit=20, global_limit=2)
        oldest = service.issue("alice", "history", "oldest")
        newer_alice = service.issue("alice", "history", "newer-alice")
        newer_bob = service.issue("bob", "history", "newer-bob")

        self.assertIsNone(service.consume(oldest, "alice"))
        self.assertIsNotNone(service.consume(newer_alice, "alice"))
        self.assertIsNotNone(service.consume(newer_bob, "bob"))

    def test_owner_binding_and_one_time_consumption_are_preserved(self):
        service = PreviewTokenService(per_owner_limit=5, global_limit=20)
        token = service.issue("alice", "history", "issue")

        self.assertIsNone(service.consume(token, "bob"))
        self.assertIsNotNone(service.consume(token, "alice"))
        self.assertIsNone(service.consume(token, "alice"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
