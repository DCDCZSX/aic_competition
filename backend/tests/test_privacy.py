"""隐私脱敏与 PII 拦截的单元测试。

运行（在 CHAIS/backend 目录下）：
    python -m unittest tests.test_privacy -v

验收对应《产品开发规格书-比赛版v-competition.md》§10：
「PII 拦截率：构造集 100%」。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.privacy import (  # noqa: E402
    BLOCKING_KINDS,
    desensitize,
    detect,
    is_blocked,
    mask_for_display,
)

# 构造 PII 集（验收：100% 拦截）
ID_CARD = "110101199003078515"
PHONE = "13812345678"
BANK = "6222021234567890123"
EMAIL = "zhangsan@example.com"


class TestDetection(unittest.TestCase):
    def test_id_card(self):
        hits = detect(f"身份证号 {ID_CARD}")
        self.assertTrue(any(h.kind == "id_card" for h in hits))

    def test_phone(self):
        hits = detect(f"联系电话 {PHONE}")
        self.assertTrue(any(h.kind == "phone" for h in hits))

    def test_name_with_prefix(self):
        hits = detect("患者：张伟，男，58岁")
        names = [h for h in hits if h.kind == "name"]
        self.assertTrue(names)
        self.assertEqual(names[0].masked, "张*")

    def test_record_number(self):
        hits = detect("病历号 A012345")
        self.assertTrue(any(h.kind == "record_no" for h in hits))

    def test_insurance_number(self):
        hits = detect("医保号 3101122334455")
        self.assertTrue(any(h.kind == "insurance" for h in hits))

    def test_email(self):
        hits = detect(f"邮箱 {EMAIL}")
        self.assertTrue(any(h.kind == "email" for h in hits))

    def test_bank_card(self):
        hits = detect(f"银行卡 {BANK}")
        self.assertTrue(any(h.kind == "bank_card" for h in hits))

    def test_address(self):
        hits = detect("住址：上海市浦东新区张江路123号")
        self.assertTrue(any(h.kind == "address" for h in hits))

    def test_clean_text_has_no_hits(self):
        text = "视物模糊3月，下肢浮肿1周，既往2型糖尿病5年，口服二甲双胍0.5g bid"
        self.assertEqual(detect(text), [])


class TestNameFalsePositives(unittest.TestCase):
    """姓名的误报回归（真实缺陷）。

    旧规则是 `(?:患者|病人|姓名|患者姓名)\\s*[:：]?\\s*([一-龥]{2,4})`——**冒号可选**，
    于是正文里的「患者转运」「患者活动后」「患者出院」「患者反应」都被当成姓名打码：
    既在脱敏预览里显示成莫名其妙的"姓名 → 转***"，又把送进分析链路的文本改坏了。
    """

    # 这些文本里没有任何人名，一个都不该打码
    CLEAN_TEXTS = (
        "患者转运至上级医院。",
        "患者活动后气促，患者出院后反应良好，患者出现胸痛。",
        "患者神志清楚，精神可，发育正常，营养中等。",
        "病人既往体健，否认家族遗传病史。",
        "患者一般情况可，查体合作。",
        "病人入院后给予对症处理，症状好转。",
        "患者：男，58岁。",                      # 标签后直接是性别，不是姓名
        "家属：无陪同人员。",
        "联系人：不详。",
        "患者主诉：视物模糊3月，双下肢浮肿1周。",
    )

    def test_no_false_positive_names(self):
        for text in self.CLEAN_TEXTS:
            with self.subTest(text=text):
                hits = [h for h in detect(text) if h.kind == "name"]
                self.assertEqual(hits, [], f"误判姓名：{[h.original for h in hits]}")

    def test_clean_text_kept_intact(self):
        """误报会改坏送进分析的文本，这里断言原文一字不动。"""
        for text in self.CLEAN_TEXTS:
            with self.subTest(text=text):
                self.assertEqual(mask_for_display(text), text)


class TestNameTruePositives(unittest.TestCase):
    """真的人名必须仍然识别（且给出命中规则，便于解释"为什么打码"）。"""

    CASES = (
        ("患者：李某某，男，58岁。", "李某某", {"name_labelled"}),
        ("姓名：张三", "张三", {"name_labelled"}),
        ("病人姓名：王五，女，30岁。", "王五", {"name_labelled"}),
        ("联系人：赵六，电话 13812345678", "赵六", {"name_labelled"}),
        ("我叫王小明，今年 58 岁。", "王小明", {"name_self"}),
        # "张某某，男，58岁" 同时符合「姓氏+某」与「姓氏+性别年龄上下文」，两条规则都算对
        ("张某某，男，58岁，因视物模糊就诊。", "张某某", {"name_mou", "name_age_context"}),
        ("李某某，女，30岁。", "李某某", {"name_mou", "name_age_context"}),
        ("患者：欧阳某某，女，30岁。", "欧阳某某", {"name_labelled"}),
    )

    def test_true_positive_names(self):
        for text, expected, rules in self.CASES:
            with self.subTest(text=text):
                hits = [h for h in detect(text) if h.kind == "name"]
                self.assertTrue(hits, f"漏掉了人名：{expected}")
                hit = hits[0]
                self.assertEqual(hit.original, expected)
                self.assertIn(hit.rule, rules)
                self.assertTrue(hit.masked.startswith(expected[0]))
                self.assertNotIn(expected, mask_for_display(text), "人名必须被打码")

    def test_surname_gate_blocks_non_surnames(self):
        """姓氏白名单是最后一道闸：非姓氏开头的一律不当作姓名。"""
        from services.privacy import _looks_like_name  # noqa: PLC0415
        for word in ("视物", "转运", "活动", "出院", "反应", "神志", "精神", "既往", "家族"):
            with self.subTest(word=word):
                self.assertFalse(_looks_like_name(word), f"{word} 不应被判为姓名")
        for name in ("张伟", "李某某", "欧阳某某", "陈明", "刘洋"):
            with self.subTest(name=name):
                self.assertTrue(_looks_like_name(name))

    def test_latin_name_after_label(self):
        hits = [h for h in detect("患者：John Smith，男，40岁") if h.kind == "name"]
        self.assertTrue(hits, "外文姓名也应打码")
        self.assertNotIn("John Smith", mask_for_display("患者：John Smith，男，40岁"))


class TestMasking(unittest.TestCase):
    def test_id_card_masked_keeps_edges(self):
        masked = mask_for_display(f"身份证 {ID_CARD}")
        self.assertIn(ID_CARD[:6], masked)
        self.assertIn(ID_CARD[-4:], masked)
        self.assertNotIn(ID_CARD, masked, "完整身份证号必须被抹掉")

    def test_phone_masked(self):
        masked = mask_for_display(f"电话 {PHONE}")
        self.assertIn("138****5678", masked)

    def test_bank_card_masked(self):
        masked = mask_for_display(f"卡号 {BANK}")
        self.assertNotIn(BANK, masked)

    def test_email_masked(self):
        masked = mask_for_display(f"联系 {EMAIL}")
        self.assertNotIn(EMAIL, masked)

    def test_multiple_hits_all_masked(self):
        text = f"患者：李四，身份证 {ID_CARD}，电话 {PHONE}"
        result = desensitize(text)
        self.assertGreaterEqual(len(result.hits), 3)
        for raw in ("李四", ID_CARD, PHONE):
            self.assertNotIn(raw, result.masked_text, f"{raw} 未被打码")

    def test_mask_does_not_break_offsets(self):
        """多处命中时，替换不应错位。"""
        text = f"电话 {PHONE}，身份证 {ID_CARD}，电话 {PHONE}"
        result = desensitize(text)
        self.assertNotIn(PHONE, result.masked_text)
        self.assertNotIn(ID_CARD, result.masked_text)
        self.assertEqual(len(result.hits), 3)


class TestBlocking(unittest.TestCase):
    def test_blocking_kinds_all_detected(self):
        """构造集：每种需阻断的类型都必须命中（G3 100%）。"""
        samples = {
            "id_card": ID_CARD,
            "phone": PHONE,
            "record_no": "病历号 A012345",
            "insurance": "医保号 3101122334455",
            "email": EMAIL,
            "bank_card": BANK,
        }
        for kind in BLOCKING_KINDS:
            with self.subTest(kind=kind):
                text = samples[kind]
                self.assertTrue(is_blocked(text), f"{kind} 应触发 G3 阻断")

    def test_clean_text_not_blocked(self):
        self.assertFalse(is_blocked("主诉：反复发热3周，伴乏力"))

    def test_name_alone_does_not_block(self):
        """姓名只提示打码，不阻断（避免病历中常见称呼导致无法使用）。"""
        self.assertFalse(is_blocked("患者：张伟，主诉发热3天"))

    def test_summary_hides_original(self):
        """给前端的明细里不得包含原文。"""
        result = desensitize(f"电话 {PHONE}")
        for item in result.summary():
            self.assertNotIn(PHONE, str(item))


if __name__ == "__main__":
    unittest.main(verbosity=2)
