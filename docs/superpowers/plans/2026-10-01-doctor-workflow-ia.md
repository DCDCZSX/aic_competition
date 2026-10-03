# 医生端病例处理工作流重构 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将医生端病例处理顺序收敛为“病例摘要 → 分流确认 → MDT 协作 → 报告审核 → 历史记录”，让每个页面只承担一个明确任务，并把 MDT 初筛与分歧处理放到实际操作页面。

**Architecture:** 保留现有 Flask API、病例状态、权限和审核接口；只调整 `doctor.html` 的页面导航、渲染组合和待办跳转。病例屏幕内部仍使用现有 `overview`、`triage`、`mdt`、`review`、`history` key，避免改变前端状态机和后端接口。

**Tech Stack:** Flask 模板、原生 HTML/CSS/JS、Python unittest、Node 前端逻辑校验。

**Spec:** 本轮对话中确认的医生端病例工作流方案；重点是页面职责分离、MDT 操作位置统一、证据随判断展示。

## Global Constraints

- 不修改后端 API、病例权限、审核状态流转和发布门禁。
- 不把患者侧“向医生确认……”直接显示在医生端；医生端使用“待医生判断”。
- 不删除 AI 原稿、证据引用、医生决定或审计数据。
- 证据作为判断的辅助信息嵌入相关页面，不新增孤立的主导航入口。
- 每个页面只保留一个主任务，当前待办必须指向实际可操作的位置。

---

### Task 1: 添加工作流结构回归测试

**Files:**
- Modify: `backend/tests/test_frontend_structure.py`
- Modify: `backend/verify_frontend_logic.mjs`

**Interfaces:**
- Consumes: 当前 `doctor.html` 导航、`renderOverview`、`renderMdt` 输出。
- Produces: 对导航顺序、页面职责和 MDT 表单位置的可重复断言。

- [x] **Step 1: Write failing Python assertions**

新增测试断言：
```python
def test_doctor_case_navigation_uses_processing_order(self):
    _, html = self.get("/doctor")
    nav = html[html.index('id="nav"'):html.index('id="s-list"')]
    for label in ("病例摘要", "分流确认", "MDT 协作", "报告审核", "历史记录"):
        self.assertIn(label, nav)
    self.assertLess(nav.index("病例摘要"), nav.index("分流确认"))
    self.assertLess(nav.index("分流确认"), nav.index("MDT 协作"))
    self.assertLess(nav.index("MDT 协作"), nav.index("报告审核"))

def test_mdt_controls_live_in_mdt_screen(self):
    _, html = self.get("/doctor")
    mdt_start = html.index('id="s-mdt"')
    review_start = html.index('id="s-review"')
    mdt = html[mdt_start:review_start]
    overview_start = html.index('id="s-overview"')
    self.assertIn("renderMdtScreening", html)
    self.assertIn("renderMdtPreparationPacket", html)
    self.assertIn("MDT 初筛与病例分流", html)
    self.assertNotIn("查看分诊与 MDT 资料", html[overview_start:mdt_start])
```

- [x] **Step 2: Run focused tests to verify RED**

Run:
```powershell
python -m unittest tests.test_frontend_structure.FrontendStructureTest.test_doctor_case_navigation_uses_processing_order tests.test_frontend_structure.FrontendStructureTest.test_mdt_controls_live_in_mdt_screen
```
Expected: FAIL because current navigation still uses “总览/判断依据/多学科” and MDT controls are rendered from overview.

- [x] **Step 3: Add Node assertions for actionable routing**

新增断言，要求总览待办的 MDT 入口指向 `go('mdt')`，并要求 `renderMdt` 输出包含初筛和会前资料包。
- [x] **Step 4: Run the focused Node assertion and confirm RED**

Run `node verify_frontend_logic.mjs`; expected failure identifies the old render location.

---

### Task 2: 重排病例导航和页面职责

**Files:**
- Modify: `backend/templates/doctor.html`

**Interfaces:**
- Consumes: 现有 `navCase`、`go`、`renderOverview`、`renderTriage`、`renderMdt`、`renderReview`。
- Produces: 页面标签和屏幕顺序符合病例处理工作流；内部 screen key 保持不变。

- [x] **Step 1: Change navigation labels and order**

保留 screen key 不变，只把可见文案改成：
```text
病例队列
病例摘要
分流确认
MDT 协作
报告审核
历史记录
```

- [x] **Step 2: Remove MDT forms from overview**

`renderOverview` 只保留病例概况、当前待办、患者资料摘要和跳转入口；删除其中的 `renderMdtScreening()` 与 `renderMdtPreparationPacket()` 直接渲染。

- [x] **Step 3: Compose MDT screen from gate to result**

`renderMdt` 先输出：
```text
MDT 初筛与病例分流
MDT 会前资料包
MDT 结果与过程
```
这样医生先作是否协作的判断，再看专科意见、共识、分歧和处理表单。

- [x] **Step 4: Run focused structure tests**

Run:
```powershell
python -m unittest tests.test_frontend_structure.FrontendStructureTest.test_doctor_case_navigation_uses_processing_order tests.test_frontend_structure.FrontendStructureTest.test_mdt_controls_live_in_mdt_screen
```
Expected: PASS.

---

### Task 3: 修正待办跳转和阶段提示

**Files:**
- Modify: `backend/templates/doctor.html`
- Modify: `backend/tests/test_frontend_structure.py`
- Modify: `backend/verify_frontend_logic.mjs`

**Interfaces:**
- Consumes: `S.review.summary`、`S.review.pending_disagreements`、病例状态。
- Produces: 当前待办按钮始终指向真实操作页面。

- [x] **Step 1: Add failing routing assertions**

验证：
- 待审条目 → `go('review'))
- MDT 分歧 → `go('mdt'))
- 未完成分流 → `go('triage'))
- 发布入口 → `go('review'))

- [x] **Step 2: Update overview todo construction**

把总览中的待办顺序改为：
1. 高风险/状态异常；
2. 分流确认；
3. MDT 初筛或分歧处理；
4. 报告审核；
5. 发布审核版。

每条只保留一个主按钮，按钮名称使用“去分流确认”“去 MDT 协作”“去报告审核”。

- [x] **Step 3: Add a workflow stage hint**

在病例标题下增加简短阶段提示，显示当前病例位于“分流确认 / MDT 协作 / 报告审核 / 已发布”中的哪一阶段，不改变病例状态值。

- [x] **Step 4: Run focused frontend tests**

Run `python -m unittest tests.test_frontend_structure tests.test_responsive_layout` and `node verify_frontend_logic.mjs`.

---

### Task 4: 同步 UI 说明并完成回归

**Files:**
- Modify: `docs/UI设计说明-医生端审核台.md`
- Modify: `docs/superpowers/plans/2026-10-01-doctor-workflow-ia.md`

- [x] **Step 1: Document the page responsibility table**

写入“病例摘要 / 分流确认 / MDT 协作 / 报告审核 / 历史记录”的职责、入口和完成条件。

- [x] **Step 2: Run full focused regression**

Run:
```powershell
python -m unittest tests.test_frontend_structure tests.test_responsive_layout tests.test_review
node verify_frontend_logic.mjs
git diff --check
```

- [ ] **Step 3: Commit**

```powershell
git add backend/templates/doctor.html backend/tests/test_frontend_structure.py backend/verify_frontend_logic.mjs docs/UI设计说明-医生端审核台.md docs/superpowers/plans/2026-10-01-doctor-workflow-ia.md
git commit -m "refine: align doctor case workflow"
```
