# 医生端队列分页与审核分组 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将医生端入口改为风险优先的服务端分页队列，并让审核详情按医生必审、已处理、免审自动处理分组，降低高病例量下的扫描与审核负担。

**Architecture:** `/api/v2/cases` 接收状态、筛选、排序、`limit`、`offset`，服务端先完成完整队列计算再返回当前页、总数和 `has_more`。医生端默认显示单列队列表；病例详情保留病例上下文和审核面板，但把决策条目按状态分组，`auto_processed` 只读展示并允许医生主动接管。当前后端已有免审发布闸门，本计划只补齐分页和前端呈现。

**Tech Stack:** Flask、SQLite、原生 HTML/CSS/JavaScript、Python `unittest`。

**Spec:** `docs/UI设计说明-医生端审核台.md` 与已确认的队列优先、服务端分页、风险分级审核方案。

## Global Constraints

- 医疗高风险条目（危象、证据冲突、关键证据缺失、科室选择与路径顺序）必须保留医生审核入口。
- `auto_processed` 不能显示成“医生已确认”，也不能写入医生决策记录。
- 分页必须在服务端返回当前页，不能由浏览器先加载全部病例再切页。
- 返回详情后必须保留队列筛选、排序、页码与滚动位置。
- 不新增第三方前端依赖，不改变现有登录、病例详情、审计接口语义。

---

### Task 1: 为病例队列增加稳定的服务端分页接口

**Files:**
- Modify: `backend/services/case_flow.py:1219-1260`
- Modify: `backend/app.py:402-414`
- Modify: `backend/services/store.py:189-195`
- Test: `backend/tests/test_store.py`
- Test: `backend/tests/test_review.py`

**Interfaces:**
- `Store.list_cases(limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]` 支持非负偏移。
- `CaseFlow.list_review_cases(limit: int = 20, offset: int = 0, state_filter: str = "", filter_kind: str = "", sort: str = "risk") -> Dict[str, Any]` 返回 `rows`、`total`、`has_more`。
- `GET /api/v2/cases?limit=20&offset=0&state=...&filter=...&sort=...` 返回 `cases`、`total`、`has_more`。

- [ ] **Step 1: Write the failing tests**

```python
def test_list_cases_honors_offset(self):
    for i in range(5):
        self.store.save_case(f"C{i}", created_at=f"2026-09-17T00:0{i}:00")
    page = self.store.list_cases(limit=2, offset=2)
    self.assertEqual([case["case_id"] for case in page], ["C2", "C1"])

def test_review_queue_returns_total_and_page(self):
    for case_id in ("C1", "C2", "C3"):
        self.flow.store.save_case(case_id, case_state="awaiting_doctor", payload={})
    result = self.flow.list_review_cases(limit=2, offset=1)
    self.assertEqual(result["total"], 3)
    self.assertEqual(len(result["rows"]), 2)
    self.assertFalse(result["has_more"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest tests.test_store tests.test_review -v` from `backend/`.

Expected: failure because `list_cases` does not accept `offset`, and `list_review_cases` currently returns a list without `total` or `has_more`.

- [ ] **Step 3: Write the minimal implementation**

Add `OFFSET` to the store query with a non-negative offset. In `CaseFlow.list_review_cases`, calculate review summaries for the candidate set, apply state/filter/sort before slicing, then return `rows`, `total`, and `has_more`. Clamp `limit` to a safe range in the Flask route and preserve the existing list return shape for no-pagination internal callers only when necessary.

- [ ] **Step 4: Run the focused tests**

Run: `python -m unittest tests.test_store tests.test_review -v` from `backend/`.

Expected: PASS, including existing pending-priority and state-filter tests.

- [ ] **Step 5: Commit**

```bash
git add backend/services/store.py backend/services/case_flow.py backend/app.py backend/tests/test_store.py backend/tests/test_review.py
git commit -m "feat(doctor): add paged review queue API"
```

### Task 2: 将医生端病例入口改为队列主屏

**Files:**
- Modify: `backend/templates/doctor.html:135-175`
- Modify: `backend/templates/doctor.html:245-370`
- Modify: `backend/static/css/dsh.css:106-125`
- Test: `backend/tests/test_frontend_structure.py`

**Interfaces:**
- `S.queue = { state, filter, sort, page, pageSize, total, hasMore, scroll }` 保存队列状态。
- `loadList({resetPage?: boolean} = {})` 请求当前页并只渲染该页。
- `renderPager()` 渲染页码、总数、上一页/下一页和每页数量。
- `go('list')` 恢复队列状态；`openCase()` 离开队列前保存滚动位置。

- [ ] **Step 1: Write the failing structure tests**

```python
def test_doctor_queue_has_server_pagination_controls(self):
    _, html = self.get("/doctor")
    self.assertIn('id="list-pager"', html)
    self.assertIn('id="f-sort"', html)
    self.assertIn("offset", html)
    self.assertIn("has_more", html)

def test_doctor_queue_does_not_render_duplicate_rail_cases(self):
    _, html = self.get("/doctor")
    self.assertNotIn('id="rail-cases"', html)
    self.assertIn('id="list-body"', html)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest tests.test_frontend_structure.FrontendStructureTest -v` from `backend/`.

Expected: failure because the current template has `rail-cases` and no pagination controls.

- [ ] **Step 3: Write the minimal implementation**

Replace the left mini-case list with queue filters and keep the main content as a single-column queue. Send `limit`, `offset`, state, filter, and sort to the API. Render only the returned page and update the pager from `total`/`has_more`. Use a stable case-row key when sorting ties and restore the saved queue state after returning from a case.

- [ ] **Step 4: Run the focused frontend tests**

Run: `python -m unittest tests.test_frontend_structure.FrontendStructureTest tests.test_responsive_layout -v` from `backend/`.

Expected: PASS, including the narrow-screen publish mirror checks.

- [ ] **Step 5: Commit**

```bash
git add backend/templates/doctor.html backend/static/css/dsh.css backend/tests/test_frontend_structure.py
git commit -m "feat(doctor): make review queue the primary paged view"
```

### Task 3: 在审核详情中呈现医生必审与免审状态

**Files:**
- Modify: `backend/templates/doctor.html:624-710`
- Test: `backend/tests/test_frontend_structure.py`
- Test: `backend/tests/test_review.py`

**Interfaces:**
- `renderReview()` 按 `needs_doctor_review` 与 `state` 将条目分为“必审待处理”“医生已处理”“免审（自动）”。
- 免审组显示 `auto_reason`，默认折叠；医生操作免审条目后，它进入医生已处理组并保留审计。
- 发布进度显示 `doctor_reviewed`、`auto_processed` 和 `pending`，不把自动处理计入医生处理数。

- [ ] **Step 1: Write the failing tests**

```python
def test_doctor_review_template_exposes_auto_processed_group(self):
    _, html = self.get("/doctor")
    self.assertIn("免审（自动）", html)
    self.assertIn("auto_processed", html)
    self.assertIn("doctor_reviewed", html)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m unittest tests.test_frontend_structure.FrontendStructureTest.test_doctor_review_template_exposes_auto_processed_group -v` from `backend/`.

Expected: failure because the current template does not group or display the new backend counters.

- [ ] **Step 3: Write the minimal implementation**

Create a small rendering helper inside the existing template script. Render the required-pending group expanded, the doctor-processed group collapsed by default, and the auto-processed group collapsed with its rule reason. Keep the five action buttons only on entries that can be actively handled; auto-processed entries expose a clear “接管并审核” action that uses the existing decision endpoint.

- [ ] **Step 4: Run review and frontend tests**

Run: `python -m unittest tests.test_review tests.test_report tests.test_frontend_structure -v` from `backend/`.

Expected: PASS, with report/audit tests confirming the automatic count remains separate from doctor decisions.

- [ ] **Step 5: Commit**

```bash
git add backend/templates/doctor.html backend/tests/test_frontend_structure.py backend/tests/test_review.py backend/tests/test_report.py
git commit -m "feat(doctor): group review items by human responsibility"
```

### Task 4: 全量验证与交付检查

**Files:**
- Test: existing backend test suite and verification scripts

- [ ] **Step 1: Run the complete backend suite**

Run: `python -m unittest discover -s tests -t .` from `backend/`.

Expected: all tests pass with zero failures and zero errors.

- [ ] **Step 2: Run frontend and workflow checks**

Run: `node verify_frontend_logic.mjs`, `python verify_e2e_cases.py --fast`, and `python -m unittest tests.test_responsive_layout` from `backend/`.

Expected: all checks pass; the queue API returns page metadata; the detail view keeps the publish mirror available at narrow widths.

- [ ] **Step 3: Inspect the final diff**

Run: `git diff --check` and `git status --short --branch`.

Expected: no whitespace errors, only intended files changed, and the worktree is clean after commits.

