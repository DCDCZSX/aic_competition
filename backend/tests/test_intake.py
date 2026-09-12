"""录入校验（G2/G3）与就医目标的单元测试。

运行（在 CHAIS/backend 目录下）：
    python -m unittest tests.test_intake -v

验收对应《产品开发规格书-比赛版v-competition.md》：
- §4.4 G2：主诉与主要问题双空 → 阻断
- §4.4 G3：命中 PII → 阻断
- §1：就医目标只调策略，不决定分诊
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.intake import (  # noqa: E402
    ADVANCED_GOALS,
    ALL_GOAL_IDS,
    MAX_TEXT_LENGTH,
    PRIMARY_GOAL,
    goal_options,
    sanitized_payload,
    validate_intake,
)

VALID_TEXT = "反复发热3周，伴乏力、盗汗，血常规提示三系减少"


class TestG2(unittest.TestCase):
    def test_both_empty_is_blocked(self):
        r = validate_intake(medical_history="", main_issue="")
        self.assertFalse(r.ok)
        self.assertEqual(r.blocked_by, "G2")

    def test_only_history_is_ok(self):
        r = validate_intake(medical_history=VALID_TEXT, main_issue="")
        self.assertTrue(r.ok, r.errors)

    def test_only_issue_is_ok(self):
        r = validate_intake(medical_history="", main_issue="该看哪个科？")
        self.assertTrue(r.ok, r.errors)

    def test_whitespace_treated_as_empty(self):
        r = validate_intake(medical_history="   ", main_issue="\n\t ")
        self.assertFalse(r.ok)


class TestLengthAndRange(unittest.TestCase):
    def test_too_long_is_blocked(self):
        r = validate_intake(medical_history="病" * (MAX_TEXT_LENGTH + 1))
        self.assertFalse(r.ok)
        self.assertTrue(any("过长" in e for e in r.errors))

    def test_length_at_limit_ok(self):
        r = validate_intake(medical_history="病" * MAX_TEXT_LENGTH)
        self.assertTrue(r.ok, r.errors)

    def test_age_out_of_range(self):
        r = validate_intake(medical_history=VALID_TEXT, age=200)
        self.assertFalse(r.ok)

    def test_age_invalid_format(self):
        r = validate_intake(medical_history=VALID_TEXT, age="abc")
        self.assertFalse(r.ok)

    def test_age_ok(self):
        r = validate_intake(medical_history=VALID_TEXT, age=58)
        self.assertTrue(r.ok, r.errors)

    def test_unusual_sex_is_warning_not_error(self):
        r = validate_intake(medical_history=VALID_TEXT, sex="M")
        self.assertTrue(r.ok)
        self.assertTrue(any("性别" in w for w in r.warnings))


class TestG3(unittest.TestCase):
    def test_phone_blocks(self):
        r = validate_intake(medical_history=f"患者电话 13812345678，{VALID_TEXT}")
        self.assertFalse(r.ok)
        self.assertEqual(r.blocked_by, "G3")

    def test_id_card_blocks(self):
        r = validate_intake(medical_history=f"身份证 110101199003078515，{VALID_TEXT}")
        self.assertFalse(r.ok)
        self.assertEqual(r.blocked_by, "G3")

    def test_record_no_blocks_and_names_reason(self):
        r = validate_intake(medical_history=f"病历号 A012345，{VALID_TEXT}")
        self.assertFalse(r.ok)
        self.assertIn("G3", " ".join(r.errors))
        self.assertIn("病历号", " ".join(r.errors))

    def test_pii_in_main_issue_also_blocks(self):
        r = validate_intake(medical_history=VALID_TEXT, main_issue="我的电话13812345678")
        self.assertFalse(r.ok)

    def test_clean_text_passes(self):
        r = validate_intake(medical_history=VALID_TEXT, main_issue="该看哪个科")
        self.assertTrue(r.ok)
        self.assertIsNotNone(r.pii)


class TestGoals(unittest.TestCase):
    def test_primary_goal_is_plain_language(self):
        """主选项必须是患者看得懂的话，且不含"MDT/术前清单"这类术语。"""
        title = PRIMARY_GOAL["title"]
        for jargon in ("MDT", "多学科会诊", "诊断"):
            self.assertNotIn(jargon, title)
        self.assertIn("科", title)

    def test_advanced_goals_plain_language(self):
        for goal in ADVANCED_GOALS:
            for jargon in ("MDT", "多学科会诊"):
                self.assertNotIn(jargon, goal["title"])

    def test_no_goal_is_allowed_with_warning(self):
        r = validate_intake(medical_history=VALID_TEXT)
        self.assertTrue(r.ok, "未选目标不应阻断")
        self.assertTrue(any("未选择就医目标" in w for w in r.warnings))

    def test_invalid_goal_filtered_with_warning(self):
        r = validate_intake(medical_history=VALID_TEXT, goals=["teleport"])
        self.assertTrue(r.ok)
        self.assertEqual(r.goals, [])
        self.assertTrue(any("未识别的就医目标" in w for w in r.warnings))

    def test_goal_gives_behavior_hint(self):
        r = validate_intake(medical_history=VALID_TEXT, goals=["surgery_prep"])
        self.assertEqual(r.behavior_hint, "D")

    def test_unknown_department_goal_has_no_hint(self):
        """主选项不预设行为类型：由病例资料自动判定（不决定分诊）。"""
        r = validate_intake(medical_history=VALID_TEXT, goals=["unknown_department"])
        self.assertIsNone(r.behavior_hint)

    def test_goal_options_shape(self):
        opts = goal_options()
        self.assertTrue(opts["skippable"])
        self.assertEqual(opts["primary"]["id"], "unknown_department")
        self.assertEqual(len(opts["advanced"]), 3)
        self.assertEqual(ALL_GOAL_IDS[0], "unknown_department")


class TestSanitizedPayload(unittest.TestCase):
    def test_sanitized_payload_masks_pii(self):
        payload = sanitized_payload("患者电话 13812345678，发热3周")
        self.assertNotIn("13812345678", payload["medical_history"])
        self.assertIn("发热3周", payload["medical_history"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
