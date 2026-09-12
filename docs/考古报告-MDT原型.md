# MDT 原型代码考古报告

> 对象：`E:\aic_competition\CHAIS`（Flask + React 项目，含 136 个文件）
> 目的：回答备赛 8 问、摸清"原型离获奖作品差哪三块砖"、定位评审硬伤与安全项

---

## 一、一句话结论

**这是一个能端到端跑通的"三智能体（主控/专科/整合）LLM 会诊原型"，底子比想象的好；
但存在"文档与实现严重不符 + 多角色套壳风险 + 合规缺失"三大硬伤，
且有闲置的 7MB 教科书语料与真实 MDT 病例可挖——升级路径清晰。**

## 二、8 个考古问题的答案

| # | 问题 | 考古结论 |
|---|---|---|
| 1 | 代码在哪 | `CHAIS/`，本次已完整拷入，可用 ✅ |
| 2 | 输入形态 | **单次文本提交**：`medical_history`（病历全文）+ `main_issue`（主要会诊问题），另带姓名/年龄/性别；非多轮对话 |
| 3 | 输出形态 | ①主控选定的科室名单 → ②各专科意见（长文本）→ ③协调整合的 MDT 结论（Markdown 长文：综合答案/诊断分析/治疗方案/协作计划/检查监测/风险预后/随访） |
| 4 | 分诊逻辑 | **"LLM JSON 输出 + 关键词规则回退"双通道**（`mdt_agents.py`）：controller_agent 让 LLM 返回 `selected_specialties/primary/secondary/reasoning`，解析失败或不足 3 科时回退到 `keyword_mapping` 正则规则（14 类），再保底补 3 科。**这已经是"分诊引擎"的雏形** |
| 5 | 模型与角色 | 全部智能体 = **同一个模型换 role prompt**：`ai_service.py` 硬编码 `qwen-max-latest`（`config.py` 里的 `Deepseek-r1-250528` 从未被引用，配置两处不一致）；OpenAI 兼容网关 `openai.uniin.cn`；temperature 0.3 / max_tokens 2000 / 重试 3 次指数退避 |
| 6 | 技术栈 | 后端 Flask 2.3.3 + Flask-CORS + requests；**无数据库**（会诊会话存内存 dict，重启即丢）。前端**两代并存**：React 版（`frontend/`）调用 `/api/conduct_discussion`——**该接口在后端不存在，是坏的、未使用**；实际可用的是内嵌 HTML 版 `backend/templates/index.html`（轮询 `/api/get_discussion_status`）；根目录 `templates/index.html` + `app/session.py` 是更老的 WebSocket 版（路由与当前后端不匹配，遗留） |
| 7 | 旧数据源 | `MDT-5.csv`：**5 个真实脱敏 MDT 病例**（列含 `会诊专家` = 参与科室金标准名单）；`MDT-5-test*.csv`：当年批处理跑过的输出（`主控体选择专科/各专科结论/最终整合结论` 已填充） |
| 8 | 做过什么测试 | `batch_mdt_processor.py` 批处理跑过两轮（test/test1 两个输出），但**无金标准对比、无定量指标**，属"冒烟测试"非"评测" |

## 三、代码里已有的"好底子"（升级时别推倒）

1. **36 个专科字典** + 中文名映射（`specialists`）
2. **三智能体角色框架**已成：controller（选科）/ specialist（专科意见）/ coordinator（整合）
3. 每专科的**分析重点提示词**（`_get_specialty_focus`，14 科有定制）
4. **LLM 失败回退关键词规则** —— 分诊引擎雏形
5. controller 已约定**结构化 JSON 输出**（可扩展 primary/secondary/reasoning）
6. **批处理管线**（CSV→逐例→结果 CSV）→ 评测脚本可直接挂载
7. **5 例真实 MDT 病例 + 科室金标准** → 分诊评测种子数据
8. `knowledge_base/`：**内/外/诊断学教科书全文 7MB+**（目前闲置，见硬伤④）

## 四、评审视角硬伤清单（按严重度）

1. 🔴 **文档与实现严重不符**：README 声称的 WebSocket 实时推送、`/mdt-consultation`、`/search-knowledge`、并行会诊、知识库"自动索引检索"——**代码里都没有**。答辩被追问必翻车；材料里绝不能照抄 README。
2. 🔴 **"多角色套壳"风险**：同一个 qwen-max 换 prompt；无共享"病例画像"事实层、无跨科信息传递、无分歧检测；`relevance` 相关度是靠关键词猜的（"高/中/低"字符串匹配）。这就是评分表"创新性 0-6 分档"的形状。
3. 🔴 **合规缺失**：全代码无免责声明、无"辅助参考/请线下就医"话术；无数据脱敏处理痕迹；演示若用真实病例需改写。
4. 🟠 **知识库闲置**：7MB 教科书全文在 `knowledge_base/`，但 grep 证实**没有任何代码读取它**（只有 `config.KNOWLEDGE_DIR` 一个字符串）。要么做真 RAG 用起来，要么材料里别提。
5. 🟠 **API Key 明文**：`ai_service.py` 与 `app/config.py` 各硬编码一份相同明文凭据；且两处模型名不一致。提交前必须改为环境变量（见 §六）。
6. 🟠 **前端两代并存且 React 版是坏的**：调用不存在的接口；根目录与 backend 下两份 templates。清理并明确"哪套是参赛版"。
7. 🟡 会诊过程**无审计落盘**；会话存内存，演示中后端重启即全部丢失。

## 五、与《MDT 架构升级设计方案》的映射（哪些已有种子）

| 升级部件 | 代码现状 | 动作 |
|---|---|---|
| ① 病例画像抽取器 | ❌ 不存在 | 新建：从 medical_history 抽结构化画像（0→1） |
| ② 分诊决策引擎 | 🟡 已有雏形（LLM JSON + 关键词回退） | 升级：规则库扩充 + 可解释理由输出 + 与金标准对比评测 |
| ③ 会诊编排器 | 🟡 已有串行编排 for 循环 | 加：共享画像约束、跨科传递、分歧检测、防幻觉规则 |
| 评测挂载点 | 🟢 批处理管线现成 | 写 eval 脚本：controller 选科 vs `会诊专家` 金标准（需先建**科室名归一化映射**：心内科→心血管内科、呼吸科→呼吸内科、风湿科→风湿免疫科、血液科→血液内科…） |
| 演示案例 | 🟢 5 例真实病例 | 改写脱敏后作演示种子 + 外部公开病例扩充 |

## 六、提交前必做的安全/合规项

1. **轮换 API Key**：把两份明文 key 从代码移除，改读 `.env` 环境变量，`.env` 不入代码包；该 key 已随目录复制多份，建议在官方控制台**直接作废重建**。
2. **病例数据脱敏**：MDT-5.csv 若含可识别患者信息，任何对外材料只用改写示例，并注明"数据已脱敏、仅用于技术验证"。
3. **文档诚实化**：以真实代码为准重写 README/答辩口径（删掉 WebSocket/并行/知识库检索等不存在能力），或把能力真做出来再写。

## 七、模型调用事实备忘（写技术方案用）

- 网关：`https://openai.uniin.cn/openapi/v2/chat/completions`（OpenAI 兼容）
- 实际运行模型：`qwen-max-latest`（ai_service.py 为准；config 里的 Deepseek-r1-250528 未生效）
- 温度 0.3、max_tokens 2000、超时 3000s、重试 3 次（1s/2s/4s 退避）
- 输出经 `markdown_to_text` 清洗；JSON 用 `extract_json_from_text`（正则兜底截取 `{}`）
