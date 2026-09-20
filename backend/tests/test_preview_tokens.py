"""Focused tests for preview-token retention and consumption semantics."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.preview_tokens import PreviewTokenService  # noqa: E402


class PreviewTokenServiceTest(unittest.TestCase):
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
