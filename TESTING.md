# DeepTutor Tray Launcher · 实机测试与打包清单

> 本清单面向 Windows 10/11 实机。源码级测试（语法、HTTP 服务器、token/Origin 校验、
> 菜单渲染、Toast 模板）已在开发环境通过；以下项目需要在实际 DeepTutor 环境验证。

---

## 一、构建 EXE（在 Windows 上执行）

```bat
cd /d "E:\Program File\AI\AI agent\WorkBuddy AI\agent\deeptutor mcp\deeptutor-tray"
python -m venv .venv
.venv\Scripts\pip install -r requirements-dev.txt
build.bat
```

产物：`dist\DeepTutorTray-v0.9.0-win64.exe`（约 18 MB，含 Python 运行时，目标机无需 Python）。

复制到 DeepTutor 安装目录（与 `start.bat` 同层，即 `E:\apps\deeptutor\`）：
```bat
copy /Y dist\DeepTutorTray-v0.9.0-win64.exe E:\apps\deeptutor\
```

如果不想装 Python，也可以先下载安装 [Python 3.11+](https://www.python.org/downloads/)（勾选 Add to PATH）。

---

## 二、自检命令（构建后先跑这四条）

在 DeepTutor 安装目录执行：

```bat
DeepTutorTray-v0.9.0-win64.exe --check
```

预期输出（服务运行时）：
```json
{
  "healthy": true,
  "backend_port": 8001,
  "frontend_url": "http://localhost:3782",
  "launcher_url": "http://127.0.0.1:47914",
  "sessions": 3,
  "auth_enabled": false,
  "ws_enabled": true,
  ...
}
```

```bat
DeepTutorTray-v0.9.0-win64.exe --test-toast
```

预期：屏幕右下角弹出 Windows Toast（标题"DeepTutor 启动器测试"，带伙伴头像），
输出 `{"toast_ok": true, ...}`。

```bat
DeepTutorTray-v0.9.0-win64.exe --dump-icon tray-icon.png
```

预期：生成 `tray-icon.png`，是彩色 logo。

```bat
DeepTutorTray-v0.9.0-win64.exe --test-dialog
```

预期：弹出「DeepTutor 弹窗图标测试」**自绘卡片**，左侧是 56pt 蓝/橙 logo（150% DPI 下约 84px），
标题/正文/取消/确定按钮完整；按 Enter 返回 `true`，Esc 返回 `false`。

---

## 三、功能验收（逐项打勾）

### 3.1 托盘与网页

- [ ] 双击 `DeepTutorTray-v0.9.0-win64.exe`：托盘出现 logo 图标，浏览器自动打开 DeepTutor 网页。
- [ ] 浏览器访问 `http://127.0.0.1:47914`：启动器页面显示"运行中"。
- [ ] 页面显示最近任务：每项有伙伴头像（emoji 圆盘）、任务标题、伙伴名、状态徽标。
- [ ] 页面「打开网页」可打开 DeepTutor。
- [ ] 页面「停止服务」：服务停止，页面状态变"已停止"，托盘图标变灰但仍存在。
- [ ] 页面「启动服务」：服务重新启动，状态变"运行中"。
- [ ] 页面「重启服务」：服务重启。
- [ ] 页面「退出启动器」：服务停止、托盘退出。
- [ ] 5 秒后页面自动刷新任务列表。

### 3.2 任务跳转

- [ ] 网页点击某个任务卡片：浏览器打开 `localhost:3782/partners/<id>`，且会话正确。
- [ ] 托盘右键：**只有四项** —— 打开网页 / 最近任务 / 新建任务 / 退出，无状态行、无启停/日志条目。
- [ ] 悬停「最近任务」：右侧子菜单显示最近 5 条任务（头像 + 标题），悬停高亮流畅。
- [ ] 任务标题很长时以 `…` 结尾，不越过面板右边框；头像为完整圆形，emoji 居中不缺失。
- [ ] **鼠标移出菜单/托盘区域（不点击）时，菜单保持显示**。
- [ ] 在菜单和托盘图标之外点击：菜单关闭；点击托盘图标本身：菜单保持。
- [ ] 点击「新建任务」：服务未启动会先拉起，然后打开 `localhost:3782/chat`。
- [ ] 点击托盘任务条目：同样跳到对应聊天。
- [ ] 服务停止时点击任务：自动先启动服务再跳转。

### 3.3 任务完成通知（核心）

前置：DeepTutor 服务运行中，`AUTH_ENABLED=false`（本机默认）。

- [ ] 检查日志出现 `ws connected: cyber-tutor`（`data\user\runtime\launcher-tray.log`）。
- [ ] 在伙伴聊天里发一条消息，等回复完成后：
  - [ ] 日志出现 `task done: <partner_id> <session_key> ...`；
  - [ ] 屏幕弹出 Windows Toast：标题=任务标题、正文=伙伴名 · 任务已完成、左侧头像；
  - [ ] 点击 Toast：浏览器打开对应伙伴聊天，且会话正确。
- [ ] 同一任务只弹一次通知（`launcher-notified.json` 持久化）。
- [ ] 退出托盘再启动，历史完成任务**不**弹通知。
- [ ] 连续右键打开/关闭菜单 10 次，不卡死、不叠层，悬停切换无拖影。

### 3.4 生命周期与安全

- [ ] 服务已在运行时，再次双击 exe：不重复启动，只打开网页。
- [ ] 端口被占且非 DeepTutor 进程：报告错误，不误杀。
- [ ] 关掉浏览器标签页：服务继续，托盘继续。
- [ ] 恶意 Origin 无法调用启动器 API（`Origin: http://evil.example` 应被 403 拒绝）。
- [ ] 其他网页无法读取启动器页面内容（无 CORS 头，浏览器跨源读取被拦）。

---

## 四、日志位置

| 文件 | 内容 |
|---|---|
| `data\user\runtime\launcher-tray.log` | 启动器动作、WS 连接、任务完成 |
| `data\user\runtime\launcher.log` | DeepTutor 服务输出 |
| `data\user\runtime\launcher-notified.json` | 已通知的 activity_id（去重） |
| `data\user\runtime\avatars\*.png` | Toast 使用的伙伴头像 |

---

## 五、已知限制（验收时请知悉）

1. 完成通知依赖启动器运行期间建立的 WebSocket；启动器未运行时完成的任务不会补发。
2. 通知以 Windows PowerShell 的 AUMID 发送，Action Center 里会归到 "Windows PowerShell"。
3. `AUTH_ENABLED=true` 的部署不会发任务通知（`ws_enabled=false`），其余功能正常。
4. 自绘菜单仍为主屏定位，多显示器/混合 DPI 下可能贴边异常。
5. 未签名 EXE 首次运行可能被 SmartScreen 拦截：更多信息 → 仍要运行。