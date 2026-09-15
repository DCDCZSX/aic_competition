# PR 记录 · fix(mdt): 修"血液科一直未完成分析"——截断的模型输出不再毁掉整科意见卡

> 分支 `fix/mdt-truncated-card` → `main`　基点 `b7fc923`（PR #6 已合入）
> 本文件是该 PR 的描述正文存档（创建 PR 时逐字使用）。

---

## 现象（用户截图发现）

某病例的多学科审阅里，**血液科一直是**：

> 血液科
> 本次未完成分析（意见卡解析失败：Expecting ',' delimiter: line 64 column 6 (char 3537)）

而且**每次都失败、也无法恢复**：点「重新分析」提示"所有分析相位都已完成，无需重试"。

## 根因（两条，都有实测证据）

### 一、`max_tokens` 硬编码 2000 + 从不检查 `finish_reason` → 输出被静默截断

血液科要写"发热查因 + 全血细胞减少"的鉴别，输出天然最长。同一条提示词实测：

| max_tokens | finish_reason | 输出 | 结果 |
|---|---|---|---|
| 2000（原代码） | **`length`（截断）** | 3780 字符，结尾停在半个字符串上 | ❌ `Expecting ',' delimiter` |
| 4000（修改后） | `stop` | 2335 token | ✅ 解析成功，卡片完整 |

库里 3 条失败记录的报错位置是 char **3537 / 3670 / 3779**——**全都在 2000 token 的截断点附近**，所以是必然失败而非偶发。

附带发现：原 `call_ai_api()` 对所有输出统一做 Markdown 清理，而清理会把 ```json 包裹的整段内容**删掉**、也会改动字符串内容——结构化输出本不该走它。

### 二、逐科失败不影响相位状态 → 永远无法重试

某个科室没跑出来时，只写了一句提示，`mdt` 相位仍记 `done`，于是：

- `POST /analysis/retry` → "所有分析相位都已完成，无需重试"（400）；
- 医生端「批量补齐分析」→ 因为"有 mdt 且已完成"被 `skipped`。

**"可见的失败"没有对应的"可操作的重试"，用户就被永久卡住。**

## 改了什么

| 层 | 改动 |
|---|---|
| 调用层 | 新增 `AIService.call_ai_api_raw()` 与 `services.ai_service.call_structured()`：结构化输出**不做 Markdown 清理**（原文返回），并返回 `finish_reason` / `truncated`；截断会打一条服务端告警便于排查 |
| 预算 | 多学科意见卡 `MDT_MAX_TOKENS = 4000`（实测需要 2335）；S2 / 质询 / 画像增强也改走结构化接口 |
| 解析层 | 新增 `services/json_recovery.py`：① 字符串与转义感知的**配平**提取；② 格式修复（全角标点、未转义内部引号、字符串内裸换行、尾逗号、注释、Python 字面量、单引号）；③ **逐字段抢救**。原则：**只取已经写完的内容，半个字符串不入库** |
| 重试策略 | 输出被截断 → 先追加"更精炼"要求**重试一次**（预算不变，靠少写换完整）→ 仍不行则用容错恢复的结果，并在卡片上如实标注"输出被长度截断，已按已写出的字段恢复" |
| 相位语义 | 逐科失败 → `analysis.mdt.failed = [科室]`、`phases.mdt.state = "partial"`（一个都没成功则 `"failed"`）、病例 `analysis.state = "partial_failed"`；**其余科室结果照常可用** |
| 可重试 | `retry_analysis` 只重跑未完成/部分完成的相位；**修复前就卡住的老病例**（没有 analysis 记录、只有 `mdt.analyses` 里的 `unavailable` 条目）也能重试；重跑前**清掉该相位的旧告警**（否则页面会一边说"已完成"一边挂着"未完成"） |
| 医生端 | 批处理认"部分未完成"；`running` 超 10 分钟视为线程已丢、允许重排 |
| 前端 | 患者端相位「部分完成」徽章、多学科页就地「重新分析未完成的科室」、修复过的卡片显示"说明"；医生端失败科目标注原因 |

## 验证

| 层 | 命令 | 结果 |
|---|---|---|
| 单元测试 | `cd CHAIS/backend && python -m unittest discover -s tests -t .` | **478 项全过**（新增 28 项） |
| 患者端 HTTP | `python verify_update_http.py` | **75 / 0** |
| 医生端 HTTP | `python verify_doctor_http.py` | **81 / 0** |
| 前端渲染逻辑 | `node verify_frontend_logic.mjs` | **49 / 0** |
| 报告真实渲染 | `node verify_report_render.mjs` | 13 段全部正常 |

**真实模型验收（本次修复的直接证据）**：对库里 3 例血液科失败病例逐个执行「重新分析」→
**3/3 科室全部 `method=llm`（干净输出，未触发修复）**，每科 6 事实 / 4 推断 / 5 检查 / 6 缺口，
陈旧告警清零，病例状态回到「分析就绪」。

新增测试要点：在**任意位置截断**的 JSON 都能恢复出已写完的字段、且不产生半个字符串；
截断会被识别并触发"精炼重试"；多学科预算为 4000；逐科失败 → partial + 可重试 + 老病例可重试 + 旧告警被清。

## 红线与合规

- [x] 不诊断、不处方；检查项一律"建议向医生确认是否需要…"
- [x] 不展示模型自报概率/置信度
- [x] 恢复只做**格式**处理，**绝不编造内容**：没有写出来的字段就是没有；半句话不入库
- [x] 失败如实说明（区分"被长度截断"与"格式异常"），不用"正在分析"掩盖，也不显示成"全部完成"
- [x] 未提交 `.env`、`*.db`、`MDT-5*.csv`、`eval_out/`、离线包、知识库

## 文档同步

`产品开发规格书` §6.0（新增"结构化输出预算"、"逐科失败可见且可重试"两行）与 §8 降级表、
`产品设计方案 v3.0` §8.0、`UI设计说明-患者端`（P4/P8）、`UI设计说明-医生端审核台`（屏 A/屏 E）、
`开发进度与交接` §8 踩坑纪律（两条新教训）+ 变更日志。

## 主要变更文件

```
M  backend/services/ai_service.py          # call_ai_api_raw / call_structured / 截断检测
A  backend/services/json_recovery.py       # 容错解析三层（配平→修复→抢救）
M  backend/services/mdt_analyst.py         # 预算 4000、精炼重试、修复标注、notice 透出
M  backend/services/case_flow.py           # 逐科失败 → partial/failed、可重试、清旧告警、批处理
M  backend/services/s2_analyst.py          # 接入容错解析 + 截断提示
M  backend/services/challenge_service.py   # 同上
M  backend/services/portrait_extractor.py  # 同上（失败原因如实写进 warnings）
M  backend/templates/patient.html          # 部分完成徽章、就地重试、"说明"行
M  backend/templates/doctor.html           # 失败科目标注原因、修复说明
A  backend/tests/test_json_recovery.py     # 19 项（含"任意位置截断"的性质测试）
A  backend/tests/test_ai_service.py        # 11 项（预算/截断/不做 Markdown 清理）
M  backend/tests/test_mdt_analyst.py       # +6 项（截断恢复、精炼重试、全角标点…）
M  backend/tests/test_create_progress.py   # +3 项（partial、可重试、老病例、批处理）
M  backend/verify_{update,doctor}_http.py  # 新增 failed 列表与相位取值断言
M  docs/（规格书 / 主设计方案 / 两份 UI 说明 / 交接文档 快照）
```
