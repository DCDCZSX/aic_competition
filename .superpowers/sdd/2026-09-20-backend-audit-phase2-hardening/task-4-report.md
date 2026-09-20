# Task 4 Report — Baseline Security Response Headers

## Scope

Implemented only the baseline security response headers from `task-4-brief.md`.

- Added one Flask `after_request` hook in `backend/app.py`.
- Added focused HTTP coverage to `backend/tests/test_backend_hardening.py`.
- Did not modify auth, preview tokens, model logging, proxy behavior, database behavior, templates, CORS configuration, or API error handling.
- Did not add `Strict-Transport-Security`.

## TDD evidence

### RED

Added these tests before production code:

- `test_html_response_has_baseline_security_headers`
- `test_api_response_has_baseline_security_headers_and_stays_json`

Command:

```text
python -m unittest tests.test_backend_hardening.HttpResponseHeaderHardeningTest -v
```

Result before implementation:

```text
Ran 2 tests in 0.036s
FAILED (failures=2)
```

Both tests failed on the missing `X-Content-Type-Options` header (`None != 'nosniff'`). The API response was still verified as JSON by the test before the header assertions.

### GREEN

Added the single `@app.after_request` hook. It applies:

- `X-Content-Type-Options: nosniff`
- `X-Frame-Options: DENY`
- `Referrer-Policy: strict-origin-when-cross-origin`
- `Content-Security-Policy: base-uri 'self'; object-src 'none'; frame-ancestors 'none'`

The CSP intentionally has no `script-src` or `default-src` directive, preserving the existing inline-template scripts while enforcing the required narrow restrictions.

## Verification

Focused header tests:

```text
python -m unittest tests.test_backend_hardening.HttpResponseHeaderHardeningTest -v
Ran 2 tests in 0.036s
OK
```

Focused hardening suite:

```text
python -m unittest tests.test_backend_hardening -v
Ran 12 tests in 0.077s
OK
```

Full backend suite:

```text
python -m unittest discover -s tests -p 'test*.py' -v
Ran 565 tests in 7.526s
OK
```

`git diff --check` reported no whitespace errors. Git emitted only the repository's existing LF/CRLF normalization warnings for the two modified Python files.

## Concerns

No blocking concerns found. The CSP is intentionally narrow and does not provide a complete script/style/source policy; expanding it is outside this task because the current templates rely on inline scripts and this phase explicitly limits CSP migration.
