"""DeepTutor 托盘启动器 + Web-Local 启动器 + 任务完成通知。

解决什么
--------
`start.bat` 关掉网页后服务仍在后台跑，再双击会报“端口占用”并退出。
根因是 `deeptutor start` 在端口被占用时会弹交互式提问（`[1] 更改端口设置 /
[2] 停止占用进程并继续启动`），批处理无法应答 → 读到 EOF → exit 130。
DeepTutor 官方排障文档也记录了同一现象，给出的解法是手动
`netstat -ano | findstr :8001` + `taskkill /PID <pid> /F` —— 本程序把它自动化。

本版本在原有托盘启动器之上新增：
  1. 本地网页启动器（http://127.0.0.1:47914）：任务列表 / 伙伴头像 / 状态 / 启停按钮。
  2. 托盘菜单「最近任务」：伙伴头像 + 任务标题，点击直达对应聊天。
  3. 任务完成 Toast 通知：通过伙伴 WebSocket activity feed 监听原生 `done`
     事件（非“最后一条消息”猜测），带伙伴头像，点击回到聊天。

设计原则：托盘即服务
--------------------
一个图标对应一个服务，两者同生同灭，不留“图标在、服务没了”或“图标没了、服务还在”的错位：

  · 点「停止服务」 → 服务停 + 图标消失
  · 点「退出」     → 停服务 + 图标消失（服务在跑时先确认一次）
  · 关掉网页       → 服务继续、图标继续（双击图标即可重开网页，这是原 bug 的正解）

启动：deeptutor start --home <home> --detach --no-browser   （约 1.5 秒返回）
停止：deeptutor stop  --home <home>

构建（Windows）：
  build.bat          # 版本号与产物名取自 version.py，这里不用手写
  pyinstaller --onefile --noconsole --icon icon.ico --name DeepTutorTray-v0.9.0-win64
      --add-data "logo.png;." --add-data "web/index.html;web" launcher.py

可用环境变量：
  DT_BACKEND_PORT   默认 8001
  DT_FRONTEND_PORT  默认 3782
  DT_HOME           DeepTutor 安装目录（默认取 exe 所在目录）
"""

from __future__ import annotations

import base64
import ctypes
import http.server
import io
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from ctypes import wintypes
from pathlib import Path

import pystray
from PIL import Image, ImageDraw, ImageFont, ImageOps

import menu as tray_menu
import notify as tray_notify
import dialog as app_dialog
from version import APP_NAME, __version__

BACKEND_PORT = int(os.environ.get("DT_BACKEND_PORT") or 8001)
FRONTEND_PORT = int(os.environ.get("DT_FRONTEND_PORT") or 3782)
FRONTEND_URL = f"http://localhost:{FRONTEND_PORT}"
NEW_TASK_PATH = "/chat"          # DeepTutor 的「新对话 / 新任务」页面
HEALTH_URL = f"http://127.0.0.1:{BACKEND_PORT}/health/ready"
MUTEX_PORT = 47913
WEB_HOST = "127.0.0.1"
WEB_PORT_BASE = 47914
READY_TIMEOUT = 150
POLL_SECONDS = 8
WATCH_POLL_SECONDS = 5
WS_WARMUP_SECONDS = 3.0
# WS 建连超时（秒）——只管握手。
WS_CONNECT_TIMEOUT = 15
# WS 收帧超时（秒）——必须大于上游 uvicorn 的 ws_ping_interval（默认 20s），
# 否则 recv 会先于服务端的 ping 超时，把长连接打断成每十几秒重连一次。
WS_RECV_TIMEOUT = 45
MAX_MENU_TASKS = 5
MAX_STATUS_ENTRIES = 200

# 「最近任务」里会话的来源。伙伴会话与主对话会话来自两个不同的上游端点，
# 字段结构也不同，这里统一成一套模型再往下游发。
#
#   partner —— 伙伴聊天（/api/partners/navigation, kind="partner"）
#   group   —— 伙伴群组聊天（/api/partners/navigation, kind="group"）
#   chat    —— 主对话会话（/api/sessions）
#
# 前两者的 session_key 是"伙伴侧的会话键"，需要先 PUT web-continuity
# 把伙伴的活动会话切过去，前端打开 /partners/<id> 才会落到那条会话；
# 主对话会话没有这个概念，直接打开 /chat/<session_id>。
SOURCE_PARTNER = "partner"
SOURCE_GROUP = "group"
SOURCE_CHAT = "chat"

# 菜单与网页里显示来源用的中文标签
SOURCE_LABELS = {
    SOURCE_PARTNER: "伙伴",
    SOURCE_GROUP: "群组",
    SOURCE_CHAT: "主对话",
}

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# 只清这些镜像名的进程；占用端口的若是别的东西，一律不动。
OUR_IMAGE_NAMES = {"node.exe", "python.exe", "pythonw.exe", "deeptutor.exe"}


def resolve_home() -> Path:
    env = os.environ.get("DT_HOME", "").strip()
    if env:
        return Path(env)
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


HOME = resolve_home()
LOG_DIR = HOME / "data" / "user" / "runtime"
LOG_FILE = LOG_DIR / "launcher-tray.log"
SERVICE_LOG = LOG_DIR / "launcher.log"
NOTIFIED_FILE = LOG_DIR / "launcher-notified.json"
AVATAR_DIR = LOG_DIR / "avatars"


def find_cli() -> str | None:
    """优先用安装目录里的 venv，其次 PATH 上的 deeptutor（pip 安装的用户）。"""
    for rel in (r".venv\Scripts\deeptutor.exe", r".venv/bin/deeptutor"):
        p = HOME / rel
        if p.exists():
            return str(p)
    return shutil.which("deeptutor")


DT_EXE = find_cli()

_state = {"text": "检测中…", "running": False, "busy": False}
_state_lock = threading.Lock()
_icon: pystray.Icon | None = None
_log_lock = threading.Lock()
_shutdown = False

# 任务/伙伴缓存（watcher 线程更新，读时加 _data_lock）
_data_lock = threading.Lock()
_partners: list[dict] = []
_sessions: list[dict] = []
_task_status: dict[str, dict] = {}
_ws_threads: dict[str, threading.Thread] = {}
_notified: set[str] = set()
_notified_lock = threading.Lock()

_token = secrets.token_hex(16)
WEB_URL = f"http://{WEB_HOST}:{WEB_PORT_BASE}"
_auth_enabled = False
_ws_auth_required = False


def log(msg: str) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with _log_lock:
            with open(LOG_FILE, "a", encoding="utf-8") as fh:
                fh.write(f"[{time.strftime('%m-%d %H:%M:%S')}] {msg}\n")
    except OSError:
        pass


# 菜单里的异常走同一个日志（窗口模式没有 stderr）。
tray_menu.log_hook = log


# ---------------------------------------------------------------- 图标

def make_image(running: bool, size: int = 64) -> Image.Image:
    """托盘图标。运行时用彩色 logo，停止时转灰并降低不透明度 —— 不去改图形本身，
    这样一眼既能认出是 DeepTutor，又能看出服务在不在。"""
    logo = _load_logo()
    if logo is not None:
        img = logo.resize((size, size), Image.LANCZOS)
        if not running:
            alpha = img.split()[3].point(lambda a: int(a * 0.55))
            grey = ImageOps.grayscale(img)
            img = Image.merge("RGBA", (grey, grey, grey, alpha))
        return img
    return _fallback_image(running, size)


_logo_cache: Image.Image | None = None
_logo_loaded = False


def _load_logo() -> Image.Image | None:
    global _logo_cache, _logo_loaded
    if _logo_loaded:
        return _logo_cache
    _logo_loaded = True
    bases = []
    if getattr(sys, "frozen", False):
        bases.append(Path(getattr(sys, "_MEIPASS", "")))
    bases.append(Path(__file__).resolve().parent)
    bases.append(HOME)
    for base in bases:
        p = base / "logo.png"
        try:
            if p.exists():
                _logo_cache = Image.open(p).convert("RGBA")
                break
        except Exception as exc:
            log(f"logo load failed ({p}): {exc}")
    return _logo_cache


def _fallback_image(running: bool, size: int = 64) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    fill = (37, 99, 235, 255) if running else (148, 150, 156, 255)
    d.ellipse((2, 2, size - 3, size - 3), fill=fill)
    try:
        d.text((size / 2, size / 2), "DT", fill=(255, 255, 255, 255), anchor="mm")
    except Exception:
        d.rectangle((size * 0.3, size * 0.3, size * 0.7, size * 0.7),
                    fill=(255, 255, 255, 255))
    return img


def set_state(text: str, running: bool, notify: str = "") -> None:
    # set_state 会被轮询线程、watcher 线程和 UI 线程同时调用，
    # 读改写 _state 必须加锁，否则 running/text 可能写串。
    with _state_lock:
        changed = (_state["text"] != text) or (_state["running"] != running)
        _state["text"] = text
        _state["running"] = running
    if _icon is None:
        return
    _icon.title = f"DeepTutor · {text}"
    if changed:
        try:
            _icon.icon = make_image(running)
        except Exception:
            pass
    try:
        _icon.update_menu()
    except Exception:
        pass
    if notify:
        try:
            _icon.notify(notify, "DeepTutor")
        except Exception:
            pass


# ---------------------------------------------------------------- 探测

def is_healthy(timeout: float = 2.0) -> bool:
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(urllib.request.Request(HEALTH_URL), timeout=timeout) as resp:
            return "ready" in resp.read().decode("utf-8", "replace").lower()
    except Exception:
        return False


def netstat_listeners() -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    text = ""
    try:
        proc = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"], capture_output=True,
            timeout=8, creationflags=NO_WINDOW, stdin=subprocess.DEVNULL,
        )
        raw = proc.stdout
        if raw:
            text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    except Exception as exc:
        log(f"netstat failed: {exc}")
        return out
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[3].upper() != "LISTENING":
            continue
        addr = parts[1]
        if ":" not in addr:
            continue
        try:
            out.append((int(addr.rsplit(":", 1)[1]), int(parts[4])))
        except ValueError:
            continue
    return out


def busy_ports() -> list[int]:
    ports = {p for p, _ in netstat_listeners()}
    return [p for p in (BACKEND_PORT, FRONTEND_PORT) if p in ports]


def _decode_console(raw: bytes | str | None) -> str:
    """控制台输出可能是 UTF-8，也可能是 GBK（中文 Windows 默认）。逐个尝试。"""
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    for enc in ("utf-8", "gbk", "cp936", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def image_name(pid: int) -> str:
    try:
        proc = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True, timeout=8,
            creationflags=NO_WINDOW, stdin=subprocess.DEVNULL,
        )
        out = _decode_console(proc.stdout)
        lines = [ln for ln in out.splitlines() if ln.strip()]
        if not lines:
            return ""
        first = lines[0]
        # tasklist 在进程不存在时返回一行提示（英文 "INFO: No tasks..." /
        # 中文 "信息: 没有运行的任务..."），它不是 CSV 行。必须按编码解出来再判断，
        # 否则中文系统下这行会变成乱码而被当成进程名。
        if "INFO" in first[:24] or "信息" in first[:24]:
            return ""
        parts = [p.strip('"') for p in first.split('","')]
        return parts[0].strip('"') if parts else ""
    except Exception as exc:
        log(f"image_name({pid}) failed: {exc}")
        return ""


def process_command_lines(pids: list[int]) -> dict[int, str]:
    """批量取进程命令行。

    CIM 查询要几秒，逐个调用会拖慢停止流程，所以一次查全表再在
    Python 侧按 PID 过滤。拿不到的 PID 不会出现在结果里。

    通过临时 .ps1 调用而不是 powershell -Command：命令行参数里的
    ``$_`` / ``$()`` 会被多层引号吃掉，脚本文件没有这个问题。
    """
    if not pids:
        return {}
    script = (
        "$ErrorActionPreference = 'SilentlyContinue'\n"
        "Get-CimInstance Win32_Process | "
        "ForEach-Object { $_.ProcessId.ToString() + '|' + $_.CommandLine }\n"
    )
    tmp = Path(tempfile.gettempdir()) / f"dt_procs_{os.getpid()}.ps1"
    try:
        tmp.write_text(script, encoding="utf-8-sig")
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-File", str(tmp)],
            capture_output=True, timeout=45,
            creationflags=NO_WINDOW, stdin=subprocess.DEVNULL,
        )
        text = _decode_console(proc.stdout)
    except Exception as exc:
        log(f"command_lines failed: {exc}")
        return {}
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass

    wanted = {int(p) for p in pids}
    out: dict[int, str] = {}
    for line in text.splitlines():
        if "|" not in line:
            continue
        raw_pid, _, cmd = line.partition("|")
        try:
            pid = int(raw_pid.strip())
        except ValueError:
            continue
        if pid in wanted:
            out[pid] = cmd.strip()
    return out


def process_command_line(pid: int) -> str:
    """取单个进程的命令行；拿不到返回空串。"""
    return process_command_lines([pid]).get(int(pid), "")


def is_deeptutor_process(pid: int, cmdlines: dict[int, str] | None = None) -> bool:
    """是否确实是 DeepTutor 的进程。

    只看镜像名会误杀用户自己的 python/node 服务（任何监听 8001/3782 的
    python.exe 都会被当成残留清掉）。这里加一道命令行校验：必须出现
    deeptutor 才认定是自家进程；拿不到命令行时一律不认。
    """
    name = image_name(pid).lower()
    if name not in OUR_IMAGE_NAMES:
        return False
    if cmdlines is None:
        cmdlines = process_command_lines([pid])
    cmd = (cmdlines.get(int(pid)) or "").lower()
    if not cmd:
        # 查不到命令行（权限/进程已退出）——保守起见不动它。
        return False
    return "deeptutor" in cmd


def _run_full(args: list[str], timeout: int = 90) -> tuple[int, str]:
    """按字节收集输出再逐个编码尝试解码——中文控制台下是 GBK，不能假定 UTF-8。"""
    try:
        proc = subprocess.run(
            args, cwd=str(HOME), capture_output=True,
            timeout=timeout, creationflags=NO_WINDOW, stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return -1, "timeout"
    except Exception as exc:
        return -1, str(exc)
    raw = (proc.stdout or b"") + (proc.stderr or b"")
    for enc in ("utf-8", "gbk", "cp936", "latin-1"):
        try:
            return proc.returncode, raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return proc.returncode, raw.decode("utf-8", "replace")


def init_endpoints() -> None:
    """确定端口：环境变量 > DeepTutor 自己的 config > 默认值。"""
    global BACKEND_PORT, FRONTEND_PORT, FRONTEND_URL, HEALTH_URL
    env_b = int(os.environ.get("DT_BACKEND_PORT") or 0)
    env_f = int(os.environ.get("DT_FRONTEND_PORT") or 0)
    cfg_b = cfg_f = 0
    if not (env_b and env_f) and DT_EXE:
        code, out = _run_full([DT_EXE, "config", "show"], timeout=25)
        if code == 0:
            brace = out.find("{")
            if brace >= 0:
                try:
                    cfg = json.loads(out[brace:])
                    ports = cfg.get("ports") or {}
                    cfg_b = int(ports.get("backend") or 0)
                    cfg_f = int(ports.get("frontend") or 0)
                except (ValueError, TypeError) as exc:
                    log(f"config parse failed: {exc}")
    BACKEND_PORT = env_b or cfg_b or 8001
    FRONTEND_PORT = env_f or cfg_f or 3782
    FRONTEND_URL = f"http://localhost:{FRONTEND_PORT}"
    HEALTH_URL = f"http://127.0.0.1:{BACKEND_PORT}/health/ready"
    log(f"endpoints: backend={BACKEND_PORT} frontend={FRONTEND_PORT}")


# ---------------------------------------------------------------- 启停

def start_service() -> bool:
    if not DT_EXE:
        set_state("环境缺失", False, "找不到 deeptutor 命令，请把 exe 放到安装目录")
        return False
    log("start: deeptutor start --detach --no-browser")
    code, out = _run_full([DT_EXE, "start", "--home", str(HOME), "--detach", "--no-browser"])
    log(f"start exit={code} out={out.strip()[:600]}")
    if code != 0:
        set_state("启动失败", False, f"启动失败（exit {code}），详见日志")
        return False
    return True


def stop_service(notify: bool = True) -> bool:
    if DT_EXE:
        log("stop: deeptutor stop")
        code, out = _run_full([DT_EXE, "stop", "--home", str(HOME)])
        log(f"stop exit={code} out={out.strip()[:400]}")

    for _ in range(10):
        if not busy_ports():
            break
        time.sleep(0.5)

    # 兜底：官方 stop 只管它自己 detach 出来的进程。残留的 node/python 才清，
    # 且必须再核对命令行确实是 deeptutor —— 否则会误杀用户自己占用同端口的
    # python/node 服务。别的程序占着端口一律不动。
    listeners = netstat_listeners()
    candidates = [(p, pid) for p, pid in listeners
                  if p in (BACKEND_PORT, FRONTEND_PORT) and pid > 0]
    if candidates:
        # 一次 CIM 查询拿全部命令行，避免逐个进程 3 秒。
        cmdlines = process_command_lines([pid for _, pid in candidates])
        for port, pid in candidates:
            name = image_name(pid).lower()
            if name not in OUR_IMAGE_NAMES:
                log(f"skip pid {pid} ({name or '?'}) on {port}: 不是 DeepTutor 的进程")
                continue
            if not is_deeptutor_process(pid, cmdlines):
                log(f"skip pid {pid} ({name}) on {port}: "
                    f"命令行未确认是 deeptutor，不终止")
                continue
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=15, creationflags=NO_WINDOW)
            log(f"taskkill {pid} {name} (port {port})")

    time.sleep(1.0)
    alive = is_healthy()
    if notify:
        set_state("运行中" if alive else "已停止", alive,
                  "停止未生效，服务仍在运行" if alive else "服务已停止")
    return not alive


def wait_ready(seconds: int = READY_TIMEOUT) -> bool:
    for _ in range(seconds):
        if is_healthy():
            return True
        time.sleep(1)
    return False


def open_site() -> None:
    open_site_url(FRONTEND_URL)


def open_site_url(url: str) -> None:
    try:
        webbrowser.open(url)
    except Exception as exc:
        log(f"open {url} failed: {exc}")


def ensure_running_and_open(path: str = "") -> None:
    """确保服务就绪后打开前端；path 用于直达 /chat 等具体页面。"""
    if _state.get("busy"):
        return
    _state["busy"] = True
    target = FRONTEND_URL + path
    try:
        if is_healthy():
            set_state("运行中", True, "服务已在运行，已打开网页")
            open_site_url(target)
            return

        leftover = busy_ports()
        if leftover:
            set_state("清理残留…", False, f"端口 {leftover} 被占用，正在清理上次残留…")
            stop_service(notify=False)
            leftover = busy_ports()
            if leftover:
                set_state("端口被占用", False,
                          f"端口 {leftover} 被其他程序占用，不是 DeepTutor 的残留，请手动释放")
                return

        set_state("启动中…", False, "正在启动 DeepTutor…")
        if not start_service():
            return
        if wait_ready():
            set_state("运行中", True, "启动完成，已打开网页")
            open_site_url(target)
        else:
            set_state("启动超时", False, "等了 150 秒仍未就绪，请查看日志")
            open_path(LOG_FILE)
    finally:
        _state["busy"] = False


def open_new_task() -> None:
    """新建任务 = 打开 DeepTutor 的新对话页（服务未启动则先启动）。"""
    ensure_running_and_open(NEW_TASK_PATH)


def restart() -> None:
    set_state("重启中…", False, "正在重启服务…")
    stop_service(notify=False)
    for _ in range(20):
        if not busy_ports():
            break
        time.sleep(0.5)
    ensure_running_and_open()


def in_background(fn, *args) -> None:
    threading.Thread(target=fn, args=args, daemon=True).start()


# ---------------------------------------------------------------- 原生弹窗

# MessageBoxW 只认系统图标；要显示自己的图标必须用 MessageBoxIndirectW +
# MB_USERICON，并把 exe 里的图标资源（构建时由 --icon 写入，组图标 ID 1）传进去。
MB_OK = 0x00000000
MB_YESNO = 0x00000004
MB_ICONERROR = 0x00000010
MB_ICONQUESTION = 0x00000020
MB_USERICON = 0x00000080
MB_SETFOREGROUND = 0x00010000
MB_TOPMOST = 0x00040000
IDYES = 6

# 64 位下 HMODULE/HICON 是指针，ctypes 默认按 c_int 返回会把高位截掉，
# GetModuleHandleW 的结果一旦被截断，LoadIconW 就找不到 exe 里的图标资源。
ctypes.windll.kernel32.GetModuleHandleW.restype = wintypes.HMODULE
ctypes.windll.user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
ctypes.windll.user32.LoadIconW.restype = wintypes.HICON
ctypes.windll.user32.MessageBoxIndirectW.restype = ctypes.c_int


class _MSGBOXPARAMS(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("hwndOwner", wintypes.HWND),
        ("hInstance", wintypes.HINSTANCE),
        ("lpszText", wintypes.LPCWSTR),
        ("lpszCaption", wintypes.LPCWSTR),
        ("dwStyle", wintypes.DWORD),
        ("lpszIcon", wintypes.LPCWSTR),
        ("dwContextHelpId", ctypes.c_size_t),
        ("lpfnMsgBoxCallback", ctypes.c_void_p),
        ("dwLanguageId", wintypes.DWORD),
    ]


def _app_icon_resource():
    """返回 (hInstance, MAKEINTRESOURCE(1))；源码运行时没有 exe 图标资源，返回 None。"""
    if not getattr(sys, "frozen", False):
        return None
    hinst = ctypes.windll.kernel32.GetModuleHandleW(None)
    if not hinst:
        return None
    icon_id = ctypes.cast(ctypes.c_void_p(1), ctypes.c_wchar_p)
    if not ctypes.windll.user32.LoadIconW(hinst, icon_id):
        return None
    return hinst, icon_id


def _message_box(text: str, title: str, style: int, fallback_icon: int) -> int:
    params = _MSGBOXPARAMS()
    params.cbSize = ctypes.sizeof(_MSGBOXPARAMS)
    params.lpszText = text
    params.lpszCaption = title
    params.dwStyle = style
    icon = _app_icon_resource()
    if icon is not None:
        params.hInstance, params.lpszIcon = icon
        params.dwStyle |= MB_USERICON
    else:
        params.dwStyle |= fallback_icon
    try:
        return int(ctypes.windll.user32.MessageBoxIndirectW(ctypes.byref(params)) or 0)
    except Exception as exc:
        log(f"message box failed: {exc}")
        return 0


def confirm(title: str, text: str) -> bool:
    """是否确认；用自绘弹窗保证 logo 按 56pt 显示，失败再回退原生 MessageBox。"""
    result = app_dialog.ask(title, text, image=_load_logo())
    if result is not None:
        return result
    return _message_box(
        text, title,
        MB_YESNO | MB_SETFOREGROUND | MB_TOPMOST,
        MB_ICONQUESTION,
    ) == IDYES


def quit_all(ask: bool = True) -> None:
    """退出 = 连服务一起停。图标和服务同生同灭，不留错位。"""
    global _shutdown
    if ask and _state["running"] and not confirm(
        "退出 DeepTutor",
        "退出会停止 DeepTutor 服务，网页将无法访问。\n\n确定要退出吗？",
    ):
        return
    # 通知 watcher / websocket 线程收尾；它们都是 daemon 线程，
    # 置位后会在下一轮循环退出，避免退出时还在重连。
    _shutdown = True
    set_state("正在退出…", False)
    stop_service(notify=False)
    if _icon is not None:
        try:
            _icon.visible = False
        except Exception:
            pass
        _icon.stop()


def stop_service_only() -> None:
    """只停服务，托盘/启动器网页继续运行（网页版「停止服务」）。"""
    set_state("正在停止…", False)
    stop_service(notify=False)


# ---------------------------------------------------------------- 打开路径

def open_path(path) -> None:
    try:
        if not Path(path).exists():
            log(f"missing: {path}")
            return
        os.startfile(str(path))
    except Exception as exc:
        log(f"open failed: {exc}")


# ---------------------------------------------------------------- 任务数据与通知

def fetch_json(url: str, timeout: float = 5.0):
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(urllib.request.Request(url, headers={"Accept": "application/json"}),
                          timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return None


def _safe_timestamp(value) -> float:
    """把上游的时间戳安全转成 float。

    /api/sessions 不像 /api/partners/navigation 那样有 _navigation_timestamp
    兜底，脏数据（字符串、None、NaN）会直接把 float() 弄崩，进而让整个
    会话列表挂掉 —— 这里统一吞掉异常。
    """
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return 0.0
    if ts != ts or ts in (float("inf"), float("-inf")):   # NaN / inf
        return 0.0
    return ts


def _fetch_partner_sessions() -> tuple[list[dict], list[dict]] | None:
    """拉取伙伴侧会话（/api/partners/navigation）。

    上游的 kind 取值域是 partner / group —— 群组聊天和伙伴聊天来自同一个
    端点、同一套字段，只是 owner 是 group 而不是 partner。以前这里只留
    partner，把 group 聊天全丢了；现在两种都收下，用 source 区分。

    返回 (partners, sessions)；拉取失败（拿不到响应）返回 None，与
    "成功但列表为空"区分开 —— 后者要清空列表，前者要保留上一次的缓存。
    """
    data = fetch_json(f"http://127.0.0.1:{BACKEND_PORT}/api/partners/navigation",
                      timeout=6)
    if data is None:
        return None
    if not isinstance(data, dict):
        return [], []
    partners = data.get("partners") or []
    groups = data.get("groups") or []
    group_names = {str(g.get("group_id")): str(g.get("name") or "")
                   for g in groups if isinstance(g, dict)}

    raw_sessions = data.get("sessions") or []
    if not isinstance(raw_sessions, list):
        raw_sessions = []
    sessions: list[dict] = []
    for s in raw_sessions:
        if not isinstance(s, dict):
            continue
        kind = str(s.get("kind") or "")
        if kind not in (SOURCE_PARTNER, SOURCE_GROUP):
            continue
        owner_id = str(s.get("owner_id") or "")
        session_key = str(s.get("session_key") or "")
        # 没有 owner 或 session_key 的条目跳不过去（URL 会缺一段），直接丢掉。
        if not owner_id or not session_key:
            continue
        if kind == SOURCE_PARTNER:
            partner = next((p for p in partners
                            if str(p.get("partner_id")) == owner_id), {})
            owner_name = str(s.get("owner_name") or partner.get("name") or owner_id)
        else:
            owner_name = str(s.get("owner_name") or group_names.get(owner_id) or owner_id)
        sessions.append({
            "source": kind,
            "partner_id": owner_id,
            "partner_name": owner_name,
            "session_key": session_key,
            "title": str(s.get("title") or "") or "（无标题会话）",
            "updated_at": _safe_timestamp(s.get("updated_at")),
        })
    return partners, sessions


def _fetch_chat_sessions() -> list[dict] | None:
    """拉取主对话会话（/api/sessions）。

    这是与伙伴聊天完全不同的来源：主对话没有"伙伴"，会话 id 直接就是
    URL 的一段（前端路由 /chat/[sessionId]），也不需要 web-continuity。

    拉取失败返回 None（保留旧缓存），成功但为空返回 []（清空）。
    """
    data = fetch_json(f"http://127.0.0.1:{BACKEND_PORT}/api/sessions?limit=50",
                      timeout=6)
    if data is None:
        return None
    if not isinstance(data, dict):
        return []
    out: list[dict] = []
    raw_sessions = data.get("sessions") or []
    if not isinstance(raw_sessions, list):
        raw_sessions = []
    for s in raw_sessions:
        if not isinstance(s, dict):
            continue
        session_key = str(s.get("session_id") or s.get("id") or "")
        if not session_key:
            continue
        out.append({
            "source": SOURCE_CHAT,
            "partner_id": "",
            "partner_name": SOURCE_LABELS[SOURCE_CHAT],
            "session_key": session_key,
            "title": str(s.get("title") or "") or "（无标题会话）",
            "updated_at": _safe_timestamp(s.get("updated_at")),
        })
    return out


def _with_locked_partners() -> list[dict]:
    with _data_lock:
        return [dict(p) for p in _partners]


def _with_locked_sessions(source: str) -> list[dict]:
    """取缓存里某个来源的会话（拉取失败时保留旧数据用）。"""
    with _data_lock:
        return [dict(s) for s in _sessions if s.get("source") == source]


def refresh_navigation() -> bool:
    """更新伙伴与最近会话缓存。

    「最近任务」现在收录两类来源：
      · 伙伴侧 —— /api/partners/navigation（partner 与 group 两种 kind）
      · 主对话 —— /api/sessions

    合并后统一按 updated_at 倒序。任何一个来源拉取失败都不影响另一个，
    伙伴聊天是主路径，优先级更高。

    拉取失败（None）与「成功但列表为空」是两回事：前者保留上一次的缓存
    （服务临时不可用时不该让用户的列表突然清空），后者正常清空。
    """
    global _partners, _sessions
    partner_result = _fetch_partner_sessions()
    chat_result = _fetch_chat_sessions()
    if partner_result is None and chat_result is None:
        return False

    if partner_result is None:
        partners, partner_sessions = _with_locked_partners(), []
    else:
        partners, partner_sessions = partner_result
    if chat_result is None:
        chat_sessions = _with_locked_sessions(SOURCE_CHAT)
    else:
        chat_sessions = chat_result

    sessions = partner_sessions + chat_sessions
    sessions.sort(key=lambda x: -x["updated_at"])

    with _data_lock:
        _partners = partners
        _sessions = sessions
        # 清理不再出现的 session 状态（保留 200 条上限）
        if len(_task_status) > MAX_STATUS_ENTRIES:
            for key in list(_task_status)[: len(_task_status) - MAX_STATUS_ENTRIES]:
                _task_status.pop(key, None)
    return True


def partner_by_id(partner_id: str) -> dict:
    with _data_lock:
        return next((p for p in _partners if str(p.get("partner_id")) == partner_id), {})


def session_lookup(partner_id: str, session_key: str) -> dict:
    """按伙伴 + 会话键查会话。

    只在伙伴侧的会话里找 —— 主对话会话没有 partner_id，不参与匹配，
    避免不同来源之间出现同键误命中。
    """
    with _data_lock:
        return next((s for s in _sessions
                     if s["partner_id"] == partner_id
                     and s["session_key"] == session_key
                     and s.get("source") in (SOURCE_PARTNER, SOURCE_GROUP)), {})


_avatar_cache: dict[tuple, Image.Image] = {}


def partner_avatar_image(partner: dict, size: int = 64) -> Image.Image:
    """伙伴头像：avatar data URL（图片）→ emoji 圆盘 → 首字母圆盘。

    结果按伙伴/头像内容缓存：菜单每次打开都会取头像，反复 base64 解码 + 缩放
    是右键菜单“点了半天才出来”的主要原因之一。
    """
    avatar = str(partner.get("avatar") or "")
    key = (str(partner.get("partner_id") or ""), avatar[:64], len(avatar),
           str(partner.get("emoji") or ""), str(partner.get("color") or ""),
           str(partner.get("name") or ""), size)
    cached = _avatar_cache.get(key)
    if cached is not None:
        return cached

    img = None
    if avatar:
        try:
            if avatar.startswith("data:"):
                _, b64 = avatar.split(",", 1)
                raw = base64.b64decode(b64)
                img = Image.open(io.BytesIO(raw)).convert("RGBA")
                img = ImageOps.fit(img, (size, size), Image.LANCZOS)
        except Exception:
            img = None
    if img is None:
        emoji = str(partner.get("emoji") or "")
        color = str(partner.get("color") or "#2563eb")
        name = str(partner.get("name") or "?")
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse((0, 0, size - 1, size - 1), fill=color)
        if emoji:
            # 直接按 anchor="mm" 画 emoji 会因字体度量偏移而在小圆盘里被裁掉一角。
            # 先画到 2 倍画布，按实际墨迹 bbox 裁剪，再等比缩放到圆盘中心。
            try:
                font = ImageFont.truetype(r"C:\Windows\Fonts\seguiemj.ttf", int(size * 0.60))
                canvas = Image.new("RGBA", (size * 2, size * 2), (0, 0, 0, 0))
                cd = ImageDraw.Draw(canvas)
                try:
                    cd.text((size, size), emoji, font=font, anchor="mm", embedded_color=True)
                except TypeError:
                    cd.text((size, size), emoji, font=font, anchor="mm")
                box = canvas.getbbox()
                if box:
                    glyph = canvas.crop(box)
                    limit = max(1, int(size * 0.62))
                    glyph.thumbnail((limit, limit), Image.LANCZOS)
                    img.alpha_composite(glyph, ((size - glyph.width) // 2,
                                                (size - glyph.height) // 2))
            except Exception:
                d.text((size / 2, size / 2), emoji, anchor="mm")
        else:
            try:
                font = ImageFont.truetype(r"C:\Windows\Fonts\msyhbd.ttc", int(size * 0.42))
                d.text((size / 2, size / 2), (name.strip()[:1] or "?").upper(),
                       font=font, fill=(255, 255, 255, 255), anchor="mm")
            except Exception:
                d.text((size / 2, size / 2), (name or "?")[0], fill=(255, 255, 255, 255), anchor="mm")

    if len(_avatar_cache) >= 64:
        _avatar_cache.clear()
    _avatar_cache[key] = img
    return img


def source_avatar_image(source: str, size: int = 64) -> Image.Image:
    """非伙伴来源（群组 / 主对话）的占位头像。

    伙伴聊天继续用各自的真实头像，展示效果不变；这里只给没有头像概念的
    来源做一个统一的圆形标识，菜单里一眼能看出这条不是伙伴聊天。
    """
    key = ("__source__", source, size)
    cached = _avatar_cache.get(key)
    if cached is not None:
        return cached

    label = SOURCE_LABELS.get(source, source)[:1] or "?"
    color = {"group": "#7c3aed", "chat": "#0f766e"}.get(source, "#64748b")
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((0, 0, size - 1, size - 1), fill=color)
    try:
        font = ImageFont.truetype(r"C:\Windows\Fonts\msyhbd.ttc", int(size * 0.42))
        d.text((size / 2, size / 2), label, font=font,
               fill=(255, 255, 255, 255), anchor="mm")
    except Exception:
        d.text((size / 2, size / 2), label, fill=(255, 255, 255, 255), anchor="mm")

    if len(_avatar_cache) >= 64:
        _avatar_cache.clear()
    _avatar_cache[key] = img
    return img


def app_logo_png(size: int = 64) -> str:
    """把打包进来的 logo.png 落成 PNG，供 Toast 的 appLogoOverride 使用。"""
    try:
        AVATAR_DIR.mkdir(parents=True, exist_ok=True)
        p = AVATAR_DIR / f"app-logo-{size}.png"
        if not p.exists():
            logo = _load_logo()
            if logo is None:
                return ""
            logo.resize((size, size), Image.LANCZOS).save(p)
        return str(p)
    except Exception:
        return ""


def avatar_png_path(partner: dict) -> str:
    try:
        AVATAR_DIR.mkdir(parents=True, exist_ok=True)
        p = AVATAR_DIR / f"{partner.get('partner_id') or 'unknown'}.png"
        if not p.exists():
            partner_avatar_image(partner, 64).save(p)
        return str(p)
    except Exception:
        return ""


def load_notified() -> None:
    global _notified
    try:
        if NOTIFIED_FILE.exists():
            _notified = set(json.loads(NOTIFIED_FILE.read_text("utf-8")))
    except Exception:
        _notified = set()


def save_notified() -> None:
    try:
        NOTIFIED_FILE.parent.mkdir(parents=True, exist_ok=True)
        NOTIFIED_FILE.write_text(json.dumps(sorted(_notified), ensure_ascii=False), "utf-8")
    except Exception:
        pass


def set_web_continuity(partner_id: str, session_key: str) -> bool:
    """把该伙伴的活动 Web 会话切到目标会话，前端打开后即回到对应聊天。"""
    try:
        body = json.dumps({"enabled": True, "session_key": session_key}).encode("utf-8")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req = urllib.request.Request(
            f"http://127.0.0.1:{BACKEND_PORT}/api/partners/"
            f"{urllib.parse.quote(partner_id)}/web-continuity",
            data=body, method="PUT",
            headers={"Content-Type": "application/json"},
        )
        with opener.open(req, timeout=5) as resp:
            resp.read()
        return True
    except Exception as exc:
        log(f"web-continuity failed: {exc}")
        return False


def handle_task_done(partner_id: str, session_key: str, activity_id: str) -> None:
    """原生 `done` 事件：更新状态、持久化去重、发 Toast、绑定会话。"""
    with _notified_lock:
        if activity_id in _notified:
            return
        _notified.add(activity_id)
        save_notified()

    partner = partner_by_id(partner_id)
    sess = session_lookup(partner_id, session_key)
    title = str((sess or {}).get("title") or "") or "（无标题会话）"
    partner_name = str(partner.get("name") or partner_id)

    with _data_lock:
        st = _task_status.setdefault(session_key, {})
        st.update({"status": "completed", "updated_at": time.time(),
                   "partner_id": partner_id, "activity_id": activity_id})

    set_web_continuity(partner_id, session_key)
    icon_path = avatar_png_path(partner) or app_logo_png()
    click_url = f"{FRONTEND_URL}/partners/{urllib.parse.quote(partner_id)}"

    def _send() -> None:
        ok = tray_notify.show_toast(
            title=title,
            message=f"{partner_name} · 任务已完成",
            icon_path=icon_path,
            click_url=click_url,
            tag=f"partner:{partner_id}:{session_key}",
        )
        if not ok and _icon is not None:
            try:
                _icon.notify(f"[{partner_name}] {title}", "DeepTutor 任务完成")
            except Exception:
                pass

    threading.Thread(target=_send, daemon=True).start()
    log(f"task done: {partner_id} {session_key} {title[:40]!r}")


def ws_watcher(partner_id: str) -> None:
    """订阅伙伴 activity feed：user_echo → running，done → 完成通知。

    只处理“本次连接热身期之后开始”的任务，重启后不会把历史完成误判为新完成。
    """
    global _ws_auth_required
    try:
        import websocket  # type: ignore
    except Exception:
        log("websocket-client 未安装，任务完成通知不可用")
        return

    while not _shutdown:
        if _ws_auth_required:
            return
        connected = False
        try:
            url = (f"ws://127.0.0.1:{BACKEND_PORT}/ws/partners/"
                   f"{urllib.parse.quote(partner_id)}")
            ws = websocket.create_connection(url, timeout=WS_CONNECT_TIMEOUT,
                                             origin=f"http://127.0.0.1:{WEB_PORT_BASE}")
            # create_connection 的 timeout 会落到 socket 上，recv() 也受它约束。
            # 上游 uvicorn 默认每 20 秒才发一次 ping（ws_ping_interval），
            # 而 ping 帧被 websocket-client 内部消费、不会唤醒 recv()，
            # 所以 recv 超时必须**大于**那个间隔，否则连接会在收到任何
            # 业务数据之前就被判定超时而断开 —— 表现就是每 15 秒重连一次，
            # 日志被刷爆，而且重连空窗期内的完成事件会丢。
            ws.settimeout(WS_RECV_TIMEOUT)
            connected = True
            ws.send(json.dumps({"action": "attach", "session_key": "tray-watcher",
                                 "include_activity": True}))
            warmup_until = time.time() + WS_WARMUP_SECONDS
            live: dict[str, float] = {}
            log(f"ws connected: {partner_id}")
            while not _shutdown:
                try:
                    raw = ws.recv()
                except websocket.WebSocketTimeoutException:
                    # 只是这段时间没有业务数据，连接本身还是好的。
                    # 探一次活然后继续等，不要断开重连。
                    try:
                        ws.ping()
                    except Exception:
                        break          # ping 失败说明连接真的断了，交给外层重连
                    continue
                if not raw:
                    break             # 服务端关闭了连接，重连
                try:
                    frame = json.loads(raw)
                except Exception:
                    continue
                ftype = frame.get("type")
                if ftype == "user_echo":
                    aid = str(frame.get("activity_id") or "")
                    key = str(frame.get("session_key") or "")
                    if aid and key:
                        live[aid] = time.time()
                        with _data_lock:
                            st = _task_status.setdefault(key, {})
                            st.update({"status": "running", "updated_at": time.time(),
                                       "partner_id": partner_id})
                elif ftype == "done":
                    aid = str(frame.get("activity_id") or "")
                    key = str(frame.get("session_key") or "")
                    started = live.get(aid)
                    if aid and key and started is not None and started >= warmup_until:
                        handle_task_done(partner_id, key, aid)
                    if aid:
                        live.pop(aid, None)
        except Exception as exc:
            if _shutdown:
                break
            code = getattr(exc, "status_code", None) or getattr(exc, "close_code", None)
            if code == 4001 or "4001" in str(exc):
                if not _ws_auth_required:
                    _ws_auth_required = True
                    log("WebSocket 需要认证（AUTH_ENABLED=true），任务完成通知已禁用")
                return
            log(f"ws {partner_id} error: {exc}")
        # 连接失败（服务未启动等）时放慢重试，避免刷日志；断开重连则快一些。
        time.sleep(3 if connected else 15)


def watcher_loop() -> None:
    """轮询导航数据 + 维护伙伴 WebSocket 连接。"""
    global _auth_enabled, _ws_auth_required
    while not _shutdown:
        status = fetch_json(f"http://127.0.0.1:{BACKEND_PORT}/api/auth/status", timeout=4)
        if status is not None:
            _auth_enabled = bool(status.get("enabled"))
        if refresh_navigation():
            if not _auth_enabled and not _ws_auth_required:
                with _data_lock:
                    ids = [str(p.get("partner_id")) for p in _partners if p.get("partner_id")]
                for pid in ids:
                    t = _ws_threads.get(pid)
                    if t is None or not t.is_alive():
                        t = threading.Thread(target=ws_watcher, args=(pid,), daemon=True)
                        _ws_threads[pid] = t
                        t.start()
        time.sleep(WATCH_POLL_SECONDS)


# ---------------------------------------------------------------- 本地网页服务器

def load_web_page() -> str:
    html = ""
    bases = []
    if getattr(sys, "frozen", False):
        bases.append(Path(getattr(sys, "_MEIPASS", "")))
    bases.append(Path(__file__).resolve().parent)
    for base in bases:
        p = base / "web" / "index.html"
        try:
            if p.exists():
                html = p.read_text("utf-8")
                break
        except Exception:
            continue
    if not html:
        html = "<!doctype html><meta charset=utf-8><h1>DeepTutor Launcher</h1><p>web/index.html 未找到</p>"
    return html.replace("__LAUNCHER_TOKEN__", _token)


def build_state() -> dict:
    with _data_lock:
        partners = [dict(p) for p in _partners]
        sessions = []
        for s in _sessions:
            item = dict(s)
            st = _task_status.get(s["session_key"], {})
            item["status"] = st.get("status", "idle")
            # 来源标签给前端直接用，省得它自己再映射一遍。
            item["source_label"] = SOURCE_LABELS.get(
                item.get("source") or SOURCE_PARTNER, "")
            sessions.append(item)
    return {
        "running": bool(_state.get("running")),
        "status_text": _state.get("text", ""),
        "frontend_url": FRONTEND_URL,
        "launcher_url": WEB_URL,
        "version": __version__,
        "partners": partners,
        "sessions": sessions,
        "auth_enabled": _auth_enabled,
        "ws_enabled": not _ws_auth_required,
    }


def dispatch_action(action: str, payload: dict) -> tuple[bool, str]:
    if action == "open":
        open_site()
    elif action == "start":
        in_background(ensure_running_and_open)
    elif action == "restart":
        in_background(restart)
    elif action == "stop":
        in_background(stop_service_only)
    elif action == "quit":
        in_background(lambda: quit_all(ask=False))
    elif action == "refresh":
        in_background(refresh_navigation)
    elif action == "open_chat":
        partner_id = str(payload.get("partner_id") or "")
        session_key = str(payload.get("session_key") or "")
        source = str(payload.get("source") or SOURCE_PARTNER)
        if source not in (SOURCE_PARTNER, SOURCE_GROUP, SOURCE_CHAT):
            source = SOURCE_PARTNER
        # 主对话会话没有 partner_id；伙伴侧会话必须有，否则跳不过去。
        if not session_key:
            return False, "缺少 session_key"
        if source != SOURCE_CHAT and not partner_id:
            return False, "缺少 partner_id"
        in_background(open_chat, partner_id, session_key, source)
    else:
        return False, f"未知动作: {action}"
    return True, ""


class LauncherHandler(http.server.BaseHTTPRequestHandler):
    server_version = f"DeepTutorLauncher/{__version__}"

    def log_message(self, fmt, *args):  # 静默访问日志
        pass

    def _send_json(self, obj: dict, code: int = 200) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_html(self, html: str) -> None:
        data = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                         "img-src data:; connect-src 'self'")
        self.end_headers()
        self.wfile.write(data)

    def _check_token(self) -> bool:
        token = self.headers.get("X-Launcher-Token", "")
        if not token:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            token = qs.get("token", [""])[0]
        return bool(token) and secrets.compare_digest(token, _token)

    def _check_origin(self) -> bool:
        origin = self.headers.get("Origin") or self.headers.get("Referer") or ""
        if not origin:
            return True
        try:
            host = urllib.parse.urlparse(origin).netloc
        except Exception:
            return False
        port = self.server.server_port
        return host in (f"{WEB_HOST}:{port}", f"localhost:{port}")

    def _check_host(self) -> bool:
        """Host 头必须指向回环地址。

        否则恶意页面可以把自有域名解析到 127.0.0.1，浏览器认为是同源，
        于是能直接读到内嵌 token 的首页 —— 这就是 DNS rebinding。
        """
        host = (self.headers.get("Host") or "").strip().lower()
        if not host:
            return False
        # 去掉端口；IPv6 写法是 [::1]:47914
        if host.startswith("["):
            host = host.split("]")[0].lstrip("[")
        else:
            host = host.rsplit(":", 1)[0] if ":" in host else host
        return host in ("127.0.0.1", "localhost")

    def do_GET(self):
        if not self._check_host():
            self._send_json({"ok": False, "error": "forbidden"}, 403)
            return
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            self._send_html(load_web_page())
            return
        if parsed.path == "/api/state":
            if not self._check_token():
                self._send_json({"ok": False, "error": "forbidden"}, 403)
                return
            self._send_json(build_state())
            return
        self._send_json({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        if not self._check_host():
            self._send_json({"ok": False, "error": "forbidden"}, 403)
            return
        if not self._check_token() or not self._check_origin():
            self._send_json({"ok": False, "error": "forbidden"}, 403)
            return
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/api/action":
            self._send_json({"ok": False, "error": "not found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except Exception:
            self._send_json({"ok": False, "error": "bad json"}, 400)
            return
        ok, error = dispatch_action(str(payload.get("action") or ""), payload)
        self._send_json({"ok": ok, "error": error})


class LauncherServer(http.server.ThreadingHTTPServer):
    # http.server 默认 allow_reuse_address=1，在 Windows 上会让两个进程
    # 绑到同一端口（请求随机落到其中一个，表现为「网页内容是旧版本」）。
    # 关掉它，端口冲突时直接失败，由 start_http_server 换端口。
    allow_reuse_address = False
    daemon_threads = True


def _port_free(host: str, port: int) -> bool:
    """端口是否真的空闲。

    http.server 的 allow_reuse_address=1 在 Windows 上允许绑定到已被占用的
    端口（SO_REUSEADDR 语义），两个进程会绑到同一个端口、请求随机落到
    其中一个 —— 表现为「网页打不开/内容是旧版本」这类怪问题。
    所以绑定前先自己探一下。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def start_http_server():
    global WEB_URL
    for port in range(WEB_PORT_BASE, WEB_PORT_BASE + 8):
        if not _port_free(WEB_HOST, port):
            continue
        try:
            httpd = LauncherServer((WEB_HOST, port), LauncherHandler)
        except OSError:
            continue
        WEB_URL = f"http://{WEB_HOST}:{port}"
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        log(f"web launcher: {WEB_URL}")
        return httpd
    log("web launcher: 端口全部被占用，启动器页面不可用")
    return None


def open_chat(partner_id: str, session_key: str, source: str = SOURCE_PARTNER) -> None:
    """打开某条会话。

    伙伴侧会话（partner / group）：先 PUT web-continuity 把该伙伴的活动
    Web 会话切到目标会话，再打开 /partners/<id>，前端打开即落到那条会话。

    主对话会话（chat）：没有 web-continuity 这回事 —— 会话 id 本身就是
    前端路由的一段，直接打开 /chat/<session_id>。
    """
    if not is_healthy():
        ensure_running_and_open()
    if source == SOURCE_CHAT:
        open_site_url(f"{FRONTEND_URL}/chat/{urllib.parse.quote(session_key)}")
        return
    set_web_continuity(partner_id, session_key)
    time.sleep(0.3)
    open_site_url(f"{FRONTEND_URL}/partners/{urllib.parse.quote(partner_id)}")


# ---------------------------------------------------------------- 托盘

class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


class _NOTIFYICONIDENTIFIER(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
                ("uID", wintypes.UINT), ("guidItem", _GUID)]


_shell32 = ctypes.windll.shell32
_shell32.Shell_NotifyIconGetRect.argtypes = [
    ctypes.POINTER(_NOTIFYICONIDENTIFIER), ctypes.POINTER(wintypes.RECT)]
_shell32.Shell_NotifyIconGetRect.restype = ctypes.c_long


def tray_icon_rect():
    """托盘图标的屏幕矩形；pystray 用 id(icon) 作为 NOTIFYICONDATAW.hID。"""
    try:
        if _icon is None:
            return None
        hwnd = getattr(_icon, "_hwnd", None)
        if not hwnd:
            return None
        nid = _NOTIFYICONIDENTIFIER()
        nid.cbSize = ctypes.sizeof(_NOTIFYICONIDENTIFIER)
        nid.hWnd = hwnd
        nid.uID = id(_icon)
        rect = wintypes.RECT()
        hr = _shell32.Shell_NotifyIconGetRect(ctypes.byref(nid), ctypes.byref(rect))
        if hr == 0:
            return (rect.left, rect.top, rect.right, rect.bottom)
        log(f"tray icon rect failed: hr={hr}")
    except Exception as exc:
        log(f"tray icon rect failed: {exc}")
    return None


def _pick_menu_tasks(sessions: list[dict], limit: int) -> list[dict]:
    """挑菜单里要显示的会话。

    菜单最多 5 条，纯按时间排序会让最近的主对话把伙伴聊天整个挤掉 ——
    而伙伴聊天是托盘的主路径，不能因此"看起来消失"。

    所以：伙伴侧会话（partner / group）先按时间取，其余名额再由其他来源
    按时间补齐，整体仍保持时间倒序的观感。
    """
    buddy = [s for s in sessions if s.get("source") in (SOURCE_PARTNER, SOURCE_GROUP)]
    others = [s for s in sessions if s.get("source") not in (SOURCE_PARTNER, SOURCE_GROUP)]
    picked = buddy[:limit]
    if len(picked) < limit:
        picked += others[: limit - len(picked)]
    picked.sort(key=lambda s: -s["updated_at"])
    return picked


def menu_items() -> list:
    """托盘右键菜单：只保留 打开网页 / 最近任务 / 新建任务 / 退出 四项。

    最近任务是二级菜单：主面板保持 4 行，任务列表按需展开，不再把整份操作表
    平铺出来。头像走 partner_avatar_image 的缓存，重复打开不重新解码。

    列表现在混合了伙伴聊天与主对话等来源，按 updated_at 倒序统一排列。
    伙伴聊天仍然用自己的真实头像；其他来源用来源标识头像区分。
    """
    with _data_lock:
        tasks = _pick_menu_tasks([dict(s) for s in _sessions], MAX_MENU_TASKS)
        partners_map = {str(p.get("partner_id")): p for p in _partners}

    recent: list = []
    for s in tasks:
        pid = s["partner_id"]
        source = s.get("source") or SOURCE_PARTNER
        title = s.get("title") or "（无标题会话）"
        try:
            if source == SOURCE_PARTNER:
                avatar = partner_avatar_image(partners_map.get(pid, {}), 64)
            else:
                avatar = source_avatar_image(source, 64)
        except Exception:
            avatar = None
        # id 里带 source，点击时才能知道是走 web-continuity 还是直接开 /chat。
        recent.append(tray_menu.MenuItem(
            "item", title, id=f"task:{source}:{pid}:{s['session_key']}", avatar=avatar,
        ))
    if not recent:
        recent.append(tray_menu.MenuItem("item", "暂无任务", id="", enabled=False))

    return [
        tray_menu.MenuItem("item", "打开网页", id="open", primary=True),
        tray_menu.MenuItem("item", "最近任务", id="recent", children=recent),
        tray_menu.MenuItem("item", "新建任务", id="new"),
        tray_menu.MenuItem("item", "退出", id="quit"),
    ]


def on_menu_choice(mid: str) -> None:
    if mid == "open":
        open_site()
    elif mid == "new":
        in_background(open_new_task)
    elif mid == "quit":
        in_background(quit_all)
    elif mid.startswith("task:"):
        # 格式 task:<source>:<partner_id>:<session_key>
        parts = mid.split(":", 3)
        if len(parts) < 4:
            return
        _, source, pid, key = parts
        if source not in (SOURCE_PARTNER, SOURCE_GROUP, SOURCE_CHAT):
            source = SOURCE_PARTNER
        in_background(open_chat, pid, key, source)


def build_menu() -> pystray.Menu:
    """原生菜单只留一个默认项 —— 左键单击用它（打开网页）。
    右键会被 install_hooks 拦掉，改走自绘菜单，不会显示出来。"""
    return pystray.Menu(
        pystray.MenuItem("打开网页", lambda: open_site(), default=True),
    )


def install_hooks() -> None:
    """拦掉 pystray 的右键分支，换成自绘菜单。"""
    from pystray import _util
    from pystray._win32 import Icon as Win32Icon

    rbutton = _util.win32.WM_RBUTTONUP
    original = Win32Icon._on_notify

    def patched(self, wparam, lparam):
        if lparam == rbutton:
            pt = wintypes.POINT()
            ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
            try:
                ctypes.windll.user32.SetForegroundWindow(self._hwnd)
            except Exception:
                pass
            rect = tray_icon_rect()
            choice = tray_menu.show_menu(menu_items(), pt.x, pt.y,
                                         keep_rects=[rect] if rect else None)
            if choice:
                on_menu_choice(choice)
            return
        return original(self, wparam, lparam)

    Win32Icon._on_notify = patched


def poll_loop() -> None:
    """定期校正状态：服务被外部停掉/启起时，图标要跟着变。"""
    while True:
        time.sleep(POLL_SECONDS)
        if _state.get("busy"):
            continue
        alive = is_healthy()
        if alive != _state["running"]:
            set_state("运行中" if alive else "已停止", alive)
        elif alive and _state["text"] not in ("运行中",):
            set_state("运行中", True)


def main() -> None:
    global _icon, _auth_enabled

    # DPI 感知必须在任何窗口创建之前设置，否则 Windows 会把窗口位图按比例拉伸。
    tray_menu.set_dpi_aware()
    init_endpoints()

    if "--dump-icon" in sys.argv:
        idx = sys.argv.index("--dump-icon")
        out = sys.argv[idx + 1] if len(sys.argv) > idx + 1 else "tray-icon.png"
        make_image(True, 256).save(out)
        print(json.dumps({"written": out, "logo": _load_logo() is not None},
                         ensure_ascii=False))
        return

    if "--test-toast" in sys.argv:
        refresh_navigation()
        with _data_lock:
            partner = dict(_partners[0]) if _partners else {}
        icon = avatar_png_path(partner) if partner else ""
        if not icon:
            icon = app_logo_png()
        ok = tray_notify.show_toast(
            title="DeepTutor 启动器测试",
            message=f"来自 {partner.get('name') or 'DeepTutor'} 的测试通知",
            icon_path=icon,
            click_url=FRONTEND_URL,
            tag="selftest",
        )
        print(json.dumps({"toast_ok": ok, "icon": icon}, ensure_ascii=False, indent=2))
        return

    if "--test-dialog" in sys.argv:
        ok = confirm(
            "DeepTutor 弹窗图标测试",
            "如果对话框左侧显示的是 DeepTutor 图标（而不是系统问号），\n"
            "说明自定义弹窗图标已生效。",
        )
        print(json.dumps({"confirm": ok,
                          "custom_icon": _app_icon_resource() is not None},
                         ensure_ascii=False))
        return

    if "--check" in sys.argv:
        refresh_navigation()
        status = fetch_json(f"http://127.0.0.1:{BACKEND_PORT}/api/auth/status", timeout=4)
        if status is not None:
            _auth_enabled = bool(status.get("enabled"))
        print(json.dumps({
            "home": str(HOME),
            "deeptutor_cli": DT_EXE,
            "healthy": is_healthy(),
            "busy_ports": busy_ports(),
            "backend_port": BACKEND_PORT,
            "frontend_url": FRONTEND_URL,
            "launcher_url": WEB_URL,
            "sessions": len(_sessions),
            "auth_enabled": _auth_enabled,
            "ws_enabled": not _ws_auth_required,
            "logo_bundled": _load_logo() is not None,
            "dialog_icon": _app_icon_resource() is not None,
            "frozen": bool(getattr(sys, "frozen", False)),
            "dpi_scale": tray_menu.dpi_scale(),
            "log": str(LOG_FILE),
        }, ensure_ascii=False, indent=2))
        return

    if not DT_EXE:
        message = ("没找到 DeepTutor 命令。\n\n请把这个 exe 放在 DeepTutor 安装目录下"
                   "（和 start.bat 同一层），或确保 deeptutor 在 PATH 上。")
        if not app_dialog.alert("DeepTutor 托盘启动器", message, image=_load_logo()):
            _message_box(message, "DeepTutor 托盘启动器",
                         MB_OK | MB_SETFOREGROUND | MB_TOPMOST, MB_ICONERROR)
        return

    load_notified()
    install_hooks()
    start_http_server()
    _icon = pystray.Icon("deeptutor-tray", make_image(False),
                         "DeepTutor · 检测中…", build_menu())
    threading.Thread(target=lambda: (time.sleep(0.3), ensure_running_and_open()),
                     daemon=True).start()
    threading.Thread(target=poll_loop, daemon=True).start()
    threading.Thread(target=watcher_loop, daemon=True).start()
    _icon.run()


if __name__ == "__main__":
    # 诊断类参数必须在单实例检查之前处理：否则托盘已在运行时，
    # 第二次调用会被判定为“重复启动”，直接开网页走人。
    if ("--check" in sys.argv or "--dump-icon" in sys.argv or "--test-toast" in sys.argv
            or "--test-dialog" in sys.argv):
        main()
        sys.exit(0)

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", MUTEX_PORT))
    except OSError:
        open_site()
        sys.exit(0)
    probe.listen(1)
    try:
        main()
    finally:
        try:
            probe.close()
        except OSError:
            pass