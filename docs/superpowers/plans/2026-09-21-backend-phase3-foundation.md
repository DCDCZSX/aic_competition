# Backend Phase 3 Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the first three backend hardening gaps identified after Phase 2: model-facing error leakage, unbounded semantic input/model work, and fragile integer environment configuration.

**Architecture:** Keep the current Flask/SQLite boundaries intact. Add one small model-error policy module and one integer configuration parser; enforce business-input limits at both HTTP and service boundaries, while preserving existing public response shapes except for stable optional error codes. Do not introduce Redis, PostgreSQL, a task queue, or a broad route refactor in this phase.

**Tech Stack:** Python 3, Flask, unittest, requests, SQLite; no new runtime dependency.

**Spec:** `docs/开发进度与交接.md` current snapshot and the backend audit findings recorded in the current task conversation.

## Global Constraints

- Backend unit tests must remain green: `python -m unittest discover -s tests -t .` from `backend/`.
- Existing auth, PII blocking, preview-token, and fallback behavior must remain compatible unless the new limit is the explicitly tested behavior.
- Raw upstream exception messages and model response bodies must never be returned from model-facing APIs or persisted in analysis warnings/phase notes.
- Every production-code change must have a test that failed before the implementation.
- Do not modify the second-phase worktree or create a commit/PR during this execution.

---

### Task 1: Define a safe model-error contract

**Files:**
- Create: `backend/services/model_errors.py`
- Create: `backend/tests/test_model_errors.py`
- Modify: `backend/services/ai_service.py`
- Test: `backend/tests/test_ai_service.py`

**Interfaces:**
- `ModelError(code: str, message: str)` is the internal value used by model calls.
- `classify_model_exception(exc, status_code=None) -> ModelError` maps timeout, 401/403, 429, 5xx, and unknown failures to stable codes/messages.
- `invalid_model_response() -> ModelError` returns `ai_invalid_response` without response-body details.
- `AIService.call_ai_api_raw()` keeps `error` as a safe human message and adds `error_code`; it never includes `str(exc)`.
- `call_structured()` remains a backward-compatible three-value return and exposes only the safe human message.

- [x] **Step 1: Write failing model-error tests**

  Add tests for timeout, 401, 429, 503, unknown exception, invalid response, and a secret string that must not appear in the returned message.

- [x] **Step 2: Run the focused tests and verify RED**

  Run from `backend/`:

  ```powershell
  python -m unittest tests.test_model_errors -v
  ```

  Expected: the new module/import or expected safe contract is absent and the tests fail for that reason.

- [x] **Step 3: Implement the minimal error policy and raw-call integration**

  Keep the existing retry loop and logging metadata. Replace raw error strings with stable messages and return `error_code` on every raw-call failure path.

- [x] **Step 4: Update focused AI tests**

  Change the exception test to assert the stable message/code and assert the secret is absent. Preserve tests for missing key, truncation, and redacted logs.

- [x] **Step 5: Run the focused tests and verify GREEN**

  ```powershell
  python -m unittest tests.test_model_errors tests.test_ai_service -v
  ```

- [x] **Step 6: Create a review checkpoint**

  Inspect the focused diff and leave it uncommitted. Commit and PR integration remain deferred until the user explicitly asks for them.

### Task 2: Remove raw model/parser details from persisted analysis output

**Files:**
- Modify: `backend/services/mdt_analyst.py`
- Modify: `backend/services/s2_analyst.py`
- Modify: `backend/services/portrait_extractor.py`
- Modify: `backend/services/challenge_service.py`
- Modify: `backend/services/case_flow.py`
- Modify: `backend/tests/test_mdt_analyst.py`
- Modify: `backend/tests/test_create_progress.py`
- Modify: `backend/tests/test_backend_hardening.py`

**Interfaces:**
- Existing result dataclasses keep their fields and Chinese user-facing categories.
- Parser/validation failures return bounded category messages such as `意见卡解析失败` or `模型输出格式无法解析`, without exception text.
- Failed analysis phase notes use a stable category and phase name, not `type(exc).__name__` plus `str(exc)`.

- [x] **Step 1: Add failing leak-regression tests**

  Inject unique secret text through a fake model exception and parser exception, run the relevant analysis path, and assert the secret is absent from the result, stored phase status, and `/api/v2/case/<id>/analysis` response.

- [x] **Step 2: Run the regression tests and verify RED**

  ```powershell
  python -m unittest tests.test_mdt_analyst tests.test_create_progress tests.test_backend_hardening -v
  ```

  Expected: at least one assertion sees the injected secret in the old output.

- [x] **Step 3: Replace raw detail with bounded categories**

  Preserve actionable distinctions such as missing model, truncated output, parse failure, and validation failure; remove exception interpolation from persisted or serialized values.

- [x] **Step 4: Run the focused suite and verify GREEN**

  ```powershell
  python -m unittest tests.test_mdt_analyst tests.test_s2_analyst tests.test_portrait_extractor tests.test_create_progress tests.test_backend_hardening -v
  ```

### Task 3: Bound semantic input and model work

**Files:**
- Modify: `backend/services/intake.py`
- Modify: `backend/services/request_validation.py`
- Modify: `backend/app.py`
- Modify: `backend/services/case_flow.py`
- Modify: `backend/services/ai_service.py`
- Modify: `backend/tests/test_intake.py`
- Modify: `backend/tests/test_backend_hardening.py`
- Modify: `backend/tests/test_case_flow.py`
- Modify: `backend/tests/test_ai_service.py`

**Interfaces:**
- Add `MAX_MAIN_ISSUE_LENGTH = 2000` and enforce it in intake validation and authenticated preview.
- Extend `object_field()` with optional `max_items` and `max_key_length`, preserving its current defaults.
- Limit answers to the current three-question contract before PII/model processing; retain the existing per-value and merged-text limits.
- Add `AI_MAX_RETRIES` upper/lower bounds and `AI_MAX_CONCURRENT_REQUESTS` as a process-local semaphore limit; default concurrency is 3.

- [x] **Step 1: Write failing input-budget tests**

  Cover an oversized `main_issue`, more than three answer items, an oversized answer key, and a retry/concurrency configuration outside the allowed range.

- [x] **Step 2: Run focused tests and verify RED**

  ```powershell
  python -m unittest tests.test_intake tests.test_case_flow tests.test_backend_hardening tests.test_ai_service -v
  ```

- [x] **Step 3: Implement the smallest boundary checks**

  Reject invalid input before desensitization/model work, and use a semaphore around the outbound request without changing the existing retry/backoff semantics.

- [x] **Step 4: Run focused tests and verify GREEN**

  Re-run the command from Step 2 and confirm the new limits are enforced without changing valid existing flows.

### Task 4: Centralize integer environment parsing

**Files:**
- Create: `backend/services/config.py`
- Create: `backend/tests/test_config.py`
- Modify: `backend/app.py`
- Modify: `backend/services/auth.py`
- Modify: `backend/services/ai_service.py`
- Modify: `.env.example`
- Modify: `README.md`

**Interfaces:**
- `read_int_env(name, default, *, minimum=None, maximum=None) -> int` parses whitespace, rejects non-integers with `ConfigurationError`, and enforces declared bounds.
- Existing callers use explicit bounds for content size, timeouts, retries, guest quotas, login limits, preview TTL, and port.
- `APP_AUTH_REQUIRED` is removed from the active configuration examples because full APIs always require authentication.

- [x] **Step 1: Write failing parser and integration tests**

  Assert valid values parse, invalid text raises `ConfigurationError` naming the variable, and out-of-range retry/timeout values are rejected before requests start.

- [x] **Step 2: Run focused config tests and verify RED**

  ```powershell
  python -m unittest tests.test_config -v
  ```

- [x] **Step 3: Implement the parser and migrate raw integer reads**

  Keep preview-token legacy fallback behavior where its existing tests require it; migrate application/auth/model settings to the shared parser.

- [x] **Step 4: Run focused and full backend tests**

  ```powershell
  python -m unittest tests.test_config tests.test_auth tests.test_ai_service tests.test_backend_hardening -v
  python -m unittest discover -s tests -t .
  ```

### Task 5: Update the current-progress document

**Files:**
- Modify: `docs/开发进度与交接.md`

- [x] **Step 1: Record only verified changes**

  Add the Phase 3 foundation status, new limits/error contract, test count, and any remaining acceptance blockers. Do not mark production architecture or live acceptance complete.

- [x] **Step 2: Run final verification before claiming completion**

  Check `git diff --check`, the full backend suite, and the tracked diff. Report exact counts and leave commit/PR integration for the user’s next instruction.
