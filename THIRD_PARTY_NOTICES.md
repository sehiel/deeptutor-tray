# Third-Party Notices

DeepTutor Tray Launcher 自身采用 MIT 许可（见 `LICENSE`）。但打包进
`DeepTutorTray-*.exe` 的第三方组件各自保留其原有许可，条款摘要如下。

---

## pystray 0.19.5 — LGPL-3.0

* 来源：https://github.com/micolous/pystray
* 许可：GNU Lesser General Public License v3.0（完整文本见
  https://www.gnu.org/licenses/lgpl-3.0.html）
* 用途：托盘图标与图标菜单事件的跨平台封装。

**LGPL 合规说明（重要，请勿删除）**

LGPLv3 要求：以二进制形式分发 LGPL 组件时，必须允许使用者**替换或重新链接**
该组件。本项目以 PyInstaller `--onefile` 方式将 pystray 静态打包进 exe，
为此提供以下补救措施：

1. **完整源码**：本仓库包含构建该 exe 所需的全部源码（`launcher.py`、
   `menu.py`、`dialog.py`、`notify.py`、`build.bat`、`version.py`）。
   使用者可据此重新构建。
2. **重新链接的可行性**：使用者可以修改本项目源码中的
   `install_hooks()`（`launcher.py`），移除对 pystray 私有 API
   （`pystray._win32.Icon._on_notify`、`pystray._util.win32`）的
   monkeypatch，改用其它托盘实现或 pystray 的公开 API，
   然后用 `build.bat` 重新打包，从而得到一个不链接该版本 pystray 的 exe。
3. **替换能力**：pystray 以纯 Python 形式运行于打包后的解释器中，
   修改 `.venv` 中的 pystray 源码后重新执行 `build.bat` 即可生成含修改后
   pystray 的 exe。
4. **本项目未修改 pystray 源码**，仅通过其公开入口使用，并对私有 API 做了
   兼容性 monkeypatch（该补丁集中于 `launcher.py` 的 `install_hooks()`，
   已在源码中完整可见）。

如上述任一方式对您而言不适用，请勿使用本二进制产物，改为自行 `build.bat` 构建。

---

## Pillow 13.0.0 — MIT-CMU

* 来源：https://github.com/python-pillow/Pillow
* 许可：MIT-CMU License（见 Pillow 发行包中的 `LICENSE`）
* 用途：托盘图标渲染、自绘菜单与弹窗的位图绘制、头像裁切与遮罩。

---

## websocket-client 1.9.2 — Apache-2.0

* 来源：https://github.com/websocket-client/websocket-client
* 许可：Apache License 2.0
* 用途：订阅伙伴 activity feed，用于检测任务完成并触发 Windows 通知。

---

## PyInstaller 6.22.3 — GPL-2.0-or-later（附打包例外）

* 来源：https://github.com/pyinstaller/pyinstaller
* 许可：GPL-2.0-or-later，**附带 PyInstaller 打包例外条款**。
  该例外明确允许把 PyInstaller 用于打包专有/任意许可的应用，
  分发产物时无需因使用 PyInstaller 而开源本项目。
* 用途：构建单文件 Windows 可执行程序。

**注意**：PyInstaller 的打包例外仅豁免「使用 PyInstaller」这一行为本身，
不改变本项目自身代码的许可，也不改变上述被打包库的许可条款。

---

## 运行时依赖（本项目未打包，仅本地安装的 DeepTutor 依赖）

以下组件由 DeepTutor 自身（Apache-2.0）安装，不包含在本项目的 exe 中，
列出仅为完整起见：aiohttp、aiosqlite、httpx、pydantic、uvicorn 等，
各自许可见其上游项目。

---

## DeepTutor

DeepTutor 由 HKUDS 开发，采用 Apache-2.0 许可
（https://github.com/HKUDS/DeepTutor/blob/main/LICENSE）。

**本项目与 HKUDS / DeepTutor 无隶属关系**，未使用其任何源代码。
本项目仅在运行时调用 DeepTutor 已安装版本提供的命令行与本地 HTTP 接口，
并读取其公开的界面图标资源用于识别。
