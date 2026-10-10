# AI 专科意见审核与患者最终报告 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将医生审核主对象和医生发布后的患者报告改为“审核后的 AI 专科评估意见 + 医生最终建议 + 后续安排”，同时保留发布门禁、历史版本和就诊前准备流程。

**Architecture:** 在现有 `ReviewItem`/`DoctorDecision` 体系中复用 `ai_analysis` 枚举，改为稳定的“AI 专科意见整体审核”单元；报告服务按病例状态生成就诊前摘要、患者最终报告和医生内部完整报告。医生端把专科意见审核与发布前条件分区，患者端在发布后切换到最终结果布局，原有病例摘要和 AI 资料包继续承担依据阅读。

**Tech Stack:** Python 3.10+, Flask, SQLite, 原生 HTML/CSS/JavaScript, `unittest`, 现有前端逻辑检查脚本。

**Spec:** `docs/superpowers/specs/2026-10-09-specialty-opinion-report-design.md`

## Global Constraints

- 不输出诊断、处方、剂量、概率或模型自报置信度。
- 患者端只在医生发布后显示审核后的专科意见；发布前仍保留就诊前准备摘要。
- AI 原文、医生决定、版本和审计记录必须可追溯，医生修改不能覆盖原文。
- 负责科室、正式 MDT、分歧和分析失败仍是发布前门禁。
- 单专科或未运行 MDT 时不能伪造多专科意见；失败的专科意见不能被空内容确认。
- 继续使用现有 PII 检查、报告版本快照和病例权限校验。

---

### Task 1: 为专科意见审核与最终报告建立失败回归

**Files:**
- Modify: `backend/tests/test_review.py`
- Modify: `backend/tests/test_report.py`
- Create: `backend/tests/test_specialty_opinion_report.py`

**Interfaces:**
- Consumes: 现有 `review_service.build_items`, `report_service.build_both`, `CaseFlow.preview_publication`。
- Produces: 明确的测试夹具与断言，供后续审核和报告实现使用。

- [ ] **Step 1: 写失败测试**

  覆盖以下契约：

  ```python
  def test_build_items_uses_one_specialty_item_per_available_analysis():
      items = build_items(case_with_two_specialty_analyses())
      assert [item.target_type for item in items].count("specialty_opinion") == 2
      assert not any(item.target_type == "path_step" for item in items)

  def test_confirmed_patient_report_contains_final_sections_only():
      reports = build_both(confirmed_case_with_reviewed_specialties())
      sections = reports["patient_final_report"]["sections"]
      assert set(sections) == {"医生审核后的专科评估意见", "医生最终建议", "后续安排"}
      assert "关键已知资料" not in sections
      assert "当前信息缺口" not in sections

  def test_pre_visit_summary_keeps_preparation_content():
      reports = build_both(unpublished_case())
      sections = reports["summary"]["sections"]
      assert "关键已知资料" in sections
      assert "当前信息缺口" in sections

  def test_specialty_decision_is_applied_without_overwriting_ai_source():
      case = confirmed_case_with_modified_specialty()
      reports = build_both(case)
      assert "医生修改后的专科意见" in json.dumps(reports["patient_final_report"])
      assert "AI 原始专科意见" in json.dumps(reports["full"])
  ```

- [ ] **Step 2: 运行测试确认失败**

  Run: `python -m unittest backend.tests.test_specialty_opinion_report -v`

  Expected: FAIL because the current item builder does not emit `specialty_opinion`, `build_both()` has no `patient_final_report`, and confirmed summaries still expose preparation sections.

- [ ] **Step 3: 补充边界夹具测试**

  添加单专科、分析失败、全部不采纳、正式 MDT、未解决分歧、无 AI Key 降级和历史版本快照的失败断言；每个夹具只使用脱敏合成文本。

- [ ] **Step 4: 再次运行并确认仍失败**

  Run: `python -m unittest backend.tests.test_specialty_opinion_report backend.tests.test_review backend.tests.test_report -v`

  Expected: 新增断言失败，既有不相关测试保持可诊断状态。

---

### Task 2: 将审核条目改为专科意见整体审核

**Files:**
- Modify: `backend/services/review_service.py`
- Modify: `backend/services/doctor_mdt.py`
- Modify: `backend/services/case_flow.py`
- Test: `backend/tests/test_specialty_opinion_report.py`, `backend/tests/test_review.py`

**Interfaces:**
- Consumes: `doctor_mdt.review_analysis(payload)` 返回的 `analyses`，每条包含 `department`, `focus`, `facts`, `inferences`, `checks`, `information_gaps`, `referral`, `status`。
- Produces: `ReviewItem(target_type="ai_analysis")`；`item_id` 形如 `ai_analysis:<source>:<department-hash>`；医生决定仍通过现有 `/api/v2/case/<id>/decisions` 写入 `DoctorDecision`。

- [ ] **Step 1: 为专科意见定义稳定摘要函数**

  在 `review_service.py` 增加一个纯函数，把一条分析卡转换成医生可审核的短文本和证据引用：只拼接关注重点、最多 4 条判断、最多 5 条待确认事项和信息缺口；不加入模型置信度；分析失败返回明确的未完成状态。

- [ ] **Step 2: 让 `build_items()` 生成专科整体审核卡**

  从 `review_analysis(payload).analyses` 生成每个可用专科一条 `ai_analysis`，`department` 和 `source_label` 由当前有效模拟/预分析决定；中文展示标签为“AI 专科意见”。保留原始 `path_step`、`consensus_item` 的派生能力仅供内部兼容，但默认审核列表不再把它们列为主审核项。

- [ ] **Step 3: 让内容修订和医生决定作用于专科卡**

  在 `current_decisions()` / `apply_decisions()` 中按 `ai_analysis:<source>:<hash>` 覆盖 `reviewer_content`，确认使用 AI 摘要、修改使用医生文本、不采纳从最终患者视图移除；旧决定继续保留在审计中。

- [ ] **Step 4: 调整发布汇总与门禁**

  `review_service.summarize()` 的主要统计改为专科卡数量；负责科室、正式 MDT、分歧、分析失败和最终建议继续写入 `publication_blockers`。分析失败不得生成可确认的空卡；单专科没有分析卡时给出“本次未运行多学科分析”的非伪造状态。

- [ ] **Step 5: 运行后端回归**

  Run: `python -m unittest backend.tests.test_specialty_opinion_report backend.tests.test_review backend.tests.test_mdt_screening backend.tests.test_doctor_workflow -v`

  Expected: 新增专科审核契约通过，既有审核门禁、修订隔离、分歧处理和协同医生权限测试通过。

---

### Task 3: 生成独立的患者最终报告并保存历史快照

**Files:**
- Modify: `backend/services/report_service.py`
- Modify: `backend/services/case_flow.py`
- Modify: `backend/templates/patient.html` (只在接口字段稳定后接入)
- Test: `backend/tests/test_specialty_opinion_report.py`, `backend/tests/test_report.py`, `backend/tests/test_doctor_result.py`

**Interfaces:**
- Consumes: 已应用决定的专科意见、`payload.review.final_advice`、医生确认后的路径/正式 MDT 后续安排。
- Produces: `report_service.build_both()` 返回 `patient_final_report`；已发布快照 `report_json` 保存该报告；患者历史接口返回当时的 `patient_final_report`。

- [ ] **Step 1: 增加 `build_patient_final_report(case)`**

  只生成三个顶层段落：`医生审核后的专科评估意见`、`医生最终建议`、`后续安排`。每张专科卡包含科室、审核状态、审核后的意见和必要的后续安排；无有效意见时使用明确的“本次未形成该科可发布意见”说明，不复制病例画像或初始缺口。

- [ ] **Step 2: 保持 `build_patient_summary()` 的就诊前职责**

  仅在病例尚未发布时继续返回原有准备字段；不要用最终报告覆盖患者填写和分析阶段仍需要的资料整理内容。

- [ ] **Step 3: 扩展 `build_both()` 和发布快照**

  返回 `summary`、`patient_final_report`、`full` 和现有 Markdown 字段；发布快照的 `report_json` 必须同时保存最终患者报告和医生内部报告。历史读取必须原样返回快照，而不是重新用当前病例生成。

- [ ] **Step 4: 加入合规清理和内容校验**

  最终报告中的医生文字继续经过 `sanitize_update_content()`；报告渲染不得输出诊断、处方、剂量或概率；来源状态统一显示“AI 生成，医生已确认/已修改”或“正式 MDT 会诊记录”。

- [ ] **Step 5: 运行报告与历史回归**

  Run: `python -m unittest backend.tests.test_specialty_opinion_report backend.tests.test_report backend.tests.test_doctor_result backend.tests.test_patient_records -v`

  Expected: 发布预览、发布快照、患者历史版本和医生结果闭环全部通过。

---

### Task 4: 重做医生端审核 UI，突出专科意见和发布门禁

**Files:**
- Modify: `backend/templates/doctor.html`
- Modify: `backend/static/css/dsh.css`
- Modify: `backend/static/css/workbench.css`
- Modify: `docs/UI设计说明-医生端审核台.md`
- Test: `backend/tests/test_frontend_structure.py`, `backend/tests/test_responsive_layout.py`, `backend/verify_frontend_logic.mjs`

**Interfaces:**
- Consumes: `/api/v2/case/<id>/review` 返回的 `specialty_opinion` 条目、审核汇总和发布门禁。
- Produces: 桌面、窄屏和移动端都能完成专科卡审核、查看依据、处理门禁、填写最终建议和预览患者最终报告。

- [ ] **Step 1: 写前端失败断言**

  检查页面包含“AI 专科意见审核与患者报告发布”“专科意见”“发布前条件”“医生审核后的专科评估意见”，并检查不再把“医疗安排”作为主审核分类标题；检查移动宽度下专科卡、门禁和预览不横向溢出。

- [ ] **Step 2: 重构审核分类与卡片渲染**

  `renderReview()` 默认按科室渲染专科意见卡；卡片内分离“AI 评估”“依据与待确认事项”“医生处理结果”。操作按钮使用语义化文案，修改和不采纳必须保持原因输入。

- [ ] **Step 3: 增加发布前条件区**

  将负责科室、正式 MDT、分歧、分析状态和最终建议显示为独立状态列表，每项提供跳转按钮。它们可以阻塞发布，但不混入专科意见卡计数。

- [ ] **Step 4: 重做患者报告预览**

  `publicationPreviewHtml()` 只渲染 `patient_final_report`，顶部突出医生最终建议，下面按科室呈现审核后的专科意见和后续安排；隐藏病例画像、信息缺口、初始咨询方向、当前任务和原始待问问题。

- [ ] **Step 5: 做视觉和行为回归**

  Run: `node backend/verify_frontend_logic.mjs`; `python -m unittest backend.tests.test_frontend_structure backend.tests.test_responsive_layout -v`。

  Expected: 1440px、1100px、640px 和 390px 断点均无横向溢出；右轨隐藏时主区仍能完成预览和发布；旧审核详情、历史入口和加载/空态仍可用。

---

### Task 5: 调整患者端最终结果 UI 与打印/历史报告

**Files:**
- Modify: `backend/templates/patient.html`
- Modify: `backend/static/css/dsh.css`
- Modify: `backend/tests/test_doctor_result.py`
- Modify: `backend/tests/test_patient_records.py`
- Modify: `backend/tests/test_responsive_layout.py`
- Modify: `docs/UI设计说明-患者端.md`

**Interfaces:**
- Consumes: `patient_final_report` 和既有 `doctor_result`。
- Produces: 患者发布后主报告、历史版本和打印版统一展示最终结果；发布前仍显示准备摘要。

- [ ] **Step 1: 写患者端失败断言**

  发布后页面必须出现“医生审核后的专科评估意见”“医生最终建议”“后续安排”，不能出现“关键已知资料”“当前信息缺口”“建议咨询方向”“当前任务”“需要向医生确认的问题”作为最终报告标题；发布前页面仍保留这些准备字段。

- [ ] **Step 2: 接入最终报告渲染**

  `switchReport()` 在 `doctor_result.published` 时使用 `patient_final_report`，在未发布时继续使用现有 `summary`；显示来源徽章和审核状态，不把 AI 未审核文本当成最终意见。

- [ ] **Step 3: 重做打印和历史版本**

  打印版只打印最终评估意见、医生最终建议和后续安排；历史报告从快照读取同样的最终结构，不重新拼接当前病例的旧资料。

- [ ] **Step 4: 运行前端与后端闭环检查**

  Run: `node backend/verify_frontend_logic.mjs`; `python -m unittest backend.tests.test_doctor_result backend.tests.test_patient_records backend.tests.test_responsive_layout -v`。

  Expected: 发布前、发布后、刷新、从“我的病历”重新打开、历史版本和打印路径都显示正确内容。

---

### Task 6: 更新文档并执行完整验证

**Files:**
- Modify: `README.md`
- Modify: `docs/开发进度与交接.md`
- Modify: `docs/UI设计说明-医生端审核台.md`
- Modify: `docs/UI设计说明-患者端.md`
- Test: 全部 `backend/tests/` 及现有 HTTP/浏览器验收脚本

- [ ] **Step 1: 同步产品边界和页面地图**

  写清楚“就诊前准备摘要”和“医生发布后的最终报告”是两种不同产物，记录专科意见审核单元、发布前门禁和患者最终报告字段。

- [ ] **Step 2: 运行 Python 回归、编译和前端逻辑检查**

  Run: `python -m compileall -q backend`; `python -m unittest discover -s backend/tests -t backend -q`; `node backend/verify_frontend_logic.mjs`。

  Expected: 编译通过，新增和既有回归通过；如环境缺少模型密钥，测试必须走规则降级并明确记录。

- [ ] **Step 3: 运行隔离 HTTP/浏览器验收**

  使用隔离 SQLite、临时上传目录、合成病例和非生产账号运行 `backend/verify_update_http.py`、`backend/verify_doctor_http.py` 及相关 Playwright 脚本，覆盖单专科、正式 MDT、部分失败、医生修改、发布、刷新、历史版本和移动端布局。

- [ ] **Step 4: 检查差异和工作区边界**

  Run: `git diff --check`; `rg -n "<<<<<<<|=======|>>>>>>>" backend docs`。

  确认只提交本功能相关文件，不覆盖用户已有未提交修改；在最终说明中报告仍未验证的外部模型质量和临床人工评测边界。
