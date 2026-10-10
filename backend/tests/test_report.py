"""报告生成与证据板的单元测试。

运行（在 CHAIS/backend 目录下）：
    python -m unittest tests.test_report -v

验收对应规格书 §10：报告必填结构字段完整率 100%；主文档 §9（证据板红线）。
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.case_flow import CaseFlow  # noqa: E402
from services.evidence_service import (  # noqa: E402
    STATE_CONFLICT,
    STATE_DIRECT,
    build_evidence_cards,
    build_timeline,
    external_evidence_for,
    summarize_evidence,
)
from services.report_service import (  # noqa: E402
    AI_BADGE,
    CAPABILITY_BOUNDARY,
    DISCLAIMER,
    REQUIRED_SECTIONS,
    build_both,
    build_mdt_preparation_packet,
    build_full_report,
    build_patient_summary,
    patient_timeline_for_report,
    render_markdown,
)
from services.store import Store  # noqa: E402

CASE_B = ("患者：张*，男，58岁。主诉：视物模糊3月，双下肢浮肿1周。"
          "既往2型糖尿病5年，高血压10年。目前口服二甲双胍0.5g bid。"
          "检查：尿蛋白++，空腹血糖9.2。")


def sample_profile():
    return {
        "age": 58, "sex": "男", "chief_complaint": "视物模糊3月，双下肢浮肿1周",
        "diseases": [{"name": "2型糖尿病", "duration": "5年", "source_refs": ["S-1:32"]}],
        "symptoms_timeline": [{"symptom": "视物模糊", "onset": "3月前",
                               "source_refs": ["S-1:8"], "confidence": "high"}],
        "exams": [{"item": "尿蛋白", "value": "++", "abnormal": True, "source_refs": ["S-1:60"]}],
        "medications": [{"name": "二甲双胍", "dose": "0.5g bid"}],
        "information_gaps": ["眼底检查：信息不足"],
    }


def sample_case():
    return {
        "case_id": "C001", "case_state": "awaiting_doctor", "report_version": 1,
        "payload": {
            "profile": sample_profile(),
            "gaps": ["眼底检查：信息不足"],
            "completeness": 0.8,
            "direct_quote_coverage": 1.0,
            "triage": {
                "behavior_type": "B",
                "primary": {"department": "内分泌科", "role": "牵头",
                            "signals": ["糖尿病/血糖异常 → 内分泌科"]},
                "secondary": [{"department": "眼科", "role": "协作",
                               "signals": ["视力/眼底问题 → 眼科"]}],
                "reliability": {"level": "中", "reasons": ["仍有未确认信息"]},
                "decision_path": "rule-dominant",
                "needs_doctor_review": True,
            },
            "path": {"steps": [{"step_id": "S1", "title": "资料准备"}], "tasks": []},
            "tasks": [{"type": "就医咨询", "content": "向医生确认是否需要：完善眼底检查",
                       "priority": "high", "state": "todo", "source": "眼科:checks"}],
            "mdt": None,
        },
    }


def sample_case_with_mdt():
    """带多学科分析的样本（用于报告展示层测试：专科视角 / 共识 / 分歧 / 证据卡都要有内容）。"""
    case = sample_case()
    case["payload"]["path"]["steps"] = [
        {"step_id": "S1", "title": "整理近 3 个月血糖记录", "detail": "就诊前准备",
         "kind": "prepare", "parallel": False, "requires_doctor": False},
        {"step_id": "S2", "title": "向医生确认是否需要完善眼底检查",
         "detail": "医疗行为需医生确认", "kind": "check", "condition": "若血糖控制不佳",
         "depends_on": ["S1"], "requires_doctor": True},
    ]
    case["payload"]["mdt"] = {
        "analyses": [
            {"department": "眼科", "status": "ok", "focus": "眼底与视网膜",
             "facts": ["视物模糊 3 月"],
             "inferences": [{"text": "需排查糖尿病视网膜病变", "confidence": "medium",
                             "evidence_refs": ["视物模糊 3 月"]}],
             "checks": [{"priority": "high", "content": "建议向医生确认是否需要完善眼底检查"}],
             "information_gaps": ["眼底检查：信息不足"]},
            {"department": "肾内科", "status": "ok", "focus": "尿蛋白与肾功能",
             "facts": ["尿蛋白 ++"],
             "inferences": [{"text": "需评估糖尿病肾病可能", "confidence": "low",
                             "evidence_refs": ["尿蛋白 ++"]}],
             "checks": [{"priority": "high", "content": "建议向医生确认是否需要查肌酐与 eGFR"}],
             "information_gaps": ["肾功能：信息不足"]},
        ],
        "aggregation": {
            "consensus": [{"topic": "眼底检查", "supporters": ["眼科", "内分泌科"],
                           "sources": ["check", "gap"], "example": "建议向医生确认是否需要眼底检查"}],
            "disagreements": [{"topic": "先查眼底 vs 先查肾功",
                               "positions": {"眼科": "先查眼底", "肾内科": "先查肾功"},
                               "reason": "两个专科在同一方向上给出了相反倾向",
                               "resolving_info": "需补充更明确的检查依据后再判断",
                               "evidence_state": "conflict"}],
            "uncertainties": [{"topic": "眼底检查", "impact": ["眼科", "内分泌科"]}],
            "open_questions": ["是否需要眼科随诊"],
        },
    }
    return case


class TestTimeline(unittest.TestCase):
    def test_timeline_kinds(self):
        tl = build_timeline(sample_profile())
        kinds = {e["kind"] for e in tl}
        self.assertIn("symptom", kinds)
        self.assertIn("diagnosis", kinds)
        self.assertIn("exam", kinds)

    def test_events_with_source_first(self):
        tl = build_timeline(sample_profile())
        self.assertTrue(tl[0]["source_refs"], "带来源引用的条目应排前面")

    def test_empty_profile(self):
        self.assertEqual(build_timeline(None), [])

    def test_patient_timeline_only_contains_timed_symptom_changes(self):
        """患者端时间线只表达病程变化，不把检查和当前用药当成时间事件。"""
        profile = sample_profile()
        profile["symptoms_timeline"].append({
            "symptom": "乏力", "onset": "", "trend": "加重", "source_refs": ["S-1:90"],
        })
        timeline = patient_timeline_for_report(profile)
        self.assertTrue(timeline)
        self.assertTrue(all(item["类型"] == "症状" for item in timeline))
        self.assertTrue(all(item["时间"] not in ("", "时间不详") for item in timeline))
        text = json.dumps(timeline, ensure_ascii=False)
        self.assertNotIn("尿蛋白", text)
        self.assertNotIn("二甲双胍", text)
        self.assertNotIn("当前用药", text)
        self.assertNotIn("检查结果", text)
        self.assertNotIn("乏力", text, "缺少起始时间的症状不能伪装成时间事件")


class TestEvidenceCards(unittest.TestCase):
    def test_cards_from_triage(self):
        cards = build_evidence_cards(sample_profile(), sample_case()["payload"]["triage"])
        self.assertTrue(cards)
        self.assertTrue(all("evidence_state" in c for c in cards))

    def test_text_similarity_does_not_become_direct_evidence(self):
        """Text overlap is only a candidate; it cannot prove support."""
        cards = build_evidence_cards(sample_profile(), sample_case()["payload"]["triage"])
        primary_card = cards[0]
        self.assertNotEqual(primary_card["evidence_state"], STATE_DIRECT)

    def test_exact_source_reference_is_direct_evidence(self):
        case = sample_case()
        case["payload"]["triage"]["primary"]["evidence_refs"] = ["S-1:32"]
        cards = build_evidence_cards(sample_profile(), case["payload"]["triage"])
        self.assertEqual(cards[0]["evidence_state"], STATE_DIRECT)
        self.assertEqual(cards[0]["supports"][0]["source_refs"], ["S-1:32"])

    def test_evidence_dimensions_are_independent(self):
        card = build_evidence_cards(sample_profile(), sample_case()["payload"]["triage"])[0]
        self.assertIn(card["evidence_sufficiency"], {"partial", "insufficient"})
        self.assertEqual(card["review_status"], "ai_unreviewed")

    def test_opposes_is_explicit_even_when_empty(self):
        """反对证据为空也要显式呈现该字段（不能只展示有利一面）。"""
        cards = build_evidence_cards(sample_profile(), sample_case()["payload"]["triage"])
        for card in cards:
            self.assertIn("opposes", card)

    def test_specialty_card_marks_gaps_as_opposes(self):
        mdt = {"analyses": [{
            "status": "ok", "department": "眼科",
            "inferences": [{"text": "需排查糖尿病视网膜病变", "confidence": "medium",
                            "evidence_refs": ["视物模糊"]}],
            "information_gaps": ["无既往眼底记录"],
        }]}
        cards = build_evidence_cards(sample_profile(), None, mdt)
        specialty = [c for c in cards if c.get("scope") == "specialty"][0]
        self.assertTrue(specialty["opposes"])
        self.assertEqual(specialty["evidence_state"], STATE_CONFLICT)

    def test_disagreement_card_is_conflict(self):
        mdt = {"aggregation": {"disagreements": [
            {"topic": "建议手术 vs 保守治疗", "positions": {"心血管内科": "手术", "肾内科": "保守"},
             "reason": "方向相反", "resolving_info": "补充检查"}]}}
        cards = build_evidence_cards(sample_profile(), None, mdt)
        self.assertTrue(any(c["evidence_state"] == STATE_CONFLICT for c in cards))

    def test_external_evidence_whitelist(self):
        found = external_evidence_for("需排查糖尿病视网膜病变，建议眼底检查")
        self.assertTrue(found)
        self.assertTrue(all("source" in f for f in found))

    def test_no_external_evidence_returns_empty(self):
        """未命中白名单 → 空列表（调用方显示"暂无可靠外部依据"，不伪造文献）。"""
        self.assertEqual(external_evidence_for("毫无关键词的判断"), [])

    def test_summary_counts(self):
        cards = build_evidence_cards(sample_profile(), sample_case()["payload"]["triage"])
        stat = summarize_evidence(cards)
        self.assertEqual(stat["total"], len(cards))


class TestPatientSummary(unittest.TestCase):
    def test_required_sections_present(self):
        report = build_patient_summary(sample_case())
        for key in ("主要问题", "关键已知资料", "当前信息缺口", "建议咨询方向",
                    "需要向医生确认的问题", "当前任务"):
            self.assertIn(key, report.sections)

    def test_badge_when_not_confirmed(self):
        report = build_patient_summary(sample_case())
        self.assertEqual(report.to_dict()["badge"], AI_BADGE)

    def test_unreviewed_report_has_explicit_patient_stage(self):
        data = build_patient_summary(sample_case()).to_dict()
        self.assertEqual(data["stage"], "ai_preparation")
        self.assertEqual(data["stage_label"], "AI 就诊准备摘要")
        self.assertEqual(data["review_status"], "ai_unreviewed")
        self.assertEqual(data["report_version"], 1)
        self.assertNotIn("医生审核版", data["stage_label"])

    def test_badge_cleared_after_doctor_confirmation(self):
        case = sample_case()
        case["case_state"] = "doctor_confirmed"
        data = build_patient_summary(case).to_dict()
        self.assertEqual(data["badge"], "")
        self.assertEqual(data["stage"], "doctor_reviewed")
        self.assertEqual(data["stage_label"], "医生审核版摘要")
        self.assertEqual(data["review_status"], "doctor_confirmed")

    def test_no_duplicate_consult_questions(self):
        case = sample_case()
        case["payload"]["tasks"] = [
            {"type": "就医咨询", "content": "向医生确认是否需要：完善眼底检查"},
            {"type": "就医咨询", "content": "向医生确认是否需要：完善眼底检查"},
        ]
        report = build_patient_summary(case)
        qs = report.sections["需要向医生确认的问题"]
        self.assertEqual(len(qs), len(set(qs)), "待确认问题不应重复")

    def test_disclaimer_always_present(self):
        self.assertEqual(build_patient_summary(sample_case()).disclaimer, DISCLAIMER)


class TestMdtPreparationPacket(unittest.TestCase):
    def test_packet_collects_screening_sources_and_open_items(self):
        case = sample_case_with_mdt()
        case["payload"]["mdt_screening"] = {
            "decision": "recommend_mdt",
            "decision_label": "医生建议多学科共同评估",
            "purpose": "明确眼底和肾功能的共同就医重点",
            "departments": ["眼科", "肾内科"],
            "reason": "资料涉及多个相关方向",
            "status": "decided",
        }

        packet = build_mdt_preparation_packet(case)

        self.assertEqual(packet["screening"]["purpose"], "明确眼底和肾功能的共同就医重点")
        self.assertEqual(packet["screening"]["departments"], ["眼科", "肾内科"])
        self.assertTrue(packet["timeline"])
        self.assertTrue(packet["evidence"])
        self.assertTrue(packet["information_gaps"])
        self.assertTrue(packet["consensus"])
        self.assertTrue(packet["disagreements"])
        self.assertTrue(packet["open_questions"])

    def test_packet_labels_specialty_content_and_never_copies_raw_history(self):
        case = sample_case_with_mdt()
        case["medical_history"] = "这段原始病史不应进入会前资料包"
        case["payload"]["masked_history"] = "脱敏后的完整病史也不应被复制"

        packet = build_mdt_preparation_packet(case)
        specialty = packet["specialty_opinions"][0]

        self.assertEqual(specialty["source_type"], "ai_reference")
        self.assertEqual(specialty["source_label"], "专科 AI 参考意见")
        self.assertNotIn("medical_history", packet)
        self.assertNotIn("masked_history", packet)
        serialized = json.dumps(packet, ensure_ascii=False)
        self.assertNotIn("原始病史不应进入", serialized)
        self.assertNotIn("脱敏后的完整病史也不应被复制", serialized)
        self.assertNotIn("confidence", serialized)


class TestFullReport(unittest.TestCase):
    def test_all_required_sections_complete(self):
        """规格书 §10：必填结构字段完整率 100%。"""
        report = build_full_report(sample_case())
        self.assertTrue(report.complete, report.missing_sections)
        for name in REQUIRED_SECTIONS:
            self.assertIn(name, report.sections)

    def test_missing_content_becomes_placeholder(self):
        """未生成的段落给说明性占位（诚实），而不是留空导致结构不完整。"""
        report = build_full_report(sample_case())   # 无 mdt
        # 其他未生成段落走通用占位文案
        placeholders = [str(v) for v in report.sections.values()]
        self.assertTrue(any("本版本未生成" in v for v in placeholders), placeholders[:3])
        # 「各专科 AI 视角」要说清"为什么没有多学科"（单科 / A 型 / 模型未生成）
        view = str(report.sections["各专科 AI 视角"])
        self.assertTrue(any(w in view for w in ("未运行多学科会诊", "仅 1 个就医方向",
                                               "本版本未生成")), view)
        self.assertEqual(report.missing_sections, [], "占位后不应再有缺失段")

    def test_mdt_placeholder_says_why(self):
        """单科 / A 型病例的占位要写成"本例以单科为主…"，而不是含糊的"模型未配置"。"""
        case = sample_case()
        case["payload"]["triage"] = {"selected": ["呼吸内科", "感染科"], "needs_mdt": False}
        case["payload"]["mdt"] = {"analyses": [], "aggregation": {"notes": []}}
        report = build_full_report(case)
        self.assertIn("未运行多学科会诊", str(report.sections["各专科 AI 视角"]))

    def test_mdt_placeholder_for_single_department(self):
        case = sample_case()
        case["payload"]["triage"] = {"selected": ["呼吸内科"], "needs_mdt": False}
        case["payload"]["mdt"] = {"analyses": [], "aggregation": {"notes": []}}
        report = build_full_report(case)
        self.assertIn("仅 1 个就医方向", str(report.sections["各专科 AI 视角"]))

    def test_no_imperative_medical_advice(self):
        case = sample_case()
        markdown = render_markdown(build_full_report(case))
        for word in ("立即停药", "自行加量", "必须手术"):
            self.assertNotIn(word, markdown)

    # ------------------------------------------------------------ 展示层（对外可读）
    def test_report_sections_are_chinese_labelled(self):
        """报告段落不得出现内部英文字段名 —— 这是"报告里冒出英文"缺陷的回归。

        允许出现的英文只有医学/技术白名单（如 CT、MRI、eGFR、AI）；
        内部结构键（judgement / evidence_state / supports / scope / confidence …）
        与内部代码值（direct / rule-dominant / awaiting_doctor / department_selection …）一律不得出现。
        """
        case = sample_case_with_mdt()
        report = build_full_report(case)
        text = json.dumps(report.sections, ensure_ascii=False)

        forbidden_keys = (
            "judgement", "evidence_state", "supports", "opposes", "external",
            "confidence", "scope", "reason\"", "resolving_info", "doctor_confirmed",
            "step_id", "depends_on", "requires_doctor", "specialty_name",
            "information_gaps", "source_refs", "target_type", "action",
            "behavior_type", "decision_path", "reliability", "case_state",
            "supporters", "positions", "impact", "topic",
        )
        for key in forbidden_keys:
            self.assertNotIn(key, text, f"报告里泄漏了内部字段名：{key}")

        forbidden_values = (
            "rule-dominant", "model-dominant", "awaiting_doctor", "doctor_confirmed",
            "department_selection", "path_step", "consensus_item", "patient_advice",
            "direct", "indirect", "missing", "specialty",
        )
        for value in forbidden_values:
            self.assertNotIn(f'"{value}"', text, f"报告里泄漏了内部代码值：{value}")

    def test_report_does_not_expose_model_confidence(self):
        """红线：报告不得出现模型自报置信度（规格书 §4.3）。"""
        report = build_full_report(sample_case_with_mdt())
        text = json.dumps(report.sections, ensure_ascii=False)
        for token in ("置信度", "confidence", "概率"):
            self.assertNotIn(token, text, f"报告里出现了不该展示的 {token}")

    def test_nested_structures_stay_renderable(self):
        """证据卡 / 路径步骤 / 专科视角必须是"中文键 → 字符串或字符串数组/卡片数组"，
        不能再出现裸对象（前端旧渲染会把它显示成 [object Object]）。"""
        report = build_full_report(sample_case_with_mdt())
        for section in ("时间线", "就医路径", "各专科 AI 视角", "证据关联"):
            value = report.sections[section]
            self.assertIsInstance(value, (list, dict), f"{section} 结构异常：{type(value)}")
        cards = report.sections["证据关联"]["证据卡片"]
        self.assertTrue(cards, "样本病例应产出证据卡")
        for card in cards:
            self.assertIsInstance(card, dict)
            for key, value in card.items():
                self.assertIsInstance(key, str)
                self.assertIn(type(value), (str, int, float, bool, list),
                              f"证据卡字段 {key} 是裸对象，前端会渲染成 [object Object]")
            self.assertIn("证据状态", card)
            self.assertIn("支持证据", card)
            self.assertIn("外部依据", card)
            # 支持证据必须是"卡片形状"的列表（含中文键），而不是裸 dict 列表
            supports = card["支持证据"]
            self.assertIsInstance(supports, list)
            for item in supports:
                self.assertIn(type(item), (str, dict))
                if isinstance(item, dict):
                    self.assertIn("证据", item)

    def test_timeline_and_path_have_chinese_labels(self):
        report = build_full_report(sample_case_with_mdt())
        timeline = report.sections["时间线"]
        self.assertTrue(timeline, "样本病例应产出时间线")
        for event in timeline:
            self.assertIn("时间", event)
            self.assertIn("类型", event)
            self.assertIn("内容", event)
            self.assertNotIn("confidence", event, "时间线不得带模型自报置信度")
            self.assertEqual(event["类型"], "症状")
        timeline_text = json.dumps(timeline, ensure_ascii=False)
        self.assertNotIn("尿蛋白", timeline_text)
        self.assertNotIn("二甲双胍", timeline_text)

        steps = report.sections["就医路径"]
        self.assertTrue(steps, "样本病例应产出就医路径")
        for step in steps:
            self.assertIn("步骤", step)
            self.assertIn("内容", step)
            self.assertNotIn("step_id", step)

    def test_markdown_has_badge_and_disclaimer(self):
        markdown = render_markdown(build_full_report(sample_case()))
        self.assertIn(AI_BADGE, markdown)
        self.assertIn("不构成诊断", markdown)
        self.assertIn("本系统不能做什么", markdown)

    def test_build_both(self):
        both = build_both(sample_case())
        self.assertIn("summary", both)
        self.assertIn("full", both)
        self.assertIn("# AI 就诊准备摘要", both["summary_markdown"])
        self.assertIn("# AI 就诊准备报告", both["full_markdown"])

    def test_markdown_exposes_stage_and_version(self):
        case = sample_case()
        case["report_version"] = 3
        markdown = render_markdown(build_patient_summary(case))
        self.assertIn("AI 就诊准备摘要", markdown)
        self.assertIn("报告版本：3", markdown)
        self.assertNotIn("已由医生审核确认", markdown)

    def test_confirmed_markdown_uses_doctor_reviewed_stage(self):
        case = sample_case()
        case["case_state"] = "doctor_confirmed"
        case["report_version"] = 2
        markdown = render_markdown(build_full_report(case))
        self.assertIn("医生审核版报告", markdown)
        self.assertIn("报告版本：2", markdown)


class TestEndToEnd(unittest.TestCase):
    def setUp(self):
        self.flow = CaseFlow(store=Store(":memory:"))

    def tearDown(self):
        self.flow.store.close()

    def test_report_from_real_case(self):
        r = self.flow.create_case(medical_history=CASE_B, main_issue="该看哪个科", age=58)
        reports = self.flow.build_reports(r.case_id)
        self.assertIsNotNone(reports)
        self.assertTrue(reports["full"]["complete"])
        self.assertEqual(reports["summary"]["badge"], AI_BADGE)
        # 就医咨询任务已生成且去重（报告段已是中文键结构：类型 = 就诊 / 检查 / 资料准备 / 复诊）
        consult = [t for t in reports["full"]["sections"]["就医路径"] if t.get("类型") == "就诊"]
        self.assertTrue(consult)
        tasks = r.path["tasks"]
        consult_tasks = [t["content"] for t in tasks if t["type"] == "就医咨询"]
        self.assertEqual(len(consult_tasks), len(set(consult_tasks)), "咨询任务不应重复")

    def test_report_unknown_case(self):
        self.assertIsNone(self.flow.build_reports("C-NOT-EXIST"))

    def test_report_labels_exempt_items_separately(self):
        """报告必须把"免审（规则判定）"与"医生审核"分开写，不能让读者以为医生看过（红线）。"""
        r = self.flow.create_case(medical_history=CASE_B, main_issue="该看哪个科", age=58)
        review = self.flow.get_review(r.case_id)
        auto = review["summary"]["auto_processed"]
        self.assertTrue(auto, "该病例应有免审条目")
        # 尚未发布：报告照实写 0 条
        before = self.flow.build_reports(r.case_id)["full"]["sections"]["审核记录与版本"]
        self.assertEqual(before.get("免审条目"), "0 条")
        # 发布后：免审条数如实单列，且写明"未计入医生审核"
        review = self.flow.get_review(r.case_id)
        for item in (review or {}).get("items", []):
            if item["review_tier"] == "required" and item["state"] == "awaiting":
                result = self.flow.submit_decision(
                    r.case_id, item["item_id"], "accept", reviewer_name="医学生")
                self.assertTrue(result["ok"], result.get("error"))
        published = self.flow.publish_review(r.case_id, reviewer_name="医学生")
        self.assertTrue(published["ok"], published.get("error"))
        after = self.flow.build_reports(r.case_id)["full"]["sections"]["审核记录与版本"]
        self.assertIn(f"{auto} 条", str(after.get("免审条目")))
        self.assertIn("未计入医生审核", str(after.get("免审条目")))
        markdown = self.flow.build_reports(r.case_id)["full_markdown"]
        self.assertIn("免审条目", markdown)


class TestSpecialtyOpinionRegression(unittest.TestCase):
    def test_confirmed_report_has_patient_final_report_projection(self):
        from tests.test_specialty_opinion_report import confirmed_case_with_reviewed_specialties

        reports = build_both(confirmed_case_with_reviewed_specialties())
        self.assertEqual(
            set(reports["patient_final_report"]["sections"]),
            {"医生审核后的专科评估意见", "医生最终建议", "后续安排"},
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
