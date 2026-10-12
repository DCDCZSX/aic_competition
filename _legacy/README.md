# _legacy/ —— 归档区（不要在这里开发）

本目录存放**已被取代或废弃**的文件，仅作留档与溯源用。参赛版一律以下层 `backend/` 为准。

| 归档目录/文件 | 原位置 | 为什么归档 | 还能用吗 |
|---|---|---|---|
| `frontend_react_abandoned/` | `CHAIS/frontend/` | React 前端只有 4 个源文件、**没有 `package.json`（不可构建）**，且调用后端不存在的 `/api/conduct_discussion` | ❌ 不可用。参赛前端 = `backend/templates/index.html`（现行）或按 UI 设计说明新建 |
| `root_templates_websocket/` | `CHAIS/templates/` | 更老的 WebSocket 版前端，路由与当前后端不匹配 | ❌ 不可用，仅留档对比 |
| `app_websocket_legacy/` | `CHAIS/app/` | 上述 WebSocket 版配套的 `config.py` / `session.py` | ❌ 不可用，仅留档 |

## 注意

- **不要在 `_legacy/` 里改代码或"修复"这些文件**——它们的价值就是保持原样，供答辩/材料核对历史。
- 另有 `__pycache__/`（4 处）已直接删除，Python 运行时会自动重建，无需恢复。
- 迁移时间：2026-09-09（见根目录 `开发进度与交接.md` §8 变更日志）。
- 如需恢复：把对应目录移回 `CHAIS/` 原位置即可（未做任何内容修改）。
