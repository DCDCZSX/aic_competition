# Acceptance and Delivery Follow-up Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Close the post-merge documentation and live-acceptance blockers without claiming production readiness or fabricating external key-rotation evidence.

**Architecture:** Keep product behavior unchanged unless the live verifier is asserting a contract that the current backend already implements. Update the progress handoff to reflect the merged PR, make the HTTP verifier explicitly exercise reviewer authorization and distinguish no-key fallback from real-model success, and fix the Node verifier's process-exit behavior only after reproducing the Windows runtime failure.

**Tech Stack:** Python 3, Flask, SQLite, `unittest`, `requests`, Node.js, GitHub CLI.

**Spec:** `docs/开发进度与交接.md`, `.superpowers/sdd/2026-09-20-backend-audit-phase2-hardening/task-6-report.md`, and `docs/备赛行动手册.md`.

## Global Constraints

- Start from `origin/main` after PR #19 merge commit `06a1441`.
- Keep all live verification data, credentials, SQLite files, and logs outside the repository.
- Do not treat an empty `AI_API_KEY` as evidence that all model phases succeeded.
- Do not claim the historical provider API key was revoked; only the account owner can verify that in the provider console.
- Preserve the backend's explicit case-access authorization contract.
- Run the full backend suite before any completion or PR claim.

---

### Task 1: Synchronize the current-progress document after PR #19

**Files:**
- Modify: `docs/开发进度与交接.md`

**Interfaces:**
- The snapshot must identify `origin/main` merge commit `06a1441` as the current baseline.
- The verified backend regression count must be 587, not 585.
- The snapshot and change log must no longer say this foundation change is uncommitted.

- [x] **Step 1: Add a failing documentation consistency check**

  Search the current snapshot for the stale phrases and values:

  ```powershell
  rg -n "尚未提交|585 项|c54b6a0|a33e007|PR #19|06a1441" docs/开发进度与交接.md
  ```

  Expected: the snapshot still contains at least one pre-merge statement or stale test count.

- [x] **Step 2: Update only verified facts**

  Change the snapshot and latest change-log row to state that PR #19 is merged at `06a1441`, the current backend suite is 587 tests, and the remaining live/production blockers are still open. Do not mark live HTTP acceptance or production architecture complete.

- [x] **Step 3: Verify the document**

  ```powershell
  rg -n "尚未提交|585 项|PR #19|06a1441|587 项|真实 HTTP|生产" docs/开发进度与交接.md
  git diff --check
  ```

  Expected: no stale current-snapshot wording; historical rows may retain historical numbers when explicitly labeled as historical.

---

### Task 2: Correct the real HTTP acceptance verifier

**Files:**
- Modify: `backend/verify_update_http.py`
- Modify: `backend/verify_report_render.mjs`
- Test: live isolated-server runs using `backend/verify_update_http.py` and `backend/verify_report_render.mjs`

**Interfaces:**
- `verify_update_http.py` must explicitly grant the newly created case to the configured doctor before testing a doctor-uploaded opinion.
- No-key mode must accept the backend's documented partial/failed model phase and retryable result; it must not expect full LLM success.
- A configured-key mode may retain the full-success assertions, but a no-key run must be independently passable and clearly labeled.
- `verify_report_render.mjs` must set `process.exitCode` rather than abruptly calling `process.exit`, so Node can close HTTP handles cleanly on Windows.

- [x] **Step 1: Reproduce both blockers before editing**

  Start `backend/app.py` with an isolated temporary `APP_DB_PATH`, `APP_DEBUG=false`, a free local port, and an explicitly empty `AI_API_KEY`; run the existing Python verifier and record each failed assertion and the full final exit code. Run the Node verifier twice with the test password supplied through environment variables and record `node --version`, business assertions, and process exit code.

- [x] **Step 2: Trace the authorization failure**

  Compare `verify_update_http.py` with the existing authorization sequence in `verify_doctor_http.py` and the `POST /api/v2/case/<case_id>/access` route. Confirm that the failure occurs because the update verifier logs in the doctor but never grants that doctor access to the patient-owned case.

- [x] **Step 3: Add the smallest verifier regression changes**

  Add the explicit owner-to-doctor access request and a named assertion before the doctor-uploaded update. Branch the MDT expectations on whether `AI_API_KEY` is non-empty; in no-key mode assert the observed documented fallback state and that retry remains available, while preserving full-success assertions for a configured-key run.

- [x] **Step 4: Test the Node hypothesis minimally**

  Change only the final process termination mechanism from immediate `process.exit(...)` to assigning `process.exitCode`, then rerun the same Node command twice. Keep the change only if the business assertions remain unchanged and the Windows assertion disappears.

- [x] **Step 5: Re-run the isolated live suite**

  With all artifacts outside the repository, run the doctor verifier, the updated patient/update verifier, and the updated report renderer. Expected no-key results: all scripts exit 0; the update verifier explicitly reports fallback/partial model semantics rather than claiming full model success; the renderer reports 13/13 sections and exits 0.

---

### Task 3: Record the external API-key confirmation boundary

**Files:**
- Modify: `docs/开发进度与交接.md`

**Interfaces:**
- Repository scans may prove that no key is stored in tracked files; they cannot prove provider-console revocation.
- The document must retain `⚠️ 待账号持有人确认` until the account owner supplies confirmation.

- [x] **Step 1: Re-run the repository secret scan**

  ```powershell
  rg -n --hidden -g '!**/.git/**' -g '!.env' -g '!**/node_modules/**' "sk-[A-Za-z0-9_-]{20,}" .
  git check-ignore .env
  ```

- [x] **Step 2: Record the limitation without changing status**

  Keep the key-rotation item explicitly pending and add the date/evidence boundary if the account owner provides it. Do not infer revocation from a clean repository scan.

---

### Task 4: Prepare the submission-materials checklist

**Files:**
- Modify: `docs/备赛行动手册.md` only when a checklist status is directly supported by repository evidence.
- Review: `docs/产品开发规格书-比赛版v-competition.md`, `README.md`, evaluation reports, and existing verification outputs.

**Interfaces:**
- Mark only technical evidence already present in the repository as ready.
- Leave video, PPT/PDF, user/clinical evidence, and CMB accuracy as pending until the corresponding external artifact exists.
- Ensure no material claims production readiness or unverified clinical efficacy.

- [x] **Step 1: Map existing evidence to the required submission items**

  Record the available test/benchmark facts: backend 587 tests, triage evaluation n=34, five-case E2E evidence, and the documented live-acceptance limitations.

- [x] **Step 2: Mark missing external deliverables explicitly**

  Keep the video, PPT/PDF, supporting evidence, clinical/manual evaluation, and CMB accuracy items unchecked unless their files and provenance are present.

- [x] **Step 3: Run a submission red-line scan**

  Search the material set for school names/logos, advisor information, unverified clinical claims, raw credentials, and statements that live acceptance or production readiness is complete.

---

### Task 5: Final verification and handoff

**Files:**
- Test: `backend/` full suite and all updated live verifiers.
- Review: `git diff`, current progress document, and branch state.

- [x] **Step 1: Run the full backend suite**

  ```powershell
  python -m unittest discover -s tests -t .
  python -m compileall -q .
  ```

- [x] **Step 2: Check the final diff and external-artifact cleanup**

  ```powershell
  git diff --check
  git status --short --branch
  ```

  Confirm that no temporary database, log, credential, or real patient data is tracked.

- [x] **Step 3: Report unresolved external blockers honestly**

  State separately what is verified in code, what is verified by live tests, and what still requires the account owner or competition team.
