"""患者注册与科室医生账号的 SQLite 持久化测试。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.store import Store  # noqa: E402


class UserStoreTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")

    def tearDown(self):
        self.store.close()

    def test_create_user_persists_hash_and_department(self):
        created = self.store.create_user(
            "doctor_eye",
            "pbkdf2_sha256$310000$salt$digest",
            role="doctor",
            label="眼科医生",
            department_code="ophthalmology",
        )
        self.assertTrue(created)
        user = self.store.load_user("doctor_eye")
        self.assertEqual(user["role"], "doctor")
        self.assertEqual(user["label"], "眼科医生")
        self.assertEqual(user["department_code"], "ophthalmology")
        self.assertEqual(user["password_hash"], "pbkdf2_sha256$310000$salt$digest")

    def test_duplicate_user_is_not_overwritten(self):
        self.assertTrue(self.store.create_user(
            "alice", "hash-one", role="patient", label="患者 A"))
        self.assertFalse(self.store.create_user(
            "alice", "hash-two", role="doctor", label="不应覆盖",
            department_code="cardiology"))
        user = self.store.load_user("alice")
        self.assertEqual(user["role"], "patient")
        self.assertEqual(user["password_hash"], "hash-one")

    def test_list_users_filters_active_doctors_by_department(self):
        self.store.create_user(
            "doctor_eye", "hash-eye", role="doctor", label="眼科医生",
            department_code="ophthalmology")
        self.store.create_user(
            "doctor_endocrine", "hash-endocrine", role="doctor", label="内分泌科医生",
            department_code="endocrinology")
        self.store.create_user(
            "doctor_disabled", "hash-disabled", role="doctor", label="停用医生",
            department_code="ophthalmology", status="disabled")

        users = self.store.list_users(role="doctor", department_code="ophthalmology")
        self.assertEqual([item["username"] for item in users], ["doctor_eye"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
