# 医生端极简临床工作台 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将医生端从固定三轨、信息同时铺开的工作台，改造成以当前审核任务为中心的单列临床工作台，同时保留现有病例权限、审核动作、发布条件和审计数据。

**Architecture:** 保留 `doctor.html` 现有渲染函数和 API 调用，只重组页面骨架、默认可见内容和折叠层级。共享 `dsh.css` 负责详情页的单列/响应式布局；医生模板内的局部样式负责队列密度、任务导航和审核条目层级。后端不改，所有裁剪都是呈现层裁剪。

**Tech Stack:** Flask/Jinja 模板、原生 JavaScript、共享 CSS tokens、Node 前端逻辑断言、Python unittest。

**Spec:** `docs/superpowers/specs/2026-09-30-doctor-workbench-redesign-design.md`

## Global Constraints

- 保留现有后端返回字段、API 调用、病例分科权限和审核状态流转。
- 保留现有 DOM 关键 id、JS 状态字段和可访问性语义；变更 id 时必须同步所有调用点和测试。
- 不引入第三方 UI 框架、图标库或外部资源。
- 数据保留，默认阅读量减少；原始分析、完整证据、历史决定最多两次操作可访问。
- 主内容使用单列阅读宽度；局部比较区最多两列；中小屏不保留固定右栏。
- 每个主要页面只保留一个主要行动，状态始终同时显示文本而非只使用颜色。
- 不修改现有未跟踪文件 `docs/superpowers/plans/2026-09-28-UI整改与仓库清理.md`。

---

### Task 1: 建立新的病例详情骨架与回归断言

**Files:**
- Modify: `backend/templates/doctor.html`（页面骨架、病例上下文头部、任务导航、右轨占位）
- Modify: `backend/static/css/dsh.css`（医生详情页布局规则）
- Modify: `backend/tests/test_frontend_structure.py`（详情骨架断言）
- Modify: `backend/tests/test_responsive_layout.py`（宽屏/中屏/移动端布局断言）
- Test: `backend/verify_frontend_logic.mjs`（导航状态和关键区域断言）

**Interfaces:**
- Consumes: 现有 `doctor-shell`, `s-list`, `s-overview`, `s-triage`, `s-evidence`, `s-mdt`, `s-review`, `s-history`, `go()`, `navCase()`。
- Produces: `.doctor-case-header`、`.doctor-case-nav`、`.doctor-case-main` 三个稳定布局选择器；`rail-right` 在病例详情中不再参与主布局，右侧内容迁回主区容器。

- [ ] **Step 1: Write failing structure assertions**

在 `test_frontend_structure.py` 增加以下断言：

```python
self.assertIn('class="doctor-case-header"', doctor)
self.assertIn('class="doctor-case-nav"', doctor)
self.assertIn('class="doctor-case-main"', doctor)
self.assertIn('data-s="overview"', doctor)
self.assertIn('data-s="review"', doctor)
```

在 `test_responsive_layout.py` 增加以下断言：

```python
_html, rules = self.page("/doctor")
self.assertTrue(rules)
selectors = {selector for _conditions, selector, _body in rules}
bodies = [body for _conditions, _selector, body in rules]
self.assertIn(".doctor-case-main", selectors)
self.assertIn(".doctor-case-nav", selectors)
self.assertFalse(any("grid-template-columns:var(--rail-left) minmax(0,1fr) var(--rail-right)" in body for body in bodies))
```

在 `verify_frontend_logic.mjs` 增加状态断言：

```js
const doctorHtml = fs.readFileSync(path.join(TPL, 'doctor.html'), 'utf8');
check('病例详情导航只突出主要任务',
  ['overview','triage','evidence','mdt','review'].every(key=>doctorHtml.includes(`data-s="${key}"`)), '');
```

- [ ] **Step 2: Run the focused tests and verify they fail**

Run from `E:\aic_competition\CHAIS\backend`:

```powershell
python -m unittest tests.test_frontend_structure tests.test_responsive_layout -q
```

Run from `E:\aic_competition\CHAIS`:

```powershell
node backend/verify_frontend_logic.mjs
```

Expected: the new selectors/structure assertions fail because the current template still uses the three-track shell.

- [ ] **Step 3: Implement the detail shell**

In `doctor.html`, keep the existing `s-*` section ids and JS navigation, but move case identity into `.doctor-case-header`, group the five primary task buttons into `.doctor-case-nav`, and place the active section plus inline secondary actions inside `.doctor-case-main`.

In `dsh.css`, add a doctor-detail override that uses one main column at all widths, removes the fixed right rail when `.has-case` is present, and keeps the left queue rail only while `body.doctor-queue-mode` is active. At `max-width:1000px`, make the task nav horizontally scrollable and keep focus outlines visible.

- [ ] **Step 4: Run the focused tests and verify they pass**

Run the same two commands from Step 2. Expected: all new skeleton and responsive assertions pass without changing API or permission tests.

- [ ] **Step 5: Commit the shell change**

```powershell
git add backend/templates/doctor.html backend/static/css/dsh.css backend/tests/test_frontend_structure.py backend/tests/test_responsive_layout.py backend/verify_frontend_logic.mjs
git commit -m "refine: simplify doctor detail shell"
```

---

### Task 2: 降低病例队列密度

**Files:**
- Modify: `backend/templates/doctor.html`（`renderQueue`、队列行详情和工具栏）
- Modify: `backend/verify_frontend_logic.mjs`（队列列数、展开详情、筛选入口断言）
- Modify: `backend/tests/test_frontend_structure.py`（队列字段结构断言）

**Interfaces:**
- Consumes: `S.cases`, `S.queueMeta`, `PRIMARY_QUEUE_FILTERS`, `MORE_QUEUE_FILTERS`, `setQueueFilter()`。
- Produces: 队列默认五列、病例行展开详情和保留现有分页/筛选行为。

- [ ] **Step 1: Write failing queue assertions**

增加以下 Node 断言：

```js
check('队列默认只显示五个核心字段',
  ['病例','主诉 / 牵头科室','风险','处理状态','更新'].every(x=>queue.includes(x))
  && !queue.includes('可靠性</div>') && !queue.includes('分析</div>'), queue);
check('队列保留低频详情入口', queue.includes('查看详情') && queue.includes('可靠性'), queue);
```

增加结构测试：

```python
self.assertIn('data-queue-details', doctor)
self.assertIn('查看详情', doctor)
```

- [ ] **Step 2: Run tests and verify the new assertions fail**

```powershell
node backend/verify_frontend_logic.mjs
python -m unittest tests.test_frontend_structure -q
```

Expected: current seven-column queue fails the core-field assertion.

- [ ] **Step 3: Implement the reduced queue row**

Change the queue header and row renderer to show only case, chief complaint/department, risk, processing state, and update time. Add a native `<details data-queue-details>` inside each row or its accessible expansion container containing reliability, analysis counts, automatic-processing count, and secondary status. Keep `openCase()` on the case row and preserve keyboard activation.

Keep the existing three primary filters and the “更多筛选” disclosure unchanged in behavior; only adjust labels if needed for the reduced table.

- [ ] **Step 4: Run queue tests and the complete frontend logic verifier**

```powershell
node backend/verify_frontend_logic.mjs
python -m unittest tests.test_frontend_structure -q
```

Expected: all existing queue/pagination/scope assertions and the new density assertions pass.

- [ ] **Step 5: Commit the queue change**

```powershell
git add backend/templates/doctor.html backend/verify_frontend_logic.mjs backend/tests/test_frontend_structure.py
git commit -m "refine: reduce doctor queue density"
```

---

### Task 3: 收敛总览、判断依据与 MDT 的默认信息

**Files:**
- Modify: `backend/templates/doctor.html`（`renderOverview`, `renderTriage`, `renderEvidence`, `renderMdt`）
- Modify: `backend/verify_frontend_logic.mjs`（默认内容和折叠内容断言）
- Modify: `backend/tests/test_frontend_structure.py`（关键字段保留断言）

**Interfaces:**
- Consumes: 现有 `S.caseData.payload`, `S.review`, `renderMdtScreening`, `renderMdtPreparationPacket`。
- Produces: 只展示决策摘要的默认视图；完整画像、规则过程、原始证据和专科意见都通过原生 `details` 进入。

- [ ] **Step 1: Write failing content-layer assertions**

增加以下 Node 断言：

```js
check('总览默认只突出当前待办', overview.includes('当前待办') && overview.includes('下一步'), overview);
check('总览将完整资料折叠', /<details[^>]*>/.test(overview) && overview.includes('完整患者资料'), overview);
check('MDT 默认突出共识分歧待确认',
  ['共识','分歧','待确认问题'].every(x=>mdt.includes(x)) && mdt.includes('专科意见'), mdt);
```

- [ ] **Step 2: Run the focused verifier and confirm failure**

```powershell
node backend/verify_frontend_logic.mjs
```

Expected: the current renderers do not expose the new collapsed labels and still show raw detail blocks by default.

- [ ] **Step 3: Implement progressive disclosure**

In `renderOverview`, keep the one-line profile, risk/status, one highest-priority todo, and one primary action outside details. Put medications, examination details, full gaps and repeated downstream summaries into labeled `<details>` blocks.

In `renderTriage` and `renderEvidence`, keep the selected department, decision, reason, and decision-linked evidence visible. Put rule hits, model intermediate data, full timeline and secondary references in `<details>` blocks.

In `renderMdt`, show consensus, disagreements, and open questions in a single primary result flow. Move per-specialty raw analysis, notes, and preparation packet details into one “专科意见” disclosure. Keep source labels and all existing data available inside it.

Use one container per semantic section; do not create nested cards for each field.

- [ ] **Step 4: Run logic and structure tests**

```powershell
node backend/verify_frontend_logic.mjs
python -m unittest tests.test_frontend_structure -q
```

Expected: existing patient-safety, source-label, MDT and navigation assertions remain green, and new disclosure assertions pass.

- [ ] **Step 5: Commit the content simplification**

```powershell
git add backend/templates/doctor.html backend/verify_frontend_logic.mjs backend/tests/test_frontend_structure.py
git commit -m "refine: distill doctor case information"
```

---

### Task 4: 重构审核发布与历史记录的层级

**Files:**
- Modify: `backend/templates/doctor.html`（`renderReview`, `publishPanelHtml`, `renderHistory`）
- Modify: `backend/static/css/dsh.css`（审核工作流和发布区样式）
- Modify: `backend/verify_frontend_logic.mjs`（审核默认分组、详情和发布位置断言）
- Modify: `backend/tests/test_frontend_structure.py`（审核结构断言）
- Modify: `backend/tests/test_responsive_layout.py`（审核单列和操作可达断言）

**Interfaces:**
- Consumes: 现有 `S.review.items`, `sum`, `tierOf`, `publishPanelHtml`, `decide()`, `openModal()`。
- Produces: 单列审核流；必审默认展开，其余分组默认折叠；发布和线下意见位于主内容末尾；审核须知为折叠区。

- [ ] **Step 1: Write failing review layout assertions**

增加以下 Node 断言：

```js
const review = el('rev-body').innerHTML + el('rev-publish-inline').innerHTML;
check('审核页默认只有必审分组展开',
  /review-group pending[^>]*open/.test(review)
  && !/review-group sample[^>]*open/.test(review)
  && !/review-group processed[^>]*open/.test(review), review);
check('审核细节默认折叠', review.includes('查看审核详情') && review.includes('分级依据'), review);
check('发布区位于审核主流程末尾', review.indexOf('rev-publish-inline') > review.indexOf('rev-body'), review);
```

增加 CSS 断言，确保 `#rev-body` 为单列：

```python
self.assertIn("#rev-body{display:grid;gap:var(--sp-12);grid-template-columns:1fr;}", doctor)
self.assertNotIn("repeat(2,minmax(0,1fr))", doctor)
```

- [ ] **Step 2: Run focused tests and confirm the layout assertions fail**

```powershell
node backend/verify_frontend_logic.mjs
python -m unittest tests.test_frontend_structure tests.test_responsive_layout -q
```

Expected: current review item markup exposes metadata directly and the wide-screen two-column rule is still present.

- [ ] **Step 3: Implement the review flow**

Keep `需要医生决定` open by default. Render `建议抽查`, `已完成审核`, and `自动处理` closed unless a user-selected state requires them to be visible. In `itemMarkup`, keep title, AI content, one evidence summary, and action buttons visible; move tier reason, evidence refs, revision history, decision history and automatic-processing rationale into a `查看审核详情` disclosure.

Move the main publish/line-offline-opinion panel into the end of `s-review`; keep the existing `rev-publish` id as a compatibility mirror only when necessary, and prevent duplicate visible actions. Convert the right-rail “审核须知” into a collapsed details block after the publish area.

Keep all action handlers, disabled publish conditions, auto-processing takeover, and stale-decision notices unchanged.

- [ ] **Step 4: Implement history as a compact timeline**

In `renderHistory`, keep version, operator, timestamp, and change summary visible. Put per-field diffs and raw audit entries under an accessible `<details>` per version. Keep published status and review version semantics unchanged.

- [ ] **Step 5: Run logic, structure, and responsive tests**

```powershell
node backend/verify_frontend_logic.mjs
python -m unittest tests.test_frontend_structure tests.test_responsive_layout -q
```

Expected: all existing review action, modal, publish-condition, revision-tracking, and responsive tests pass with the new single-column assertions.

- [ ] **Step 6: Commit the review flow change**

```powershell
git add backend/templates/doctor.html backend/static/css/dsh.css backend/verify_frontend_logic.mjs backend/tests/test_frontend_structure.py backend/tests/test_responsive_layout.py
git commit -m "refine: streamline doctor review workflow"
```

---

### Task 5: 完成视觉与可访问性验证

**Files:**
- Modify: `backend/templates/doctor.html` only for bounded visual fixes found by verification
- Modify: `backend/static/css/dsh.css` only for bounded responsive/focus fixes found by verification
- Test: `backend/verify_frontend_logic.mjs`
- Test: `backend/tests/test_frontend_structure.py`
- Test: `backend/tests/test_responsive_layout.py`

**Interfaces:**
- Consumes: Tasks 1–4 的结构和折叠组件。
- Produces: 通过静态扫描、逻辑测试、结构测试和响应式测试的医生端工作台。

- [ ] **Step 1: Run all targeted verification**

```powershell
node backend/verify_frontend_logic.mjs
python -m unittest tests.test_frontend_structure tests.test_responsive_layout -q
git diff --check
```

- [ ] **Step 2: Run the Impeccable mechanical detector once over changed UI targets**

```powershell
node C:\Users\DCZSX\.agents\skills\impeccable\scripts\detect.mjs --json backend/templates/doctor.html backend/static/css/dsh.css
```

Record any findings, distinguish pre-existing findings from changes introduced by this redesign, and fix only findings caused by the changed UI.

- [ ] **Step 3: Perform a bounded visual inspection**

Use the existing local server and browser tooling to inspect the doctor queue, a loaded case overview, the review view, and the mobile-width layout. Confirm that the main action is visible, details are discoverable, no right rail reserves empty space, and no buttons become unreachable.

- [ ] **Step 4: Re-run the focused tests after visual fixes**

```powershell
node backend/verify_frontend_logic.mjs
python -m unittest tests.test_frontend_structure tests.test_responsive_layout -q
git diff --check
```

- [ ] **Step 5: Commit the final verification fixes**

```powershell
git add backend/templates/doctor.html backend/static/css/dsh.css backend/verify_frontend_logic.mjs backend/tests/test_frontend_structure.py backend/tests/test_responsive_layout.py
git commit -m "test: verify minimal doctor workbench layout"
```

## Self-review checklist

- Spec coverage: Tasks 1–2 cover the new shell and queue; Task 3 covers overview, triage, evidence, and MDT; Task 4 covers review and history; Task 5 covers responsive, accessibility, detector, and final verification.
- Placeholder scan: no unresolved placeholders or unspecified implementation steps are used.
- Interface consistency: existing function names and state fields are retained; new selectors are explicitly named in Task 1 and reused by later tasks.
- Scope: no backend/API work is included; the implementation remains a doctor frontend information-architecture redesign.
