# PR 记录 · feat(stage3-6): 证据链闭环、医生工作台、产品首页与界面统一

> 分支 `feat/stage3-6-review-and-ui` → `main`　提交 `fcecf3f`（功能提交 `4af35e2`）
> 本文件是该 PR 的描述正文存档（创建 PR 时逐字使用），便于日后回溯"这次交付了什么、怎么验证的"。

---

## 这个 PR 做了什么

分四块，都是可复跑验证的功能增量，不是文档堆叠。

### 一、阶段 3 证据链与报告体系（`f59678b`，本次一并带入）
- **证据板** `services/evidence_service.py`：时间线（症状/疾病/检查事件，带 `source_refs`）+ 判断↔证据关联（direct / indirect / missing / conflict）+ 证据卡支持/反对双列；外部依据只认白名单 8 条（教材/共识），未命中显示"暂无可靠外部依据"——**不接 RAG、不伪造文献**。
- **报告体系** `services/report_service.py`：一页就医摘要（8 项）+ 完整报告（13 段）；未审核角标、免责声明、能力边界必带；返回 `section_order` 作为权威段落顺序（Flask 的 `jsonify` 会排序 key，前端不能用 `Object.keys`）。

### 二、阶段 3.4 / 3.6 任务闭环与就诊后回路
- `services/task_service.py`：任务状态机（显式转移表 todo→doing→done→reopened / cancelled→todo，非法转移报错并返回 400，每次流转留痕；只有"就诊后资料更新"类任务完成才标记触发重算）。
- `services/update_service.py`：就诊后资料更新——用"字段 ↔ 专科"依赖矩阵判断受影响科室 → **只重算分诊与路径**（未受影响的多学科分析不重跑）→ 新资料命中缺口关键词则消除对应缺口 → 生成变化摘要（新增了什么 / 哪些判断变了 / 哪些未受影响 / 是否需医生重新确认）。
- 新接口：`POST /api/v2/case/<id>/updates`、`GET /api/v2/case/<id>/changes`、`POST /api/v2/case/<id>/tasks/<task_id>`。
- 🔴 **修复红线缺陷**：患者转述的"医生说……"原先会被记成**已验证的医生确认意见**；现在原样保留来源、`is_verified=false`、不进"AI 与医生一致"列表，并提示"需医生端确认"。

### 三、阶段 4 医生工作台（屏 A-G）
- `services/review_service.py`：把病例展开成**可逐条审核的决策条目**（科室入选 / 路径步骤 / 共识分歧 / 患者建议四类），维护条目状态机、发布条件，并过滤概率数字（**只处理"概率/置信度 + 数字"，不动"空腹血糖 9.2""二甲双胍 0.5g"这类真实数据**）。
- 5 个接口：`GET /api/v2/cases`、`GET /api/v2/case/<id>/review`、`POST /api/v2/case/<id>/decisions`、`POST /api/v2/case/<id>/publish`、`GET /api/v2/case/<id>/audit`。
- **后台鉴权** `_guard_doctor()`：医生端接口**始终**要求医生角色（未登录 401、患者角色 403），不当"只做前端跳转"；`/updates` 以"医生上传"为来源时同样要求医生角色（患者端不承担"替医生写意见"）。
- `templates/doctor.html`：病例列表 / 病例总览 / 分诊依据（规则→模型→编排三栏流水线）/ 证据板 / 多学科审阅（含"▶ 播放过程"）/ 报告审核（逐条接受·修改·驳回·暂不确定，修改与驳回原因必填）/ 审核记录与版本。
- 发布审核版：全部条目终态才可发布，状态 → `doctor_confirmed`、报告版本 +1、角标转绿；`accept_all` 也会**逐条留痕**（不是静默通过）；"暂不确定"带原因进入报告待确认问题清单。

### 四、阶段 5.4 / 5.6 / 6 对外呈现与界面
- `README.md` 按真实产品重写：清除旧产品名"智能MDT多学科会诊系统"与不存在的 WebSocket / React / 知识库检索能力。
- **对外呈现口径**：全站去掉"演示/demo"痕迹——账号统一 `patient` / `doctor`，入口统一「患者端 / 医生工作台」，默认审核人改「审核医生」；交接文档 §3 新增第 7 条硬约束（含提交前 grep 自查命令）。旧轮询页移出路由并归档到 `_legacy/root_templates_polling/`。
- `templates/home.html`：暖色编辑风产品首页（固定顶栏下拉导航 / Hero + 双入口 / 痛点与能力 / 七步流程 / 场景标签页 / 安全护栏与红线 / 评测与可信度 / FAQ / 深色页脚）；**零外链、零构建、自绘 SVG**。
- `static/css/dsh.css`：三页共用的设计系统（暖色 token + 三轨外壳 + 组件库），两端由蓝白统一为暖色。
- 患者端：三轨工作台（左流程导航可点击回跳 / 中工作区 / 右病例上下文）；画像与缺口两列、追问两列、分诊结果三栏流水线、路径与任务两栏、报告文档式（左目录 + 右正文 + 滚动高亮）、就诊后更新左表单右说明。
- 医生工作台：左轨待办病例 + 审阅视图 + 审核进度，右轨常驻审核面板（进度 / 发布 / 录入线下意见）。
- 登录页：全屏两栏 + **一端一入口**（`/patient` 只出现患者端入口、`/doctor` 只出现医生工作台入口，跨端仅留页脚弱链接），修掉"内容只堆在左上角"的样式回归。

## ⚠️ 范围说明（评审请先看这条）

`main` 当前只到**阶段 0-2**：PR #4 合进去的是 `a2d06d1`，而 `f59678b`（阶段 3 证据板 + 报告体系）当时**没被合进去**。所以这个 PR 从**阶段 3 起全部包含**，diff 比分支名看起来更大——这是刻意的，不是混入了无关改动。

## 验证证据

| 层 | 命令 | 结果 |
|---|---|---|
| 单元测试 | `cd CHAIS/backend && python -m unittest discover -s tests -t .` | **410 项全过** |
| 患者端 HTTP 端到端 | `python verify_update_http.py` | **37 / 0**（建病例→任务状态机→上传眼底检查缺口 7→6→状态转 awaiting_recheck→患者转述意见仅列 unverified→医生上传才进 agree→非法输入 400） |
| 医生工作台 HTTP 端到端 | `python verify_doctor_http.py` | **72 / 0**（未登录 401 / 患者角色 403→逐条审核→缺原因被拒→发布前置条件→批量接受逐条留痕→版本 v1→v2→角标转绿→请求补资料→need_more_info） |
| 前端渲染逻辑 | `node verify_frontend_logic.mjs` | **27 / 0**（Node + 最小 DOM 桩，断言两条轨道、进度条、病例列表真的被填进容器） |
| 前端结构与合规 | `python -m unittest tests.test_frontend_structure -v` | **14 / 0**（Jinja 标记残留、三轨结构、业务屏必须在 `main` 内、无外链、无"演示"文案、登录页一端一入口） |

页面可达性：`/`、`/patient`、`/doctor`、`/static/css/dsh.css` 均 200；旧路由 `/legacy` 已 404。

## 红线与合规检查

- [x] 不出诊断结论、不出处方/剂量；医疗行为类文案统一为"建议向医生确认是否需要 ××"
- [x] 界面与报告不展示模型自报概率/置信度（并有过滤 + 单测断言）
- [x] AI 不覆盖医生：医生意见与 AI 建议并列；患者转述内容不作为医生意见采信
- [x] 未提交 `.env`（密钥）、`*.db`（病例数据）、`MDT-5*.csv`（金标准）、`eval_out/`、`offline_packages/`、`knowledge_base/`
- [x] 无第三方外链资源、无第三方品牌素材（字体为系统字体栈、插画为自绘 SVG）；旧产品名与旧 UI 不再对外路由

## 界面变化（建议这样看）

1. `cd CHAIS/backend && python app.py`，打开 http://127.0.0.1:5000/
2. 首页 → 点「进入患者端」，走一遍：录入 → 脱敏 → 画像 → 追问 → 就医方向（三栏流水线）→ 路径任务 → 报告（切"完整报告"看左目录滚动高亮）→ 就诊后更新
3. 退出登录 → 打开 http://127.0.0.1:5000/doctor → 用 `doctor` 账号进 → 左轨选一个待审病例 → 分诊依据 → 多学科审阅（点"▶ 播放过程"）→ 报告审核（接受/修改/驳回，然后"全部接受并发布"）→ 审核记录
4. 宽屏（≥1440px）看三轨铺满；把窗口拉窄看右轨折到主区顶部、左轨变横向步骤条

## 已知限制 / 后续

- 阶段 6.D 未完成：首页仍自带一份内联 token（未引用 `dsh.css`）；1440/1200/992/768/390 四档响应式与打印**尚未逐档像素核对**。
- 阶段 5.1-5.3 未完成：评测集扩容 n≥30、实验二/实验三、演示录屏与材料。
- 报告审核屏目前是两列网格，可进一步做成"左条目列表 + 右详情"的主从布局。
- `services/mdt_service.py`、`mdt_agents.py`、`models/` 为旧链路留档，不在本 PR 范围内。

## 变更文件（29 个，+6966 / −437）

```
M  README.md
R  backend/templates/index.html → _legacy/root_templates_polling/index.html
M  backend/app.py
M  backend/schemas.py
M  backend/services/auth.py
M  backend/services/case_flow.py
A  backend/services/evidence_service.py
M  backend/services/path_service.py
M  backend/services/portrait_extractor.py
A  backend/services/report_service.py
A  backend/services/review_service.py
A  backend/services/task_service.py
A  backend/services/update_service.py
A  backend/static/css/dsh.css
A  backend/templates/doctor.html
A  backend/templates/home.html
M  backend/templates/patient.html
M  backend/tests/test_auth.py
A  backend/tests/test_frontend_structure.py
A  backend/tests/test_report.py
A  backend/tests/test_review.py
M  backend/tests/test_schemas.py
M  backend/tests/test_store.py
A  backend/tests/test_update_flow.py
A  backend/verify_doctor_http.py
A  backend/verify_frontend_logic.mjs
A  backend/verify_update_http.py
M  docs/开发进度与交接.md
M  start_mdt.py
```
