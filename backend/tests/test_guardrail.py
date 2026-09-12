"""Guardrail 输入侧（G1 危象 / G6 非医学内容）的单元测试。

运行（在 CHAIS/backend 目录下）：
    python -m unittest tests.test_guardrail -v

验收对应《产品开发规格书-比赛版v-competition.md》：
- §4.1：危象词表 ≥40 条 + 组合规则，词表可审计
- §10：危象集 n≥10 → **100% 触发 G1**
- §4.4 G6：非医学内容礼貌拒绝
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.guardrail import (  # noqa: E402
    CRISIS_ADVICE,
    CRISIS_RULES,
    audit_list,
    crisis_rule_count,
    detect_crisis,
    looks_non_medical,
    screen,
)
from services.intake import validate_intake  # noqa: E402

# 规格书 §10 要求：构造危象集 n≥10，且 100% 触发
CRISIS_CASES = [
    "患者持续胸痛伴大汗2小时，含服硝酸甘油不缓解",
    "突发意识不清，家人叫不醒",
    "呕血约500ml，伴头晕",
    "严重呼吸困难，口唇发紫",
    "突然出现口角歪斜、右侧肢体无力",
    "患者说自己不想活了，有自杀念头",
    "误服农药约50ml",
    "全身抽搐不止，持续10分钟",
    "过敏性休克，血压测不到",
    "板状腹，全腹压痛反跳痛",
    "车祸伤，头部外伤后呕吐",
    "孕妇腹痛伴阴道出血",
    "突然看不见了，视力骤降",
    "幼儿高热伴抽搐",
]

NORMAL_CASES = [
    "视物模糊3月，双下肢浮肿1周，既往2型糖尿病5年，口服二甲双胍0.5g bid",
    "反复发热3周伴乏力、盗汗，血常规提示三系减少",
    "咳嗽咳痰1周，无发热，既往慢阻肺病史",
    "体检发现甲状腺结节，无明显不适",
]


class TestCrisisWordlist(unittest.TestCase):
    def test_rule_count_at_least_40(self):
        """规格书 §4.1：内置 ≥40 条可审计清单。"""
        self.assertGreaterEqual(crisis_rule_count(), 40)

    def test_every_rule_is_auditable(self):
        """每条规则都要有名称、分类、理由与匹配条件。"""
        for rule in CRISIS_RULES:
            with self.subTest(rule=rule.label):
                self.assertTrue(rule.label.strip())
                self.assertTrue(rule.category.strip())
                self.assertTrue(rule.reason.strip(), "缺少审计理由")
                self.assertTrue(rule.keywords or rule.all_of, "缺少匹配条件")

    def test_audit_list_shape(self):
        items = audit_list()
        self.assertEqual(len(items), crisis_rule_count())
        self.assertIn("reason", items[0])
        self.assertIn("keywords", items[0])


class TestCrisisDetection(unittest.TestCase):
    def test_all_constructed_cases_trigger(self):
        """核心验收：构造危象集 100% 触发 G1。"""
        missed = []
        for text in CRISIS_CASES:
            if not detect_crisis(text):
                missed.append(text)
        self.assertEqual(missed, [], f"以下危象用例未触发：{missed}")

    def test_normal_cases_do_not_trigger(self):
        for text in NORMAL_CASES:
            with self.subTest(text=text[:16]):
                self.assertEqual(detect_crisis(text), [], "正常病例不应误触发危象")

    def test_combination_rule_chest_pain_plus_sweat(self):
        """组合规则：需"胸痛 + 大汗"同时出现。"""
        self.assertTrue(detect_crisis("胸痛伴大汗"))
        self.assertTrue(detect_crisis("患者胸痛，伴大汗淋漓"))
        # 只有胸痛（无大汗）时，仍可能命中其他规则；这里用一个不含其他危象词的句子
        self.assertEqual(detect_crisis("偶有轻微胸部不适，休息后缓解"), [])

    def test_screen_returns_g1_and_advice(self):
        result = screen("患者持续胸痛伴大汗")
        self.assertTrue(result.blocked)
        self.assertEqual(result.code, "G1")
        self.assertTrue(result.is_crisis)
        self.assertIn("120", result.advice)
        self.assertTrue(result.reasons, "必须记录触发依据")
        self.assertIn("不代表不存在急症", result.disclaimer)


class TestNonMedical(unittest.TestCase):
    def test_ad_spam_rejected(self):
        self.assertTrue(looks_non_medical("加微信领取优惠，包邮下单"))
        result = screen("加微信购买保健品，优惠多多")
        self.assertTrue(result.blocked)
        self.assertEqual(result.code, "G6")

    def test_homework_rejected(self):
        self.assertTrue(looks_non_medical("帮我写作文，题目是我的假期"))

    def test_medical_text_with_ad_words_not_rejected(self):
        """真实病历里若夹杂广告词，但有医学特征 → 不判为非医学。"""
        text = "患者主诉胸痛3天，就诊于某医院，医生开了优惠套餐药物"
        self.assertFalse(looks_non_medical(text))

    def test_normal_medical_text_passes(self):
        result = screen(NORMAL_CASES[0])
        self.assertFalse(result.blocked)


class TestIntakeIntegration(unittest.TestCase):
    """G1 必须比 G2/G3 更早判定（安全优先）。"""

    def test_crisis_blocks_intake(self):
        r = validate_intake(medical_history="持续胸痛伴大汗2小时")
        self.assertFalse(r.ok)
        self.assertEqual(r.blocked_by, "G1")
        self.assertTrue(r.is_crisis)

    def test_crisis_blocks_even_without_main_issue(self):
        r = validate_intake(medical_history="患者突发昏迷")
        self.assertFalse(r.ok)
        self.assertEqual(r.blocked_by, "G1")

    def test_crisis_takes_priority_over_pii(self):
        """同时含 PII 与危象时，应先报 G1（安全优先）。"""
        r = validate_intake(medical_history="持续胸痛伴大汗，联系电话 13812345678")
        self.assertEqual(r.blocked_by, "G1")

    def test_non_medical_blocks_intake(self):
        r = validate_intake(medical_history="加微信下单可享优惠")
        self.assertFalse(r.ok)
        self.assertEqual(r.blocked_by, "G6")

    def test_normal_text_not_blocked_by_guardrail(self):
        r = validate_intake(medical_history=NORMAL_CASES[1], main_issue="该看哪个科")
        self.assertTrue(r.ok, r.errors)
        self.assertEqual(r.blocked_by, "")

    def test_intake_returns_screen_result(self):
        r = validate_intake(medical_history=NORMAL_CASES[1])
        self.assertIsNotNone(r.screen)
        self.assertFalse(r.screen.blocked)


if __name__ == "__main__":
    unittest.main(verbosity=2)
