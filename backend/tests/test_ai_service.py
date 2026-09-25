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
import threading
import time
import unittest
from unittest import mock

import requests

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
        self.timeouts = []

    def post(self, url, headers=None, json=None, timeout=None):  # noqa: A002
        self.bodies.append(json or {})
        self.timeouts.append(timeout)
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

    def test_connect_and_read_timeouts_are_bounded(self):
        with mock.patch.dict(os.environ, {"AI_CONNECT_TIMEOUT": "10", "AI_READ_TIMEOUT": "60"}):
            service = AIService()
            service.api_key = "test-key"
            fake = _FakeHTTP(_response("ok"))
            with mock.patch("services.ai_service.requests.post", fake.post):
                service.call_ai_api_raw("提示词")
        self.assertEqual(fake.timeouts, [(10.0, 60.0)])

    def test_retry_and_concurrency_limits_are_configured(self):
        with mock.patch.dict(os.environ, {
            "AI_MAX_RETRIES": "5",
            "AI_MAX_CONCURRENT_REQUESTS": "1",
        }):
            service = AIService()
        self.assertEqual(service.max_retries, 5)
        self.assertEqual(service.max_concurrent_requests, 1)

    def test_outbound_requests_obey_process_local_concurrency_limit(self):
        with mock.patch.dict(os.environ, {
            "AI_MAX_RETRIES": "1",
            "AI_MAX_CONCURRENT_REQUESTS": "1",
        }):
            service = AIService()
        service.api_key = "test-key"
        active = 0
        peak = 0
        lock = threading.Lock()

        def slow_post(*args, **kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.05)
            with lock:
                active -= 1
            return mock.Mock(raise_for_status=lambda: None,
                             json=lambda: _response("ok"))

        with mock.patch("services.ai_service.requests.post", side_effect=slow_post):
            threads = [threading.Thread(target=service.call_ai_api_raw, args=("提示词",))
                       for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=2)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(peak, 1)

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
        self.assertEqual(result["error_code"], "ai_missing_api_key")
        self.assertNotIn("AI_API_KEY", result["error"])

    def test_gateway_error_is_reported_not_raised(self):
        secret = "gateway-secret-detail"

        def boom(*args, **kwargs):
            raise RuntimeError(secret)

        with mock.patch("services.ai_service.requests.post", boom):
            result = self.service.call_ai_api_raw("提示词")
        self.assertFalse(result["ok"])
        self.assertEqual(result["content"], "")
        self.assertEqual(result["error_code"], "ai_request_failed")
        self.assertNotIn(secret, result["error"])

    def test_invalid_payload_log_redacts_response_body(self):
        secret = "invalid-payload-secret-7f3c"
        payload = {"choices": [], "error": secret}

        with self.assertLogs("services.ai_service", level="WARNING") as captured:
            self._call(payload)

        logs = "\n".join(captured.output)
        self.assertNotIn(secret, logs)
        self.assertNotIn(json.dumps(payload), logs)
        self.assertIn("top_level_keys", logs)

    def test_request_failure_log_redacts_exception_message(self):
        secret = "request-failure-secret-9a2b"

        def boom(*args, **kwargs):
            raise requests.RequestException(secret)

        with mock.patch("services.ai_service.requests.post", boom), \
                mock.patch("services.ai_service.time.sleep"):
            with self.assertLogs("services.ai_service", level="WARNING") as captured:
                self.service.call_ai_api_raw("提示词")

        logs = "\n".join(captured.output)
        self.assertNotIn(secret, logs)
        self.assertIn("RequestException", logs)
        self.assertIn("attempt=1/3", logs)


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
        secret = "gateway-secret-detail"

        class Broken:
            api_key = "k"

            def call_ai_api(self, prompt, role_description=None):
                raise RuntimeError(secret)

        content, truncated, error = call_structured(Broken(), "p")
        self.assertEqual(content, "")
        self.assertEqual(error, "模型服务调用失败")
        self.assertNotIn(secret, error)


if __name__ == "__main__":
    unittest.main(verbosity=2)
