"""专科意见审核单位与患者最终报告的回归契约。

本模块只放脱敏合成病例。当前测试先固定后续实现需要满足的接口，
因此在生产实现合入前应当按预期失败。
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.report_service import build_both  # noqa: E402
from services.review_service import build_items  # noqa: E402


def _analysis(department, *, status="ok", opinion=None):
    return {
        "department": department,
        "status": status,
        "focus": f"{department}相关评估",
        "facts": [f"{department}合成事实"],
        "inferences": [{"text": opinion or f"{department}原始专科意见",
                        "confidence": "medium", "evidence_refs": ["合成资料-1"]}],
        "checks": [{"priority": "high", "content": f"请医生确认{department}相关资料"}],
        "information_gaps": [] if status == "ok" else [f"{department}分析失败"],
    }


def unpublished_case():
    return {
        "case_id": "SYN-OP-001",
        "case_state": "awaiting_doctor",
        "report_version": 1,
        "main_issue": "合成主要问题",
        "payload": {
            "profile": {"age": 42, "sex": "女", "chief_complaint": "合成症状",
                        "diseases": [], "exams": [], "medications": []},
            "gaps": ["检查资料：信息不足"],
            "triage": {"primary": {"department": "眼科", "role": "牵头"},
                       "secondary": [], "reliability": {"level": "中", "reasons": ["待补资料"]}},
            "path": {"steps": [], "tasks": []},
            "mdt": {"analyses": [_analysis("眼科")],
                    "aggregation": {"consensus": [], "disagreements": [],
                                    "uncertainties": [], "open_questions": []}},
            "tasks": [],
        },
    }


def case_with_two_specialty_analyses():
    case = unpublished_case()
    case["case_id"] = "SYN-OP-002"
    case["payload"]["mdt"]["analyses"] = [_analysis("眼科"), _analysis("肾内科")]
    return case


def confirmed_case_with_reviewed_specialties():
    case = case_with_two_specialty_analyses()
    case["case_state"] = "doctor_confirmed"
    case["report_version"] = 2
    case["payload"]["review"] = {
        "published": True, "published_by": "合成审核医生",
        "published_at": "2026-10-09T10:00:00", "final_advice": "按审核后的安排复诊",
    }
    case["payload"]["decisions"] = [
        {"decision_id": "syn-ai-1", "target_type": "ai_analysis", "target_id": "眼科",
         "action": "accept", "before": "眼科原始专科意见", "after": None,
         "reason": "证据充分", "reviewer": {"role": "合成审核医生"},
         "created_at": "2026-10-09T09:00:00"},
        {"decision_id": "syn-ai-2", "target_type": "ai_analysis", "target_id": "肾内科",
         "action": "accept", "before": "肾内科原始专科意见", "after": None,
         "reason": "证据充分", "reviewer": {"role": "合成审核医生"},
         "created_at": "2026-10-09T09:01:00"},
    ]
    return case


def confirmed_case_with_modified_specialty():
    case = confirmed_case_with_reviewed_specialties()
    case["payload"]["decisions"][0].update(
        action="modify", before="眼科原始专科意见", after="医生修改后的专科意见",
        reason="补充合成证据")
    return case


class SpecialtyOpinionReviewContractTest(unittest.TestCase):
    def test_build_items_uses_one_ai_analysis_item_per_available_analysis(self):
        items = build_items(case_with_two_specialty_analyses())
        self.assertEqual([item.target_type for item in items].count("ai_analysis"), 2)
        self.assertFalse(any(item.target_type == "path_step" for item in items))
        self.assertEqual({item.department for item in items if item.target_type == "ai_analysis"}, {"眼科", "肾内科"})

    def test_single_specialty_still_has_one_whole_card_review_item(self):
        items = build_items(unpublished_case())
        specialty = [item for item in items if item.target_type == "ai_analysis"]
        self.assertEqual(len(specialty), 1)
        self.assertEqual(specialty[0].department, "眼科")

    def test_failed_analysis_is_reviewable_and_flagged(self):
        case = unpublished_case()
        case["payload"]["mdt"]["analyses"] = [_analysis("眼科", status="failed")]
        items = build_items(case)
        specialty = [item for item in items if item.target_type == "ai_analysis"]
        self.assertEqual(len(specialty), 1)
        failed = specialty[0] if specialty else None
        self.assertIsNotNone(failed)
        if failed is None:
            return
        self.assertTrue(failed.analysis_failed)
        self.assertTrue(failed.needs_doctor_review)


class PatientFinalReportContractTest(unittest.TestCase):
    def test_confirmed_patient_report_contains_final_sections_only(self):
        reports = build_both(confirmed_case_with_reviewed_specialties())
        final_report = reports.get("patient_final_report")
        self.assertIsNotNone(final_report)
        sections = (final_report or {}).get("sections", {})
        self.assertEqual(set(sections), {"医生审核后的专科评估意见", "医生最终建议", "后续安排"})
        self.assertNotIn("关键已知资料", sections)
        self.assertNotIn("当前信息缺口", sections)

    def test_pre_visit_summary_keeps_preparation_content(self):
        reports = build_both(unpublished_case())
        sections = reports["summary"]["sections"]
        self.assertIn("关键已知资料", sections)
        self.assertIn("当前信息缺口", sections)

    def test_specialty_decision_is_applied_without_overwriting_ai_source(self):
        case = confirmed_case_with_modified_specialty()
        reports = build_both(case)
        self.assertIn("医生修改后的专科意见", json.dumps(reports.get("patient_final_report"), ensure_ascii=False))
        self.assertIn("AI 原始专科意见", json.dumps(reports["full"], ensure_ascii=False))

    def test_rejected_specialty_is_not_presented_as_final_opinion(self):
        case = confirmed_case_with_reviewed_specialties()
        case["payload"]["decisions"][0].update(action="reject", after=None, reason="证据不足")
        reports = build_both(case)
        final_report = reports.get("patient_final_report")
        self.assertIsNotNone(final_report)
        final = json.dumps(final_report, ensure_ascii=False)
        self.assertNotIn("眼科原始专科意见", final)

    def test_formal_mdt_and_unresolved_disagreement_are_visible_in_follow_up(self):
        case = confirmed_case_with_reviewed_specialties()
        case["payload"]["mdt_formal_record"] = {
            "status": "completed", "conclusion": "正式 MDT 合成结论",
            "unresolved": "仍需补充合成检查", "next_step": "复诊时复核",
        }
        case["payload"]["mdt"]["aggregation"]["disagreements"] = [
            {"topic": "先做检查甲还是乙", "positions": {"眼科": "甲", "肾内科": "乙"},
             "resolving_info": "等待合成资料"}
        ]
        reports = build_both(case)
        final = json.dumps(reports.get("patient_final_report"), ensure_ascii=False)
        self.assertIn("仍需补充合成检查", final)
        self.assertIn("未解决", final)

    def test_final_report_keeps_only_key_points_per_specialty(self):
        case = confirmed_case_with_reviewed_specialties()
        analysis = case["payload"]["mdt"]["analyses"][0]
        analysis["inferences"] = [{"text": f"眼科评估{i}。", "evidence_refs": ["合成资料-1"]} for i in range(1, 5)]
        analysis["checks"] = [{"content": f"建议向医生确认是否需要合成检查{i}"} for i in range(1, 6)]
        analysis["information_gaps"] = ["合成缺口不应出现在患者报告"]
        reports = build_both(case)
        row = reports["patient_final_report"]["sections"]["医生审核后的专科评估意见"][0]
        self.assertEqual(row["科室"], "眼科")
        self.assertEqual(row["专科评估意见"], ["眼科评估1", "眼科评估2"])
        self.assertEqual(row["建议向医生确认"], ["是否需要合成检查1", "是否需要合成检查2"])
        self.assertEqual(row["审核"], "医生已确认")
        final = json.dumps(reports["patient_final_report"], ensure_ascii=False)
        self.assertNotIn("合成缺口不应出现在患者报告", final)
        self.assertNotIn("证据出处", final)

    def test_final_report_header_reads_like_consultation_form(self):
        case = confirmed_case_with_reviewed_specialties()
        case["payload"]["mdt_screening"] = {"decision": "recommend_mdt", "departments": ["眼科", "肾内科"]}
        case["payload"]["triage_doctor_confirmation"] = {
            "status": "confirmed", "primary_department": "眼科", "secondary_departments": ["肾内科"]}
        header = build_both(case)["patient_final_report"]["header"]
        self.assertEqual(header["报告名称"], "多学科会诊意见报告")
        self.assertEqual(header["报告编号"], "SYN-OP-002-v2")
        self.assertEqual((header["性别"], header["年龄"]), ("女", "42 岁"))
        self.assertEqual(header["主责科室"], "眼科")
        self.assertEqual(header["会诊科室"], ["肾内科"])
        self.assertEqual(header["处理方式"], "多学科会诊（MDT）")
        self.assertEqual((header["审核医生"], header["报告日期"]), ("合成审核医生", "2026-10-09 10:00"))

    def test_formal_mdt_conclusion_is_its_own_section_before_advice(self):
        case = confirmed_case_with_reviewed_specialties()
        case["payload"]["mdt_formal_record"] = {
            "status": "completed", "conclusion": "正式 MDT 合成结论", "unresolved": "无", "next_step": "两周后复诊",
        }
        report = build_both(case)["patient_final_report"]
        self.assertEqual(report["section_order"],
                         ["医生审核后的专科评估意见", "多学科会诊结论", "医生最终建议", "后续安排"])
        self.assertEqual(report["sections"]["多学科会诊结论"], "正式 MDT 合成结论")
        self.assertEqual(report["sections"]["后续安排"], ["会诊后续安排：两周后复诊"])

    def test_no_ai_key_fallback_is_explicit(self):
        case = unpublished_case()
        case["payload"]["mdt"] = {"analyses": [], "aggregation": {}}
        case["payload"]["ai_unavailable_reason"] = "未配置 AI Key"
        reports = build_both(case)
        self.assertIn("未配置 AI Key", json.dumps(reports["full"], ensure_ascii=False))

    def test_history_snapshot_is_retained_in_full_report(self):
        case = confirmed_case_with_reviewed_specialties()
        case["payload"]["review_history"] = [{"report_version": 1, "label": "首次审核快照"}]
        reports = build_both(case)
        self.assertIn("首次审核快照", json.dumps(reports["full"], ensure_ascii=False))


if __name__ == "__main__":
    unittest.main(verbosity=2)
