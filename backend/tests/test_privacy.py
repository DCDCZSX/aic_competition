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
