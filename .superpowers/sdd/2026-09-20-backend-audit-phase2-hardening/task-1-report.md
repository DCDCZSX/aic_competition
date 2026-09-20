# Task 1 Implementation Report — Redact model failure logs

## Scope

Implemented only the Phase 2 Task 1 logging hardening for `backend/services/ai_service.py`.

## Changes

- Replaced model-service `print(...)` calls with the module logger (`services.ai_service`).
- Changed malformed-response logging to emit response-shape metadata only: top-level keys/counts, choices count, and content length; it no longer serializes the response body.
- Changed request-failure logging to emit exception type, retry attempt, and an optional HTTP status; it does not log the exception message.
- Changed processing-failure logging to emit only the exception type; the existing caller-visible error return remains unchanged.
- Kept the existing missing-key error text, truncation detection/log metadata, retry/backoff behavior, and structured result contract unchanged.
- Added focused `assertLogs` tests for malformed payload and request-exception redaction.

## TDD evidence

The two new focused tests were run before the production change and failed as expected because the current implementation used `print(...)` rather than the module logger. The failure output also confirmed the old logs exposed the serialized invalid payload and the request exception secret.

After the implementation, both focused tests passed.

## Tests and outcomes

- `python -m unittest tests.test_ai_service.TestRawCall.test_invalid_payload_log_redacts_response_body tests.test_ai_service.TestRawCall.test_request_failure_log_redacts_exception_message` — PASS (2 tests).
- `python -m unittest tests.test_ai_service` — PASS (14 tests).
- `python -m unittest discover -s tests` — PASS (555 tests, 0 failures).
- `git diff --check` — PASS; only normal Windows line-ending conversion warnings were reported by Git.

## Files changed

- `backend/services/ai_service.py`
- `backend/tests/test_ai_service.py`
- `.superpowers/sdd/2026-09-20-backend-audit-phase2-hardening/task-1-report.md`

## Concerns

- The pre-existing caller-visible error contract still returns raw exception text in the result for request/processing failures. This was preserved as required; that text is no longer written to model-service logs.
- No other Phase 2 areas, preview-token behavior, auth behavior, response headers, documentation, or verification scripts were changed.
