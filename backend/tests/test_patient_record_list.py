"""“我的病历”列表：标题使用患者本人的脱敏主诉摘要。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.case_flow import CaseFlow  # noqa: E402
from services.store import Store  # noqa: E402


class PatientRecordListTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.flow = CaseFlow(self.store)

    def tearDown(self):
        self.store.close()

    def test_list_includes_short_chief_complaint_summary(self):
        long_complaint = "反复咳嗽伴胸闷气促" * 10
        self.store.save_case("YX20261010-0001", owner_id="p1", payload={
            "profile": {"chief_complaint": long_complaint}})
        self.store.save_case("YX20261010-0002", owner_id="p1", payload={})
        rows = {row["case_id"]: row for row in self.flow.list_patient_cases("p1")["cases"]}
        summary = rows["YX20261010-0001"]["summary"]
        self.assertTrue(summary.startswith("反复咳嗽伴胸闷气促"))
        self.assertLessEqual(len(summary), 61)
        self.assertTrue(summary.endswith("…"))
        self.assertEqual(rows["YX20261010-0002"]["summary"], "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
