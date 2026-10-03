# 医生端 MDT 状态边界 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 将医生端 MDT 从“AI 过程 + 医生会诊 + 发布审核”混合展示改为清晰的预分析、启动判断、正式记录和发布审核链路。

**Architecture:** 保留现有 AI `payload.mdt` 和医生 `payload.mdt_screening`，新增轻量的 `payload.mdt_formal_record` 存储真实 MDT 记录。医生模板将会前资料包限制为只读会前材料，正式记录只在 `recommend_mdt` 后出现，报告审核只引用最终来源。

**Tech Stack:** Flask、Python `CaseFlow`、Jinja 模板中的原生 JavaScript、现有 unittest 与 `verify_frontend_logic.mjs`。

**Spec:** `docs/superpowers/specs/2026-10-01-doctor-mdt-boundary.md`

## Global Constraints

- 不改变患者端的安全文案、病例权限和急症分流规则。
- 不把 AI 分析渲染成真实医生结论；任何未录入的正式 MDT 都必须显示待记录状态。
- 保留现有分歧处理接口和审核发布阻断规则。
- 继续使用现有 `esc()`、共享 CSS token 和中文医生端标签。
- 保留当前原目录已有的未跟踪计划文档，不覆盖或删除。

---

### Task 1: Add failing coverage for the MDT boundary

**Files:**
- Modify: `backend/tests/test_frontend_structure.py`
- Modify: `backend/verify_frontend_logic.mjs`
- Modify: `backend/tests/test_mdt_screening.py`
- Test: same files

**Interfaces:**
- Consumes: current `renderMdt()`, `renderMdtPreparationPacket()` and `CaseFlow.submit_mdt_screening()`.
- Produces: failing assertions for four screening options, AI-only labels, conditional formal record, and persisted formal record shape.

- [x] **Step 1: Write the failing tests**
  - Assert the doctor template contains `AI 多专科预分析`, `正式 MDT 会诊记录`, `need_more_info`, `urgent_care`, and a conditional `mdt_formal_record` renderer.
  - Extend the JS fixture with an undecided case and a `recommend_mdt` case; assert the former does not render a formal record editor and the latter renders “待记录”.
  - Add a backend test calling the new formal-record method with `planned`, `in_progress`, and `completed`, asserting the latest record is persisted under `payload.mdt_formal_record`.
- [x] **Step 2: Run the focused tests and verify they fail**
  - Run `python -m unittest tests.test_frontend_structure.FrontendStructureTest.test_doctor_mdt_boundary_labels tests.test_mdt_screening.MdtScreeningFlowTest.test_formal_mdt_record_persists` from `backend` with a temporary `APP_DB_PATH`.
  - Run `node verify_frontend_logic.mjs` and confirm the new boundary checks fail.

### Task 2: Persist formal MDT records through the existing case flow

**Files:**
- Modify: `backend/schemas.py`
- Modify: `backend/services/case_flow.py`
- Modify: `backend/app.py`
- Modify: `backend/tests/test_mdt_screening.py`

**Interfaces:**
- Consumes: `payload.mdt_screening`, existing case serialization and doctor permission guard.
- Produces: `CaseFlow.submit_mdt_formal_record(case_id, status, participants, conclusion, unresolved, next_step, reviewer_id)` and `POST /api/v2/case/<case_id>/mdt-formal-record`.

- [x] **Step 1: Add constants and validation tests**
  - Add statuses `planned`, `in_progress`, `completed` with Chinese labels.
  - Require `recommend_mdt` before accepting a formal record; require a conclusion when status is `completed`; sanitize all free text and cap participants at 12 entries.
- [x] **Step 2: Implement the case-flow mutation**
  - Load the case, reject published cases and non-`recommend_mdt` screenings, normalize participants, sanitize conclusion/unresolved/next step, and write one latest record with `updated_at` and `reviewer_id` to `payload.mdt_formal_record`.
  - Add an audit event `mdt_formal_record_updated` without writing raw medical text into the audit detail.
- [x] **Step 3: Add the guarded Flask route**
  - Use `_guard_doctor_case(case_id, 'decision')`, parse bounded fields with existing validators, call the case-flow method, and return `200` or `400` following existing patterns.
- [x] **Step 4: Run backend tests**
  - Run `python -m unittest tests.test_mdt_screening` and confirm all tests pass.

### Task 3: Rebuild the MDT page around explicit states

**Files:**
- Modify: `backend/templates/doctor.html`
- Modify: `backend/tests/test_frontend_structure.py`
- Modify: `backend/verify_frontend_logic.mjs`

**Interfaces:**
- Consumes: `S.review.mdt_preparation_packet`, `S.review.mdt_screening`, `S.review.mdt_formal_record`, and `payload.mdt`.
- Produces: `renderMdtPreparationPacket()` for pre-analysis only, `renderMdtFormalRecord()` for formal records, and `renderMdt()` with state-aware routing.

- [x] **Step 1: Rename AI content and add the safety boundary**
  - Change “MDT 过程” to “AI 多专科预分析”.
  - Add a visible notice that these are AI-generated pre-meeting references and not a doctor conclusion.
- [x] **Step 2: Expose all four screening choices**
  - Add `need_more_info` and `urgent_care` options, update conditional fields and helper copy, and retain the existing purpose/departments requirement only for `recommend_mdt`.
- [x] **Step 3: Restrict the preparation packet**
  - Render screening, triage, evidence, specialty facts/focus/inferences, information gaps, and suggested discussion questions.
  - Remove consensus, disagreement handling forms, and `human_reviewed` from the preparation packet output.
- [x] **Step 4: Add conditional formal-record UI**
  - For pending screening show the decision prompt.
  - For single specialty, need-more-info, or urgent care show a route-specific result and no formal meeting editor.
  - For `recommend_mdt`, show selected departments, status, participants, conclusion, unresolved issues, next step, and save action; use existing disagreement controls below the formal record.
- [x] **Step 5: Add the formal-record submit function**
  - Post to `/api/v2/case/${S.caseId}/mdt-formal-record`, refresh the case, and keep the user on `mdt` with a success/error message.
- [x] **Step 6: Run frontend tests**
  - Run `node --check verify_frontend_logic.mjs`, `node verify_frontend_logic.mjs`, and the focused structure tests.

### Task 4: Align overview and report-review copy with the new state machine

**Files:**
- Modify: `backend/templates/doctor.html`
- Modify: `backend/tests/test_frontend_structure.py`

**Interfaces:**
- Consumes: `mdt_screening.decision`, `mdt_formal_record.status`, and existing review summary.
- Produces: conditional stage hints, next-step cards, and report-review source labels.

- [x] **Step 1: Make overview routing conditional**
  - Show “启动正式 MDT” only for `recommend_mdt` or pending screening; show “单专科处理” and route directly to review for `single_specialty`; show supplement or urgent route for the other two choices.
- [x] **Step 2: Clarify report review’s role**
  - Replace the generic MDT reminder with a source-aware message such as “报告审核只确认最终发布内容；详细 MDT 记录请回到 MDT 协作查看”.
- [x] **Step 3: Add structure assertions**
  - Assert overview no longer presents MDT as mandatory after single-specialty screening and report review contains the source-aware copy.

### Task 5: Update docs and perform verification

**Files:**
- Modify: `docs/UI设计说明-医生端审核台.md`
- Modify: `docs/superpowers/specs/2026-10-01-doctor-mdt-boundary.md`
- Modify: `docs/superpowers/plans/2026-10-01-doctor-mdt-boundary.md`

**Interfaces:**
- Consumes: implemented labels, fields, endpoints, and state transitions.
- Produces: documentation matching shipped behavior.

- [x] **Step 1: Document the four-layer flow and data boundaries**
- [x] **Step 2: Run verification**
  - `python -m unittest tests.test_frontend_structure tests.test_responsive_layout tests.test_review tests.test_mdt_screening` with a temporary DB.
  - `node verify_frontend_logic.mjs` and `node --check verify_frontend_logic.mjs`.
  - `git diff --check`.
  - `node C:\Users\DCZSX\.agents\skills\impeccable\scripts\detect.mjs --json backend/templates/doctor.html`.
- [x] **Step 3: Commit, merge, and rerun the focused suite on the original branch**

