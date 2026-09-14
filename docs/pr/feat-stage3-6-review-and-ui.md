# PR 记录 · feat(stage3-6): 证据链闭环、医生工作台、产品首页与界面统一、分析任务化

> 分支 `feat/stage3-6-review-and-ui` → `main`　最新提交 `c60a52c`
> 本文件是该 PR 的描述正文存档（创建 PR 时逐字使用），便于日后回溯"这次交付了什么、怎么验证的"。

---

## 这个 PR 做了什么

分七块，全部是可复跑验证的功能增量。

### 一、阶段 3 证据链与报告体系（`f59678b`，本次一并带入）
- **证据板** `services/evidence_service.py`：时间线（症状/疾病/检查事件，带 `source_refs`）+ 判断↔证据关联（direct / indirect / missing / conflict）+ 证据卡支持/反对双列；外部依据只认白名单 8 条（教材/共识），未命中如实写"暂无可靠外部依据"——**不接 RAG、不伪造文献**。
- **报告体系** `services/report_service.py`：一页就医摘要（8 项）+ 完整报告（13 段）；未审核角标、免责声明、能力边界必带。

### 二、阶段 3.4 / 3.6 任务闭环与就诊后回路
- `services/task_service.py`：任务状态机（显式转移表，非法转移报错，每次流转留痕）。
- `services/update_service.py`：就诊后资料更新——依赖矩阵定受影响科室 → **只重算分诊与路径** → 新资料填补对应缺口 → 变化摘要。
- 🔴 **修复红线缺陷**：患者转述的"医生说……"原先会被记成已验证的医生确认意见；现在原样保留来源、`is_verified=false`、不进"一致"列表。

### 三、阶段 4 医生工作台（屏 A-G）
- `services/review_service.py`：决策条目枚举（科室入选 / 路径步骤 / 共识分歧 / 患者建议）+ 逐条审核状态机 + 发布条件 + 概率数字过滤（只处理"概率/置信度 + 数字"，不动真实检验值）。
- 5 个接口 + `_guard_doctor()`：医生端**始终**要求医生角色（未登录 401、患者角色 403）。
- 发布审核版：全部条目终态才可发布 → `doctor_confirmed`、版本 +1、角标转绿；`accept_all` 也逐条留痕。

### 四、阶段 5.4 / 5.6 / 6 对外呈现与界面统一
- README 按真实产品重写（清除旧产品名与不存在的 WebSocket/React 能力）。
- **对外呈现口径**：全站去掉"演示/demo"痕迹；账号统一 `patient` / `doctor`；入口统一「患者端 / 医生工作台」。
- `templates/home.html`：暖色编辑风产品首页（固定顶栏下拉 / Hero 双入口 / 能力与流程 / 场景标签页 / 安全护栏 / 评测与可信度 / FAQ / 深色页脚），零外链、零构建、自绘 SVG。
- `static/css/dsh.css`：三页共用设计系统（暖色 token + 三轨外壳 + 组件库）。
- 患者端三轨工作台（左流程导航 / 中工作区 / 右病例上下文）；医生工作台左轨病例列表 + 右轨常驻审核面板。
- 登录页全屏两栏 + **一端一入口**（修掉"内容堆在左上角"）。

### 五、修复报告页"冒出英文与 `[object Object]`"（`2ac49cb`）
- 后端新增**报告展示层**：时间线 / 路径 / 证据卡 / 专科视角 / 共识分歧 / 审核记录全部转成中文键结构，代码值翻中文，并**丢弃模型自报置信度**（规格书 §4.3 红线）。
- 前端 `renderSectionValue` 重写：对象→定义列表、对象数组→卡片、布尔→是/否、空→（无），**永不出现 `[object Object]`**。
- `app.json.sort_keys = False`：否则 Flask 会按字母重排段落内字段，阅读顺序被打乱。

### 六、修复脱敏预览把人名误判（`b295577`）
- 旧姓名正则的**冒号是可选的**，于是正文里的「患者转运」「患者活动后」「患者出院」「患者反应」都被当人名打码——不只误报，还会把送进分析链路的病历文本改坏。
- 改为专用规则：① 标签 + **冒号必填** ② 姓氏 + 某 ③ 姓氏 + 性别年龄上下文；所有候选必须过姓氏白名单 + 非人名词表；命中规则写进提示（"命中规则：姓名标签…"）。

### 七、等待体验：两段式 → 渐进式分析任务（`b281c12`、`c60a52c`）
实测原始耗时：校验 0.01s / 脱敏 0.00s / **画像 1.34s** / 追问 0.00s / **S2 发言 1.93s** / 路径 0.00s / **多学科 26.54s** = **29.8s**，患者为"看自己的画像"白等半分钟。

- **渐进式秒开**：`progressive:true` 只跑确定性规则（画像抽取 / 追问 / 规则分诊 / 路径），**实测首屏 35 ms**；模型三相后台跑，结果到达**自动替换**前台。
- **后台三相任务**：`profile → triage → mdt`，单相失败不连坐；相位状态 / 时间 / 失败原因全部落库，刷新不丢。
- **统一进度接口** `GET /api/v2/case/<id>/analysis`：三相 + MDT 第几科 + 子步骤（"正在读取资料并形成意见卡"）+ 已就绪结果 + warnings。
- **可重试** `POST /api/v2/case/<id>/analysis/retry`：只重跑未完成相位；全完成时 400 并说明"无需重试"。
- **按需跳过 MDT**：只有 1 个就医方向时不跑会诊，如实标注"已跳过"，不假装分析过。
- **医生端批处理** `POST /api/v2/cases/analyze`：未完成病例排队（≤3 例/次），列表每行带 `analysis_state` 徽章。
- **优雅降级**：模型不可用时相位仍收尾，但 warnings 如实写明降级原因。

## ⚠️ 范围说明（评审请先看这条）

`main` 当前只到**阶段 0-2**：PR #4 合进去的是 `a2d06d1`，而 `f59678b`（阶段 3 证据板 + 报告体系）当时**没被合进去**。所以这个 PR 从**阶段 3 起全部包含**，diff 比分支名看起来更大——这是刻意的，不是混入了无关改动。

## 验证证据（全部可复跑）

| 层 | 命令 | 结果 |
|---|---|---|
| 单元测试 | `cd CHAIS/backend && python -m unittest discover -s tests -t .` | **439 项全过** |
| 患者端 HTTP 端到端 | `python verify_update_http.py` | **72 / 0**（含渐进式首屏 35 ms、三相齐备、两段式、报告可读性、脱敏预览无误报） |
| 医生工作台 HTTP 端到端 | `python verify_doctor_http.py` | **79 / 0**（含鉴权 401/403、逐条审核、发布前置条件、批处理、分析状态徽章） |
| 前端渲染逻辑 | `node verify_frontend_logic.mjs` | **49 / 0**（Node + 最小 DOM 桩：两条轨道、进度条、报告渲染器、分析横幅） |
| 报告真实渲染 | `node verify_report_render.mjs` | 真实病例 **13 段**全部渲染正常，无 `[object Object]`、无英文键泄漏 |
| 前端结构与合规 | `python -m unittest tests.test_frontend_structure -v` | **15 / 0**（Jinja 标记残留、三轨结构、业务屏必须在 `main` 内、无外链、无"演示"文案、登录页一端一入口、JSON 键序） |

页面可达性：`/`、`/patient`、`/doctor`、`/static/css/dsh.css` 均 200；旧路由 `/legacy` 已 404。

## 红线与合规检查

- [x] 不出诊断结论、不出处方/剂量；医疗行为统一写"建议向医生确认是否需要 ××"
- [x] 界面与报告不展示模型自报概率/置信度（有过滤 + 单测断言）
- [x] AI 不覆盖医生：医生意见与 AI 建议并列；患者转述内容不作为医生意见采信
- [x] 规则结果**先于**模型结果呈现；模型只做增量补充，不推翻规则命中的强项
- [x] 降级与失败如实说明，**不用"正在分析"掩盖失败**；单科病例不假装跑过多学科
- [x] 未提交 `.env`（密钥）、`*.db`（病例数据）、`MDT-5*.csv`（金标准）、`eval_out/`、`offline_packages/`、`knowledge_base/`
- [x] 无第三方外链资源、无第三方品牌素材；旧产品名与旧 UI 不再对外路由

## 界面变化（建议这样看）

1. `cd CHAIS/backend && python app.py` → http://127.0.0.1:5000/
2. 首页 → 「进入患者端」→ 目标 → 录入 → 脱敏预览（看"共识别 N 处敏感信息"）→ **点提交几乎瞬间进画像确认**，看屏顶「分析进度」三相位逐个变绿、右轨科数进度条推进
3. 追问 → 就医方向（三栏流水线）→ 路径任务 → 多学科（此时已就绪）→ 报告（"完整报告"左目录 + 右正文，滚动高亮）→ 就诊后更新 → 变化摘要
4. 退出 → http://127.0.0.1:5000/doctor（`doctor` 账号）→ 病例列表看分析状态徽章 →「批量补齐分析」→ 分诊依据 → 多学科审阅（▶ 播放过程）→ 报告审核（接受/修改/驳回 → 全部接受并发布）→ 审核记录

## 已知限制 / 后续

- 阶段 6.D：首页仍自带一份内联 token（未引用 `dsh.css`）；四档响应式与打印**尚未逐档像素核对**。
- 阶段 5.1-5.3：评测集扩容 n≥30、实验二/实验三、演示录屏与材料。
- 后台分析任务是**进程内线程**：重启进程未完成的任务会丢失（任务状态落库，但不会自动续跑）。
- 报告审核屏目前是两列网格，可进一步做成"左条目列表 + 右详情"主从布局。
- `services/mdt_service.py`、`mdt_agents.py`、`models/` 为旧链路留档，不在本 PR 范围内。

## 主要变更文件

```
M  README.md
R  backend/templates/index.html → _legacy/root_templates_polling/index.html
M  backend/app.py                              # v2 路由、SSE、analysis/retry、批处理、json 键序
M  backend/services/case_flow.py               # 分析任务三相、渐进式、两段式、重试、批处理
A  backend/services/evidence_service.py        # 证据板
A  backend/services/report_service.py          # 摘要 + 完整报告 + 展示层
A  backend/services/review_service.py          # 医生端逐条审核
A  backend/services/task_service.py            # 任务状态机
A  backend/services/update_service.py          # 就诊后更新 + 变化摘要
M  backend/services/evidence_service.py        # 证据卡不再暴露模型自报置信度
M  backend/services/privacy.py                 # 姓名识别专用规则（修误报）
A  backend/static/css/dsh.css                  # 三页共用设计系统
A  backend/templates/home.html                 # 产品首页
M  backend/templates/patient.html              # 三轨工作台 + 渐进式进度 + 文档式报告
A  backend/templates/doctor.html               # 医生工作台屏 A-G
A  backend/tests/{test_report,test_review,test_update_flow,test_create_progress,test_frontend_structure}.py
A  backend/verify_{update_http,doctor_http,frontend_logic,report_render}.{py,mjs}
M  docs/（规格书 / 主设计方案 / 两份 UI 说明 / 交接文档 快照）
M  start_mdt.py
```
