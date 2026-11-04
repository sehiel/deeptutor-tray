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

### 1.7 会话来源的统一模型

「最近任务」要同时展示伙伴聊天、伙伴群组聊天和主对话，但它们来自**两个结构不同的上游端点**，
所以启动器把它们归一化成同一条记录：

| `source` | 上游来源 | 跳转方式 |
|---|---|---|
| `partner` | `/api/partners/navigation`，`kind="partner"` | 先 PUT `web-continuity`，再开 `/partners/<id>` |
| `group` | 同上，`kind="group"` | 同上 |
| `chat` | `/api/sessions` | 直接开 `/chat/<session_id>` |

统一后的记录字段：

```python
{
  "source": "partner" | "group" | "chat",
  "partner_id": str,        # chat 来源为空串
  "partner_name": str,      # chat 来源固定为 "主对话"
  "session_key": str,
  "title": str,
  "updated_at": float,
}
```

**为什么要区分 source**：两个端点的会话键语义不同 —— 伙伴侧的 `session_key` 是"伙伴的
某条会话"，必须先 `web-continuity` 把伙伴的活动会话切过去，前端打开 `/partners/<id>`
才会落到那条会话；主对话的 `session_key` 本身就是 URL 的一段（前端路由
`/chat/[sessionId]`），没有任何绑定步骤。

**健壮性**（上游数据不可信）：

- `_safe_timestamp()`：伙伴端点有 `_navigation_timestamp` 兜底，`/api/sessions` **没有**，
  脏数据（`"bad"` / `NaN` / `inf`）会直接把 `float()` 弄崩，连带整个会话列表挂掉。
- 空 `session_key` / 空 `owner_id` 的条目直接丢弃 —— 否则拼出来的 URL 缺一段，跳转必失败。
- 响应不是 dict、`sessions` 不是 list、条目不是 dict —— 全部安全降级。
- **「拉取失败」与「成功但为空」是两回事**：前者（返回 `None`）保留上一次的缓存，
  服务临时不可用时不该让用户的列表突然清空；后者（返回 `[]`）正常清空。

### 1.8 服务的启停

```
start: deeptutor start --home <home> --detach --no-browser
stop : deeptutor stop  --home <home>
```

`--detach` 在 Windows 上用 `CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS`，
所以服务不是托盘进程的子进程。`deeptutor stop` 只能停掉**它自己以 `--detach` 启动的**
launcher —— 靠的是 `data/user/runtime/launcher.json` 里登记的 PID。

> ⚠️ 运维要点：那个文件登记的是**当前** launcher 的 PID。进程已死而文件还在时，
> `deeptutor stop` 会返回「当前没有后台 DeepTutor launcher 在运行」（exit 1），
> 新的 `deeptutor start` 也会把文件重写成 `status: starting` 的失效状态。
> 手动停服后应检查并清理这个文件。

**端口兜底清理**：官方 stop 只管它自己 detach 出来的进程。残留的 node/python 才清，
且必须再核对命令行确实含 `deeptutor`（见 CHANGELOG 的安全条目）—— 否则会误杀用户自己
占用同端口的 python/node 服务。取命令行用 PowerShell CIM（`Get-CimInstance Win32_Process`），
因为新版 Windows 已移除 `wmic`；一次查全表再在 Python 侧过滤，逐个查要 3 秒。

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

主面板只保留四项，「最近任务」收进二级菜单：

```
┌────────────────────────┐
│ 打开网页               │   加粗（primary）
│ 最近任务        ›      │   右侧箭头 = 有二级菜单
│ 新建任务               │
│ 退出                   │
└────────────────────────┘
```

二级菜单（最近任务）：

```
┌────────────────────────────────────┐
│[主] Redis未授权访问利用            │   主对话 · 标识盘
│[伙] 【词汇自测结果，请更新基线】   │   伙伴 · 真实头像
│[伙] 我想学 SQL 注入，应该从知识库… │   伙伴 · 长标题按像素宽度截断
│[主] RAG 检索失败                   │   主对话 · 标识盘
│[群] 四级冲刺计划                   │   群组 · 标识盘
└────────────────────────────────────┘
```

- 面板是圆角卡片 + 投影（逐像素 alpha，不是系统主题画的）。
- 左侧 22px 圆形头像：**伙伴用真实头像**（emoji 或图片裁成圆形），**其他来源用来源标识盘**
  —— 主对话是深青色「主」字、群组是紫色「群」字。

- 任务条目在 `MenuItem` 上带 `avatar: Image.Image | None`，渲染时左侧画 22px 圆形头像
  （avatar data URL 解码，否则 emoji/颜色圆盘），文字从 x=40 起单行截断。
- 菜单宽度 320，行高 34，任务区最多 5 条。
- **伙伴/群组聊天在菜单里优先占位**（`_pick_menu_tasks`）：菜单只有 5 行，
  纯按时间排序会让大量主对话把伙伴聊天整个挤掉，而伙伴聊天是托盘的主路径。
  网页端空间充足（12 条），那里是纯时间倒序。
- 点击任务 → `open_chat(partner_id, session_key, source)`，`source` 决定跳转方式
  （见 §4）。

### 6.1 为什么菜单是自绘的

最初用 pystray 原生菜单，走 Win32 `TrackPopupMenuEx`，有两个问题：

1. **无法定制外观** —— 字体、行高、配色、间距全部由系统主题决定。
2. **在缩放显示器上发虚** —— 进程不声明 DPI 感知时，Windows 会把整个窗口按位图拉伸，
   150% 缩放下 1× 菜单被放大 1.5 倍，文字边缘就糊了。

所以 `menu.py` 把菜单渲染进 32 位 RGBA 位图，通过 `WS_EX_LAYERED` 窗口 +
`UpdateLayeredWindow` 显示：文字按真实设备像素栅格化（不缩放），圆角和投影是
真正的逐像素 alpha。右键菜单分支靠 monkey-patch pystray 的 `_on_notify` 拦掉，
托盘图标本身仍由 pystray 提供，其余部分无需重写。

**fork 时注意**：`menu.py` 拥有弹出窗口及其消息循环，`launcher.py` 只提供条目列表。

### 6.2 界面实现细节

- **自绘弹窗**（`dialog.py`）：`logo.png` 以 56pt 画在卡片上。Windows 的
  `MessageBoxIndirect` 即使在 150% DPI 下也只给 32×32 的图标控件，48×48 的图会被
  缩小/裁切；自绘后尺寸完全可控，创建失败时仍回退原生 MessageBox。
- **头像渲染**：等比裁切 + 抗锯齿圆形遮罩；emoji 按墨迹 bbox 居中（直接用
  `anchor="mm"` 会被字体度量偏移裁掉一角），先画到 2 倍画布再缩放。
- **标题省略**：按**实测像素宽度**截断，不按字符数 —— 否则长中文标题会顶出右边框。
- **菜单卡死修复**：窗口类曾每次打开都重新注册，导致第二次打开时消息落到已销毁的
  Python 回调。现在只注册一次（模块级窗口过程），字体/面板缓存，悬停只重绘一行。
- **菜单不因移出而消失**：只有点击「菜单 + 托盘图标」之外的区域、按 Esc、
  或选中条目才关闭。托盘图标矩形用 `Shell_NotifyIconGetRect` 获取，
  这样点图标本身也会保持菜单打开。

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

### 7.1 安全：不可信文本绝不进代码路径

任务标题、伙伴名来自 DeepTutor 的会话数据，属于**不可信输入**。若直接插进
PowerShell 脚本，`@"..."@`（可展开 here-string）会展开其中的 `$(...)`，
等于让一条会话标题获得执行任意命令的能力。

因此：

1. 模板用**单引号** here-string `@'...'@` —— PowerShell 不展开内容；
2. 所有动态值通过**环境变量**传入（`DT_AUMID` / `DT_XML` / `DT_TAG`），脚本内只引用 `$env:XXX`；
3. XML 用 `xml.etree.ElementTree` 在 Python 侧构造，序列化时自动转义。

三条一起用，输入既进不了代码路径，也进不了 XML 结构。实测同一载荷修复前会
执行、修复后原样显示为字面量。

> 注意：改写模板时**不要漏掉** `[Windows.Data.Xml.Dom.XmlDocument, ...] | Out-Null`
> 这行 WinRT 类型加速器 —— 少了它 `New-Object` 找不到类型，toast 会全部静默失效。

### 7.2 WS 保活与超时（踩过的坑）

`websocket.create_connection(url, timeout=N)` 的 `timeout` 会落到 socket 上，
**`recv()` 也受它约束**。而 uvicorn 默认 `ws_ping_interval=20.0`（DeepTutor 未覆盖）——
服务端每 20 秒发一次 ping 帧，ping 帧被 websocket-client 内部消费、
**不会唤醒 `recv()`**（recv 只返回 data frame）。

所以 `timeout=15`（< 20）→ 每次都在收到服务端 ping 之前先超时 → 连接被打断 →
15 秒重连一次。表现为日志被刷爆，且**重连空窗期内的 `done` 事件会丢**。

因此拆成两个常量：`WS_CONNECT_TIMEOUT=15`（只管握手）、`WS_RECV_TIMEOUT=45`（收帧，
必须 > 上游 ping 间隔）。超时后不直接断开，而是 `ws.ping()` 探活后继续等 ——
万一上游关掉 ping 也不会退化成假性重连。另外 `recv()` 返回空串代表服务端已关闭
连接，此时应 `break` 重连（早期版本写成 `continue`，会变成忙循环）。

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