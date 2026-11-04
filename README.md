# DeepTutor Tray Launcher

A small Windows tray launcher for [DeepTutor](https://github.com/HKUDS/DeepTutor) — fixes the
"port already in use" dead end, keeps the service one click away, and notifies you when a
partner task really finishes.

<p align="center">
  <img src="preview.png" alt="运行中 / 已停止 两种托盘状态" width="220">
</p>

> **Unofficial.** Not affiliated with, endorsed by, or maintained by the DeepTutor project.
> Tested against DeepTutor 1.7.0 on Windows 11.

---

## Install

1. Download `DeepTutorTray-v0.9.0-win64.exe` from [**Releases**](https://github.com/sehiel/deeptutor-tray/releases/latest)
   (not from the repo tree).
2. Put it **next to `start.bat`** — the DeepTutor install directory (the folder holding
   `.venv/` and `data/`).
3. Double-click it.

No Python installation is needed on the target machine — this is a PyInstaller one-file build.

### Requirements

- Windows 10 / 11, 64-bit
- DeepTutor installed and run at least once with `deeptutor init`
- DeepTutor's `AUTH_ENABLED` disabled (the default for local installs) for task-completion
  toasts; everything else works either way

### Windows SmartScreen / antivirus

This is an unsigned executable, so on first run you may see **"Windows protected your PC"**
or an antivirus warning. That is expected for any unsigned PyInstaller build — it is not a
sign of tampering.

- SmartScreen: click **More info** → **Run anyway**
- If your antivirus quarantines it: add an exclusion, or build it yourself (`build.bat`) —
  the whole thing is ~2,600 lines of Python, nothing hidden

Code signing certificates cost money and this is a free utility, so the honest options are
"run anyway" or "build it yourself". Both are fine.

---

## The problem

DeepTutor's `start` command **prompts interactively** when a port is already taken:

```
检测到端口被占用:
  前端端口 3782 被以下进程占用:  PID 11104 · node.exe
  [1] 更改端口设置   [2] 停止占用进程并继续启动
请选择 [1/2]:
```

A batch file cannot answer that, reads EOF, and exits with code 130. Closing the browser tab
leaves the backend and the frontend running, so the next double-click hits exactly this.

DeepTutor 1.7.1 also ships an **Open DeepTutor desktop app** for Windows x64 — but it is
still a *preview*: built from a `dev` workflow, requires GitHub sign-in, **expires after seven
days**, and is unsigned. This project is a stable, permanently downloadable complement rather
than a replacement.

---

## What it does

| Situation | Behaviour |
|---|---|
| Service already healthy | Opens the browser. **Does not touch any process.** |
| Port busy but service unhealthy | Runs the official `deeptutor stop`, then re-checks; only reports an error if something else still holds the port |
| Ports free | `deeptutor start --detach --no-browser`, waits for `/health/ready`, then opens the browser |
| Port held by a foreign program | Reports it. Never kills a process it cannot identify as DeepTutor's |

- **Tray menu** — 打开网页 / 最近任务 / 新建任务 / 退出. Recent tasks live in a submenu;
  the service controls are on the local dashboard.
- **Local dashboard** at `http://127.0.0.1:47914` (this machine only) — service status,
  recent sessions, one-click start / restart / stop / quit, click a session to open it.
- **Recent sessions from every source** — partner chats, partner group chats and main
  conversations, merged and sorted by recency. The dashboard shows the source as a badge;
  the tray uses a placeholder disc for non-partner entries. Partner chats keep their own
  avatars and look exactly as before.
- **Task-completion toast** — fires on the native `done` event from the partner activity
  feed, not a "a last message exists" guess. Shows the task title, partner name and avatar;
  clicking it opens that chat. Never fires twice for the same task, and never reports
  work that finished before the launcher started.

### Tray and service live together

The icon and the service are born and die together: the only entry that stops the service is
「退出」, and it takes the icon with it. Closing the browser tab keeps everything running —
double-click the icon to reopen it. The icon is full colour while running and desaturated
while stopped, and re-checks every 8 seconds, so it catches up if the service is started or
stopped from a terminal.

### Security

The launcher runs on your machine only, but it does touch process state and the network:

- **Port cleanup is conservative.** It kills a process only if the image name is
  `node.exe` / `python.exe` / `pythonw.exe` / `deeptutor.exe` **and** the command line
  actually contains `deeptutor`. Your own `python` service on port 8001 is never touched,
  and if the command line cannot be read the process is left alone.
- **The local web page is loopback-only**, requires a random per-run token, and validates the
  `Host` header so a hostile page cannot use DNS rebinding to read that token.
- **Toast text is never executed.** Task titles and partner names reach the notification as
  data, not as PowerShell code — a title containing `$(...)` is shown literally.

---

## DeepTutor compatibility

| DeepTutor version | What works |
|---|---|
| **1.7.0+** | Full feature set: start/stop/open, recent sessions, task-completion toasts, click-to-chat. Tested with 1.7.0. |
| 1.6.3 – 1.6.15 | Start / stop / open the web UI only. These releases lack the endpoints the session list and toasts need. |
| 1.4 – 1.6.2 | Not supported: `start --detach` is missing, so the launcher cannot manage the service. |
| 1.3 and older | Not supported. |

1.7.1 is the current upstream release and is expected to work, but was not tested here.

---

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `DT_HOME` | exe's directory | DeepTutor workspace root (containing `data/`) |
| `DT_BACKEND_PORT` | read from `deeptutor config show`, else `8001` | Backend port to probe |
| `DT_FRONTEND_PORT` | read from `deeptutor config show`, else `3782` | Frontend port, also the URL opened |

Ports are read from DeepTutor's own config at startup, so non-default ports set during
`deeptutor init` work without extra configuration. Environment variables override the config.

Command-line diagnostics:

```bat
DeepTutorTray-v0.9.0-win64.exe --check        :: JSON status report
DeepTutorTray-v0.9.0-win64.exe --test-dialog  :: show the confirm dialog, check the logo
DeepTutorTray-v0.9.0-win64.exe --test-toast   :: send a test toast with a partner avatar
DeepTutorTray-v0.9.0-win64.exe --dump-icon p.png :: export the tray icon bitmap
```

Logs live under `data\user\runtime\` in your DeepTutor directory — `--check` prints the
active path.

---

## Build from source

```bat
git clone https://github.com/sehiel/deeptutor-tray.git
cd deeptutor-tray
python -m venv .venv
.venv\Scripts\pip install -r requirements-dev.txt
build.bat
```

Output: `dist\DeepTutorTray-v0.9.0-win64.exe`.

**The version number lives in exactly one place** — [`version.py`](version.py). Change
`__version__` there and re-run `build.bat`; the exe filename, the PE version resource, the
`User-Agent` and the dashboard footer all follow automatically.

> `requirements-dev.txt` pins exact versions on purpose: the launcher monkey-patches pystray's
> private API (`pystray._win32.Icon._on_notify`), which a newer pystray would break silently.

### Why the version is 0.9 and not 1.0

SemVer reserves `1.0` for a promise that the public interface is stable and will not change
in breaking ways — but **that interface is owned by DeepTutor, not by this project**. The
launcher depends on upstream internals, so an upstream refactor can break it at any time.
`0.9.0` means *feature-complete and close to stable, but still tracking upstream*. When
upstream settles, `1.0.0` becomes meaningful and it will be released then.

---

## Limitations

- **Windows only.** The tray uses pystray's Win32 backend, the menu is a layered window
  drawn with PIL, and port/process logic shells out to `netstat` and `tasklist`.
- The exe is ~18 MB because it bundles a Python runtime. A native rewrite would be a few
  hundred KB.
- Tied to DeepTutor's CLI surface (`start --detach`, `stop`, `config show`, `/health/ready`).
  A breaking CLI change upstream will break this.
- Task-completion toasts need `AUTH_ENABLED=false` (the default local install). With auth
  enabled the WebSocket needs a token, so notifications are disabled; the dashboard and tray
  tasks still work.
- Toasts use the registered Windows PowerShell app id, so they appear in Action Center under
  "Windows PowerShell". The avatar and title still show on the toast itself.
- No autostart. Drop a shortcut into `shell:startup` if you want it.
- Only one instance per user session. A second launch just opens the browser.

---

## 中文说明

**这是什么**：DeepTutor 的 Windows 托盘启动器。修掉「端口被占用 → `deeptutor start`
弹交互式提问 → 批处理读到 EOF 退出（exit 130）」这条死路，并把服务状态收进右下角托盘。

**怎么用**：从 [Releases](https://github.com/sehiel/deeptutor-tray/releases/latest) 下载
`DeepTutorTray-v0.9.0-win64.exe`，放到 DeepTutor 安装目录（与 `start.bat` 同层），双击运行。
目标机不需要装 Python。

**功能**
- 托盘菜单四项：打开网页 / 最近任务 / 新建任务 / 退出；启停按钮在本地网页上。
- **最近任务收录全部聊天**：伙伴聊天、伙伴群组聊天、主对话，按时间倒序。
  网页端在标题旁显示来源标签；托盘里非伙伴条目用「主」「群」标识盘代替伙伴头像。
  伙伴聊天保留自己的头像、**不加**来源标签，显示效果与之前完全一致。
  托盘只有 5 行空间，所以伙伴/群组聊天优先占位，不会被主对话挤掉；网页端 12 条纯时间倒序。
- **任务完成 Toast**：订阅伙伴 WebSocket activity feed，收到原生 `done` 事件才发通知
  （不靠「最后一条消息」猜）；带伙伴头像，点击回到聊天；重启不重复通知、不误报历史。

**托盘与服务同生共死**：点「退出」会连服务一起停（运行中会先确认），图标一起消失；
只关浏览器标签页则服务继续跑，双击图标就能重开。

**兼容性**：完整功能需要 **DeepTutor 1.7.0+**（1.7.0 实测）；1.6.3–1.6.15 只能启停和打开网页；
1.4–1.6.2 没有 `start --detach`，不支持。

**端口**从 `deeptutor config show` 读取，`deeptutor init` 时改过端口也能直接用。

**首次运行**碰到 SmartScreen 提示是正常的（未签名的自打包程序），点「更多信息 → 仍要运行」；
不放心可以用 `build.bat` 自己编译，全部源码都在这个仓库里。

---

## Credits

Icon artwork supplied by the repository owner. DeepTutor itself is
[Apache-2.0](https://github.com/HKUDS/DeepTutor/blob/main/LICENSE); this launcher is an
independent work and ships none of its code.

## License

MIT — see [`LICENSE`](LICENSE).

The bundled third-party components keep their own licenses; see
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md). Note that **pystray is LGPL-3.0**:
the notice file explains how to rebuild or relink the executable without it, as LGPL requires.

See [`CHANGELOG.md`](CHANGELOG.md) for what changed, and
[`ARCHITECTURE.md`](ARCHITECTURE.md) for how it works.
