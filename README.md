# DeepTutor Tray Launcher

A small Windows tray launcher for [DeepTutor](https://github.com/HKUDS/DeepTutor) — fixes the
"port already in use" dead end, keeps the service one click away, adds a local web dashboard
for your recent chats (partner, group and main conversations), and sends a Windows toast when
a partner task really completes.

<p align="center">
  <img src="preview.png" alt="运行中 / 已停止 两种托盘状态" width="220">
</p>

> **Unofficial.** Not affiliated with, endorsed by, or maintained by the DeepTutor project.
> Tested against DeepTutor 1.7.0 on Windows 11.

---

## The problem

Since DeepTutor 1.7.1 the project also ships an **Open DeepTutor desktop app** for
Windows x64 — but it is still a *preview*: the build is published from a `dev`
branch workflow (GitHub sign-in required) and **the artifact expires after seven
days**, so it is not something you can download and keep. It is also unsigned.

The everyday way to start DeepTutor on Windows is still `deeptutor start` in a
terminal (keep the window open), or a `.bat` wrapper. Closing the browser tab leaves
the backend **and** the Next.js frontend running — which is intentional, the service
is still alive.

But re-running the launcher fails:

```
检测到端口被占用:
  前端端口 3782 被以下进程占用:  PID 11104 · node.exe
  [1] 更改端口设置   [2] 停止占用进程并继续启动
请选择 [1/2]:
```

`deeptutor start` **prompts interactively** when a port is taken. A batch file cannot answer it,
reads EOF, and exits with code 130. The user sees "port occupied" and has no obvious way out.

This launcher automates exactly that, and puts the state in the tray so you never have to guess.
It is a **stable, permanently downloadable** complement to the official desktop preview —
not a replacement for it.

## What it does

| Situation | Behaviour |
|---|---|
| Service already healthy | Opens the browser. **Does not touch any process.** |
| Port busy but service unhealthy | Runs the official `deeptutor stop`, then re-checks; only reports an error if something else still holds the port |
| Ports free | `deeptutor start --detach --no-browser`, waits for `/health/ready`, then opens the browser |
| Port held by a foreign program | Reports it. Never kills a process it cannot identify as DeepTutor's |

Port clean-up only targets `node.exe` / `python.exe` / `pythonw.exe` / `deeptutor.exe`.
Any other listener is left alone.

### Custom dialogs, readable task rows, a menu that stays open

- **Dialogs are drawn by the app** (`dialog.py`) and place `logo.png` at 56 pt next to the text.
  Windows' `MessageBoxIndirect` keeps a 32×32 icon control even at 150% DPI while loading a 48×48
  icon, so the embedded artwork looked tiny/cropped — this removes that limitation. The native
  message box is still used as a fallback if the custom window cannot be created.
- **Recent task rows**: avatars are aspect-fitted (no stretching), masked with an antialiased
  circle, and emoji glyphs are centred by their ink bounds. Labels are now ellipsized by measured
  pixel width instead of character count, so long Chinese titles no longer cross the right border.
- **The menu no longer closes on mouse-leave.** It stays open when the pointer leaves the menu or
  the tray icon, and is dismissed only by a click outside both, by Esc, or by choosing an item.
  The tray icon rectangle is obtained with `Shell_NotifyIconGetRect` so clicks on the icon keep it.

### Compact menu, app icon in dialogs

- **Tray menu trimmed to four entries**: 打开网页 / 最近任务 / 新建任务 / 退出. The start, restart,
  stop, log and open-directory entries are gone; the local launcher page still offers the
  service controls. Recent tasks moved into a **submenu** so the main menu stays four rows high.
- **新建任务** opens DeepTutor's new chat page (`/chat`), starting the service first if needed.
- **Message boxes use the app icon**: `MessageBoxIndirectW` + `MB_USERICON` with the icon resource
  embedded by `--icon` (superseded by the custom dialog above). Toasts fall back to the bundled
  logo when a partner has no avatar.
- **Menu freeze fixed**: the window class was re-registered on every open, so from the second
  open onwards the window kept a dangling Python callback from the previous popup. The class is
  now registered once with a module-level window procedure, fonts/panel are cached, and hover
  repaints only the affected row.

### Web dashboard + recent tasks + completion toasts

- **Local web dashboard** at `http://127.0.0.1:47914` (only reachable from this machine):
  service status, recent chat sessions (avatar / title / source / status), one-click
  start / restart / stop / quit, click a session to jump into that conversation.
- **Tray "最近任务" submenu**: up to 5 recent sessions, each rendered with its
  avatar and title. Clicking opens the exact conversation.
- **Task-completion toast**: the launcher subscribes to each partner's WebSocket activity feed
  (`/ws/partners/<id>`) and listens for the native `done` event — not a heuristic based on
  "last message exists". When a turn really finishes, a Windows toast shows the task title,
  partner name and avatar; clicking it opens that chat (via the `web-continuity` API).
  Duplicate notifications are suppressed across restarts (`launcher-notified.json`), and
  tasks that finished before the launcher started are never re-reported.

#### Which sessions are listed

"最近任务" is not limited to partner chats. It merges **two upstream sources** and
sorts everything by recency:

| Source | Where it comes from | How it opens |
|---|---|---|
| **伙伴** (partner) | `/api/partners/navigation`, `kind="partner"` | `web-continuity` is set, then `/partners/<id>` |
| **群组** (group) | `/api/partners/navigation`, `kind="group"` | same as above |
| **主对话** (main chat) | `/api/sessions` | straight to `/chat/<session_id>` |

The two live in different upstream APIs with different shapes, so the launcher
normalises them into one record and tags it with a `source`. In the dashboard the
source appears as a small badge next to the title; in the tray menu, non-partner
entries get a placeholder disc (主 / 群) instead of a partner avatar.

Partner chats keep their own avatars and get **no** source badge — they look
exactly as before. Because the tray submenu only has room for five rows, partner
and group chats are given priority there, so a burst of main chats can never push
them out of sight; the dashboard has room for twelve and lists everything in pure
recency order.

## Menu semantics

| Action | Service | Tray icon |
|---|---|---|
| 「打开网页」 / double-click | starts it if stopped, otherwise unchanged | stays |
| 「最近任务」 → task | starts it if stopped | stays |
| 「新建任务」 | starts it if stopped, opens `/chat` | stays |
| 「退出」 | **stops** (asks first if running) | **disappears** |
| Close the browser tab | keeps running | stays — double-click to reopen |

The tray icon and the service live and die together: the only entry that stops the service is
「退出」, and it takes the icon with it. The icon is full colour while running and desaturated
while stopped. State is re-checked every 8 seconds, so if the service dies (or is started from a
terminal) the icon catches up on its own.

The menu is **not** dismissed by moving the pointer away. It stays open until you click outside
the menu *and* the tray icon, press Esc, or pick an entry; clicking the tray icon itself keeps it
open.

### Why the menu is drawn by hand

The first version used pystray's native menu, which goes through Win32 `TrackPopupMenuEx`.
Two problems with that:

1. **You cannot style it.** Font, row height, colours and padding all come from the system theme.
2. **It goes blurry on scaled displays.** Unless the process declares DPI awareness, Windows
   bitmap-stretches the whole window — at 150% scaling a 1× menu is blown up 1.5×, so text
   loses its edge.

So `menu.py` renders the menu into a 32-bit RGBA bitmap and shows it through a
`WS_EX_LAYERED` window with `UpdateLayeredWindow`. Text is rasterised at real device pixels
(no scaling), and the rounded corners and drop shadow are genuine per-pixel alpha.
Right-click is intercepted by monkey-patching pystray's `_on_notify`, so the tray icon itself
still comes from pystray and nothing else had to be rewritten.

If you are forking this, that is the one non-obvious part: `menu.py` owns the popup and its
message loop, `launcher.py` only supplies the item list.

## Install

1. Download `DeepTutorTray-v0.9.0-win64.exe` from **Releases** (not from the repo tree).
2. Put it **next to `start.bat`**, i.e. in your DeepTutor install directory
   (the folder containing `.venv/` and `data/`).
3. Double-click it.

No Python installation is needed on the target machine — this is a PyInstaller one-file build.

If you installed DeepTutor with `pip install deeptutor` and there is no `.venv` folder,
the launcher falls back to `deeptutor` on `PATH`; set `DT_HOME` to your workspace root.

### Requirements

- Windows 10 / 11, 64-bit
- DeepTutor installed and run at least once with `deeptutor init`
- DeepTutor's `AUTH_ENABLED` disabled (the default for local installs) for task-completion
  toasts; the rest of the launcher works either way

### DeepTutor compatibility

| DeepTutor version | What works |
|---|---|
| **1.7.0+** | Full feature set: start/stop/open, recent tasks, task-completion toasts, click-to-chat. Tested with 1.7.0. |
| 1.6.3 – 1.6.15 | Start / stop / open the web UI only. These releases have `deeptutor start --detach --no-browser`, but not `/api/partners/navigation`, so the task list stays empty and toasts are unavailable. |
| 1.4 – 1.6.2 | Not supported: `start --detach` is missing, so the launcher cannot manage the service. |
| 1.3 and older | Not supported. |

Full features require DeepTutor's `/api/partners/navigation`, `/api/partners/{id}/web-continuity`
and `/ws/partners/{id}` endpoints, which are present in 1.7.0 (partners moved to the
`deeptutor-partners` module in 1.7.x; install the full distribution, not a minimal server-only
subset). 1.7.1 is the current release and is expected to work, but was not tested here.

### Security notes

The launcher runs on your machine only, but it does touch process state and the
network, so here is exactly what it does:

- **Port cleanup is conservative.** When a port is still held after `deeptutor stop`,
  the launcher kills a process **only if** its image name is `node.exe` /
  `python.exe` / `pythonw.exe` / `deeptutor.exe` **and** its command line actually
  contains `deeptutor`. Your own `python` service on port 8001 is never touched,
  and if the command line cannot be read the process is left alone.
- **The local web page is loopback-only** and requires a random per-run token.
  It additionally validates the `Host` header, so a malicious web page cannot
  use DNS rebinding to reach it and read the token.
- **Toast notifications never execute session text.** Task titles and partner names
  reach the notification as XML data, never as PowerShell code — a title containing
  `$(...)` is shown literally instead of being run.

### Windows SmartScreen / antivirus

This is an unsigned executable, so on first run you may see
**“Windows protected your PC”** or an antivirus warning. That is expected for any unsigned
PyInstaller build — it is not a sign of tampering.

- SmartScreen: click **More info** → **Run anyway**.
- If your antivirus quarantines it: add an exclusion, or build it yourself from source
  (`build.bat`) — the whole thing is ~2,600 lines of Python, nothing hidden.

Code signing certificates cost money and this is a free utility, so the honest options are
"run anyway" or "build it yourself". Both are fine.

## Build from source

```bat
git clone https://github.com/<your-name>/deeptutor-tray.git
cd deeptutor-tray
python -m venv .venv
.venv\Scripts\pip install -r requirements-dev.txt
build.bat
```

Output: `dist\DeepTutorTray-v0.9.0-win64.exe`.

**The version number lives in exactly one place**: [`version.py`](version.py).
Change `__version__` there and re-run `build.bat` — the exe filename, the PE
version resource (`version_info.txt` is generated from it), the HTTP
`User-Agent` and the launcher page footer all follow automatically. Nothing
else needs to be edited.

> `requirements-dev.txt` pins exact versions on purpose: the launcher
> monkey-patches pystray's private API (`pystray._win32.Icon._on_notify`),
> which a newer pystray would break silently.

### Why the version is 0.9 and not 1.0

This launcher deliberately stays on `0.x`. SemVer reserves `1.0` for a promise
that the public interface is stable and will not change in breaking ways — but
**that interface is owned by DeepTutor, not by this project**. The launcher
depends on upstream internals (`deeptutor start --detach`,
`/api/partners/navigation`, `/ws/partners/{id}`), so an upstream refactor can
break it at any time. See the compatibility table above for exactly which
upstream versions are supported.

`0.9.0` means: *feature-complete and close to stable, but still tracking
upstream*. When upstream settles and this project has gone a few months without
a breaking change, `1.0.0` becomes meaningful and it will be released then.

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `DT_HOME` | exe's directory | DeepTutor workspace root (containing `data/`) |
| `DT_BACKEND_PORT` | read from `deeptutor config show`, else `8001` | Backend port to probe |
| `DT_FRONTEND_PORT` | read from `deeptutor config show`, else `3782` | Frontend port, also the URL opened |

Ports are read from DeepTutor's own config at startup, so non-default ports set during
`deeptutor init` work without extra configuration. Environment variables override the config.

Command-line diagnostics:
- `DeepTutorTray-v0.9.0-win64.exe --check` — JSON status report (ports, health, task count, WS state,
  whether the bundled dialog icon loaded).
- `DeepTutorTray-v0.9.0-win64.exe --test-dialog` — show the custom confirm dialog and check the logo.
- `DeepTutorTray-v0.9.0-win64.exe --test-toast` — send a test Windows toast with a partner avatar
  (falls back to the bundled logo when there is no partner).
- `DeepTutorTray-v0.9.0-win64.exe --dump-icon <path>` — export the tray icon bitmap.

## Logs

- Launcher actions: `data\user\runtime\launcher-tray.log`
- DeepTutor service output: `data\user\runtime\launcher.log`
- Notified task ids: `data\user\runtime\launcher-notified.json`
- Toast avatar images: `data\user\runtime\avatars\`

`--check` prints the active log path; the files live under `data\user\runtime\`.

## How it starts/stops the service

```
start: deeptutor start --home <home> --detach --no-browser
stop : deeptutor stop  --home <home>
```

`--detach` on Windows uses `CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS`, so the service is not a
child of the tray process. `deeptutor stop` only stops a launcher that was itself started with `--detach`.

## How task completion is detected

1. The launcher polls `GET /api/partners/navigation` every 5 s to keep the task list fresh.
2. For each partner it keeps a WebSocket connection to `/ws/partners/<id>` and subscribes to
   the activity feed (`{"action":"attach","include_activity":true}`).
3. The feed mirrors every turn (web and IM) as `user_echo` / `stream_event` / `content` / `done`
   frames, each carrying `session_key` and `activity_id`. `done` is the native completion event.
4. A 3-second warm-up after connecting filters out replayed history, so tasks completed before
   the launcher started are never reported as new. `activity_id`s are persisted, so the same
   task never toasts twice, even across restarts.
5. On `done`, the launcher binds the conversation via `PUT /api/partners/<id>/web-continuity`
   and shows a toast whose click opens `http://localhost:3782/partners/<id>` in the browser.

## Limitations

- **Windows only.** The tray uses `pystray`'s Win32 backend; the menu is a layered window drawn
  with PIL; port/process logic shells out to `netstat` and `tasklist`.
- Menu font falls back to SimHei and then PIL's bitmap font if Microsoft YaHei is missing.
  On a non-Chinese Windows the text still renders, just with a different typeface.
- The exe is ~18 MB because it bundles a Python runtime. A native rewrite would be a few hundred KB.
- Tied to DeepTutor's CLI surface (`start --detach`, `stop`, `config show`, `/health/ready`).
  A breaking CLI change upstream will break this.
- Task-completion toasts require `AUTH_ENABLED=false` (the default local install). With auth
  enabled, the WebSocket needs a token, so notifications are disabled with a log entry; the
  dashboard and tray tasks still work.
- Toasts use the registered Windows PowerShell app id, so they appear in Action Center under
  "Windows PowerShell". The avatar image and title are still shown on the toast itself.
- No autostart. Drop a shortcut into `shell:startup` if you want it.
- Only one instance per user session (single-instance guard on a local port). A second launch just
  opens the browser.

## 中文说明

DeepTutor 官方在 Windows 上除了终端 `deeptutor start`（要一直开着窗口）或 `.bat`，
从 1.7.1 起还提供了 Open DeepTutor 桌面版（Windows x64）——但它仍是 preview：
需 GitHub 登录从 dev 分支工作流下载、**产物 7 天过期**、未签名，不是能长期下载安装的正式发行物。
本项目提供的是一个**稳定可获取**的补充，而非替代。

关掉网页后后端和前端仍在后台跑，再次双击 bat 会因为 `deeptutor start` 弹出交互式端口询问而失败退出。

这个小工具做了三件事：**先判断再启动**（已在运行就直接开网页，不碰进程）、**必要时用官方
`deeptutor stop` 清残留**（只清认得出是 DeepTutor 的进程）、**把状态放到右下角托盘**。

网页启动器与任务通知：
- **本地网页启动器** `http://127.0.0.1:47914`：显示最近聊天记录（头像 / 标题 / 来源 / 状态），
  点击直达聊天；支持一键启动 / 重启 / 停止 / 退出。
- **托盘「最近任务」**：最近 5 条聊天记录，带头像，点击打开对应聊天。
- **任务完成 Toast**：订阅伙伴 WebSocket activity feed，收到原生 `done` 事件才发通知
  （不靠“最后一条消息”猜）；带伙伴头像，点击回到聊天；重启不重复通知、不误报历史。

「最近任务」收录的不只是伙伴聊天：
- **伙伴**（`/api/partners/navigation` 的 `kind="partner"`）
- **群组**（同一端点的 `kind="group"`，此前被过滤掉了）
- **主对话**（`/api/sessions`，此前完全没接）

两个来源字段结构不同，启动器归一化成同一条记录并用 `source` 标记来源。
网页端在标题旁显示来源标签；托盘里非伙伴条目用「主」「群」标识盘代替伙伴头像。
伙伴聊天保留自己的头像、**不加**来源标签，显示效果与之前完全一致。
托盘子菜单只有 5 行空间，所以伙伴/群组聊天优先占位 —— 不会被主对话挤掉；
网页端空间充足（12 条），纯按时间倒序。

菜单精简：
- **托盘右键菜单只留四项**：打开网页 / 最近任务 / 新建任务 / 退出。最近任务改为二级菜单，
  启停、日志、打开目录等条目移除（本地启动器页面仍保留启停按钮）。
- **新建任务** 打开 DeepTutor 的新对话页（`/chat`），服务未启动会先拉起。
- **弹窗图标** 改用 exe 内嵌的 DeepTutor 图标（`MessageBoxIndirectW` + `MB_USERICON`），
  不再显示系统问号；Toast 在没有伙伴头像时回退到内置 logo。
- **修复右键菜单卡死**：窗口类改为只注册一次，避免第二次打开时消息落到已销毁回调；
  字体/面板缓存，悬停只重绘一行。

弹窗与菜单细节：
- **弹窗改为自绘**（`dialog.py`），`logo.png` 以 56pt 画在卡片上。Windows 的 MessageBox
  在 150% DPI 下图标控件只有 32×32，图标会被缩小/裁切；自绘后尺寸完全可控，
  构建后同样生效。创建失败时仍回退原生 MessageBox。
- **最近任务头像/文字**：头像等比裁切 + 抗锯齿圆形遮罩，emoji 按墨迹居中；标题按实际
  像素宽度省略，不再按字符数截断，长中文标题不会顶出右边框。
- **菜单不再“移出即消失”**：鼠标移出菜单或托盘都不关闭；只有点击「菜单 + 托盘图标」
  之外的区域、按 Esc、或选中条目才关闭。托盘图标矩形通过 `Shell_NotifyIconGetRect` 获取。

兼容性：完整功能（最近任务 / 完成通知 / 点击跳转）需要 **DeepTutor 1.7.0+**（1.7.0 实测）；
1.6.3–1.6.15 只能启停和打开网页，任务列表为空、没有通知；1.4–1.6.2 没有 `start --detach`，不支持。

托盘和服务的生命周期绑定：点「退出」会连服务一起停（运行中会先确认），图标一起消失；
只关浏览器标签页则服务继续跑，双击图标就能重开。

端口从 `deeptutor config show` 读取，`deeptutor init` 时改过端口也能直接用。

首次运行碰到 SmartScreen 提示是正常的（未签名的自打包程序），点「更多信息 → 仍要运行」；
不放心可以用 `build.bat` 自己编译，全部源码都在这个仓库里。

## Credits

Icon artwork supplied by the repository owner. DeepTutor itself is
[Apache-2.0](https://github.com/HKUDS/DeepTutor/blob/main/LICENSE); this launcher is an independent
work and ships none of its code.

## License

MIT — see [`LICENSE`](LICENSE).

The bundled third-party components keep their own licenses; see
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md). Note that **pystray is LGPL-3.0**:
the notice file explains how to rebuild or relink the executable without it, as
LGPL requires.