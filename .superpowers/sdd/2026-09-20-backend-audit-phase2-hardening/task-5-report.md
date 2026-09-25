# Task 5 Documentation Report — Phase 2 single-machine boundary

Date: 2026-09-20
Worktree: `CHAIS/.worktrees/backend-review-phase2`
Scope: documentation/configuration only

## Status

Complete for the requested documentation scope. The implementation documents verified Task 1–4 behavior and preserves the Task 6 live-check result as **blocked**. It does not claim production readiness or successful overall live acceptance.

## Requirements covered

- Preview-token limits are documented as `APP_PREVIEW_MAX_PER_USER=5` and `APP_PREVIEW_MAX_TOTAL=5000`. Expired entries are purged before capacity enforcement, and the oldest outstanding entries are evicted when a per-user or global cap is reached.
- Model-request logs are documented as metadata-only. The documentation explicitly excludes full prompts, model output, upstream response bodies, API keys, and raw request exception messages. It does not claim that all caller-visible error strings were globally sanitized.
- Login behavior is documented as generic JSON HTTP 401 for ordinary credential failures, and JSON HTTP 429 with `code=rate_limited` and a positive `Retry-After` header during cooldown.
- The baseline headers are documented exactly:
  - `X-Content-Type-Options: nosniff`
  - `X-Frame-Options: DENY`
  - `Referrer-Policy: strict-origin-when-cross-origin`
  - `Content-Security-Policy: base-uri 'self'; object-src 'none'; frame-ancestors 'none'`
- The default HTTP demo intentionally omits HSTS. The documentation does not represent this demo as an HTTPS production deployment.
- Sessions, preview tokens, login-failure counters, and background work are explicitly described as process-local. Phase 2 is bounded to one controlled process on one controlled machine.
- The application is documented as using `request.remote_addr` and not reading or trusting `Forwarded` or `X-Forwarded-For`. A future reverse-proxy deployment is called out as requiring a separately reviewed trusted-proxy, TLS-termination, and production-WSGI boundary.
- Redis, centralized identity/session state, data lifecycle, SQL queue scaling, and production WSGI are retained as deferred production architecture work.
- The current snapshot records the verified Phase 2 code commits and the Task 6 live evidence. The change log records this documentation update without changing historical notes.

## Files changed

- `README.md`
  - Added Phase 2 preview, login, metadata-log, response-header, HSTS, process-local, forwarded-header, and deferred-architecture boundaries.
  - Kept the caller-visible error-string caveat explicit.
- `.env.example`
  - Added comments for preview caps/eviction, the `request.remote_addr` source boundary, HSTS omission, and deferred production architecture.
- `docs/开发进度与交接.md`
  - Added the verified Task 1–4 commit/test snapshot.
  - Preserved the Task 6 live result as blocked with its exit codes and blockers.
  - Added current security/compliance and risk entries plus a new Task 5 change-log row.
- `.superpowers/sdd/2026-09-20-backend-audit-phase2-hardening/task-5-report.md`
  - Added this report.

No Python source or verification script was modified. No subagent or reviewer was dispatched.

## Evidence referenced

- Task 1 report: metadata-only AI-service logging; 555 backend tests passed.
- Task 2 report: preview caps and deterministic oldest-entry eviction; 562 backend tests passed after the configuration-hardening round.
- Task 3 report: generic 401 versus cooldown 429 with `Retry-After`; 563 backend tests passed.
- Task 4 report: baseline headers and intentional HSTS omission; 565 backend tests passed.
- Task 6 report: isolated HTTP run remained blocked. `verify_doctor_http.py` exited 0 with 90 PASS / 0 FAIL; `verify_update_http.py` exited 1 with 69 PASS / 6 FAIL; `verify_report_render.mjs` twice produced the expected 13-section content checks but exited `-1073740791` after the Windows Node `UV_HANDLE_CLOSING` assertion.

## Concerns

1. Overall live HTTP acceptance remains blocked. The patient-update verifier has a reproducible authorization/fallback expectation mismatch, and the report-render command has a reproducible Windows Node runtime assertion after its business checks.
2. The documented process-local boundary is deliberate and limits this Phase 2 result to a single controlled process/machine. The deferred production architecture is not implemented here.
3. The existing development-progress document contains older historical test counts and notes; those were preserved as required, while the current snapshot remains authoritative.
