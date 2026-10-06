# CHAIS 产品主页与全系统体验改造 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 CHAIS 首页从“流程说明页”改成更有记忆点的产品价值入口，并同步修复病例状态、审核版本、患者端、医生端、移动端和可访问性问题。

**Architecture:** 首页继续使用现有 Flask 模板和原生 HTML/CSS/JavaScript，不引入 React Bits 或其他前端依赖。病例更新统一进入一个“审核轮次重开”事务，患者端和医生端共用同一套可见状态词；界面通过渐进增强、单一首要行动和移动端单滚动降低认知负担。

**Tech Stack:** Flask、SQLite、原生 HTML/CSS/JavaScript、Python `unittest`、现有前端结构测试与响应式测试。

**Spec:** 本计划承接 2026-10-05 产品级只读审查结论；实施前需要用户确认首页视觉方向及“医生确认后患者是否允许补充资料”的产品规则。

## Global Constraints

- 首页不再使用流程图、箭头链路或真实病例预览；右侧视觉只使用抽象、不可识别的产品能力示意。
- 首页首屏内容默认可见；动画只能作为渐进增强，JavaScript 失败时不能隐藏主内容。
- 不新增第三方前端依赖，不改变现有登录入口、角色权限和病例所有权规则。
- 首页文案只描述资料整理、方向提示、缺口标记和医生审核，不宣称诊断、处方或替代医生。
- 患者端和医生端使用同一套主状态词：`资料整理中`、`待医生审核`、`需要补资料`、`医生已确认`、`新资料待复核`。
- 任何会影响画像、分诊、路径、MDT 或报告内容的新资料，都必须进入新的审核轮次。
- 已发布报告不可被静默覆盖；新版本只能在新审核轮次完成后发布。
- 医生病例详情在窄屏只保留一个页面滚动容器；导航不能通过视觉禁用但仍响应键盘操作。
- 所有输入字段必须有可关联的 `label`；placeholder 只能提供示例，不能承担字段名称。
- 每项行为修改都必须有针对性回归测试，测试失败时不能用放宽断言掩盖状态问题。

---

### Task 1: 重做首页价值表达与视觉中心

**Files:**
- Modify: `backend/templates/home.html`
- Test: `backend/tests/test_frontend_structure.py`
- Test: `backend/tests/test_responsive_layout.py`

**Interfaces:**
- 保留 `GET /`、`enterAs('patient')`、`enterAs('doctor')` 和现有锚点入口。
- 删除流程图相关结构：`.workflow-preview`、`.workflow-track`、`.workflow-node`、步骤箭头和对应的 `role="img"`。
- 新增语义结构：`.hero-visual`、`.evidence-canvas`、`.evidence-card`、`.role-entry`。
- 首页视觉内容不依赖后端病例数据，不读取患者姓名、病例号或医疗文本。

- [ ] **Step 1: 写首页结构回归测试**

```python
def test_home_uses_value_canvas_instead_of_workflow_graph(self):
    _, html = self.get("/")
    self.assertIn('class="evidence-canvas"', html)
    self.assertIn('资料已整理', html)
    self.assertIn('缺口已标记', html)
    self.assertIn('需要医生确认', html)
    self.assertNotIn('class="workflow-preview"', html)
    self.assertNotIn('role="img"', html)

def test_home_keeps_two_role_entries(self):
    _, html = self.get("/")
    self.assertIn("enterAs('patient')", html)
    self.assertIn("enterAs('doctor')", html)

def test_home_content_is_not_hidden_by_default(self):
    _, html = self.get("/")
    self.assertNotIn('.reveal > *{opacity:0', html)
```

- [ ] **Step 2: 运行测试确认当前实现不符合新方向**

Run: `python -m unittest tests.test_frontend_structure tests.test_responsive_layout -v` from `backend/`.

Expected: the new canvas and no-flowchart assertions fail before implementation；现有测试如果被 SQLite 锁阻断，先记录环境阻断，不修改断言绕过它。

- [ ] **Step 3: 实现首页新信息架构**

保留暖色编辑风，但把首屏改成两列：左侧是单一主标题、两句产品价值说明和两个角色入口；右侧是一个深色或浅色对比明确的 `evidence-canvas`。画布使用三张抽象信息卡：

```html
<div class="evidence-canvas" aria-labelledby="canvas-title">
  <div class="evidence-canvas-title" id="canvas-title">清楚的下一步</div>
  <div class="evidence-cards">
    <article class="evidence-card"><span>01</span><strong>资料已整理</strong><p>把分散描述变成有出处的摘要</p></article>
    <article class="evidence-card accent"><span>02</span><strong>缺口已标记</strong><p>明确还需要补充哪些信息</p></article>
    <article class="evidence-card"><span>03</span><strong>需要医生确认</strong><p>把建议与最终判断分开呈现</p></article>
  </div>
  <p class="evidence-canvas-note">AI 负责整理与提示，最终判断权留给医生。</p>
</div>
```

画布用层叠、留白、轻微光晕和卡片错位制造视觉记忆点，不使用顺序箭头。主页下方只保留三项能力说明、三项安全边界和一个底部 CTA；移除原来的独立流程区，避免首页变成长说明书。

- [ ] **Step 4: 修复首页渐进增强和移动布局**

让正文默认可见；若要保留滚动揭示，只在脚本启动后给根元素添加 `.js-ready`，再由 `.js-ready .reveal > *` 控制动画。移动端保留“了解产品”锚点或显示一个可访问的菜单按钮，不隐藏后又没有替代入口。所有按钮保持至少 44px 高度。

- [ ] **Step 5: 运行首页验证**

Run: `python -m unittest tests.test_frontend_structure tests.test_responsive_layout -v` and `git diff --check` from `backend/`.

Expected: 首页结构断言通过；1440px、768px、390px 下主标题、两个角色入口和画布都可见；不出现水平溢出。

- [ ] **Step 6: Commit**

```bash
git add backend/templates/home.html backend/tests/test_frontend_structure.py backend/tests/test_responsive_layout.py
git commit -m "feat(home): replace workflow graphic with product value canvas"
```

### Task 2: 建立统一的审核轮次失效与重开机制

**Files:**
- Modify: `backend/services/case_flow.py:537-645`
- Modify: `backend/services/case_flow.py:1874-2059`
- Modify: `backend/services/case_flow.py:2495-2620`
- Modify: `backend/services/review_service.py`
- Test: `backend/tests/test_case_flow.py`
- Test: `backend/tests/test_update_flow.py`
- Test: `backend/tests/test_backend_optimization.py`
- Test: `backend/tests/test_review.py`

**Interfaces:**
- 新增 `CaseFlow._begin_recheck(case, payload, *, actor, reason, affected_item_ids=()) -> Dict[str, Any]`。
- 该方法负责：将发布状态标记为失效、保存旧报告版本、增加受影响条目的 `content_revision`、写入审计事件，并在需要时把病例转为 `awaiting_recheck`。
- `submit_answers`、`add_update`、前置分流/MDT 修改和分析重试必须通过同一入口处理“新内容影响旧审核”的语义。
- `publish_review` 对当前已发布轮次返回幂等结果，不重复生成版本；只有新的审核轮次完成后才允许创建新版本。

- [ ] **Step 1: 写失败测试覆盖已确认病例回答**

```python
def test_answers_after_confirmation_open_new_review_round(self):
    self.flow.store.transition_case(self.case_id, "doctor_confirmed", actor="doctor")
    result = self.flow.submit_answers(self.case_id, {"blood_glucose_control": "最近控制不佳"})
    self.assertTrue(result["ok"])
    case = self.flow.store.load_case(self.case_id)
    self.assertEqual(case["case_state"], "awaiting_recheck")
    self.assertFalse((case["payload"].get("review") or {}).get("published", False))
```

- [ ] **Step 2: 写失败测试覆盖就诊后更新和重复发布**

```python
def test_update_invalidates_published_review(self):
    self._publish_sample_case()
    result = self.flow.add_update(self.case_id, kind="检查结果", content="眼底检查正常")
    self.assertTrue(result["ok"])
    case = self.flow.store.load_case(self.case_id)
    self.assertEqual(case["case_state"], "awaiting_recheck")
    self.assertFalse((case["payload"].get("review") or {}).get("published", False))

def test_publish_is_idempotent_for_current_published_round(self):
    first = self._publish_sample_case()
    second = self.flow.publish_review(self.case_id, reviewer_name="医学生")
    self.assertTrue(second["ok"])
    self.assertTrue(second.get("idempotent"))
    self.assertEqual(second["version"], first["version"])
```

- [ ] **Step 3: 实现 `_begin_recheck` 并接入患者回答与资料更新**

当当前状态为 `doctor_confirmed` 或 `review.published` 为真时，先创建新轮次，再更新 triage/path/MDT。受影响条目重开，未受影响条目保留原决定；旧报告快照只读保留，当前病例不再显示为“已发布”。

- [ ] **Step 4: 收紧发布状态和版本条件**

`publish_review` 只允许 `awaiting_doctor`、`doctor_reviewed`、`awaiting_recheck` 中审核条目全部满足条件时发布。对同一轮次重复请求返回现有版本；对有新修订但未重新审核的病例返回待审核错误。

- [ ] **Step 5: 运行状态与报告测试**

Run: `python -m unittest tests.test_case_flow tests.test_update_flow tests.test_backend_optimization tests.test_review -v` from `backend/`.

Expected: 新增状态回归通过；旧报告快照仍可读取；发布版本只在新审核轮次完成后增加。

- [ ] **Step 6: Commit**

```bash
git add backend/services/case_flow.py backend/services/review_service.py backend/tests/test_case_flow.py backend/tests/test_update_flow.py backend/tests/test_backend_optimization.py backend/tests/test_review.py
git commit -m "fix(review): invalidate published rounds when case content changes"
```

### Task 3: 收紧分析失败、重试和 MDT 状态机

**Files:**
- Modify: `backend/services/case_flow.py:840-981`
- Modify: `backend/services/case_state.py:38-58`
- Modify: `backend/app.py:840-854`
- Modify: `backend/services/case_flow.py:1582-1701`
- Test: `backend/tests/test_case_flow_jobs.py`
- Test: `backend/tests/test_case_state.py`
- Test: `backend/tests/test_mdt_screening.py`
- Test: `backend/tests/test_review.py`

**Interfaces:**
- 已确认病例不能直接触发分析重试；必须先有新资料进入 `awaiting_recheck`。
- 分析 `failed` 或 `partial_failed` 时，病例主状态不得伪装成可正常审核的 `awaiting_doctor`；接口返回明确的失败阶段和恢复动作。
- 正式 MDT 状态只能按 `planned → in_progress → completed` 前进；重新打开必须使用显式原因并记录审计。
- 分流确认、MDT 初筛和正式记录改变参与科室或结论时，调用 Task 2 的审核轮次重开入口。

- [ ] **Step 1: 写失败测试**

```python
def test_retry_is_rejected_for_confirmed_case(self):
    self.store.update_case(self.case_id, case_state="doctor_confirmed")
    result = self.flow.retry_analysis(self.case_id)
    self.assertFalse(result["ok"])
    self.assertIn("新资料", result["error"])

def test_formal_mdt_status_cannot_go_backwards(self):
    self._record_formal_mdt("completed")
    result = self.flow.submit_mdt_formal_record(
        self.case_id, status="planned", participants=[], reviewer_id="doctor"
    )
    self.assertFalse(result["ok"])
```

- [ ] **Step 2: 实现状态门禁和失败展示数据**

保留失败原因、失败科室、可重试阶段和当前报告版本；不要在后台任务失败后无条件调用 `transition_case(..., STATE_AWAITING_DOCTOR)`。

- [ ] **Step 3: 运行任务、状态和 MDT 测试**

Run: `python -m unittest tests.test_case_flow_jobs tests.test_case_state tests.test_mdt_screening tests.test_review -v` from `backend/`.

Expected: 失败分析和已确认病例重试均有可预测响应；MDT 状态回退被拒绝并留痕。

- [ ] **Step 4: Commit**

```bash
git add backend/services/case_flow.py backend/services/case_state.py backend/app.py backend/tests/test_case_flow_jobs.py backend/tests/test_case_state.py backend/tests/test_mdt_screening.py backend/tests/test_review.py
git commit -m "fix(workflow): gate retries and enforce forward MDT states"
```

### Task 4: 患者端信息架构、状态词和可访问性

**Files:**
- Modify: `backend/templates/patient.html:210-259`
- Modify: `backend/templates/patient.html:325-333`
- Modify: `backend/templates/patient.html:520-620`
- Modify: `backend/templates/patient.html:1650-1700`
- Test: `backend/tests/test_frontend_structure.py`
- Test: `backend/tests/test_patient_records.py`
- Test: `backend/tests/test_responsive_layout.py`

**Interfaces:**
- 无病例时只显示一个突出动作：“新增病例”。
- 有未完成病例时突出“继续处理”；有已确认病例时突出“查看报告”；有新资料时突出“提交复核”。
- 患者端显示统一状态词，不直接把 `MDT`、`S1/S2/S3` 作为主要操作名称。
- 年龄、性别、症状、用药、检查结果、更新内容等字段拥有稳定 `id` 和匹配的 `label for`。

- [ ] **Step 1: 写结构和状态词测试**

```python
def test_patient_inputs_have_explicit_labels(self):
    _, html = self.get("/patient")
    self.assertIn('for="f-age"', html)
    self.assertIn('for="f-sex"', html)
    self.assertIn('for="f-symptoms"', html)
    self.assertIn('for="f-meds"', html)
    self.assertIn('for="f-exams"', html)

def test_patient_uses_canonical_review_status_words(self):
    _, html = self.get("/patient")
    for label in ("待医生审核", "医生已确认", "新资料待复核"):
        self.assertIn(label, html)
```

- [ ] **Step 2: 重排病例入口和当前状态卡**

把“我的病历”保留为次级入口；登录后先渲染一个状态卡和一个主按钮。历史记录、版本和变化摘要继续可访问，但不与新建病例竞争首屏注意力。

- [ ] **Step 3: 绑定标签并修正语义结构**

为所有表单控件添加 `id`，将可见标签与输入控件绑定；错误消息继续使用 `role="alert"`，状态变化使用 `aria-live="polite"`，不要只依赖颜色传达状态。

- [ ] **Step 4: 运行患者端和响应式测试**

Run: `python -m unittest tests.test_frontend_structure tests.test_patient_records tests.test_responsive_layout -v` from `backend/`.

Expected: 空病例、未完成病例、已确认病例和待复核病例的主按钮文案分别明确；390px 宽度没有水平溢出。

- [ ] **Step 5: Commit**

```bash
git add backend/templates/patient.html backend/tests/test_frontend_structure.py backend/tests/test_patient_records.py backend/tests/test_responsive_layout.py
git commit -m "feat(patient): clarify next action and accessible form semantics"
```

### Task 5: 医生端审核工作区和移动端滚动模型

**Files:**
- Modify: `backend/templates/doctor.html:134-140`
- Modify: `backend/templates/doctor.html:410-416`
- Modify: `backend/templates/doctor.html:720-850`
- Modify: `backend/static/css/dsh.css:250-420`
- Test: `backend/tests/test_frontend_structure.py`
- Test: `backend/tests/test_responsive_layout.py`
- Test: `backend/tests/test_security_access.py`

**Interfaces:**
- 病例详情页的核心顺序固定为：病例摘要 → 分流确认 → MDT（如适用）→ 审核条目 → 发布。
- 当前医生的科室、病例协作角色和可执行权限在病例头部可见。
- 不可用阶段使用真实 `disabled` 或移除键盘焦点，不能只设置 `aria-disabled`。
- `@media (max-width: 720px)` 下取消病例外层 `height` 和 `overflow:hidden`，让页面使用单一滚动；阶段导航可横向滚动但不再嵌套纵向滚动。

- [ ] **Step 1: 写导航和响应式测试**

```python
def test_doctor_unavailable_nav_is_not_clickable(self):
    _, html = self.get("/doctor")
    self.assertNotIn('aria-disabled="true" onclick="navCase', html)

def test_doctor_mobile_has_single_scroll_strategy(self):
    _, html = self.get("/doctor")
    self.assertIn("@media (max-width:720px)", html)
    self.assertIn("overflow:visible", html)
```

- [ ] **Step 2: 实现单一病例决策路径**

减少同屏的解释卡和次要操作，将批量筛选、审计历史和低优先级信息放入折叠区域；保留高风险、冲突、证据缺口和分析失败等关键标记。

- [ ] **Step 3: 修正可用状态与键盘行为**

阶段未开放时使用 `disabled` 按钮；阶段开放后再移除 `disabled` 并设置 `aria-current="step"`。保留“返回病例队列”作为固定出口。

- [ ] **Step 4: 运行医生端测试**

Run: `python -m unittest tests.test_frontend_structure tests.test_responsive_layout tests.test_security_access -v` from `backend/`.

Expected: 医生只能看见有权限的病例；窄屏只有一个主要滚动区域；键盘无法进入未开放阶段。

- [ ] **Step 5: Commit**

```bash
git add backend/templates/doctor.html backend/static/css/dsh.css backend/tests/test_frontend_structure.py backend/tests/test_responsive_layout.py backend/tests/test_security_access.py
git commit -m "feat(doctor): simplify case review navigation and mobile scrolling"
```

### Task 6: SQLite 启动并发与最终验收

**Files:**
- Modify: `backend/services/store.py`
- Modify: `backend/services/auth.py`
- Test: `backend/tests/test_store.py`
- Test: `backend/tests/test_user_store.py`
- Test: `backend/tests/test_verification_scripts.py`

**Interfaces:**
- SQLite 连接设置明确的 `busy_timeout` 和 WAL 策略；启动账号种子写入必须幂等，不因已有服务进程导致 `database is locked`。
- 不改变现有账号、角色、病例所有权和医生授权语义。

- [ ] **Step 1: 写并发启动测试**

```python
def test_account_seed_is_idempotent_when_store_is_reopened(self):
    first = AuthService(store=Store(self.db_path))
    second = AuthService(store=Store(self.db_path))
    self.assertIsNotNone(first)
    self.assertIsNotNone(second)
```

- [ ] **Step 2: 实现连接等待和幂等种子**

在每个连接创建后设置忙等待时间和 WAL；账号种子使用已存在用户的更新/忽略语义，不在并发启动时重复创建同一账号。

- [ ] **Step 3: 运行完整验证**

Run from `backend/`:

```bash
python -m unittest discover -s tests -t .
node verify_frontend_logic.mjs
python verify_e2e_cases.py --fast
git diff --check
```

Expected: 全部测试通过；若存在外部服务或数据库锁阻断，记录具体进程、路径和恢复步骤，不删除测试或放宽断言。

- [ ] **Step 4: Commit**

```bash
git add backend/services/store.py backend/services/auth.py backend/tests/test_store.py backend/tests/test_user_store.py backend/tests/test_verification_scripts.py
git commit -m "fix(storage): make SQLite startup safe under concurrent access"
```

## Product decisions to confirm before implementation

- 医生确认后的病例是否允许患者继续补充资料？本计划默认允许，但一定会开启新的审核轮次。
- “医生已确认”是冻结整份报告，还是只冻结已审核条目？本计划默认冻结当前发布版本，新增内容进入下一轮。
- 正式 MDT 是否只有在初筛选择“建议 MDT”时才阻塞发布？本计划默认按初筛结果决定，不对单专科病例额外阻塞。
- 首页是否接受“抽象证据画布”作为流程图的替代视觉？本计划默认接受，且不使用真实病例内容。

