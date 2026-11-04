"""自绘托盘弹出菜单（Win32 分层窗口 + PIL 渲染）。

为什么不用原生菜单
------------------
pystray 走 `TrackPopupMenuEx`，外观、字号、行高全由系统主题决定，改不了样式；进程
不是 DPI 感知时还会被系统位图拉伸，于是又糊又不跟手。

这里改用 `WS_EX_LAYERED` 弹出窗口 + `UpdateLayeredWindow` 提交一张 32 位带 alpha 的
位图：文字按设备像素渲染（不缩放、不糊），圆角和投影都是真实 alpha，可任意调样式。

稳定性
------
早期版本每次打开菜单都调用 `RegisterClassExW` 注册同名窗口类。第一次成功后，后续注册
静默失败，`CreateWindowExW` 仍然成功，但窗口过程仍是**第一次**那个 `_Popup` 实例的
Python 回调。实例销毁后该回调是悬挂指针，第二次右键就可能卡死或崩溃。

现在窗口类只注册一次，窗口过程是模块级常量；所有消息按 HWND 分发到当前实例。
渲染也做了缓存：面板底图只画一次，悬停只重绘一行，字体只加载一次。

子菜单
------
`MenuItem.children` 非空时，悬停/点击会在右侧弹出二级面板（`WS_EX_NOACTIVATE`，
不会抢主面板焦点）。两级面板共用同一个消息循环。

用法
----
    items = [MenuItem("item", "打开网页", id="open"),
             MenuItem("item", "最近任务", id="recent", children=[...]),
             MenuItem("item", "新建任务", id="new"),
             MenuItem("item", "退出", id="quit")]
    choice = show_menu(items, x, y)      # 阻塞直到选择或取消；返回 id 或 None
"""

from __future__ import annotations

import ctypes
import sys
import traceback
from ctypes import wintypes
from dataclasses import dataclass, field

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

# ---------------------------------------------------------------- Win32

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# 64 位下句柄是指针，必须声明返回类型，否则 ctypes 按 c_int 截断高位。
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
]
user32.CreateWindowExW.restype = wintypes.HWND
user32.GetDC.restype = wintypes.HDC
user32.GetForegroundWindow.restype = wintypes.HWND
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateDIBSection.restype = wintypes.HBITMAP

WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x00080000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST = 0x00000008
WS_EX_NOACTIVATE = 0x08000000
ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01

WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_ACTIVATE = 0x0006
WM_KEYDOWN = 0x0100
WM_MOUSEMOVE = 0x0200
WM_LBUTTONUP = 0x0202
WM_TIMER = 0x0113
WM_NULL = 0x0000
VK_ESCAPE = 0x1B
VK_LBUTTON = 0x01
VK_RBUTTON = 0x02
SW_HIDE = 0
SW_SHOW = 5
SW_SHOWNOACTIVATE = 4
HWND_TOPMOST = -1
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOACTIVATE = 0x0010
DIB_RGB_COLORS = 0
IDC_ARROW = 32512
ERROR_CLASS_ALREADY_EXISTS = 1410

kernel32.GetCurrentThreadId.restype = wintypes.DWORD
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT,
                                      wintypes.WPARAM, wintypes.LPARAM]
user32.PostThreadMessageW.restype = wintypes.BOOL

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT), ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH), ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", wintypes.HICON),
    ]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


class RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


# GDI 句柄同样是指针宽度；不声明 argtypes，ctypes 会按 C int 传参并溢出。
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.POINTER(BITMAPINFO),
                                   wintypes.UINT, ctypes.POINTER(ctypes.c_void_p),
                                   wintypes.HANDLE, wintypes.DWORD]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.GetDeviceCaps.argtypes = [wintypes.HDC, ctypes.c_int]
user32.GetDC.argtypes = [wintypes.HWND]
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.UpdateLayeredWindow.argtypes = [
    wintypes.HWND, wintypes.HDC, ctypes.POINTER(wintypes.POINT),
    ctypes.POINTER(wintypes.SIZE), wintypes.HDC, ctypes.POINTER(wintypes.POINT),
    wintypes.DWORD, ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD,
]
user32.UpdateLayeredWindow.restype = wintypes.BOOL


user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]

# ---------------------------------------------------------------- 视觉参数

WIDTH = 236
SUB_WIDTH = 280
PAD_TOP = 6
PAD_BOTTOM = 6
ITEM_H = 32
SEP_H = 8
RADIUS = 8
TEXT_PAD_X = 12
SHADOW = 14
AVATAR_SIZE = 20
AVATAR_X = 11
TASK_TEXT_X = AVATAR_X + AVATAR_SIZE + 9
ARROW_RIGHT = 13

BG = (255, 255, 255, 255)
BORDER = (224, 226, 231, 255)
HOVER = (238, 243, 252, 255)
TEXT = (26, 30, 35, 255)
TEXT_MUTED = (150, 156, 164, 255)
TEXT_DISABLED = (192, 196, 203, 255)
SEP = (238, 239, 242, 255)
ARROW = (150, 156, 164, 255)
SHADOW_COLOR = (15, 23, 42, 255)

FONT_PATHS = {
    False: (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simhei.ttf"),
    True: (r"C:\Windows\Fonts\msyhbd.ttc", r"C:\Windows\Fonts\msyh.ttc"),
}

_fonts: dict[tuple[int, bool], ImageFont.ImageFont] = {}
# launcher 会把它接到 launcher-tray.log；窗口模式没有 stderr，不能靠 print_exc。
log_hook = None


def _report_exc() -> None:
    try:
        if log_hook is not None:
            log_hook(traceback.format_exc())
        elif sys.stderr is not None:
            traceback.print_exc()
    except Exception:
        pass


@dataclass
class MenuItem:
    kind: str
    text: str = ""
    id: str = ""
    enabled: bool = True
    primary: bool = False
    avatar: Image.Image | None = None   # 任务条目左侧头像（RGBA 方形图，绘制时裁圆）
    children: list["MenuItem"] | None = None
    _y0: int = field(default=0, repr=False)
    _y1: int = field(default=0, repr=False)


def dpi_scale() -> float:
    try:
        hdc = user32.GetDC(0)
        dpi = gdi32.GetDeviceCaps(hdc, 88)
        user32.ReleaseDC(0, hdc)
        return max(1.0, dpi / 96.0)
    except Exception:
        return 1.0


def _font(px: int, bold: bool = False):
    key = (px, bold)
    cached = _fonts.get(key)
    if cached is not None:
        return cached
    font = None
    for path in FONT_PATHS[bold]:
        try:
            font = ImageFont.truetype(path, px, index=0)
            break
        except Exception:
            continue
    if font is None:
        font = ImageFont.load_default()
    _fonts[key] = font
    return font


def _layout(items: list[MenuItem], s: float, width: int) -> tuple[int, int]:
    y = int(PAD_TOP * s)
    for it in items:
        h = {"header": 26, "sep": SEP_H}.get(it.kind, ITEM_H)
        it._y0, it._y1 = y, y + int(h * s)
        y = it._y1
    return int(width * s), y + int(PAD_BOTTOM * s)


def _ellipsize(text: str, font: ImageFont.ImageFont, max_w: float) -> str:
    """按像素宽度截断，末字保留省略号。

    不能按字符数截断：中文 13px 字在 150% DPI 下接近 20 物理像素，
    18 个字就会顶出 280pt 宽的子菜单。这里用 getlength 量真实宽度。
    """
    if max_w <= 0:
        return ""
    try:
        if font.getlength(text) <= max_w:
            return text
    except Exception:
        return text
    ell = "…"
    try:
        ell_w = font.getlength(ell)
    except Exception:
        return text
    if ell_w > max_w:
        return ""
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if font.getlength(text[:mid]) + ell_w <= max_w:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo] + ell


# ---------------------------------------------------------------- 渲染

class Panel:
    """一块菜单面板：底图只渲染一次，悬停时仅复制底图并重绘一行。"""

    def __init__(self, items: list[MenuItem], s: float, width: int = WIDTH):
        self.items = items
        self.s = s
        self.body_w, self.body_h = _layout(items, s, width)
        self.pad = int(SHADOW * s)
        self.w = self.body_w + self.pad * 2
        self.h = self.body_h + self.pad * 2
        self._avatars: dict[int, Image.Image] = {}
        self._masks: dict[int, Image.Image] = {}
        self.base = self._build()

    def _avatar(self, it: MenuItem) -> Image.Image | None:
        if it.avatar is None:
            return None
        size = max(1, int(AVATAR_SIZE * self.s))
        cached = self._avatars.get(id(it))
        if cached is None or cached.width != size:
            # 等比裁切到正方形，不拉伸；非方形头像只裁掉溢出部分。
            cached = ImageOps.fit(it.avatar, (size, size), Image.LANCZOS,
                                  centering=(0.5, 0.5))
            self._avatars[id(it)] = cached
        return cached

    def _circle_mask(self, size: int) -> Image.Image:
        mask = self._masks.get(size)
        if mask is None:
            ss = 4                      # 4 倍超采样，圆边不锯齿
            big = Image.new("L", (size * ss, size * ss), 0)
            ImageDraw.Draw(big).ellipse((0, 0, size * ss - 1, size * ss - 1), fill=255)
            mask = big.resize((size, size), Image.LANCZOS)
            self._masks[size] = mask
        return mask

    def _draw_row(self, img: Image.Image, d: ImageDraw.ImageDraw,
                  it: MenuItem, hovered: bool) -> None:
        pad, s = self.pad, self.s
        top = pad + it._y0
        mid = pad + (it._y0 + it._y1) // 2
        if it.kind == "sep":
            d.line((pad + int(12 * s), mid, pad + self.body_w - int(12 * s), mid),
                   fill=SEP, width=max(1, round(s)))
            return
        if it.kind == "header":
            d.text((pad + int(TEXT_PAD_X * s), mid), it.text,
                   font=_font(int(12 * s)), fill=TEXT_MUTED, anchor="lm")
            return

        if hovered and it.enabled:
            d.rounded_rectangle(
                (pad + int(4 * s), top + int(2 * s),
                 pad + self.body_w - int(4 * s), top + int(ITEM_H * s) - int(2 * s)),
                radius=int(6 * s), fill=HOVER)

        text_x = pad + int(TEXT_PAD_X * s)
        avatar = self._avatar(it)
        if avatar is not None:
            size = avatar.width
            ax = pad + int(AVATAR_X * s)
            ay = mid - size // 2
            img.paste(avatar, (ax, ay), self._circle_mask(size))
            d.ellipse((ax, ay, ax + size - 1, ay + size - 1),
                      outline=(226, 229, 235, 255), width=max(1, round(s)))
            text_x = pad + int(TASK_TEXT_X * s)

        font = _font(int(13 * s), it.primary)
        right = pad + self.body_w - int(12 * s)
        if it.children is not None:
            right -= int(14 * s)
        label = _ellipsize(it.text, font, max(0, right - text_x))
        d.text((text_x, mid), label, font=font,
               fill=TEXT if it.enabled else TEXT_DISABLED, anchor="lm")

        if it.children is not None:
            cx = pad + self.body_w - int(ARROW_RIGHT * s)
            cy = mid
            a = int(4 * s)
            width = max(1, round(s * 0.9))
            d.line((cx - a, cy - a, cx, cy), fill=ARROW, width=width)
            d.line((cx, cy, cx - a, cy + a), fill=ARROW, width=width)

    def _build(self) -> Image.Image:
        pad = self.pad
        img = Image.new("RGBA", (self.w, self.h), (0, 0, 0, 0))

        shadow = Image.new("L", img.size, 0)
        ImageDraw.Draw(shadow).rounded_rectangle(
            (pad, pad + int(3 * self.s), pad + self.body_w - 1, pad + self.body_h - 1),
            radius=int(RADIUS * self.s), fill=58)
        shadow = shadow.filter(ImageFilter.GaussianBlur(int(6 * self.s)))
        img.paste(Image.new("RGBA", img.size, SHADOW_COLOR), (0, 0), shadow)

        d = ImageDraw.Draw(img)
        d.rounded_rectangle((pad, pad, pad + self.body_w - 1, pad + self.body_h - 1),
                            radius=int(RADIUS * self.s), fill=BG,
                            outline=BORDER, width=max(1, round(self.s)))
        for it in self.items:
            self._draw_row(img, d, it, False)
        return img

    def hovered(self, idx: int) -> Image.Image:
        if idx < 0 or idx >= len(self.items):
            return self.base
        it = self.items[idx]
        if it.kind != "item" or not it.enabled:
            return self.base
        img = self.base.copy()
        d = ImageDraw.Draw(img)
        self._draw_row(img, d, it, True)
        return img

    def item_at(self, y: int) -> int:
        ly = y - self.pad
        for idx, it in enumerate(self.items):
            if it.kind == "item" and it._y0 <= ly < it._y1:
                return idx
        return -1


# ---------------------------------------------------------------- 分层窗口

_MENU_CLASS = "DeepTutorTrayMenuV3"
_windows: dict[int, "_PanelWindow"] = {}
_class_registered = False
_active = False
modal_open = False          # dialog.py 打开确认框时置位，避免菜单叠在弹窗上
_session: "_MenuSession | None" = None


def _dispatch(hwnd, msg, wparam, lparam):
    """模块级窗口过程：所有面板共用，按 HWND 路由到实例。"""
    win = _windows.get(int(hwnd or 0))
    if win is not None:
        try:
            return win.on_message(hwnd, msg, wparam, lparam)
        except Exception:
            _report_exc()
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


# 必须常驻：窗口类保存的是这个 Python 回调的地址，回收后消息就会打到野指针上。
_WINDOW_PROC = WNDPROC(_dispatch)


def _ensure_class() -> None:
    global _class_registered
    if _class_registered:
        return
    hinst = kernel32.GetModuleHandleW(None)
    wc = WNDCLASSEXW()
    wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
    wc.style = 0
    wc.lpfnWndProc = _WINDOW_PROC
    wc.hInstance = hinst
    wc.hCursor = user32.LoadCursorW(0, ctypes.cast(ctypes.c_void_p(IDC_ARROW),
                                                   ctypes.c_wchar_p))
    wc.lpszClassName = _MENU_CLASS
    if not user32.RegisterClassExW(ctypes.byref(wc)):
        err = ctypes.get_last_error()
        if err != ERROR_CLASS_ALREADY_EXISTS:
            raise ctypes.WinError(err)
    _class_registered = True


class _PanelWindow:
    def __init__(self, items: list[MenuItem], s: float, width: int = WIDTH,
                 no_activate: bool = False):
        _ensure_class()
        self.panel = Panel(items, s, width)
        self.hover = -1
        self.ox = self.oy = 0
        self._hdc_screen = None
        self._hdc_mem = None
        self._hbmp = None
        self._bits = None
        self._dib_size = (0, 0)
        self.paint_ok = True

        ex = WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_TOPMOST
        if no_activate:
            ex |= WS_EX_NOACTIVATE
        hinst = kernel32.GetModuleHandleW(None)
        self.hwnd = user32.CreateWindowExW(
            ex, _MENU_CLASS, "menu", WS_POPUP,
            0, 0, self.panel.w, self.panel.h, 0, 0, hinst, 0)
        if not self.hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        _windows[int(self.hwnd)] = self

    # -- 绘制：把 PIL 图交给分层窗口
    def _ensure_dib(self, w: int, h: int) -> None:
        if self._dib_size == (w, h):
            return
        self._release_dib()
        self._hdc_screen = user32.GetDC(0)
        self._hdc_mem = gdi32.CreateCompatibleDC(self._hdc_screen)
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h       # 负数 = 自上而下
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0   # BI_RGB
        bits = ctypes.c_void_p()
        self._hbmp = gdi32.CreateDIBSection(self._hdc_mem, ctypes.byref(bmi),
                                            DIB_RGB_COLORS, ctypes.byref(bits), None, 0)
        self._bits = bits
        gdi32.SelectObject(self._hdc_mem, self._hbmp)
        self._dib_size = (w, h)

    def _release_dib(self) -> None:
        try:
            if self._hbmp:
                gdi32.DeleteObject(self._hbmp)
            if self._hdc_mem:
                gdi32.DeleteDC(self._hdc_mem)
            if self._hdc_screen:
                user32.ReleaseDC(0, self._hdc_screen)
        except Exception:
            pass
        self._hbmp = self._hdc_mem = self._hdc_screen = self._bits = None
        self._dib_size = (0, 0)

    def paint(self, img: Image.Image | None = None) -> None:
        img = img or self.panel.base
        w, h = img.size
        self._ensure_dib(w, h)
        ctypes.memmove(self._bits, img.tobytes("raw", "BGRA"), w * h * 4)
        blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        pt_dst = wintypes.POINT(self.ox, self.oy)
        size = wintypes.SIZE(w, h)
        pt_src = wintypes.POINT(0, 0)
        ok = user32.UpdateLayeredWindow(self.hwnd, self._hdc_screen, ctypes.byref(pt_dst),
                                        ctypes.byref(size), self._hdc_mem, ctypes.byref(pt_src),
                                        0, ctypes.byref(blend), ULW_ALPHA)
        self.paint_ok = bool(ok)

    def set_hover(self, idx: int) -> None:
        if idx == self.hover:
            return
        self.hover = idx
        self.paint(self.panel.hovered(idx))

    def show(self, no_activate: bool = False) -> None:
        user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE if no_activate else SW_SHOW)
        if no_activate:
            user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        else:
            user32.SetForegroundWindow(self.hwnd)

    def hide(self) -> None:
        if self.hwnd:
            user32.ShowWindow(self.hwnd, SW_HIDE)

    def destroy(self) -> None:
        hwnd = self.hwnd
        if hwnd:
            self.hwnd = 0
            _windows.pop(int(hwnd), None)
            user32.DestroyWindow(hwnd)
        self._release_dib()

    def on_message(self, hwnd, msg, wparam, lparam):
        session = _session
        if msg == WM_MOUSEMOVE:
            if session:
                session.on_move(self, lparam)
            return 0
        if msg == WM_LBUTTONUP:
            if session:
                session.on_click(self, lparam)
            return 0
        if msg == WM_TIMER:
            if session:
                session.on_timer()
            return 0
        if msg == WM_KEYDOWN and wparam == VK_ESCAPE:
            if session:
                session.close()
            return 0
        if msg == WM_ACTIVATE:
            # 失活不再收起：按需求，只有“菜单+托盘之外”的点击才关闭。
            return 0
        if msg == WM_CLOSE:
            if session:
                session.close()
            return 0
        if msg == WM_DESTROY:
            _windows.pop(int(hwnd or 0), None)
            self.hwnd = 0
            if session:
                session.on_destroyed(self)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


class _MenuSession:
    def __init__(self, items: list[MenuItem], x: int, y: int,
                 keep_rects: list[tuple[int, int, int, int]] | None = None):
        self.s = dpi_scale()
        self.main = _PanelWindow(items, self.s, WIDTH)
        self.sub = None
        self._sub_owner: str | None = None
        self._sub_visible = False
        self.choice: str | None = None
        self.alive = True
        self._mouse_down = self._button_down()
        # 托盘图标本身也算“内部”：在托盘上点击不收起菜单。
        self.keep_rects = [tuple(r) for r in (keep_rects or []) if r]
        if not self.keep_rects:
            self.keep_rects = [(x - 30, y - 30, x + 30, y + 30)]

        sw = user32.GetSystemMetrics(0)
        sh = user32.GetSystemMetrics(1)
        pad = self.main.panel.pad
        self.main.ox = min(max(0, x - self.main.panel.w + pad + 18),
                           max(0, sw - self.main.panel.w))
        self.main.oy = min(max(0, y - self.main.panel.h + pad),
                           max(0, sh - self.main.panel.h))

    # -- 子菜单
    def _show_sub(self, it: MenuItem) -> None:
        if self.sub is not None and self._sub_owner == it.id:
            if not self._sub_visible:
                self.sub.show(no_activate=True)
                self._sub_visible = True
            return

        if self.sub is not None:
            self.sub.destroy()
            self.sub = None
        self.sub = _PanelWindow(it.children or [], self.s, SUB_WIDTH, no_activate=True)
        self._sub_owner = it.id
        self._sub_visible = True

        pad = self.main.panel.pad
        sub = self.sub
        x = self.main.ox + self.main.panel.w - pad + 2
        y = self.main.oy + pad + it._y0 - int(6 * self.s)
        sw = user32.GetSystemMetrics(0)
        sh = user32.GetSystemMetrics(1)
        if x + sub.panel.w > sw:
            x = self.main.ox - sub.panel.w + pad - 2
        sub.ox = max(0, min(x, max(0, sw - sub.panel.w)))
        sub.oy = max(0, min(y, max(0, sh - sub.panel.h)))
        sub.paint()
        sub.show(no_activate=True)

    def _hide_sub(self) -> None:
        if self.sub is not None:
            self.sub.hide()
            self.sub.hover = -1
        self._sub_visible = False

    def _cursor(self) -> tuple[int, int]:
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        return pt.x, pt.y

    def _cursor_in_sub(self) -> bool:
        if self.sub is None or not self.sub.hwnd:
            return False
        px, py = self._cursor()
        rect = RECT()
        user32.GetWindowRect(self.sub.hwnd, ctypes.byref(rect))
        return rect.left <= px < rect.right and rect.top <= py < rect.bottom

    # -- 交互
    def on_move(self, win: _PanelWindow, lparam) -> None:
        x = ctypes.c_short(lparam & 0xFFFF).value
        y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
        idx = win.panel.item_at(y)
        win.set_hover(idx)
        if win is not self.main:
            return
        if 0 <= idx < len(win.panel.items):
            it = win.panel.items[idx]
            if it.enabled and it.children is not None:
                self._show_sub(it)
                return
        if not self._cursor_in_sub():
            self._hide_sub()

    def on_click(self, win: _PanelWindow, lparam) -> None:
        y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
        idx = win.panel.item_at(y)
        if idx < 0:
            return
        it = win.panel.items[idx]
        if not it.enabled:
            return
        if win is self.main and it.children is not None:
            self._show_sub(it)
            return
        self.choice = it.id
        self.close()

    def _button_down(self) -> bool:
        return bool(user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000) or \
               bool(user32.GetAsyncKeyState(VK_RBUTTON) & 0x8000)

    def _inside(self, px: int, py: int) -> bool:
        for win in (self.main, self.sub):
            if win is None or not win.hwnd:
                continue
            if win is self.sub and not user32.IsWindowVisible(win.hwnd):
                continue
            r = RECT()
            user32.GetWindowRect(win.hwnd, ctypes.byref(r))
            if r.left <= px < r.right and r.top <= py < r.bottom:
                return True
        for left, top, right, bottom in self.keep_rects:
            if left <= px < right and top <= py < bottom:
                return True
        return False

    def on_timer(self) -> None:
        """只检测“在菜单+托盘之外按下鼠标”这一件事；移出不关闭。"""
        if not self.alive:
            return
        down = self._button_down()
        if down and not self._mouse_down:
            self._mouse_down = True
            px, py = self._cursor()
            if not self._inside(px, py):
                self.close()
        elif not down:
            self._mouse_down = False

    def on_destroyed(self, win: _PanelWindow) -> None:
        if win is self.main:
            self.close()

    def close(self) -> None:
        if not self.alive:
            return
        self.alive = False
        if self.sub is not None:
            self.sub.destroy()
            self.sub = None
        if self.main.hwnd:
            self.main.destroy()
        user32.PostThreadMessageW(kernel32.GetCurrentThreadId(), WM_NULL, 0, 0)

    def show(self) -> str | None:
        self.main.paint()
        self.main.show()
        user32.SetTimer(self.main.hwnd, 1, 40, None)
        msg = wintypes.MSG()
        try:
            while self.alive and user32.GetMessageW(ctypes.byref(msg), 0, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            if self.main.hwnd:
                user32.KillTimer(self.main.hwnd, 1)
            self.close()
        return self.choice


def set_dpi_aware() -> None:
    """必须在建窗口之前调用，否则 Windows 会把整窗位图放大，字就糊了。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # PER_MONITOR_AWARE_V2
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass


def show_menu(items: list[MenuItem], x: int, y: int,
              keep_rects: list[tuple[int, int, int, int]] | None = None) -> str | None:
    global _session, _active
    if _active or modal_open:   # 已有菜单/弹窗时不再叠一层
        return None
    set_dpi_aware()
    _active = True
    session = None
    try:
        session = _MenuSession(items, x, y, keep_rects)
        _session = session
        return session.show()
    except Exception:
        _report_exc()
        return None
    finally:
        _session = None
        _active = False
        if session is not None and session.main.hwnd:
            session.close()


if __name__ == "__main__":
    set_dpi_aware()
    demo = [
        MenuItem("item", "打开网页", id="open", primary=True),
        MenuItem("item", "最近任务", id="recent", children=[
            MenuItem("item", "示例任务一", id="task:1"),
            MenuItem("item", "示例任务二：比较长的标题会被截断", id="task:2"),
        ]),
        MenuItem("item", "新建任务", id="new"),
        MenuItem("item", "退出", id="quit"),
    ]
    print("dpi scale:", dpi_scale())
    print("choice:", show_menu(demo, 1200, 900))
