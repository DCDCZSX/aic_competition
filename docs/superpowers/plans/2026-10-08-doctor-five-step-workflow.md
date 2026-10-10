# 医生五步流程实施计划

> **For agentic workers:** 使用 executing-plans 顺序执行并在各任务结束时复核；当前工作区已有本项目相关未提交修改，继续使用当前分支并保留这些修改。用户已批准本方案及实施，无需再次确认，不提交或推送。

**Goal:** 病例摘要 → AI 资料包 → 分流确认 → MDT 协作与会诊记录 → 报告审核，科室与处理方向只确认一次，医生模拟按确认范围实际生成。

**Architecture:** 复用已有资料包、专科独立研判、质询、正式记录与发布校验。统一分流接口在同病例锁内保存科室和处理方向。医生模拟单独存储并关联资料/科室/目标指纹；后台任务不覆盖新一轮或新资料，模型失败不阻止真实会诊。

**Tech Stack:** Flask、SQLite、标准库线程，原生 HTML/JS；不新增依赖。

**Spec:** 本聊天中用户于 2026-10-08 确认的五步流程方案。

## Global Constraints

- AI 模拟与真实医生会诊记录分开，不能自动填写正式结论。
- 资料包先看事实与来源及推荐依据，不要求先完成分流或模拟。
- 单专科、补资料、急症分支直接进入报告审核；正式 MDT 完成实际记录后才能发布。
- 保留患者预分析、已发布报告快照和审核权限；模拟生成不作为发布硬性门禁。
- 未生成、生成中、完成、部分失败、失败、过期均需明确展示；旧结果保留作历史，过期不能当作本轮结果。
- 不重试此前被策略拒绝的真实服务重启；用合成病例与隔离服务验收。

## Task 1: 统一分流与按医生范围生成模拟

Files: `backend/services/case_flow.py`, `backend/services/doctor_mdt.py`, `backend/services/review_service.py`, `backend/app.py`, `backend/tests/test_doctor_workflow.py`。

- [x] RED：统一分流非法 MDT 范围不得部分保存；模拟只覆盖医生确认科室（包括超过原预分析三科上限）、携带会诊目标；生成中不重复；修改目标/资料使结果过期；迟到结果不覆盖新任务；权限及患者投影隔离。

```python
before = store.load_case(case_id)['payload']
result = flow.submit_doctor_routing(case_id, primary_department='眼科', secondary_departments=[], decision='recommend_mdt', purpose='共同判断', reason='资料依据', reviewer_id='doctor')
assert not result['ok']
assert store.load_case(case_id)['payload'] == before
```

- [x] GREEN：`submit_doctor_routing` 在锁内先校验再复用现有两项保存；`start_doctor_mdt_simulation` 生成 run_id 后后台运行独立研判/必要质询，进度落库，结果与输入指纹绑定。提供主责/协调人 POST 生成、病例授权医生 GET 状态。有效模拟参与医生审核；重新生成不能复用旧审核决定。
- [x] VERIFY：`python -m unittest tests.test_doctor_workflow tests.test_mdt_screening tests.test_review tests.test_doctor_result -q`。

## Task 2: 五步 UI 与明确跳转

Files: `backend/templates/doctor.html`, `backend/verify_frontend_logic.mjs`, `backend/tests/test_frontend_structure.py`。

- [x] RED：验证摘要继续进入资料包，统一分流按分支跳转，模拟网络失败提示、轮询刷新不清空正式记录输入、病例切换不接收旧请求。

```javascript
D.doctorWorkflowNav('overview');
check('摘要继续资料包', D.doctorWorkflowNav('overview').includes("go('packet')"));
```

- [x] GREEN：增加 AI 资料包步骤，移除 MDT 页重复科室决策。分流页同时确认处理方向/牵头科室/参与科室/会诊目标。MDT 页按范围摘要 → 模拟与状态 → 分歧处理 → 正式记录排列；模拟刷新只更新模拟区，保留正式记录输入。保留上一轮正式记录成功反馈和状态刷新。
- [x] VERIFY：`node backend/verify_frontend_logic.mjs` 及前端结构/响应式测试。

## Task 3: 浏览器闭环与文档

Files: 验收脚本/截图位于仓库外 `output/playwright/`；更新医生 UI 说明及两份开发交接文档。

- [x] 在 1440/390px 用合成病例验证五步顺序、单专科与 MDT 两条闭环、模拟失败仍可正式记录、修改范围旧模拟过期、完成后发布及患者读取。
- [x] 运行相关回归、编译与 `git diff --check`，检查页面错误和横向溢出。
- [x] 关闭隔离服务，记录验证结果及真实服务尚未加载代码的状态。

验收结果：346 项相关 Python 测试、185 项前端行为检查通过；桌面与手机真实浏览器闭环通过。独立复核补齐并验证新轮次旧决定投影、旧表单迟到提交和缺修订请求的锁外竞态；历史决定仍保留。使用合成病例及内存库，未调用外部模型服务。当前 5000 服务仍提供旧页面，需手动重启后端后刷新；此前重启自动审批被策略拒绝，本轮未重试。
