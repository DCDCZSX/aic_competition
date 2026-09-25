# Task 3 — Login throttling HTTP semantics report

## Result

Implemented the minimal login cooldown distinction required by Task 3.

During cooldown, the auth service now raises `AuthRateLimitError` with a positive integer `retry_after`. `/api/login` maps that error to JSON HTTP 429 with `code=rate_limited` and a numeric `Retry-After` header. Other `AuthError` cases remain JSON HTTP 401 with `code=invalid_credentials` and the existing generic credential message.

Existing failure threshold, failure window, cooldown duration, account/IP keying, and in-process storage were preserved. No preview-token, model-logging, unrelated-header, documentation, proxy, or database behavior was changed.

## Files changed

- `backend/services/auth.py`
  - Added `AuthRateLimitError(retry_after)`.
  - Changed the cooldown check to return the ceiling of the remaining cooldown seconds while preserving the existing thresholds, windows, cooldown, and keys.
- `backend/app.py`
  - Added the specific 429 route branch before the generic `AuthError` 401 branch.
- `backend/tests/test_backend_hardening.py`
  - Added an HTTP-level regression test covering two generic 401 failures, the third cooldown attempt as 429, `rate_limited`, generic wording, positive numeric `Retry-After`, and a fresh-service ordinary bad login as 401.

## TDD evidence

1. Added the HTTP test before production changes.
2. Ran the focused test against the original all-401 route:

   `python -m unittest tests.test_backend_hardening.HttpInputHardeningTest.test_login_throttle_returns_http_429_with_retry_after`

   It failed as expected at the cooldown assertion: expected status `429`, received `401`, with `code=invalid_credentials`.
3. Implemented the minimal distinct exception and route handling.
4. Re-ran the same focused test; it passed.

## Verification

- Focused auth/HTTP tests: `python -m unittest tests.test_auth tests.test_backend_hardening` — **39 tests passed**.
- Full backend suite: `python -m unittest discover -s tests` — **563 tests passed**.
- `git diff --check` completed without whitespace errors.

## Concerns

No functional concerns within the requested scope. The full suite emits existing diagnostic log lines from unrelated AI/API tests, but the suite exits successfully with all tests passing.
