"""SQLite 持久化层的单元测试。

运行（在 CHAIS/backend 目录下）：
    python -m unittest tests.test_store -v

验收对应《产品开发规格书-比赛版v-competition.md》任务 0.5：
"重启后能读到历史病例与审核记录"（用"关闭连接再重新打开"模拟重启）。
"""

import os
import sqlite3
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.store import Store, default_db_path, new_run_id  # noqa: E402


class StoreTestBase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmpdir.name, "test_mdt.db")
        self.store = Store(self.db_path)

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass
        self._tmpdir.cleanup()

    def reopen(self) -> Store:
        """模拟进程重启：关掉旧连接，重新打开同一个库。"""
        self.store.close()
        self.store = Store(self.db_path)
        return self.store


class TestCasePersistence(StoreTestBase):
    def test_save_and_load_case(self):
        self.store.save_case(
            "C001", patient_name="张*", main_issue="视物模糊3月",
            status="running", case_state="analyzing",
            payload={"age": 58, "sex": "女"},
        )
        case = self.store.load_case("C001")
        self.assertIsNotNone(case)
        self.assertEqual(case["patient_name"], "张*")
        self.assertEqual(case["case_state"], "analyzing")
        self.assertEqual(case["payload"]["age"], 58)

    def test_data_survives_restart(self):
        """核心验收：重启后仍能读到历史病例。"""
        self.store.save_case("C002", patient_name="李*", main_issue="发热查因",
                             status="completed", case_state="awaiting_doctor")
        self.reopen()
        case = self.store.load_case("C002")
        self.assertIsNotNone(case, "重启后应仍能读到病例")
        self.assertEqual(case["status"], "completed")
        self.assertEqual(case["case_state"], "awaiting_doctor")

    def test_update_case_partial(self):
        self.store.save_case("C003", status="running", case_state="analyzing")
        self.store.update_case("C003", status="completed", case_state="awaiting_doctor",
                               report_version=2)
        case = self.store.load_case("C003")
        self.assertEqual(case["status"], "completed")
        self.assertEqual(case["report_version"], 2)

    def test_list_cases_returns_all(self):
        for i in range(3):
            self.store.save_case(f"C10{i}", main_issue=f"问题{i}")
        cases = self.store.list_cases()
        self.assertEqual(len(cases), 3)

    def test_list_cases_honors_offset(self):
        for i in range(5):
            self.store.save_case(f"C20{i}", main_issue=f"问题{i}")
        all_cases = self.store.list_cases(limit=10)
        page = self.store.list_cases(limit=2, offset=2)
        self.assertEqual(page, all_cases[2:4])

    def test_upsert_same_case_id(self):
        self.store.save_case("C004", status="running")
        self.store.save_case("C004", status="completed")
        self.assertEqual(len(self.store.list_cases()), 1, "同一 case_id 应更新而非重复插入")
        self.assertEqual(self.store.load_case("C004")["status"], "completed")

    def test_owner_is_persisted_and_not_erased_by_legacy_save(self):
        self.store.save_case("C-OWN", owner_id="patient-a", payload={"a": 1})
        self.store.save_case("C-OWN", payload={"a": 2})
        case = self.store.load_case("C-OWN")
        self.assertEqual(case["owner_id"], "patient-a")
        self.assertEqual(case["payload"], {"a": 2})

    def test_mutate_case_serializes_payload_updates(self):
        self.store.save_case("C-MUTATE", payload={"count": 0})

        def increment_many():
            for _ in range(50):
                def increment(case):
                    payload = case.setdefault("payload", {})
                    payload["count"] = int(payload.get("count") or 0) + 1
                self.store.mutate_case("C-MUTATE", increment)

        threads = [threading.Thread(target=increment_many) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(self.store.load_case("C-MUTATE")["payload"]["count"], 200)


class TestSchemaMigration(unittest.TestCase):
    def test_existing_database_gets_owner_column(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "legacy.db")
            conn = sqlite3.connect(path)
            conn.execute("""CREATE TABLE cases (
                case_id TEXT PRIMARY KEY, patient_name TEXT, main_issue TEXT, status TEXT,
                case_state TEXT, report_version INTEGER, created_at TEXT, updated_at TEXT,
                payload TEXT)""")
            conn.execute("INSERT INTO cases (case_id, payload) VALUES ('C-OLD', '{}')")
            conn.commit()
            conn.close()

            store = Store(path)
            try:
                self.assertEqual(store.load_case("C-OLD")["owner_id"], "")
            finally:
                store.close()


class TestAuditLog(StoreTestBase):
    def test_add_and_list_audit(self):
        run_id = self.store.add_audit("C001", "consultation_started", "主要问题：发热")
        self.assertTrue(run_id)

        self.store.add_audit("C001", "consultation_completed", "待医生审核", run_id=run_id)
        logs = self.store.list_audit("C001")
        self.assertEqual(len(logs), 2)
        self.assertEqual(logs[0]["run_id"], logs[1]["run_id"], "同一 run_id 应可串联一次运行")

    def test_audit_survives_restart(self):
        self.store.add_audit("C005", "analysis_anomaly", "内分泌科超时")
        self.reopen()
        logs = self.store.list_audit("C005")
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["event"], "analysis_anomaly")

    def test_new_run_id_unique(self):
        self.assertNotEqual(new_run_id(), new_run_id())


class TestDecisions(StoreTestBase):
    def test_save_and_list_decisions(self):
        decision = {
            "decision_id": "DD-001", "case_id": "C001",
            "target_type": "department_selection", "target_id": "ophthalmology",
            "action": "modify", "before": "先看眼科", "after": "先查眼底再看眼科",
            "reason": "补充检查优先级", "reviewer": {"role": "审核医生", "dept": "眼科"},
            "version": 3, "created_at": "2026-09-11T14:02:00",
        }
        self.store.save_decision(decision)

        self.reopen()  # 重启后仍可读
        got = self.store.list_decisions("C001")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["action"], "modify")
        self.assertEqual(got[0]["reason"], "补充检查优先级")
        self.assertEqual(got[0]["reviewer"]["dept"], "眼科")

    def test_decision_upsert(self):
        base = {"decision_id": "DD-002", "case_id": "C002", "target_type": "path_step",
                "target_id": "s1", "action": "accept", "version": 1, "created_at": "t"}
        self.store.save_decision(base)
        updated = dict(base, action="reject", reason="依据不足")
        self.store.save_decision(updated)
        got = self.store.list_decisions("C002")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["action"], "reject")


class TestTransitions(StoreTestBase):
    def test_save_and_list_transitions(self):
        self.store.save_transition("C001", "pending_materials", "analyzing",
                                   actor="system", reason="提交会诊")
        self.store.save_transition("C001", "analyzing", "need_more_info",
                                   actor="system", reason="分析异常：超时", is_anomaly=True)

        self.reopen()
        rows = self.store.list_transitions("C001")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["to_state"], "analyzing")
        self.assertEqual(rows[1]["case_state"], "need_more_info")
        self.assertEqual(rows[1]["is_anomaly"], 1, "异常流转必须留痕")

    def test_transition_case_records_each_step_of_valid_path(self):
        self.store.save_case("C-PUBLISH", case_state="awaiting_doctor")
        path = self.store.transition_case("C-PUBLISH", "doctor_confirmed", actor="doctor")
        self.assertEqual(path, ["doctor_reviewed", "doctor_confirmed"])
        rows = self.store.list_transitions("C-PUBLISH")
        self.assertEqual([(row["from_state"], row["to_state"]) for row in rows], [
            ("awaiting_doctor", "doctor_reviewed"),
            ("doctor_reviewed", "doctor_confirmed"),
        ])

    def test_transition_case_rejects_unknown_state(self):
        from services.case_state import UnknownState

        self.store.save_case("C-BAD", case_state="awaiting_doctor")
        with self.assertRaises(UnknownState):
            self.store.transition_case("C-BAD", "invented_state")
        self.assertEqual(self.store.load_case("C-BAD")["case_state"], "awaiting_doctor")


class TestMemoryStore(unittest.TestCase):
    def test_memory_db_works(self):
        store = Store(":memory:")
        store.save_case("M001", status="running")
        self.assertEqual(store.load_case("M001")["status"], "running")
        store.close()


class TestDefaultPath(unittest.TestCase):
    def test_env_override(self):
        old = os.environ.get("APP_DB_PATH")
        try:
            os.environ["APP_DB_PATH"] = r"D:\tmp\custom.db"
            self.assertEqual(default_db_path(), r"D:\tmp\custom.db")
        finally:
            if old is None:
                os.environ.pop("APP_DB_PATH", None)
            else:
                os.environ["APP_DB_PATH"] = old

    def test_default_points_to_backend_data(self):
        old = os.environ.pop("APP_DB_PATH", None)
        try:
            path = default_db_path()
            self.assertTrue(path.endswith(os.path.join("data", "mdt.db")), path)
        finally:
            if old is not None:
                os.environ["APP_DB_PATH"] = old


if __name__ == "__main__":
    unittest.main(verbosity=2)
