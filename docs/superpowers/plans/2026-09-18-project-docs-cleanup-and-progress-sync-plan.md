# Project Documentation Cleanup and Progress Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 清理明确重复或已失效的项目文档，并把项目进度与当前 `main` 代码、测试和最新医生队列功能同步。

**Architecture:** 工作区根目录是设计文档的编辑源，`CHAIS/docs/` 是随代码入库的版本化快照；两处保留同名现行文档的一致内容。历史评审、评测基线、PR 记录和外部视觉参考不删除，只从“当前主线”中剥离，避免丢失可追溯依据。

**Tech Stack:** Markdown、PowerShell 只读审计、Git、Python `unittest`、Node 前端渲染校验。

**Spec:** `E:/aic_competition/README.md`、`E:/aic_competition/CHAIS/docs/README.md`、`E:/aic_competition/开发进度与交接.md`。

## Global Constraints

- 不修改业务代码、测试代码、评测数据或本地病例数据库。
- 不删除历史评审意见、`MDT-5`/评测基线、PR 记录、官方赛题规则、视觉参考资产或 `.dsh-uploads/`。
- 不保留指向已删除文档的悬空引用；根目录文档是编辑源，`CHAIS/docs/` 同名文件必须同步。
- 不在任何文档写入 API Key、密码或病例原文；清理旧文档时一并移除已失效的密钥/旧分支描述。
- 项目现状以当前 `main`、`backend/` 代码和新鲜验证结果为准，不以旧变更日志中的历史数字冒充当前状态。

---

### Task 1: 建立文档分类与删除清单

**Files:**
- Modify: `E:/aic_competition/README.md`
- Modify: `E:/aic_competition/CHAIS/docs/README.md`
- Modify: `E:/aic_competition/CHAIS/docs/工作区文档地图（根目录副本）.md`

- [x] **Step 1: Confirm current repository state**

Run `git status --short --branch`, `git log -1 --format="%h %ad %s" --date=short`, and enumerate Markdown files in the workspace and `CHAIS/docs/`.

- [x] **Step 2: Classify documents**

Use four categories in the indexes: current source, versioned snapshot, historical reference, and external/unrelated material. Mark the exact duplicate and absorbed-input documents for deletion.

- [x] **Step 3: Update index wording**

Remove deleted files from all indexes and explicitly state that historical review/evaluation material remains intentionally preserved.

---

### Task 2: Delete redundant and superseded documents

**Files:**
- Delete: `E:/aic_competition/产品补充.md`
- Delete: `E:/aic_competition/登录账号功能设计建议.md`
- Delete: `E:/aic_competition/CHAIS/docs/产品补充.md`
- Delete: `E:/aic_competition/CHAIS/docs/登录账号功能设计建议.md`
- Delete: `E:/aic_competition/CHAIS/docs/工作区文档地图（根目录副本）.md`
- Delete: `E:/aic_competition/CHAIS/main-3更新说明.md`
- Delete: `E:/aic_competition/CHAIS/main-3相较main-2总优化与遗留问题.md`
- Delete: `E:/aic_competition/CHAIS/main-4更新说明.md`
- Delete: `E:/aic_competition/_archive/optimized_ai_multidisciplinary_medical_assistance_system.md`
- Delete: `E:/aic_competition/_archive/AI多学科就医辅助系统-最终产品设计方案-v3.0（旧命名副本）.md`
- Delete: `E:/aic_competition/CHAIS/docs/_archive/optimized_ai_multidisciplinary_medical_assistance_system.md`
- Delete: `E:/aic_competition/CHAIS/docs/_archive/AI多学科就医辅助系统-最终产品设计方案-v3.0（旧命名副本）.md`

- [x] **Step 1: Re-scan references before deletion**

Search the workspace for every deleted basename and record the live references that must be rewritten before removing the files.

- [x] **Step 2: Delete only the listed files**

Use `apply_patch` delete operations for the explicit targets. Leave the remaining archive, evaluation, PR, rule, and visual-reference files untouched.

- [x] **Step 3: Remove dangling provenance references**

Replace references such as “对应产品补充建议” with “早期外部评审输入（已吸收）” where the deleted source name would otherwise become a dead link.

---

### Task 3: Rewrite the current progress snapshot

**Files:**
- Modify: `E:/aic_competition/开发进度与交接.md`
- Modify: `E:/aic_competition/CHAIS/docs/开发进度与交接.md`

- [x] **Step 1: Correct the current-state header**

Set the current repository to `main` at `2a38b95`, remove the stale `feat/patient-flow-mvp` claim, and state that the old polling endpoints are archived and return 404.

- [x] **Step 2: Update implemented scope**

Record the current patient flow, guest mode, ownership/access authorization, one-time preview tokens, progressive analysis, retry/batch analysis, evidence/report flow, and the latest server-side paged doctor queue with human-responsibility review groups.

- [x] **Step 3: Reconcile stale statuses and risks**

Mark already implemented stages as complete, move remaining work to the actual open items (CMB/medical review, recording/materials, external key revocation if still unconfirmed, and production identity/session limitations), and remove obsolete “not wired yet” and old UI statements.

- [x] **Step 4: Add fresh verification evidence**

Record the current verified baseline: 541 Python tests passed, `node verify_frontend_logic.mjs` 81/0, clean `git diff --check`, and no tracked-file changes before this task. Do not claim external LLM execution was verified.

- [x] **Step 5: Append the documentation-cleanup change log**

Add a 2026-09-18 entry listing deleted duplicates, synchronized snapshots, current source-of-truth rules, and verification commands.

---

### Task 4: Synchronize current README and indexes

**Files:**
- Modify: `E:/aic_competition/CHAIS/README.md`
- Modify: `E:/aic_competition/README.md`
- Modify: `E:/aic_competition/CHAIS/docs/README.md`
- Modify: `E:/aic_competition/多学科就医智助系统-产品设计方案-v3.0.md`
- Modify: `E:/aic_competition/CHAIS/docs/多学科就医智助系统-产品设计方案-v3.0.md`
- Modify: `E:/aic_competition/UI设计说明-医生端审核台.md`
- Modify: `E:/aic_competition/CHAIS/docs/UI设计说明-医生端审核台.md`

- [x] **Step 1: Update current test count and latest feature wording**

Change stale “531 tests” claims to the freshly verified current count where they describe the current checkout, and add the server-side doctor queue pagination/review-responsibility feature to the current architecture/status summary.

- [x] **Step 2: Fix direct references to deleted input documents**

Use neutral provenance wording while preserving the design rationale that was actually adopted.

- [x] **Step 3: Keep historical numbers explicitly historical**

Do not rewrite old evaluation baselines or PR narratives as if they were current results; only update current README/index summaries.

---

### Task 5: Verify the cleanup

**Files:**
- Test: existing `CHAIS/backend/tests/`
- Test: `CHAIS/backend/verify_frontend_logic.mjs`

- [x] **Step 1: Verify deleted files and references**

Assert each listed deleted path is absent and search for deleted basenames in active docs; only intentional historical wording may remain.

- [x] **Step 2: Verify snapshot parity**

Compare each root/snapshot pair updated in this task and ensure the contents match.

- [x] **Step 3: Run regression checks**

Run `python -m unittest discover -s tests -t .`, `node verify_frontend_logic.mjs`, and `git diff --check`.

- [x] **Step 4: Review final status**

Run `git status --short --branch` and inspect the final diff for unintended code/data changes before reporting the result.
