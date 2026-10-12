"""科室域的单元测试。

运行（在 CHAIS/backend 目录下）：
    python -m unittest tests.test_department_domain -v

验收对应《产品开发规格书-比赛版v-competition.md》：
- §4.2 科室域 v1：37 门类、别名表、OOV 三序
- §10 质量指标表：「单测：别名映射全通过；域外走 OOV 记录」
"""

import os
import sys
import unittest

# 让测试可以直接 `python -m unittest` 运行（把 backend/ 加入 sys.path）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.department_domain import (  # noqa: E402
    ALIAS_MAP,
    ALL_NAMES,
    DEPARTMENTS,
    GROUP_AUXILIARY,
    GROUP_INTERNAL,
    GROUP_SPECIALTY,
    GROUP_SURGERY,
    NEAREST_MAP,
    OOVLog,
    PRIMARY_ELIGIBLE_NAMES,
    SPECIALISTS,
    STATUS_ALIAS,
    STATUS_EXACT,
    STATUS_NEAREST,
    STATUS_OOV,
    can_be_primary,
    dedupe_names,
    group_counts,
    is_auxiliary,
    is_department,
    resolve,
    resolve_many,
    to_code,
    to_name,
)


class TestDepartmentSet(unittest.TestCase):
    """字典本身的结构约束（规格书 §4.2）。"""

    def test_total_is_37(self):
        self.assertEqual(len(DEPARTMENTS), 37)
        self.assertEqual(len(ALL_NAMES), 37)

    def test_group_distribution(self):
        counts = group_counts()
        self.assertEqual(counts[GROUP_INTERNAL], 13, "临床内科应为 13")
        self.assertEqual(counts[GROUP_SURGERY], 8, "外科系应为 8")
        self.assertEqual(counts[GROUP_SPECIALTY], 10, "临床专科应为 10")
        self.assertEqual(counts[GROUP_AUXILIARY], 6, "辅助科应为 6")

    def test_names_and_codes_unique(self):
        names = [d.name for d in DEPARTMENTS]
        codes = [d.code for d in DEPARTMENTS]
        self.assertEqual(len(names), len(set(names)), "科室名存在重复")
        self.assertEqual(len(codes), len(set(codes)), "科室 code 存在重复")

    def test_legacy_dict_compatible(self):
        """兼容旧代码：SPECIALISTS 为 {code: 中文名}，且包含既有 code。"""
        self.assertEqual(SPECIALISTS["cardiology"], "心血管内科")
        self.assertEqual(SPECIALISTS["nephrology"], "肾内科")
        self.assertEqual(SPECIALISTS["ophthalmology"], "眼科")
        self.assertEqual(to_code("眼科"), "ophthalmology")
        self.assertEqual(to_name("ophthalmology"), "眼科")

    def test_primary_eligible_excludes_auxiliary(self):
        """牵头位仅限临床科：辅助科 6 个必须全部被排除。"""
        self.assertEqual(len(PRIMARY_ELIGIBLE_NAMES), 31)
        for name in ("影像科", "超声科", "病理科", "检验科", "介入科", "麻醉科"):
            self.assertNotIn(name, PRIMARY_ELIGIBLE_NAMES, f"{name} 不应可牵头")
            self.assertTrue(is_auxiliary(name))
            self.assertFalse(can_be_primary(name), f"{name} 不应可牵头")

    def test_clinical_departments_can_be_primary(self):
        for name in ("内分泌科", "肾内科", "眼科", "心胸外科", "急诊科"):
            self.assertTrue(can_be_primary(name), f"{name} 应可牵头")


class TestAliasMap(unittest.TestCase):
    """别名映射：规格书点名的那批必须全部命中（§4.2）。"""

    REQUIRED_ALIASES = {
        "心内科": "心血管内科",
        "呼吸科": "呼吸内科",
        "肾内": "肾内科",
        "血液内科": "血液科",          # 规格书示例："血液科/血液内科→血液科"
        "感染性疾病科": "感染科",
        "风湿科": "风湿免疫科",
        "免疫科": "风湿免疫科",
        "康复科": "康复医学科",
        "内分泌二区": "内分泌科",
        "耳鼻咽喉科": "耳鼻喉科",
        "神外": "神经外科",
        "ICU": "重症医学科",
        "普外": "普通外科",
        "胸外科": "心胸外科",
        "彩超": "超声科",
        "放射科": "影像科",
        "介入治疗科": "介入科",
    }

    def test_required_aliases_resolve(self):
        for alias, expected in self.REQUIRED_ALIASES.items():
            with self.subTest(alias=alias):
                r = resolve(alias)
                self.assertEqual(r.status, STATUS_ALIAS)
                self.assertEqual(r.name, expected)

    def test_alias_targets_are_valid_departments(self):
        for alias, target in ALIAS_MAP.items():
            with self.subTest(alias=alias):
                self.assertTrue(is_department(target), f"别名 {alias} 指向了域外科室 {target}")

    def test_alias_resolution_cleans_suffix_noise(self):
        """"心内科门诊" 这类带后缀的写法也要能归一。"""
        self.assertEqual(resolve("心内科门诊").name, "心血管内科")
        self.assertEqual(resolve("呼吸科专家门诊").name, "呼吸内科")

    def test_exact_name_wins(self):
        r = resolve("心血管内科")
        self.assertEqual(r.status, STATUS_EXACT)
        self.assertEqual(r.name, "心血管内科")


class TestOOVStrategy(unittest.TestCase):
    """OOV 三序：① 别名 → ② 最近似科（附提示） → ③ 记日志并原样呈现。"""

    def test_nearest_mapping(self):
        r = resolve("口腔颌面外科")
        self.assertEqual(r.status, STATUS_NEAREST)
        self.assertEqual(r.name, "普通外科")
        self.assertIn("已按近似科 普通外科 处理", r.note)
        self.assertTrue(r.needs_review)

    def test_nearest_targets_are_valid(self):
        for raw, target in NEAREST_MAP.items():
            with self.subTest(raw=raw):
                self.assertTrue(is_department(target), f"{raw} 的近似科 {target} 不在域内")

    def test_unknown_department_is_oov_not_dropped(self):
        r = resolve("营养科")
        self.assertEqual(r.status, STATUS_OOV)
        self.assertIsNone(r.name)
        self.assertIn("营养科", r.note, "OOV 科室名必须原样保留，不能静默丢弃")
        self.assertFalse(r.ok)
        self.assertTrue(r.needs_review)

    def test_empty_input(self):
        r = resolve("   ")
        self.assertEqual(r.status, STATUS_OOV)
        self.assertIsNone(r.name)

    def test_oov_log_records_nearest_and_oov(self):
        log = OOVLog()
        results = resolve_many(["内分泌科", "心内科", "口腔颌面外科", "营养科"], log=log)

        self.assertEqual([r.status for r in results],
                         [STATUS_EXACT, STATUS_ALIAS, STATUS_NEAREST, STATUS_OOV])
        # 域内（exact/alias）不入日志，仅 nearest/oov 入日志
        self.assertEqual(len(log), 2)
        summary = log.summary()
        self.assertEqual(summary[0]["raw"], "口腔颌面外科")
        self.assertEqual(summary[1]["raw"], "营养科")


class TestDedupe(unittest.TestCase):
    """别名归一后应合并重复科室（分诊候选去重用）。"""

    def test_alias_merges_duplicates(self):
        got = dedupe_names(["心内科", "心血管内科", "心脏内科", "肾内", "肾内科"])
        self.assertEqual(got, ["心血管内科", "肾内科"])

    def test_oov_dropped_from_dedupe(self):
        got = dedupe_names(["眼科", "营养科"])
        self.assertEqual(got, ["眼科"], "未建模科室不进标准候选集（但报告需原样呈现）")


class TestEvalV0Regression(unittest.TestCase):
    """回归：评测 v0 指出的"系统未建模科室"必须已补齐。

    见 CHAIS/评测v0结果与改进方向.md 缺陷 5（超声科、肝胆外科缺失）。
    """

    def test_previously_missing_departments_exist(self):
        for name in ("超声科", "肝胆外科", "介入科"):
            with self.subTest(name=name):
                self.assertTrue(is_department(name), f"{name} 应已纳入科室域")
                self.assertIn(name, ALL_NAMES)


if __name__ == "__main__":
    unittest.main(verbosity=2)
