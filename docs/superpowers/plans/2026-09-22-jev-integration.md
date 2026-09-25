# Jev Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Jev as an optional, independently configured structured-decision provider for offline evaluation and shadow-mode triage verification without replacing the existing rule, generation-model, or doctor-review paths.

**Architecture:** Keep the existing `AIService` for generated text. Add a `TypeSafeDecisionService` that calls TypeSafe's `/v1/systemone` endpoint and returns a normalized internal result with version, answers, latency, usage, and stable error codes. First integrate it into an offline/explicit shadow path for S1 candidate verification; only allow later production decisions behind a feature flag and preserve the current rule-first fallback.

**Tech Stack:** Python 3.10+, Flask backend, `requests`, `unittest`, existing `.env` configuration, TypeSafe HTTP API.

**Spec:** `docs/jev接入分析方案.md`

## Global Constraints

- Preserve the existing S1 emergency rules and S3 deterministic orchestration as the final safety boundary.
- Jev must be optional; an absent key, timeout, 429, malformed response, or provider outage must not break the current flow.
- Do not send raw identifiers or unredacted patient text to Jev.
- Pin the initial model to `jev-1.13.0`; do not use `jev-latest` for calibrated thresholds.
- Do not expose Jev probabilities or confidence values to patients or public reports.
- Do not use Jev to generate MDT opinion-card prose or patient-facing explanations.
- Do not add a production dependency that requires network access for the existing offline test suite.

---

## Repository and baseline

The repository is `CHAIS/`, remote `origin` is `git@github.com:DCDCZSX/aic_competition.git`. Before implementation, work from an isolated feature branch based on the freshly fetched repository state. The current remote main is `9607ea5`; the active branch was already fast-forward checked with `git pull --ff-only`.

## Task 1: Create the isolated feature workspace and record baseline

**Files:**
- Create: `.worktrees/jev-integration` (git worktree)
- Branch: `feat/jev-integration`
- Test: `backend/tests/` existing suite

**Interfaces:**
- Consumes: current fetched repository state.
- Produces: isolated feature branch with a clean baseline and recorded test result.

- [ ] Create the worktree from the current implementation baseline after workspace confirmation.
- [ ] Run `python -m unittest discover -s tests -t .` from `backend/`.
- [ ] Record the baseline count and any pre-existing failures before changing code.

No Jev API key is needed for this task.

## Task 2: Add Jev configuration and normalized result types

**Files:**
- Create: `backend/services/typesafe_decision_service.py`
- Modify: `.env.example`
- Test: `backend/tests/test_typesafe_decision_service.py`

**Interfaces:**
- Produces `TypeSafeDecisionService.__init__(api_key=None, api_url=None, model=None, session=None, ...)`.
- Produces `TypeSafeDecisionService.system_one(state, questions) -> DecisionResult`.
- `DecisionResult` contains `ok`, `provider`, `model`, `answers`, `usage`, `latency_ms`, `error_code`, and a safe `error_message`.
- `system_one` accepts JSON-compatible text-only state and question definitions; it rejects empty questions before a network call.

- [ ] Write failing tests for missing key, empty questions, successful typed response, timeout, HTTP 429, HTTP error, malformed JSON, and model-version capture.
- [ ] Run the focused test file and confirm the failures are caused by the missing service.
- [ ] Implement the smallest requests-based adapter using `Authorization: Bearer <key>` and `POST /v1/systemone`.
- [ ] Add bounded connect/read timeouts and a small retry policy for transient errors only.
- [ ] Normalize provider errors to stable codes such as `not_configured`, `timeout`, `rate_limited`, `upstream_error`, and `invalid_response`.
- [ ] Ensure exception details and response bodies are not returned to user-facing callers.
- [ ] Add `TYPESAFE_API_KEY`, `TYPESAFE_API_URL`, `TYPESAFE_MODEL`, `TYPESAFE_CONNECT_TIMEOUT`, `TYPESAFE_READ_TIMEOUT`, `TYPESAFE_MAX_RETRIES`, and `TYPESAFE_ENABLED` to `.env.example` with safe defaults.
- [ ] Run the focused tests and then the existing backend suite.

No Jev API key is needed for unit tests; the HTTP session is injected and fake responses are used.

## Task 3: Add structured S1 candidate verification

**Files:**
- Create: `backend/services/jev_triage.py`
- Modify: `backend/services/triage.py` only if a small normalization hook is required
- Test: `backend/tests/test_jev_triage.py`

**Interfaces:**
- Produces `build_candidate_questions(evaluation, ...) -> dict`.
- Produces `verify_candidates(decision_service, state, evaluation) -> JevTriageResult`.
- `JevTriageResult` records per-department `supported`, `confidence`, `reason_code`, and whether the result is usable; it must not alter S3 on its own.

- [ ] Write failing tests for one candidate, multiple candidates, empty rule results, unknown department names, and provider failure.
- [ ] Run the focused tests and verify they fail before implementation.
- [ ] Implement one atomic `Noul` question per candidate, with literal criteria and no arithmetic delegated to Jev.
- [ ] Limit state to the redacted case summary and the evidence relevant to the candidate; do not send the entire raw case by default.
- [ ] Keep the result as an evaluation artifact; do not silently remove strong rule hits.
- [ ] Add deterministic conversion of the provider response into internal booleans and confidence metadata.
- [ ] Run focused tests and the full backend suite.

No Jev API key is needed for unit tests. A key is needed for a real sample run.

## Task 4: Add an explicit shadow-mode integration point

**Files:**
- Modify: `backend/services/case_flow.py` at the S1/S2 boundary
- Modify: `backend/app.py` only for configuration wiring if required
- Modify: `backend/services/store.py` or existing audit service only if an established event path exists
- Test: `backend/tests/test_case_flow.py` or a focused new test

**Interfaces:**
- Consumes: `TypeSafeDecisionService` and `verify_candidates`.
- Produces: an opt-in shadow result that is logged as internal audit metadata and does not change patient-facing triage.

- [ ] Write a failing test proving `TYPESAFE_ENABLED=false` does not instantiate or call Jev.
- [ ] Write a failing test proving an enabled shadow call cannot change the selected departments.
- [ ] Implement the smallest feature-flagged call after S1 and before existing S2/S3 processing.
- [ ] Record only model version, result status, candidate summaries, latency, and error code; avoid raw state and raw upstream responses.
- [ ] Preserve existing no-key and provider-failure fallback behavior.
- [ ] Run focused tests and the full backend suite.

No Jev API key is needed for the disabled-path tests. A key is needed to validate the live shadow path.

## Task 5: Add offline evaluation runner and report

**Files:**
- Create: `eval_jev_triage.py`
- Create: `docs/jev接入评测方案.md`
- Test: `backend/tests/test_jev_eval.py` if shared helpers are introduced

**Interfaces:**
- Consumes: existing `eval_cases_selfbuilt.json`, fixed cases, and the current triage evaluation helpers.
- Produces: timestamped JSON results under the existing ignored evaluation output directory, separated by dataset subset and method.

- [ ] Write a failing test for deterministic metric aggregation with mocked Jev answers.
- [ ] Implement comparison groups: pure rules, current full chain, and S1 + Jev verification.
- [ ] Report Macro recall, Macro precision, F1, full-hit count, average departments, emergency interception, latency, provider errors, and fallback rate.
- [ ] Keep real MDT, fixed specification cases, self-built cases, emergency cases, short cases, and long cases as separate subsets.
- [ ] Add a short report explaining that Jev confidence requires calibration and is not a clinical probability.
- [ ] Run the evaluation in mock mode and verify output shape.

No Jev API key is needed for mock-mode evaluation. A key is required for real model comparison.

## Task 6: Live smoke test, documentation, and verification

**Files:**
- Modify: `docs/jev接入分析方案.md` with actual integration status and measured results
- Modify: `docs/开发进度与交接.md` only after implementation is verified
- Modify: `README.md` only if a user-facing configuration section is needed
- Test: full backend tests, HTTP acceptance scripts, and one redacted Jev smoke test

**Interfaces:**
- Consumes: completed adapter, shadow integration, and evaluation runner.
- Produces: verified implementation status and a go/no-go recommendation for further rollout.

- [ ] Run all local tests without a Jev key.
- [ ] If a Jev key is available, run one redacted live request against pinned `jev-1.13.0` and record latency, model version, and response shape without recording the case text.
- [ ] Run the existing HTTP acceptance checks with Jev disabled.
- [ ] Inspect `git diff --check` and search tracked files for accidental keys or raw case data.
- [ ] Update the integration documents with measured results and known limitations.
- [ ] Do not claim Jev improves clinical quality unless the project evaluation and human review support that claim.

## API key requirement

Implementation, unit tests, mock evaluation, disabled-path integration, and existing HTTP acceptance do **not** require a Jev key.

A key is required only for:

1. A real TypeSafe API smoke test;
2. Live Jev-vs-current-model evaluation;
3. Shadow-mode latency, quota, and failure testing.

If requested, obtain the key from the TypeSafe console and place it only in a local ignored `.env` entry:

```dotenv
TYPESAFE_API_KEY=<local-secret>
TYPESAFE_MODEL=jev-1.13.0
```

Never put the key in code, this plan, test fixtures, logs, commits, or pull requests.

