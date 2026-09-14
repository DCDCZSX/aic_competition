# PR 记录 · fix(stage3-6): 报告可读性、脱敏误报、两段式提交、分析任务化（承接 PR #5）

> 分支 `fix/stage3-6-followup` → `main`　分支基点已合并 `main`（`2d5d807`）
> 本文件是该 PR 的描述正文存档（创建 PR 时逐字使用）。

---

## 前置说明（评审请先看这条）

`feat/stage3-6-review-and-ui` 的 **PR #5 已经合入 `main`**（合并提交 `2d5d807`，内容到 `604918d` 为止）。
本 PR 是那之后在**同一分支上继续做的两轮修复 + 一轮优化**，为便于追溯另开分支 `fix/stage3-6-followup`，已经先把 `main` 合并进来（合并提交无内容变更），因此**可干净合并、无冲突**。

diff 只包含下面 4 个功能/修复提交 + 1 个文档提交，**23 文件 / +2499 −212**。

## 这个 PR 做了什么

### 一、修报告页"冒出英文与 `[object Object]`"（`2ac49cb`）

现象：就医报告里「时间线 / 就医路径 / 证据卡片」直接显示 `doctor_confirmed: false; evidence_state: indirect; supports: [object Object]`。

两层根因，两层都修：

- **后端**新增**报告展示层**：时间线 / 路径步骤 / 证据卡片 / 专科视角 / 共识 / 分歧 / 不确定性 / 审核记录全部转成**中文键 → 字符串或卡片数组**，代码值翻中文（`rule-dominant` → "以规则为主（模型仅作补充）"），并**丢弃模型自报置信度**（规格书 §4.3 红线）。
- **前端** `renderSectionValue` 重写：对象 → 定义列表、对象数组 → 卡片、字符串数组 → 项目符号、布尔 → 是/否、空 →（无），**永不出现 `[object Object]`**。
- `app.json.sort_keys = False`：否则 Flask 会按码点重排段落内字段，"判断 → 依据 → 证据状态"的阅读顺序被打乱。
- 医生工作台审核记录补 `target_label`（原来直接显示 `department_selection`）。

### 二、修脱敏预览把人名误判（`b295577`，真实缺陷）

旧姓名正则 `(?:患者|病人|姓名|患者姓名)\s*[:：]?\s*([一-龥]{2,4})` 的**冒号写成了可选**，于是「患者**转运**」「患者**活动**后」「患者**出院**」「患者**反应**」都被当人名打码 —— 不只误报：**脱敏后的文本要送进分析链路**，"患者活动后气促"被改成"患者活\*\*\*"等于把病历内容改坏。

改为专用规则：①「标签 + 冒号」冒号必填 ②「姓氏 + 某/某某」③「姓氏 + 性别年龄上下文」兜底；所有候选必须过姓氏白名单（单姓/复姓）+ 非人名词表；命中规则写进 reason，预览可解释"为什么打码"。

### 三、两段式提交 + 真实进度条（`b281c12`）

实测原始耗时（逐阶段打点）：校验 0.01s / 脱敏 0.00s / **画像 1.34s** / 追问 0.00s / **S2 发言 1.93s** / 路径 0.00s / **多学科 26.54s** = **29.8s** —— 患者为"看自己的画像"白等半分钟。

- **两段式**：`defer_mdt=true` 先交付画像/追问/分诊/路径（实测 **2.6–3.6 秒**），多学科分析转后台线程续跑。
- 后端 8 阶段进度上报（`CREATE_STAGES` + `on_progress`），`?stream=1` 走 SSE 逐阶段推送。
- 患者端：提交按钮防重复点击（原来连点会建出两个病例）、进度条 + 阶段文案 + 已用秒数、"正在分析 眼科（第 2 科）· 已完成 1 / 3"、失败如实写明原因。
- 踩坑留档：SSE 响应必须带 `Connection: close`，否则流结束后客户端复用 keep-alive 连接，werkzeug 开发服务器不处理，实测挂到客户端超时。

### 四、分析任务化：等待体验 5 项优化（`c60a52c`）

| 优化 | 做法 | 实测 |
|---|---|---|
| **渐进式秒开** | `progressive:true` 只跑确定性规则（画像/追问/规则分诊/路径），模型三相后台跑并**自动替换**前台 | 首屏 **35 ms** |
| **按需跳过 MDT** | 只有 1 个就医方向时不跑会诊，如实标注 `mdt.state=skipped`，不假装分析过 | — |
| **可重试** | 相位异常标 `failed` + 原因，`/analysis/retry` 只重跑未完成相位；全完成时 400 并说明 | — |
| **医生端批处理** | `POST /api/v2/cases/analyze` 排队（≤3 例/次），列表每行带 `analysis_state` 徽章 | started 3 / skipped 1 |
| **子步骤进度** | 统一进度接口 `GET /api/v2/case/<id>/analysis`，MDT 逐科上报"正在读取资料并形成意见卡 · 眼科（第 2 科）" | 逐科 7.6s / 12.2s |

配套：后台三相任务 `profile → triage → mdt`（单相失败不连坐、状态落库、刷新不丢）、模型不可用时优雅降级并把原因写进 warnings。
后台实测（真实模型）：首屏 35 ms；profile 1.5s → triage 3.1s → MDT 逐科 7.6s/12.2s → **16.7s 全部完成、3 张意见卡就绪**。

## 验证证据（全部可复跑）

| 层 | 命令 | 结果 |
|---|---|---|
| 单元测试 | `cd CHAIS/backend && python -m unittest discover -s tests -t .` | **439 项全过** |
| 患者端 HTTP 端到端 | `python verify_update_http.py` | **72 / 0**（含渐进式首屏 35 ms、三相齐备、两段式、报告可读性、脱敏预览无误报） |
| 医生工作台 HTTP 端到端 | `python verify_doctor_http.py` | **79 / 0**（含鉴权 401/403、逐条审核、发布前置条件、批处理、分析状态徽章） |
| 前端渲染逻辑 | `node verify_frontend_logic.mjs` | **49 / 0**（Node + 最小 DOM 桩：两条轨道、进度条、报告渲染器、分析横幅） |
| 报告真实渲染 | `node verify_report_render.mjs` | 真实病例 **13 段**全部正常，无 `[object Object]`、无英文键泄漏 |
| 前端结构与合规 | `python -m unittest tests.test_frontend_structure -v` | **15 / 0**（Jinja 标记残留、三轨结构、无外链、无"演示"文案、登录页一端一入口、JSON 键序） |

## 红线与合规检查

- [x] 不出诊断结论、不出处方/剂量；医疗行为统一写"建议向医生确认是否需要 ××"
- [x] 界面与报告不展示模型自报概率/置信度（有过滤 + 单测断言）
- [x] AI 不覆盖医生；患者转述内容不作为医生意见采信
- [x] 规则结果**先于**模型结果呈现；模型只做增量补充
- [x] 降级与失败如实说明，**不用"正在分析"掩盖失败**；单科病例不假装跑过多学科
- [x] 未提交 `.env`、`*.db`、`MDT-5*.csv`、`eval_out/`、`offline_packages/`、`knowledge_base/`
- [x] 无第三方外链资源；脱敏预览**只打码、不改写**原始病历文本（有单测断言正文一字不改）

## 已知限制 / 后续

- 阶段 6.D：首页仍自带一份内联 token（未引用 `dsh.css`）；四档响应式与打印**尚未逐档像素核对**。
- 阶段 5.1-5.3：评测集扩容 n≥30、实验二/实验三、演示录屏与材料。
- 后台分析任务是**进程内线程**：重启进程未完成的任务会丢失（任务状态落库，但不会自动续跑）。

## 主要变更文件

```
M  backend/app.py                       # analysis/retry、批处理、json 键序、SSE
M  backend/services/case_flow.py        # 分析任务三相、渐进式、两段式、重试、批处理
M  backend/services/report_service.py   # 报告展示层（中文键 + 丢弃置信度）
M  backend/services/privacy.py          # 姓名识别专用规则（修误报）
M  backend/services/evidence_service.py # 证据卡去掉模型自报置信度
M  backend/services/review_service.py   # TARGET_TYPE_LABELS
M  backend/templates/patient.html       # 进度条、分析横幅、文档式报告渲染
M  backend/templates/doctor.html        # 分析状态徽章 + 批量补齐分析
M  backend/static/css/dsh.css           # 报告排版与进度组件
A  backend/tests/test_create_progress.py
A  backend/verify_report_render.mjs
M  backend/verify_{update_http,doctor_http}.py、verify_frontend_logic.mjs
M  docs/（规格书 §6.0 / 主设计方案 §5.4 §8.0 / 两份 UI 说明 / 交接文档 快照）
```
