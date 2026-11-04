# 交接说明（DeepTutor Tray Launcher）

给接下来接手的人。**先看这一页，再看 README**。README 写的是"这个东西是什么"，
这里写的是"它现在到哪一步、哪里还没弄好、改样式去哪儿改"。

---

## 1. 交付内容（当前版本 0.9.0）

```
launcher/
├─ launcher.py            主程序：托盘、端口判断、启停、单实例、状态轮询、
│                         本地网页服务器(127.0.0.1:47914)、伙伴 WS 观察器、
│                         任务完成 Toast 通知、聊天 deep-link
├─ menu.py                自绘右键菜单（Win32 分层窗口 + PIL 渲染，支持头像）
├─ dialog.py              自绘确认/错误弹窗（56pt logo，不受 MessageBox 32px 限制）
├─ notify.py              Windows 原生 Toast（PowerShell + Windows.UI.Notifications）
├─ web/index.html         启动器网页（单文件，浅色/深色，任务列表 + 启停按钮）
├─ version.py             版本号单一真源（改版只改这里）
├─ gen_version_info.py    由 version.py 生成 PE 版本资源
├─ logo.png               托盘/程序图标源图（512×512，透明底）
├─ icon.ico               多尺寸 ico（16/24/32/48/64/128/256）
├─ preview.png            README 里用的两态图标预览
├─ version_info.txt       exe 版本资源（由 build.bat 自动生成，勿手改）
├─ build.bat              一键构建（已加入 web/index.html 打包）
├─ requirements-dev.txt   pystray / pillow / websocket-client / pyinstaller（锁定精确版本）
├─ README.md              英文为主 + 中文说明，含 SmartScreen 处理
├─ ARCHITECTURE.md        第二阶段架构设计（API/通信/托盘/通知/打包方案）
├─ THIRD_PARTY_NOTICES.md 第三方许可声明（pystray 为 LGPL-3.0）
├─ LICENSE                MIT
└─ .gitignore
```

构建产物在 `dist/DeepTutorTray-v0.9.0-win64.exe`（约 18 MB）。`build/`、`data/` 是构建与运行残留，不用管。

## 2. 已经做完并且验证过的（v1.1 基线）

| 项 | 验证方式 |
|---|---|
| 已在运行时不重复启动、直接开网页 | 实机：`healthy=true` 时走 open 分支，不发启动命令 |
| 冷启动（端口空）→ 拉起 → 轮询就绪 → 开网页 | 实机端到端跑通，状态变为"运行中" |
| 端口被占但不健康 → 先 `deeptutor stop` 清理 → 仍占才报错 | 代码路径 + 日志确认 |
| 端口清理不误杀：只清 node/python/pythonw/deeptutor | 代码逻辑 |
| 端口从 `deeptutor config show` 读取，非硬编码 | 实机输出 `backend_port: 8001` |
| CLI 路径回退：`.venv\Scripts\deeptutor.exe` → PATH 上的 `deeptutor` | 实机 |
| 托盘图标 = logo（非蓝圈 DT） | `--dump-icon` 导出图片目检 |
| 自绘菜单的圆角/投影/文字锐度 | 显示后 `ImageGrab` 截实屏目检 |
| exe 版本信息写入 | 构建日志 + 属性面板 |

诊断命令：`DeepTutorTray-v0.9.0-win64.exe --check`（JSON 状态）、`--dump-icon <path>`（导出托盘图标）、
`--test-toast`（发一条测试 Toast，验证通知通道）。

## 3. v1.2 新增功能（源码已完成，**实机验证待做**）

1. **本地网页启动器** `http://127.0.0.1:47914`
   - `GET /` 返回 `web/index.html`（内联 token 注入 `__LAUNCHER_TOKEN__`）。
   - `GET /api/state`、`POST /api/action` 都要求 `X-Launcher-Token` 头；POST 还校验 Origin。
   - 页面每 5s 轮询状态，任务卡片点击走 `open_chat`。
2. **托盘「最近任务」**：`menu_items()` 里 `_sessions[:5]`，条目带 `avatar`（PIL 圆形图）。
   - 头像渲染 `partner_avatar_image()`：avatar data URL → emoji 圆盘 → 首字母圆盘。
   - 点击 `task:<partner_id>:<session_key>` → `open_chat()`。
3. **任务完成 Toast**：
   - `watcher_loop()` 每 5s 拉 `/api/partners/navigation`，并为每个伙伴起
     `ws_watcher()` 线程连接 `/ws/partners/<id>`，发 `attach` 订阅 activity feed。
   - `user_echo` 帧标记会话 running；`done` 帧触发 `handle_task_done()`。
   - **防历史误报**：连接后 3s 热身期内的 `user_echo` 不算数，只有热身期后开始的
     turn 完成才通知。
   - **防重复**：`activity_id` 写入 `data/user/runtime/launcher-notified.json`。
   - 通知前先 `PUT /api/partners/<id>/web-continuity` 绑定会话，点击 toast 用
     `activationType="protocol"` 打开 `http://localhost:3782/partners/<id>`。
   - `notify.py` 借 Windows PowerShell 已注册 AUMID 发 Toast，免注册。
4. **Web 动作**：`start/restart/stop/quit/open_chat/refresh`；网页「停止服务」走
   `stop_service_only()`（只停服务，托盘保留），托盘「停止服务」仍是 `stop_then_quit()`。

## 4. 没验证 / 需要接手者确认的（重点）

1. **v1.2 全部实机行为未验证**：网页加载/轮询、任务列表显示、点击跳转、托盘任务区渲染、
   Toast 弹出、点击 toast 打开聊天、WS 连接与 `done` 触发。源码只做了语法检查。
2. **PowerShell Toast 在目标机的实际显示**：`notify.py` 用的 PowerShell AUMID 是社区通行做法，
   但 Windows 11 不同版本对"非打包应用"的 toast 策略有差异。若 toast 不显示，先手动跑：
   ```
   python notify.py C:\...\avatars\cyber-tutor.png
   ```
   失败时 `launcher-tray.log` 会有记录；代码已回退到 `pystray.Icon.notify()` 气泡。
3. **WS activity feed 是否真的推送 web 聊天**：源码显示 `_web_activity_mirrored` 会把 web turn
   镜像到 activity feed（`runtime.py`），但**实机未验证**。若浏览器聊天不触发通知，检查
   `launcher-tray.log` 是否出现 `ws connected` 与 `task done`。
4. **托盘图标的实际显示**（沿用 v1.1 遗留）：只能 `--dump-icon` 验证生成图，无法确认通知区
   真的换了。接手后：完全退出托盘（任务管理器确认无 `DeepTutorTray-v0.9.0-win64.exe`）→ 重新双击 → 看图标。
5. **自绘菜单在真实右键流程下的表现**（沿用 v1.1 遗留）：`pystray._win32.Icon._on_notify`
   monkeypatch 位置依赖 pystray 0.19.5，升级 pystray 需重新验证。
6. **多显示器 / 混合 DPI**（沿用 v1.1 遗留）：定位用主屏尺寸钳制，多屏可能贴边异常。
7. **非中文 Windows**：菜单文案硬编码中文；字体回退链 微软雅黑 → 黑体 → PIL 位图字体。
8. **SmartScreen / 杀软**：未签名，首次运行大概率被拦。README 已写处理方式。

## 5. 想改样式，改哪儿

全在 `menu.py` 顶部，都是常量：

```python
WIDTH = 320        # 菜单宽度（逻辑像素）
ITEM_H = 34        # 每项行高
HEADER_H = 30      # 顶部状态行高度
SEP_H = 9          # 分隔条占用高度
RADIUS = 9         # 圆角
TEXT_PAD_X = 14    # 文字左边距
SHADOW = 20        # 投影留白
AVATAR_SIZE = 22   # 任务条目头像直径
AVATAR_X = 12      # 头像左边距
TASK_TEXT_X = AVATAR_X + AVATAR_SIZE + 10
BG / BORDER / HOVER / TEXT / TEXT_MUTED / TEXT_DISABLED / SEP   # 配色
```

字号在 `render()` 里：条目 13px、状态行 12px，都乘了 DPI 缩放。
「打开网页」加粗是因为 `primary=True` —— 在 `launcher.py` 的 `menu_items()` 里去掉即可。

任务条目的标题截断在 `launcher.py::menu_items()`（22 字符 + …）；最多 5 条（`MAX_MENU_TASKS`）。

网页样式全在 `web/index.html` 顶部 `<style>`（CSS 变量，浅色/深色两套）。

## 6. 三个不改会踩的坑（已处理，别回退）

**坑 1：内层消息循环不能调 `PostQuitMessage`。**
自绘菜单的 `show()` 在 pystray 的消息循环线程里跑嵌套 `GetMessage` 循环。
`WM_DESTROY` 里调 `PostQuitMessage` 会把外层 pystray 循环一起退掉，托盘直接消失。
现在用 `alive` 标志 + `PostThreadMessageW(WM_NULL)` 唤醒，见 `menu.py::_Popup.show/_wndproc`。
注意 `GetCurrentThreadId` 在 kernel32，不在 user32。

**坑 2：诊断参数必须在单实例检查之前处理。**
`__main__` 里先拦 `--check` / `--dump-icon`，再做 47913 端口占位。否则托盘正在运行时，
第二次调用会被判为"重复启动"，直接开网页走人。

**坑 3：DPI 感知必须在建窗口之前。**
`tray_menu.set_dpi_aware()` 在 `main()` 最前面。晚于窗口创建就无效，菜单会重新变糊。

**坑 4：任务通知不能靠"最后一条消息存在"判断。**
DeepTutor 的原生完成信号是伙伴 WebSocket activity feed 的 `done` 帧（带 `session_key`）。
`launcher.py::ws_watcher` 用 3 秒热身期过滤重放的历史帧；`launcher-notified.json` 持久化
`activity_id` 防重复。别把这些逻辑简化掉。

## 7. 为什么不用原生菜单、不用 Tk、不用第三方 toast 库

- **原生菜单**：pystray 走 `TrackPopupMenuEx`，样式完全由系统主题决定，改不了。
- **Tk**：构建环境里的 Python 没有 tkinter，用 conda 打包会让 exe 体积膨胀。
- **winotify/win10toast**：多一个依赖且未注册 AUMID 时 Win11 可能静默失败；
  直接调 PowerShell 的 WinRT API 并借用 PowerShell 已注册 AUMID，零额外依赖。

## 8. 构建

```bat
python -m venv .venv
.venv\Scripts\pip install -r requirements-dev.txt
build.bat
```

产物 `dist\DeepTutorTray-v0.9.0-win64.exe`，拷到 DeepTutor 安装目录（和 `start.bat` 同一层）使用。

## 9. 实机验收清单（v1.2）

详见 `TESTING.md`（构建、自检命令、逐项功能验收）。要点：

1. 双击 `DeepTutorTray-v0.9.0-win64.exe` → 托盘图标出现、网页自动打开。
2. 浏览器开 `http://127.0.0.1:47914` → 状态为运行中，任务列表显示最近会话（头像/标题/伙伴）。
3. 点击一个任务 → 打开 `localhost:3782/partners/<id>` 且会话正确。
4. 在伙伴聊天里发一条消息，等回复完成后 → 应弹出 Windows Toast（标题=任务标题，带头像）。
5. 点击 toast → 打开对应聊天。
6. 同一任务不应重复弹通知；重启 `DeepTutorTray-v0.9.0-win64.exe` 后不弹历史通知。
7. 托盘右键 → 「最近任务」显示头像+标题，点击跳到聊天。
8. 网页「停止服务」→ 服务停，托盘还在；「启动服务」→ 服务起。
9. `DeepTutorTray-v0.9.0-win64.exe --check` 输出含 `launcher_url`、`sessions`、`ws_enabled`。
10. `DeepTutorTray-v0.9.0-win64.exe --test-toast` 弹出一条测试通知。