# Workspace Root Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 清理 `CHAIS/` 之外已经完成使命、无当前引用或属于临时生成物的文件，同时保留项目历史依据、工作台管理目录和不属于本项目的用户材料。

**Architecture:** `E:/aic_competition/` 是工作区，不是 Git 仓库；`CHAIS/` 是唯一代码仓库。根目录现行设计文档继续保留，外部视觉克隆研究包及其伴随资产从工作区移除，`README.md` 只记录仍存在且有用途的目录。

**Tech Stack:** PowerShell 只读盘点、Markdown、`apply_patch` 删除、Git 状态与引用扫描。

**Spec:** `E:/aic_competition/README.md`、`E:/aic_competition/CHAIS/docs/README.md`、`E:/aic_competition/开发进度与交接.md`。

## Global Constraints

- 只删除盘点中确认无当前引用的临时产物或外部视觉克隆资产，不删除现行设计、规格、评测、规则和历史评审依据。
- 不删除 `_archive/`、官方规则 PDF、现行根目录 Markdown、不属于项目的 `论文创新点评审与修改建议.md`。
- 不删除 `.dsh/`、`.dsh-uploads/`，它们是工作台管理目录；空目录也不做清理。
- 不把 Anthropic/Claude 的品牌、字体、视频或图片继续保留在项目工作区。
- 删除后必须扫描活动文档与代码，确认没有指向删除路径的引用。

---

### Task 1: 固化根目录分类

**Files:**
- Modify: `E:/aic_competition/README.md`

- [x] **Step 1: Remove deleted external-artifact rows**

从工作区地图中移除 `NOTES.md`、`PRODUCT.md`、`ui_*.png`、`fa67.../` 和 `_pdf_fulltext.txt` 的现行条目，并补充 `.dsh/`、`.dsh-uploads/` 是工作台临时目录的说明。

- [x] **Step 2: Keep project history explicit**

保留 `_archive/` 的历史评审/规格文件、官方规则 PDF和当前项目文档；不要把“历史”误判成“无用”。

### Task 2: Delete confirmed disposable and superseded assets

**Files:**
- Delete: `E:/aic_competition/fa67efc7-ae2d-4c90-b4f9-fad778c368b9/`
- Delete: `E:/aic_competition/NOTES.md`
- Delete: `E:/aic_competition/PRODUCT.md`
- Delete: `E:/aic_competition/ui_linear_redesign_v2.png`
- Delete: `E:/aic_competition/ui_linear_redesign_v3.png`
- Delete: `E:/aic_competition/ui_reimagined_command_center.png`
- Delete: `E:/aic_competition/ui_review_mockup.png`
- Delete: `E:/aic_competition/ui_review_mockup_v2.png`
- Delete: `E:/aic_competition/_js2/`
- Delete: `E:/aic_competition/output/playwright/`
- Delete: `E:/aic_competition/tmp/pdfs/`
- Delete: `E:/aic_competition/_pdf_fulltext.txt`

- [x] **Step 1: Confirm every target is unreferenced**

搜索活动 Markdown、Python、HTML、CSS、JS（排除计划文件和待删目录），确认不存在对这些目录或文件的有效引用。

- [x] **Step 2: Delete only the explicit targets**

使用 `apply_patch` 删除文件；目录内容用逐文件删除，避免递归删除未审计的其他目录。

### Task 3: Verify workspace cleanup

**Files:**
- Test: `E:/aic_competition/README.md`
- Test: `E:/aic_competition/CHAIS/backend/tests/`
- Test: `E:/aic_competition/CHAIS/backend/verify_frontend_logic.mjs`

- [x] **Step 1: Verify deletion and reference parity**

确认所有目标路径不存在，活动引用扫描为空；确认 `_archive/`、`.dsh/`、`.dsh-uploads/` 和无关论文材料仍存在。

- [x] **Step 2: Run project regression checks**

运行 `python -m unittest discover -s tests -t .`、`node verify_frontend_logic.mjs` 和 `git diff --check`，确保工作区清理没有触碰代码行为。

- [x] **Step 3: Review final status**

检查根目录清单和 `CHAIS` Git 状态；不提交、不推送，除非用户另行要求。
