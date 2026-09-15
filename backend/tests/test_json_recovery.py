# -*- coding: utf-8 -*-
"""容错 JSON 解析（services/json_recovery.py）测试。

覆盖真实故障：deepseek-chat 输出被 max_tokens 截断 → `Expecting ',' delimiter`
→ 整个科室意见卡被丢弃。这里逐条验证三层处理，并断言**不编造内容**。
"""

import json
import unittest

from services import json_recovery as jr


VALID = {
    "focus": "全血细胞减少伴发热的血液系统病因排查",
    "facts": ["35岁女性，咳嗽2月余", "血小板 43×10^9/L"],
    "inferences": [
        {"text": "需排查噬血细胞综合征", "confidence": "medium",
         "evidence_refs": ["铁蛋白", "sCD25"]},
    ],
    "checks": [{"priority": "high", "content": "建议向医生确认是否需要骨髓穿刺"}],
    "information_gaps": ["铁蛋白具体数值"],
    "referral": None,
}
VALID_TEXT = json.dumps(VALID, ensure_ascii=False, indent=2)


class TestStrictPaths(unittest.TestCase):
    def test_plain_json(self):
        data, strategy = jr.loads_lenient(VALID_TEXT)
        self.assertEqual(strategy, jr.STRATEGY_STRICT)
        self.assertEqual(data["focus"], VALID["focus"])

    def test_fenced_with_prose(self):
        raw = "好的，以下是结果：\n```json\n" + VALID_TEXT + "\n```\n希望有帮助。"
        data, strategy = jr.loads_lenient(raw)
        self.assertEqual(strategy, jr.STRATEGY_STRICT)
        self.assertEqual(len(data["facts"]), 2)

    def test_first_object_wins_when_two_objects(self):
        raw = VALID_TEXT + "\n另外补充：" + json.dumps({"note": "第二段"}, ensure_ascii=False)
        data, _ = jr.loads_lenient(raw)
        self.assertIn("focus", data)
        self.assertNotIn("note", data)

    def test_braces_inside_string_do_not_confuse_scanner(self):
        raw = json.dumps({"focus": "剂量{不详}，需核对", "facts": ["a"]}, ensure_ascii=False)
        data, strategy = jr.loads_lenient(raw)
        self.assertEqual(strategy, jr.STRATEGY_STRICT)
        self.assertEqual(data["focus"], "剂量{不详}，需核对")

    def test_empty_and_garbage(self):
        self.assertEqual(jr.loads_lenient(""), (None, ""))
        self.assertEqual(jr.loads_lenient("   "), (None, ""))
        data, strategy = jr.loads_lenient("模型今天不想说话")
        self.assertIsNone(data)
        self.assertEqual(strategy, "")


class TestRepairs(unittest.TestCase):
    def test_fullwidth_separators(self):
        raw = '{"focus"："血细胞异常"， "facts"：["a"，"b"]}'
        data, strategy = jr.loads_lenient(raw)
        self.assertEqual(strategy, jr.STRATEGY_REPAIRED)
        self.assertEqual(data["facts"], ["a", "b"])

    def test_fullwidth_punctuation_inside_string_is_kept(self):
        """修复只能动结构，不能改内容：字符串里的全角逗号必须原样保留。"""
        raw = '{"focus"："血压高，血糖高"，"facts"：["咳嗽2月余，加重3天"]}'
        data, _ = jr.loads_lenient(raw)
        self.assertEqual(data["focus"], "血压高，血糖高")
        self.assertEqual(data["facts"], ["咳嗽2月余，加重3天"])

    def test_trailing_comma(self):
        raw = '{"focus": "x", "facts": ["a", "b",],}'
        data, _ = jr.loads_lenient(raw)
        self.assertEqual(data["facts"], ["a", "b"])

    def test_unescaped_inner_quotes(self):
        raw = '{"focus": "排查", "facts": ["患者称"血糖高"需复查", "血小板低"]}'
        data, strategy = jr.loads_lenient(raw)
        self.assertIn(strategy, (jr.STRATEGY_REPAIRED, jr.STRATEGY_TRUNCATED))
        self.assertEqual(data["facts"], ['患者称"血糖高"需复查', "血小板低"])

    def test_raw_newline_inside_string(self):
        raw = '{"focus": "第一行\n第二行", "facts": ["a"]}'
        data, _ = jr.loads_lenient(raw)
        self.assertEqual(data["focus"], "第一行\n第二行")

    def test_python_literals_and_single_quotes(self):
        raw = "{'focus': '排查', 'facts': ['a'], 'referral': None, 'flag': True}"
        data, strategy = jr.loads_lenient(raw)
        self.assertEqual(strategy, jr.STRATEGY_PY_LITERAL)
        self.assertEqual(data["focus"], "排查")
        self.assertIsNone(data["referral"])
        self.assertIs(data["flag"], True)

    def test_comments_removed(self):
        raw = '{\n  // 说明\n  "focus": "x", /* 注释 */ "facts": ["a"]\n}'
        data, strategy = jr.loads_lenient(raw)
        self.assertEqual(data["focus"], "x")
        self.assertIn(strategy, (jr.STRATEGY_REPAIRED, jr.STRATEGY_TRUNCATED))


class TestTruncation(unittest.TestCase):
    def test_every_cut_point_recovers_only_complete_values(self):
        """在任意位置截断，都要能恢复出**已经写完**的字段，且绝不出现半个值（血液科故障）。"""
        focus_close = VALID_TEXT.index('",', VALID_TEXT.index('"focus"')) + 1
        for cut in range(1, len(VALID_TEXT)):
            data, strategy = jr.loads_lenient(VALID_TEXT[:cut])
            if data is not None:
                self.assertTrue(data, f"截断到 {cut} 字符时恢复出空数据")
                if "focus" in data:
                    self.assertEqual(data["focus"], VALID["focus"],
                                     f"截断到 {cut} 字符时恢复出半个字符串")
            if cut >= focus_close:
                self.assertIsNotNone(data, f"截断到 {cut} 字符时连 focus 都没恢复出来")
                self.assertEqual(data["focus"], VALID["focus"])

    def test_truncated_mid_array_keeps_complete_items_only(self):
        raw = ('{\n  "focus": "血液系统病因排查",\n  "facts": [\n'
               '    "35岁女性，咳嗽2月余",\n    "血小板 43×10^9/L",\n'
               '    "铁蛋白升高、甘油三酯升高",\n    "NK细胞活性检测结果",\n'
               '    "EBV、CMV等病毒DNA及抗体')
        data, strategy = jr.loads_lenient(raw)
        self.assertIn(strategy, (jr.STRATEGY_TRUNCATED, jr.STRATEGY_SALVAGE))
        self.assertEqual(data["focus"], "血液系统病因排查")
        self.assertEqual(data["facts"][:3], ["35岁女性，咳嗽2月余", "血小板 43×10^9/L",
                                            "铁蛋白升高、甘油三酯升高"])
        self.assertNotIn("EBV、CMV等病毒DNA及抗体", data["facts"])   # 半个字符串不入库

    def test_truncated_keeps_complete_inference_objects(self):
        raw = ('{\n  "inferences": [\n'
               '    {"text": "需排查噬血细胞综合征", "confidence": "medium"},\n'
               '    {"text": "需评估淋巴瘤", "confidence": "low"\n')
        data, strategy = jr.loads_lenient(raw)
        self.assertIn(strategy, (jr.STRATEGY_TRUNCATED, jr.STRATEGY_SALVAGE))
        self.assertEqual([i["text"] for i in data["inferences"]],
                         ["需排查噬血细胞综合征", "需评估淋巴瘤"])

    def test_last_complete_field_survives_alone(self):
        data, strategy = jr.loads_lenient('{"focus": "只有一个字段"')
        self.assertEqual(strategy, jr.STRATEGY_TRUNCATED)
        self.assertEqual(set(data), {"focus"})

    def test_incomplete_object_item_is_dropped(self):
        """最后一条 `text` 字符串自身没写完 → 整条丢弃；键名不能当成条目。"""
        raw = '{"inferences": [{"text": "A", "confidence": "medium"}, {"text": "半句话'
        data, _ = jr.loads_lenient(raw)
        self.assertEqual([i["text"] for i in data["inferences"]], ["A"])

    def test_complete_value_in_unclosed_object_is_kept(self):
        """`{"text": "B"` 只是少了收尾括号，值本身写完了 → 保留（不算半个值）。"""
        raw = '{"inferences": [{"text": "A", "confidence": "medium"}, {"text": "B"'
        data, _ = jr.loads_lenient(raw)
        self.assertEqual([i["text"] for i in data["inferences"]], ["A", "B"])

    def test_repaired_flag(self):
        self.assertFalse(jr.result_is_repaired(jr.STRATEGY_STRICT))
        self.assertTrue(jr.result_is_repaired(jr.STRATEGY_TRUNCATED))
        self.assertTrue(jr.result_is_repaired(jr.STRATEGY_SALVAGE))


if __name__ == "__main__":
    unittest.main(verbosity=2)
