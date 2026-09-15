# -*- coding: utf-8 -*-
"""模型调用层的单元测试（不发真实请求）。

重点覆盖真实故障的根因：
1. `max_tokens` 原先硬编码 2000，血液科意见卡需要 ~2335 token → 被截断成非法 JSON；
2. 原先不检查 `finish_reason`，截断只能表现成"解析失败"，看不出真因；
3. 原先对**所有**输出做 Markdown 清理，而清理会把 ```json 包裹的整段内容删掉、
   也会改动字符串里的内容 —— 结构化输出必须拿到原文。
"""

import io
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.ai_service import (  # noqa: E402
    DEFAULT_MAX_TOKENS,
    AIService,
    call_structured,
)


def _response(content, finish_reason="stop"):
    return {"choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
            "usage": {"completion_tokens": 10}}


class _FakeHTTP:
    def __init__(self, payload):
        self.payload = payload
        self.bodies = []

    def post(self, url, headers=None, json=None, timeout=None):  # noqa: A002
        self.bodies.append(json or {})
        return mock.Mock(raise_for_status=lambda: None,
                         json=lambda: self.payload)


class TestRawCall(unittest.TestCase):
    def setUp(self):
        self.service = AIService()
        self.service.api_key = "test-key"

    def _call(self, payload, **kwargs):
        fake = _FakeHTTP(payload)
        with mock.patch("services.ai_service.requests.post", fake.post):
            result = self.service.call_ai_api_raw("提示词", role_description="系统", **kwargs)
        return result, fake

    def test_default_budget_unchanged(self):
        _, fake = self._call(_response("ok"))
        self.assertEqual(fake.bodies[0]["max_tokens"], DEFAULT_MAX_TOKENS)

    def test_budget_can_be_raised(self):
        _, fake = self._call(_response("ok"), max_tokens=4000)
        self.assertEqual(fake.bodies[0]["max_tokens"], 4000)

    def test_truncation_is_detected(self):
        result, _ = self._call(_response('{"facts": ["a"', finish_reason="length"))
        self.assertTrue(result["ok"])
        self.assertTrue(result["truncated"])
        self.assertEqual(result["finish_reason"], "length")

    def test_normal_finish_is_not_truncated(self):
        result, _ = self._call(_response('{"facts": ["a"]}'))
        self.assertFalse(result["truncated"])

    def test_raw_call_keeps_code_fence(self):
        """结构化调用**不做** Markdown 清理：```json 包裹的内容必须原样保留。"""
        fenced = '```json\n{"facts": ["a"]}\n```'
        result, _ = self._call(_response(fenced))
        self.assertIn('{"facts": ["a"]}', result["content"])
        self.assertIn("```", result["content"])

    def test_legacy_call_still_strips_markdown(self):
        """纯文本用途沿用旧行为（清理 Markdown），避免影响既有调用方。"""
        fake = _FakeHTTP(_response("# 标题\n**加粗**内容"))
        with mock.patch("services.ai_service.requests.post", fake.post):
            text = self.service.call_ai_api("提示词")
        self.assertNotIn("**", text)
        self.assertNotIn("#", text)

    def test_missing_key_is_reported(self):
        self.service.api_key = ""
        result = self.service.call_ai_api_raw("提示词")
        self.assertFalse(result["ok"])
        self.assertIn("AI_API_KEY", result["error"])

    def test_gateway_error_is_reported_not_raised(self):
        def boom(*args, **kwargs):
            raise RuntimeError("网关炸了")

        with mock.patch("services.ai_service.requests.post", boom):
            result = self.service.call_ai_api_raw("提示词")
        self.assertFalse(result["ok"])
        self.assertEqual(result["content"], "")


class TestCallStructured(unittest.TestCase):
    def test_prefers_raw_interface(self):
        class Raw:
            api_key = "k"

            def __init__(self):
                self.budgets = []

            def call_ai_api_raw(self, prompt, role_description=None, max_tokens=None):
                self.budgets.append(max_tokens)
                return {"ok": True, "content": "{}", "truncated": False, "error": ""}

            def call_ai_api(self, prompt, role_description=None):   # pragma: no cover
                raise AssertionError("有 raw 接口时不应走 Markdown 清理那条路")

        ai = Raw()
        content, truncated, error = call_structured(ai, "p", max_tokens=4000)
        self.assertEqual((content, truncated, error), ("{}", False, ""))
        self.assertEqual(ai.budgets, [4000])

    def test_falls_back_to_legacy_interface(self):
        class Legacy:
            api_key = "k"

            def call_ai_api(self, prompt, role_description=None):
                return '{"facts": ["a"]}'

        content, truncated, error = call_structured(Legacy(), "p")
        self.assertEqual(json.loads(content), {"facts": ["a"]})
        self.assertFalse(truncated)
        self.assertEqual(error, "")

    def test_exception_becomes_error(self):
        class Broken:
            api_key = "k"

            def call_ai_api(self, prompt, role_description=None):
                raise RuntimeError("网关不可用")

        content, truncated, error = call_structured(Broken(), "p")
        self.assertEqual(content, "")
        self.assertIn("网关不可用", error)


if __name__ == "__main__":
    unittest.main(verbosity=2)
