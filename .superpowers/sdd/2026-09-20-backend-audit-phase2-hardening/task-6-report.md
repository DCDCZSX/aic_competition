# Task 6 真实 HTTP 验收报告

日期：2026-09-20
工作树：`CHAIS/.worktrees/backend-review-phase2`
范围：仅执行既有 live verification scripts 并记录证据；未修改验证脚本或产品代码。

## 结论

真实 HTTP acceptance **未整体通过，状态为 blocked**。

- 医生端 HTTP 校验通过：exit 0，90 PASS / 0 FAIL。
- 患者更新 HTTP 校验未通过：exit 1，69 PASS / 6 FAIL。
- 报告渲染的业务内容检查通过，但命令两次均以相同的 Windows Node 运行时 assertion 非零码退出，不能按脚本 exit 结果标记通过。
- 本次使用空 `AI_API_KEY`，确实覆盖了 no-key fallback 路径；由于现有患者更新脚本仍要求部分模型相位“running/全科成功”，不能把本次结果称为“无 Key fallback 模式通过”。

## 隔离运行条件

- 由本工作树的 `backend/app.py` 启动独立服务，工作目录置于仓库外的系统临时目录，避免从仓库 cwd 自动加载 `.env`。
- `APP_DB_PATH` 指向本次临时 SQLite；服务 stdout/stderr 和三个脚本日志也只写入该临时目录。
- 绑定 `127.0.0.1:5000`，因为两个 Python 校验器固定使用该地址；启动前确认端口空闲。
- `APP_DEBUG=false`。
- 仅使用本次进程环境内的非生产测试账号；凭据未写入任何跟踪文件或报告。
- `AI_API_KEY` 显式为空；未使用真实 AI key，也未调用真实模型端点。
- 健康检查：`GET http://127.0.0.1:5000/` 返回 HTTP 200，用时约 0.69 秒。
- 服务启动日志确认：debug 关闭，且提示未配置 `AI_API_KEY`；未观察到 Python traceback。

## Live 命令结果

### `backend/verify_update_http.py`

命令：`python backend/verify_update_http.py`
退出码：`1`
结果：`69 PASS / 6 FAIL`

失败摘要（已省略账号、病例标识、完整输入和响应 payload）：

1. 医生会话提交线下意见未成功：当前授权约束要求患者先显式授权医生，该脚本在医生登录后没有为同一病例执行授权步骤，因此医生写入被拒；后续“意见进入 agree”与更新累计数断言随之失败。
2. no-key fallback 下 MDT 查询未观察到 `running` 状态，直接观察到 `partial_failed`。
3. no-key fallback 下 MDT `failed` 列表非空，与脚本“全部科室成功、failed 为空”的断言不符。
4. no-key fallback 下重试接口返回可重试结果，而脚本期待“全部完成后 400 拒绝重试”。

这些是可复现的 live blockers；本次没有修改脚本。第一项需要决定是补脚本授权步骤还是调整脚本对当前显式授权契约的期望；后三项需要明确 no-key fallback 的验收口径，不能用“无 Key”推导出完整模型成功。

### `backend/verify_doctor_http.py`

命令：`python backend/verify_doctor_http.py`
退出码：`0`
结果：`90 PASS / 0 FAIL`

该脚本自行创建病例并执行显式病例授权后完成医生端访问、审核、决策、发布与审计相关检查。

### `backend/verify_report_render.mjs`

命令：`node backend/verify_report_render.mjs`，随后原命令无改动重跑一次。
两次退出码均为：`-1073740791`
两次业务输出均包含：13 个报告段落；折叠段落/目录链接/正文容器/摘要提示均为 `13 / 13 / 13 / 13`；结论为全部段落渲染正常，且无 `[object Object]`、无英文键泄漏；未出现 `❌`。

但两次结论后均出现相同运行时错误：

`Assertion failed: !(handle->flags & UV_HANDLE_CLOSING), file src\\win\\async.c, line 76`

因此报告渲染的页面内容断言可作为部分证据，命令本身仍是失败状态。该现象在 Node `v24.14.0` Windows 环境下重复出现；未改动脚本。

## 清理与安全核对

- 仅停止本次启动的服务 PID；未停止或操作其他进程。
- 已删除本次创建的临时 SQLite 主文件、`-shm`/`-wal` sidecar、服务日志和三个脚本日志；临时目录已移除。
- 仓库 `backend/data/mdt.db` 仍存在，核对大小为 61440 字节，时间戳保持为本次运行前的值；没有把服务指向仓库默认 DB。
- 未使用仓库 `.env`、生产凭据、真实模型 key；报告不包含账号密码、API key、完整 prompt 或完整病例 payload。

## 关注事项

1. 当前 live acceptance 不能标记为通过，也不应据此声称生产就绪。
2. `verify_update_http.py` 与显式病例授权契约之间存在可复现的脚本前置条件缺口；按本任务范围仅记录，不修改脚本。
3. no-key fallback 的 partial failure 与 retry 行为需要单独确认产品验收口径；本次结果只证明 fallback 路径被执行并被脚本部分观测到。
4. `verify_report_render.mjs` 的业务断言已输出全通过，但 Node 进程退出码和 assertion 必须在单独的运行时/脚本决策中处理。
