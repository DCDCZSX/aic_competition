# aic_competition-main-3 更新说明

更新日期：2026-09-16

本版本基于 `aic_competition-main-2` 创建，目标是修复上一版仍存在的业务错误与安全风险，同时补齐独立运行环境、自动化测试和项目说明。`main-1` 与 `main-2` 未被修改。

## 一、核心更新概览

本次主要完成以下改进：

1. 强化账号认证与病例所有权隔离；
2. 下线可绕过新版流程的旧 HTTP API；
3. 限制跨域访问来源，并移除 URL 中的 token；
4. 修复 SQLite JSON 并发更新可能互相覆盖的问题；
5. 强制病例状态按合法状态机逐步流转；
6. 修复 MDT 实际参与科室与页面分诊结果不一致的问题；
7. 阻止同一病例重复启动后台分析任务；
8. 拒绝内容为空的 MDT 专科意见卡；
9. 缩短并拆分 AI 请求连接和读取超时；
10. 建立 `main-3` 独立虚拟环境并补充安全回归测试。

## 二、认证与权限修复

### 2.1 移除代码中的默认密码

患者和医生密码不再硬编码在 Python 代码中，统一从 `.env` 读取：

- `APP_PATIENT_USERNAME`
- `APP_PATIENT_PASSWORD`
- `APP_DOCTOR_USERNAME`
- `APP_DOCTOR_PASSWORD`

未配置密码时，对应账号不能登录。密码比较使用恒定时间比较函数，降低字符串比较带来的侧信道风险。

### 2.2 默认开启认证

`APP_AUTH_REQUIRED` 现在默认是 `true`。未登录访问患者病例接口时返回 `401`。

免密角色登录由 `APP_ALLOW_ROLE_LOGIN` 控制，默认是 `false`。患者端、医生端和首页不再通过 `{role: "patient"}` 或 `{role: "doctor"}` 直接换取会话。

### 2.3 token 只允许放在 Authorization 头

接口只接受：

```http
Authorization: Bearer <token>
```

不再接受 `?token=...` 查询参数，也不接受没有 `Bearer` 前缀的原始 token，避免 token 出现在浏览器历史、代理日志和服务器访问日志中。

### 2.4 增加病例所有权

SQLite 的 `cases` 表新增 `owner_id` 字段，并包含旧数据库自动迁移逻辑。

权限规则如下：

- 患者只能访问 `owner_id` 与当前登录账号一致的病例；
- 患者访问他人病例时返回 `404`，避免泄露病例是否存在；
- 医生凭授权 scope 可以访问病例；
- 旧数据库中 `owner_id` 为空的病例仅医生可访问，不会自动归属给任意患者；
- 创建病例时，所有者由服务端当前会话决定；客户端传入的 `case_id` 会被忽略。

## 三、旧接口与跨域安全

### 3.1 下线旧版 API

以下旧接口已经从 Flask 路由中删除：

```text
/api/start_consultation
/api/get_discussion_status/<id>
/api/get_all_discussions
```

这些接口属于旧内存会诊链路，未经过新版病例所有权、脱敏和状态机约束，继续暴露会形成安全旁路。现在访问这些地址会返回 `404`。

### 3.2 限制 CORS 来源

CORS 不再允许任意来源。允许的来源由 `APP_CORS_ORIGINS` 配置，默认仅包含：

```text
http://127.0.0.1:5000
http://localhost:5000
```

任意第三方 Origin 不会被服务器反射到 `Access-Control-Allow-Origin`。

## 四、数据一致性与并发修复

### 4.1 SQLite JSON 原子更新

病例的主要业务数据保存在 `payload` JSON 字段中。旧实现中，不同线程可能分别读取旧 payload、各自修改后再整体写回，导致后写入的一方覆盖先写入的数据。

本版本增加 `Store.mutate_case()`：

- 在同一把可重入锁中完成读取、修改、序列化和写入；
- 分析进度、告警等局部更新改用原子修改；
- 增加并发计数测试验证多线程更新不会丢失。

### 4.2 同一病例操作串行化

`CaseFlow` 为每个病例维护独立的可重入锁，对以下关键操作进行串行化：

- 提交追问答案；
- 启动和执行后台分析；
- 更新任务状态；
- 添加就诊后资料；
- 提交医生审核决定；
- 发布审核版报告。

这样可以降低同一病例在分析、更新和审核同时发生时的数据覆盖风险。

### 4.3 防止重复后台任务

新增活动任务注册表。若某病例已有仍在运行的后台分析线程，再次启动会返回失败，不会重复调用模型或并发写入同一病例。

任务结束后会自动从活动任务表清理，使正常重试仍然可用。

## 五、病例状态机修复

新增 `Store.transition_case()`，状态更新必须遵循 `services/case_state.py` 中定义的合法状态关系。

当业务目标状态需要经过多个中间状态时，系统会逐步写入状态和流转日志。例如发布审核版报告时记录：

```text
awaiting_doctor -> doctor_reviewed -> doctor_confirmed
```

不再直接记录：

```text
awaiting_doctor -> doctor_confirmed
```

此外，为支持合法的分析重试，增加了：

```text
awaiting_doctor -> analyzing
```

### 就诊后更新状态修正

旧逻辑会把尚未经过医生确认的病例在患者补充资料后直接变成 `awaiting_recheck`。本版本改为：

- 当前状态为 `doctor_confirmed`：补充资料后进入 `awaiting_recheck`；
- 尚未医生确认：保持或回到 `awaiting_doctor`。

避免产生“尚未确认却等待重新审核”的业务矛盾。

## 六、MDT 业务修复

### 6.1 MDT 使用最终持久化分诊结果

旧流程在后台 MDT 阶段会重新运行一遍纯规则分诊，可能丢失 S2 模型受限发言补充的科室，形成：

```text
页面展示的科室 != 实际参与 MDT 的科室
```

本版本直接读取并使用上一阶段已经持久化的 `triage.selected`，确保界面、报告与 MDT 分析使用同一组科室。

对于旧病例缺少 `behavior_type` 的情况，只要已有多个持久化入选科室，就不会误判为 A 型单科病例而跳过 MDT。只有明确标记为 A 型时才跳过。

### 6.2 拒绝空意见卡

模型返回 `{}`、只有非法转诊字段或清洗后没有任何有效内容时，不再生成“成功”的专科卡片。

一张有效意见卡至少需要包含以下一种有意义内容：

- 关注点；
- 病例事实；
- 分析推断；
- 待确认检查；
- 信息缺口。

空卡会被记录为该专科分析失败，并进入已有的部分失败与重试机制。

## 七、AI 请求超时优化

原版本模型请求默认超时过长，接口异常时可能等待很久。本版本拆分为：

```dotenv
AI_CONNECT_TIMEOUT=10
AI_READ_TIMEOUT=60
```

- 连接超时默认 10 秒；
- 响应读取超时默认 60 秒；
- 旧的 `AI_TIMEOUT` 只作为读取超时的兼容配置；
- 请求仍保留有限次数的重试与指数退避。

## 八、前端登录调整

患者端和医生端的角色卡不再发起免密登录请求，而是：

- 展开或聚焦账号密码表单；
- 自动填写对应的默认账号名；
- 要求用户输入 `.env` 中配置的密码。

首页“进入患者端”和“进入医生工作台”按钮只负责导航到对应页面，不再自动创建会话。

页面中“密码同账号名”的旧文案已经移除。

## 九、环境配置

`main-3` 已建立独立虚拟环境：

```text
aic_competition-main-3/.venv
```

依赖来源为项目的精简依赖清单：

```text
Flask==2.3.3
Flask-CORS==4.0.0
requests==2.31.0
markdown==3.5.1
python-dotenv==1.0.0
```

由于本机 PyPI 代理存在 TLS 证书链错误，无法安全联网安装，因此使用同一台电脑上 `main-2` 已验证、版本完全一致的 Python 3.12.12 虚拟环境包进行离线配置。最终导入路径已确认指向 `main-3/.venv`，并且 `pip check` 输出：

```text
No broken requirements found.
```

## 十、本机配置与启动

根目录已创建被 `.gitignore` 忽略的 `.env`。当前本机账号为：

```text
患者账号：patient
患者密码：AicPatient-2026!

医生账号：doctor
医生密码：AicDoctor-2026!
```

这些密码只用于当前本机运行。正式展示或部署前应修改 `.env` 中的密码。

当前未填写 `AI_API_KEY`，因此模型阶段会安全降级，规则分诊和其他不依赖模型的功能仍可运行。

启动命令：

```powershell
Set-Location "C:\Users\lile1\Desktop\aic的比赛\aic_competition-main-3\backend"
..\.venv\bin\python.exe app.py
```

访问地址：

- 首页：<http://127.0.0.1:5000/>
- 患者端：<http://127.0.0.1:5000/patient>
- 医生工作台：<http://127.0.0.1:5000/doctor>

## 十一、测试与验证结果

### 11.1 自动化测试

使用 `main-3` 自己的虚拟环境运行：

```powershell
Set-Location backend
..\.venv\bin\python.exe -m unittest discover -s tests -t . -q
```

结果：

```text
Ran 521 tests
OK
```

相较原有 501 项测试，新增了 20 项左右的安全、并发和业务回归覆盖，包括：

- 默认认证与禁用免密角色登录；
- 未配置密码时拒绝登录；
- URL token 不生效；
- 患者横向越权访问被拒绝；
- 无主旧病例仅医生可访问；
- 创建病例时写入所有者；
- 客户端不能指定病例号；
- 旧 API 返回 404；
- 任意 Origin 不被 CORS 反射；
- 旧数据库自动增加 `owner_id`；
- JSON payload 并发更新不丢失；
- 状态机逐跳流转；
- 重复后台任务被拒绝；
- MDT 使用持久化的最终科室；
- 空 MDT 卡被判为失败；
- AI 连接和读取超时生效。

### 11.2 真实 HTTP 冒烟测试

真实启动 Flask 服务后验证结果：

| 验证项 | 结果 |
|---|---:|
| 首页访问 | 200 |
| 患者登录 | 成功 |
| 创建病例 | 成功 |
| 客户端自定义病例号 | 已忽略 |
| 病例所有者 | `patient` |
| 医生读取病例 | 200 |
| 未登录读取病例 | 401 |
| URL token 读取病例 | 401 |
| 旧接口访问 | 404 |
| 恶意 Origin 被反射 | 否 |

冒烟测试创建的数据库已经删除，随后重新启动了空数据库服务，交付时病例数为 0。

## 十二、涉及的主要文件

核心实现：

- `backend/app.py`
- `backend/schemas.py`
- `backend/services/auth.py`
- `backend/services/store.py`
- `backend/services/case_state.py`
- `backend/services/case_flow.py`
- `backend/services/ai_service.py`
- `backend/services/mdt_analyst.py`

前端模板：

- `backend/templates/home.html`
- `backend/templates/patient.html`
- `backend/templates/doctor.html`

新增测试：

- `backend/tests/test_security_access.py`
- `backend/tests/test_case_flow_jobs.py`

同步更新：

- `.env.example`
- `.gitignore`
- `requirements.txt`
- `README.md`
- `docs/开发进度与交接.md`

## 十三、仍需注意的边界

本版本已经解决本轮识别到的业务错误与安全风险，但仍属于比赛和单机部署形态：

1. 会话保存在进程内存中，服务重启后需要重新登录；
2. 多实例部署时必须改用 Redis 等集中式会话存储；
3. 本地账号系统不等同于医疗机构统一身份认证；
4. SQLite 适合当前单机规模，高并发正式环境应评估 PostgreSQL 等数据库；
5. 未配置 `AI_API_KEY` 时不会产生真实模型意见卡；
6. 本系统仍是就医信息整理和决策辅助工具，不提供诊断、处方，也不能替代医生判断。

