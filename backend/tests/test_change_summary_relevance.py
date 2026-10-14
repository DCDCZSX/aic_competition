"""回归：变化摘要只列与参与科室相关的缺口，受影响科室不再写成“未受影响”。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import update_service as us  # noqa: E402
from services.case_flow import CaseFlow  # noqa: E402
from services.store import Store  # noqa: E402

HEMA_TRIAGE = {"primary": {"department": "血液科"}, "selected": ["血液科"],
               "reliability": {"level": "中"}}
DIABETES_GAPS = ["血糖控制水平：信息不足", "尿蛋白 / 肾功能：信息不足", "眼底检查：信息不足"]


class ChangeSummaryRelevanceTest(unittest.TestCase):
    def test_irrelevant_gaps_are_not_listed_for_hematology_case(self):
        summary = us.build_change_summary(
            case_id="C1", old_triage=HEMA_TRIAGE, new_triage=HEMA_TRIAGE,
            old_path={}, new_path={}, updates=[],
            unresolved_gaps=DIABETES_GAPS + ["凝血/出血倾向：信息不足"],
            affected_departments=["血液科"])
        text = "\n".join(summary.needs_doctor_review)
        for label in ("血糖控制水平", "尿蛋白", "眼底检查"):
            self.assertNotIn(label, text)
        self.assertIn("仍未确认：凝血/出血倾向：信息不足", summary.needs_doctor_review)

    def test_affected_department_is_not_reported_as_unchanged(self):
        summary = us.build_change_summary(
            case_id="C1", old_triage=HEMA_TRIAGE, new_triage=HEMA_TRIAGE,
            old_path={}, new_path={}, updates=[], affected_departments=["血液科"])
        self.assertFalse(any("血液科 的参与判断未受影响" in x for x in summary.unchanged))
        self.assertTrue(any(x.startswith("血液科：新资料涉及该科判断") for x in summary.needs_doctor_review))

    def test_relevant_gaps_keeps_old_behavior_without_selected_departments(self):
        self.assertEqual(us.relevant_gaps(DIABETES_GAPS, []), DIABETES_GAPS)
        self.assertEqual(us.relevant_gaps(DIABETES_GAPS, ["眼科"]),
                         ["血糖控制水平：信息不足", "眼底检查：信息不足"])
        self.assertEqual(us.relevant_gaps(["自定义缺口"], ["血液科"]), ["自定义缺口"])


class HematologyUpdateEndToEndTest(unittest.TestCase):
    def test_blood_count_update_on_hematology_case(self):
        flow = CaseFlow(store=Store(":memory:"))
        try:
            case = flow.create_case(
                medical_history="女，58岁。反复乏力、面色苍白3个月，加重1周伴牙龈出血。"
                                "血常规：血红蛋白 78g/L，血小板 45×10^9/L。",
                main_issue="该看哪个科", age=58)
            result = flow.add_update(case.case_id, kind="检查结果",
                                     content="复查血常规：血红蛋白 92g/L，血小板 120×10^9/L")
            self.assertTrue(result["ok"], result)
            summary = result["change_summary"]
            review_text = "\n".join(summary["needs_doctor_review"])
            for label in ("血糖控制水平", "眼底检查"):
                self.assertNotIn(label, review_text)
            selected = (result["triage"] or {}).get("selected") or []
            if "血液科" in selected:
                self.assertFalse(any(x.startswith("血液科 的参与判断未受影响")
                                     for x in summary["unchanged"]))
        finally:
            flow.store.close()


class CaseLevelGapRelevanceTest(unittest.TestCase):
    """队列风险、医生 MDT 资料包和患者报告只使用与病例科室相关的缺口。"""

    HEMA_PAYLOAD = {
        "gaps": DIABETES_GAPS + ["凝血/出血倾向：信息不足", "过敏史：信息不足"],
        "triage": {"primary": {"department": "血液科"}, "selected": ["血液科"]},
    }

    def test_case_departments_union(self):
        from services.dependency_matrix import case_departments
        payload = {
            "triage": {"primary": {"department": "血液科"}, "selected": ["血液科"]},
            "triage_doctor_confirmation": {"status": "confirmed", "selected": ["血液科", "肾内科"]},
            "mdt_screening": {"departments": ["血液科", "肾内科"]},
            "routing": {"requested_department": {"code": "hematology", "name": "血液科"}},
        }
        self.assertEqual(case_departments(payload), ["血液科", "肾内科"])

    def test_doctor_confirmed_department_keeps_its_gaps(self):
        from services.dependency_matrix import case_relevant_gaps
        payload = dict(self.HEMA_PAYLOAD,
                       triage_doctor_confirmation={"status": "confirmed",
                                                   "selected": ["血液科", "肾内科"]})
        gaps = case_relevant_gaps(payload)
        self.assertIn("尿蛋白 / 肾功能：信息不足", gaps)   # 肾内科被医生加入
        self.assertNotIn("眼底检查：信息不足", gaps)

    def test_reports_only_list_relevant_gaps(self):
        from services import report_service
        case = {"case_id": "C1", "case_state": "awaiting_doctor", "payload": dict(self.HEMA_PAYLOAD)}
        summary = report_service.build_patient_summary(case).to_dict()
        full = report_service.build_full_report(case).to_dict()
        packet = report_service.build_mdt_preparation_packet(case)
        import json
        for text in (json.dumps(summary, ensure_ascii=False),
                     json.dumps(full, ensure_ascii=False),
                     json.dumps(packet.get("information_gaps"), ensure_ascii=False)):
            self.assertNotIn("眼底检查：信息不足", text)
            self.assertNotIn("血糖控制水平：信息不足", text)
            self.assertIn("凝血/出血倾向：信息不足", text)

    def test_queue_gap_count_ignores_irrelevant_gaps(self):
        flow = CaseFlow(store=Store(":memory:"))
        try:
            case = flow.create_case(
                medical_history="女，58岁。反复乏力、面色苍白3个月，加重1周伴牙龈出血。"
                                "血常规：血红蛋白 78g/L，血小板 45×10^9/L。",
                main_issue="该看哪个科", age=58)
            stored = flow.store.load_case(case.case_id)
            from services.dependency_matrix import case_relevant_gaps
            expected = len(case_relevant_gaps(stored["payload"]))
            self.assertLess(expected, len(stored["payload"].get("gaps") or []),
                            "合成病例应同时含相关与无关缺口")
            row = next(r for r in flow.list_review_cases(limit=20)["rows"]
                       if r["case_id"] == case.case_id)
            self.assertEqual(row["gap_count"], expected)
        finally:
            flow.store.close()


if __name__ == "__main__":
    unittest.main()
