"""按日期编号的病例号：新建分配与旧编号迁移。"""

import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)

from services.case_flow import new_case_id  # noqa: E402
from services.store import Store  # noqa: E402


class CaseIdTest(unittest.TestCase):
    def test_store_allocates_daily_sequence(self):
        store = Store(":memory:")
        try:
            first = store.next_case_id("YX", day="20261010")
            second = store.next_case_id("YX", day="20261010")
            self.assertEqual((first, second), ("YX20261010-0001", "YX20261010-0002"))
            store.save_case("YX20261010-0007")
            self.assertEqual(store.next_case_id("YX", day="20261010"), "YX20261010-0008")
            self.assertEqual(store.next_case_id("YX", day="20261011"), "YX20261011-0001")
        finally:
            store.close()

    def test_concurrent_allocation_never_repeats(self):
        store = Store(":memory:")
        ids, lock = [], threading.Lock()

        def worker():
            for _ in range(20):
                value = new_case_id(store)
                with lock:
                    ids.append(value)

        try:
            threads = [threading.Thread(target=worker) for _ in range(5)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(len(ids), len(set(ids)))
            self.assertTrue(all(value.startswith("YX") for value in ids))
        finally:
            store.close()

    def test_renumber_tool_rewrites_old_ids_everywhere_and_keeps_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "cases.db")
            store = Store(db)
            store.save_case("C1A2B3C4D", payload={"note": "病例 #C1A2B3C4D"})
            store.save_case("YX20261010-0002")
            store.add_audit("C1A2B3C4D", "created", "合成审计 C1A2B3C4D")
            with store._lock:
                store._conn.execute("UPDATE cases SET created_at='2026-10-10T08:00:00' WHERE case_id='C1A2B3C4D'")
                store._conn.commit()
            store.close()
            result = subprocess.run(
                [sys.executable, os.path.join(BACKEND, "tools", "renumber_case_ids.py"), "--db", db],
                capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(result.returncode, 0, result.stderr)
            conn = sqlite3.connect(db)
            try:
                ids = sorted(r[0] for r in conn.execute("SELECT case_id FROM cases"))
                self.assertEqual(ids, ["YX20261010-0002", "YX20261010-0003"])
                payload = conn.execute("SELECT payload FROM cases WHERE case_id='YX20261010-0003'").fetchone()[0]
                self.assertIn("YX20261010-0003", payload)
                audit = conn.execute("SELECT case_id, detail FROM audit_log").fetchall()
                self.assertTrue(all("C1A2B3C4D" not in " ".join(map(str, row)) for row in audit))
            finally:
                conn.close()
            self.assertTrue(any(name.startswith("cases.db.bak-") for name in os.listdir(tmp)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
