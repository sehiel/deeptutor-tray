# DeepTutor Web-Local Launcher · 架构设计（第二阶段输出）

> 基于对 DeepTutor 1.7.0（安装于 `E:\apps\deeptutor`，`pip` 模块化安装）真实源码的调查。
> 本文件是第二阶段的交付：新增/修改文件清单、API 设计、通信方案、托盘菜单方案、通知方案、打包方案。
> 第三、四阶段按本设计实现。

---

## 1. 源码调查结论（第一阶段摘要）

所有结论均来自 `.venv\Lib\site-packages\deeptutor*` 与 git 仓库 `web/` 目录的真实代码，非猜测。

### 1.1 伙伴数据与头像

- 路由：`deeptutor/api/routers/partners.py`
- `GET /api/partners` → `visible_partners()`，每条含 `partner_id / name / description / emoji / color / avatar / running / ...`
- `GET /api/partners/navigation`（partners.py `partner_navigation`）→ 一次返回：
  - `partners[]`：`{partner_id, name, description, emoji, color, avatar, can_manage, running}`
  - `groups[]`：伙伴群组（`group_id / name / ...`）
  - `sessions[]`：`{kind, owner_id, owner_name, session_key, title, updated_at(float)}`
  - `kind` 的取值域是 `Literal["partner", "group"]`（见 `NavigationSession` 模型）—— 群组聊天与伙伴
    聊天同源同字段，只是 owner 是 group。启动器原先只取 `kind == "partner"`，群组聊天被丢掉。
- **头像形态**（`deeptutor/services/partners/manager.py` `PartnerConfig.avatar`）：
  - `avatar` 是 **内联 data URL（SVG）**，存在 `config.yaml` 里，前端 `<img>` 直接渲染，无需鉴权文件接口。
  - 本机两个伙伴 `cyber-tutor`、`cet4-coach` 的 `avatar` 均为空字符串 → 必须回退到 **emoji + color 圆盘**（与前端 `PartnerAvatar.tsx` 一致：image → emoji 圆盘 → 首字母圆盘）。

### 1.1b 主对话会话（第二数据源）

- 路由：`deeptutor/api/routers/sessions.py`
- `GET /api/sessions?limit=50` → `{"sessions": [...]}`，每条：
  `{id, session_id, title, capability, status, updated_at, message_count, preferences, content_workspace_id, ...}`
  —— **与伙伴会话字段完全不同**（没有 owner、没有 partner 概念）。
- `id` 与 `session_id` 取值相同，形如 `unified_<ts>_<hash>`。
- 前端路由是 `/chat/[sessionId]`（见 `.next/server/app-paths-manifest.json`），所以打开主对话
  只需 `/chat/<session_id>`，**不需要** web-continuity（那是伙伴侧专用接口）。
- 这个端点**没有**伙伴端点那样的 `_navigation_timestamp` 兜底，脏数据会直接把 `float()` 弄崩，
  启动器侧要用 `_safe_timestamp` 兜住。

### 1.2 会话（聊天任务）数据结构

- `deeptutor/services/partners/sessions.py::PartnerSessionStore.list_sessions()`
- 每个会话摘要：`{session_key, title, message_count, updated_at(ISO), last_message, archived, chat_id?, scope?}`
- `title` = 首条用户消息第一行（≤60 字符）；`updated_at` = JSONL 文件 mtime。
- 会话文件在 `data/partners/<id>/sessions/*.jsonl`，`session_key` 形如 `web_<uuid>` / `web-xxxx`。

### 1.3 任务完成机制（原生事件）

- 伙伴聊天走 WebSocket：`/ws/partners/{partner_id}`（`partners.py::partner_chat_ws`，前缀来自 `modules/manifests/partners.json`）。
- 服务端帧协议：`ready / stream_event / content / done / error / proactive / accepted / user_echo / resuming / attach_busy / attach_idle / stale_session / turn_busy / stopped`。
- **关键发现**：每个 turn（含 web 聊天）都会镜像到 `PartnerActivityFeed`（`manager.py::PartnerActivityFeed`，`runtime.py::_emit_channel_activity`），帧带 `activity_id` 与 `session_key`：
  - `{"type": "user_echo", "content", "activity_id", "session_key", "channel"}`
  - `{"type": "content", ...}`
  - `{"type": "done", "activity_id", "session_key", "channel"}` ← **原生任务完成事件**
- WS 客户端发送 `{"action": "attach", "session_key": "...", "include_activity": true}` 即可订阅该账户的 activity feed，**被动**收到所有 turn 完成事件（浏览器发起也收得到）。
- 因此完成检测**不依赖**"聊天记录最后一条消息存在"，而是等 `done` 帧。

**关于连接超时（踩过的坑）**

`websocket.create_connection(url, timeout=N)` 的 `timeout` 会落到 socket 上，**`recv()` 也受它约束**。
而 uvicorn 默认 `ws_ping_interval=20.0`（DeepTutor 未覆盖）—— 服务端每 20 秒发一次 ping 帧，
ping 帧被 websocket-client 内部消费、**不会唤醒 `recv()`**（`recv()` 只返回 data frame）。

所以：
- `timeout=15`（< 20）→ 每次都在收到服务端 ping 之前先超时 → 连接被打断 → 15 秒重连一次。
  表现为日志被刷爆，且**重连空窗期内的 `done` 事件会丢**。
- `timeout=45`（> 20）→ socket 每 20 秒被 ping 帧唤醒，永远不会超时 → 连接稳定。

启动器因此拆成两个常量：`WS_CONNECT_TIMEOUT=15`（只管握手）、`WS_RECV_TIMEOUT=45`（收帧）。
超时后不直接断开，而是 `ws.ping()` 探活后继续等 —— 万一上游关掉 ping 也不会退化成假性重连循环。
另外 `recv()` 返回空串代表服务端已关闭连接，此时应 `break` 重连（早期版本写成 `continue`，会变成忙循环）。

### 1.4 聊天跳转路由

- 前端路由：`/(workspace)/partners/[partnerId]` → URL `http://localhost:3782/partners/<partner_id>`。
- 会话 key 默认存在浏览器 localStorage，URL 不带参；要"点任务进对应聊天"，用服务端 API：
  - `PUT /api/partners/{partner_id}/web-continuity`，body `{"enabled": true, "session_key": "<key>"}`
  - 前端 `PartnerDetail` 初始化时读取 `getPartnerWebContinuity`，若 enabled 则用服务端 `session_key`（`partners.py::put_partner_web_continuity` + `web/.../page.tsx` 已确认）。
  - 然后 `webbrowser.open("http://localhost:3782/partners/<partner_id>")` 即回到该会话。

### 1.5 系统状态与端口

- `GET /health/ready`（`deeptutor/api/main.py`）：`{"status": "ready"}` / 503。
- `deeptutor config show` 输出 JSON 含 `ports.backend` / `ports.frontend`（`deeptutor_cli/config_cmd.py`）。
- 本机实际：backend 8001、frontend 3782，`data/user/runtime/launcher.json` 显示服务由 `deeptutor start --detach` 托管。

### 1.6 CORS / 安全

- `main.py::_build_cors_settings`：`AUTH_ENABLED=false` 时 `allow_origin_regex = r"https?://.*"` → 任意本地源可调 API。
- `AUTH_ENABLED=true` 时只允许配置源 → 启动器网页跨端口调用会被拦（本机无此问题，作为已知限制）。
- WebSocket 鉴权：`ws_require_auth` 在 `AUTH_ENABLED=false` 时直接放行为 local-admin（`auth.py`）。

---

## 2. 总体架构

```
┌────────────────────────────────────────────────────────────┐
│  Launcher.exe (PyInstaller onefile, Python + pystray)       │
│                                                            │
│  ┌──────────────┐   ┌───────────────┐   ┌───────────────┐  │
│  │ 托盘图标/自绘菜单│   │ HTTP 服务器     │   │ 任务/通知引擎   │  │
│  │ (menu.py)     │   │ 127.0.0.1:47914│   │ (watcher)     │  │
│  └──────┬───────┘   └──────┬────────┘   └──────┬────────┘  │
│         │                  │  /api/state        │          │
│         │                  │  /api/action       │ WS 订阅   │
└─────────┼──────────────────┼────────────────────┼──────────┘
          │ 右键菜单选择      │                    │
          ▼                  ▼                    ▼
    ┌────────────────────────────────────────────────────┐
    │ DeepTutor 本地服务 (127.0.0.1:8001 / localhost:3782) │
    │  REST: /api/partners/navigation /web-continuity    │
    │  WS:   /ws/partners/{id}  (activity feed)          │
    │  CLI:  deeptutor start/stop/config show            │
    └────────────────────────────────────────────────────┘
          ▲
          │ 浏览器访问
   ┌──────┴─────────────────────────────┐
   │ 启动器网页 http://127.0.0.1:47914   │  ← 新增
   │ 任务列表/伙伴头像/状态/启停按钮       │
   └────────────────────────────────────┘
```

复用现有 `launcher.py`（生命周期、托盘、端口处理），新增三块：本地 HTTP 服务器、任务/WS 观察器、Toast 通知。

---

## 3. 新增 / 修改文件清单

| 文件 | 动作 | 说明 |
|---|---|---|
| `launcher.py` | **修改** | 加入 HTTP 服务器、WS 观察器、通知引擎、deep-link、web 动作；菜单加入"最近任务"区 |
| `menu.py` | **修改** | `MenuItem` 支持头像（PIL 图）；渲染任务条目（头像 + 单行标题）；修复 WM_DESTROY 消息循环隐患 |
| `notify.py` | **新增** | PowerShell Toast 通知（零第三方依赖），图标本地化、协议激活点击 |
| `web/index.html` | **新增** | 启动器网页（单文件，内联 CSS/JS，浅色/深色模式，任务列表+启停按钮） |
| `requirements-dev.txt` | **修改** | 增加 `websocket-client` |
| `build.bat` | **修改** | `--add-data "web/index.html;web"`，打包网页资源 |
| `version.py` | **新增** | 版本号单一真源（`__version__` / `APP_NAME`），exe 名与版本资源都从这里取 |
| `gen_version_info.py` | **新增** | 由 `version.py` 生成 PE 版本资源 `version_info.txt`，避免两处维护版本号 |
| `version_info.txt` | **生成** | 不再手工维护，由 `build.bat` 调用 `gen_version_info.py` 重新生成 |
| `THIRD_PARTY_NOTICES.md` | **新增** | 第三方许可声明（pystray 为 LGPL-3.0，含重新链接/替换说明） |
| `README.md` / `HANDOFF.md` | **修改** | 补充新功能与测试状态 |

不修改 DeepTutor 安装目录下任何文件；不新增 DeepTutor 侧代码。

---

## 4. API 设计（启动器本地 HTTP）

Base：`http://127.0.0.1:47914`（仅绑定回环；每次启动生成随机 token，注入页面并校验）。

| 方法/路径 | 请求 | 响应 |
|---|---|---|
| `GET /` | — | 启动器网页（HTML，内联 `__TOKEN__`） |
| `GET /api/state` | Header `X-Launcher-Token` | `{running, status_text, frontend_url, launcher_url, partners[], sessions[], version}` |
| `POST /api/action` | Header `X-Launcher-Token`，JSON `{action, partner_id?, session_key?, source?}` | `{ok, error?}` |

`action` 取值：`open`（打开 DeepTutor 网页）、`start`、`restart`、`stop`（只停服务，托盘保留）、`quit`（停服务并退出）、`refresh`（强制刷新任务）、`open_chat`（deep-link 到会话）。

安全：仅 `127.0.0.1`；POST 校验 `Origin`/`Referer`（仅允许本机来源或空）；token 由页面注入，跨源页面无法读取。

`sessions[]` 结构：`{source, source_label, partner_id, partner_name, session_key, title, updated_at, status}`，
`status ∈ running/completed/idle`（来自 WS 观察，非猜测）。

`source ∈ partner | group | chat` —— 伙伴侧会话来自 `/api/partners/navigation`，
主对话会话来自 `/api/sessions`，启动器归一化成同一结构后按 `updated_at` 倒序。
`open_chat` 按 `source` 分流：`chat` 直接开 `/chat/<session_key>`，其余先 PUT
`web-continuity` 再开 `/partners/<partner_id>`。

---

## 5. 通信方案

1. **任务数据**：启动器每 5s 轮询 `GET /api/partners/navigation`，缓存 `partners` 与 `sessions`（排序取最近 10 条）。
2. **完成事件**：每个伙伴一条 `websocket-client` 长连接，连接后发 `attach`（`include_activity: true`）；收到 `user_echo` 标记会话 running，收到 `done` 触发完成流程。
   - **防误报**：连接后 3s 为热身期，热身期内的 `user_echo` 不视为"本次运行中开始的任务"；`done` 只有对应 `user_echo` 在热身期后到达才通知 → 重启后不会把历史完成任务误判为新完成。
   - **防重复**：`activity_id` 记入持久化文件 `data/user/runtime/launcher-notified.json`，已通知的不再发。
3. **聊天跳转**：`open_chat` → `PUT /api/partners/{id}/web-continuity` 设置目标会话 → 打开 `/partners/<id>`。
4. **服务启停**：沿用现有 `deeptutor start --detach --no-browser` / `deeptutor stop --home` 与端口清理逻辑。

---

## 6. 托盘菜单方案

继续使用自绘分层窗口（`menu.py`），扩展条目类型：

```
┌────────────────────────────┐
│ 状态：运行中                │  ← header
│ ────────────────────────── │
│ ● 打开网页                  │  ← primary
│ ────────────────────────── │
│ 最近任务                    │  ← header
│ (头像) SQL 注入学习顺序…     │  ← task 条目：头像 + 标题
│ (头像) 四级 6 周冲刺计划…    │
│ (头像) 暂无更多              │
│ ────────────────────────── │
│ 打开启动器页面               │
│ 刷新任务                    │
│ 启动服务 / 重启 / 停止       │
│ 查看日志 / 安装目录          │
│ ────────────────────────── │
│ 退出（同时停止服务）          │
└────────────────────────────┘
```

- 任务条目在 `MenuItem` 上增加 `avatar: Image.Image | None` 字段，渲染时左侧画 22px 圆角头像（avatar data URL 解码，否则 emoji/颜色圆盘），文字从 x=40 开始单行截断。
- 菜单宽度从 268 增加到 320，行高保持 34，任务区最多 5 条。
- 点击任务 → `open_chat(partner_id, session_key)`。

---

## 7. 通知方案

- 用 **PowerShell + Windows.UI.Notifications** 发原生 Toast（`notify.py`），零第三方 Python 依赖。
- AUMID 借用 Windows PowerShell 已注册 ID（`{1AC14E77-...}\WindowsPowerShell\v1.0\powershell.exe`），免注册即可显示。
- Toast XML：
  - `activationType="protocol"`，`launch=http://localhost:3782/partners/<id>` → 点击在默认浏览器打开对应聊天。
  - `appLogoOverride` = 伙伴头像本地 PNG（avatar data URL 解码，或 PIL 画 emoji 圆盘），存 `data/user/runtime/avatars/`。
  - 标题 = 任务标题（截断）；正文 = `伙伴名 · 任务已完成`。
  - `Tag` = `partner:<id>:<session_key>` 防止同任务重复弹。
- 发送前先 `PUT web-continuity` 绑定会话，保证点击直达。
- 若 PowerShell 被策略拦截或失败，回退 `pystray.Icon.notify()`（气泡通知）。

---

## 8. 打包方案

- PyInstaller onefile，沿用现有 `build.bat`，新增 `--add-data "web/index.html;web"`。
- 依赖：`pystray`、`Pillow`、`websocket-client`、`pyinstaller`（`requirements-dev.txt` 中**锁定精确版本**，因为托盘依赖 pystray 私有 API）。
- 产物：`dist/DeepTutorTray-v0.9.0-win64.exe`（约 17–18 MB），复制到 DeepTutor 安装目录（与 `start.bat` 同层）即可运行，目标机无需 Python。
- 明确**不**打包 DeepTutor 本体（按需求优先独立 EXE）。
- 版本资源由 `gen_version_info.py` 从 `version.py` 生成，改版只改 `version.py` 一处。

---

## 9. 已知限制（如实说明）

1. **完成通知仅在启动器运行且 WS 连接成功时有效**；启动器未运行时浏览器里完成的任务不会补发通知（符合"重启不误报历史"要求）。
2. `AUTH_ENABLED=true` 的部署：WS 需要 token、网页跨源会被 CORS 拦。本机为 `false`，不受影响；如需支持，后续可加 token 登录与 API 代理。
3. 自绘菜单仍是单屏定位（主屏边界钳制），多显示器/混合 DPI 下可能贴边异常（沿用现有实现，未在本次范围）。
4. 托盘图标显示、真实右键链路此前未完全验证（见旧 HANDOFF），本次实现后需实机复核。
5. 任务"状态"是启动器运行期间的实时观测值，重启后历史状态重置为 `idle`，不伪造历史。