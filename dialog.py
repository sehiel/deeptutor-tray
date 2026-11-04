"""自绘确认/错误弹窗。

为什么不用 MessageBox
--------------------
Windows 的 MessageBoxIndirect + MB_USERICON 可以用 exe 内嵌图标，但在 150% DPI 下
消息框的图标控件仍只有 32×32，而图标资源会按系统默认尺寸加载成 48×48，结果 logo
被缩得很小甚至被裁切——看起来就像“图标没生效”。

这里用和托盘菜单同一套分层窗口方案，直接把 logo.png 画到 56pt 的卡片上：
图标尺寸、留白、圆角、按钮都由自己控制，源码运行和打包后表现一致。

用法
----
    from dialog import ask, alert
    if ask("退出 DeepTutor", "退出会停止服务，确定吗？", image=logo):
        ...
    alert("无法启动", "找不到 deeptutor 命令。", image=logo)
"""

from __future__ import annotations

import ctypes
import traceback
from ctypes import wintypes

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

import menu

user32 = menu.user32
gdi32 = menu.gdi32
kernel32 = menu.kernel32

WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x00080000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST = 0x00000008
ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01

WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_KEYDOWN = 0x0100
WM_MOUSEMOVE = 0x0200
WM_LBUTTONUP = 0x0202
WM_NULL = 0x0000
SW_SHOW = 5
VK_RETURN = 0x0D
VK_ESCAPE = 0x1B

WIDTH = 430
PAD = 20
LOGO = 56
GAP = 16
BTN_W = 84
BTN_H = 32
BTN_GAP = 10
RADIUS = 10
SHADOW = 16

BG = (255, 255, 255, 255)
BORDER = (224, 226, 231, 255)
TEXT = (26, 30, 35, 255)
TEXT_MUTED = (110, 116, 126, 255)
PRIMARY = (37, 99, 235, 255)
PRIMARY_HOVER = (29, 78, 216, 255)
SECONDARY = (244, 245, 247, 255)
SECONDARY_HOVER = (234, 236, 241, 255)
SECONDARY_BORDER = (222, 225, 230, 255)
SHADOW_COLOR = (15, 23, 42, 255)

_CLASS = "DeepTutorDialogV1"
_dialogs: dict[int, "_Dialog"] = {}
_class_registered = False


def _dispatch(hwnd, msg, wparam, lparam):
    dlg = _dialogs.get(int(hwnd or 0))
    if dlg is not None:
        try:
            return dlg.on_message(hwnd, msg, wparam, lparam)
        except Exception:
            menu._report_exc()
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


_PROC = menu.WNDPROC(_dispatch)


def _ensure_class() -> None:
    global _class_registered
    if _class_registered:
        return
    hinst = kernel32.GetModuleHandleW(None)
    wc = menu.WNDCLASSEXW()
    wc.cbSize = ctypes.sizeof(menu.WNDCLASSEXW)
    wc.lpfnWndProc = _PROC
    wc.hInstance = hinst
    wc.hCursor = user32.LoadCursorW(0, ctypes.cast(ctypes.c_void_p(32512), ctypes.c_wchar_p))
    wc.lpszClassName = _CLASS
    if not user32.RegisterClassExW(ctypes.byref(wc)):
        if ctypes.get_last_error() != 1410:
            raise ctypes.WinError(ctypes.get_last_error())
    _class_registered = True


def _wrap(text: str, font: ImageFont.ImageFont, max_w: float) -> list[str]:
    lines: list[str] = []
    for para in str(text).split("\n"):
        if not para:
            lines.append("")
            continue
        line = ""
        for ch in para:
            try:
                fits = font.getlength(line + ch) <= max_w
            except Exception:
                fits = True
            if fits:
                line += ch
            else:
                if line:
                    lines.append(line)
                line = ch
        if line:
            lines.append(line)
    return lines


class _Dialog:
    def __init__(self, title: str, text: str, image: Image.Image | None,
                 primary: str, secondary: str | None):
        self.s = menu.dpi_scale()
        self.title = title
        self.text = text
        self.image = image
        self.primary_text = primary
        self.secondary_text = secondary
        self.result = False
        self.alive = True
        self.hover = 0
        self._hdc_screen = None
        self._hdc_mem = None
        self._hbmp = None
        self._bits = None

        self.title_font = menu._font(int(15 * self.s), True)
        self.body_font = menu._font(int(13 * self.s), False)
        self.logo_px = int(LOGO * self.s)
        self.text_x = int(PAD * self.s) + self.logo_px + int(GAP * self.s)
        self.text_w = int(WIDTH * self.s) - self.text_x - int(PAD * self.s)
        self.lines = _wrap(text, self.body_font, self.text_w)
        self.line_h = int(20 * self.s)
        self.title_h = int(22 * self.s)
        content_h = max(self.logo_px, self.title_h + int(8 * self.s)
                        + len(self.lines) * self.line_h)
        self.body_h = int(PAD * self.s) * 2 + content_h + int(18 * self.s) \
            + int(BTN_H * self.s)
        self.pad = int(SHADOW * self.s)
        self.w = int(WIDTH * self.s) + self.pad * 2
        self.h = self.body_h + self.pad * 2

        btn_y = self.pad + self.body_h - int(PAD * self.s) - int(BTN_H * self.s)
        btn_w = int(BTN_W * self.s)
        btn_h = int(BTN_H * self.s)
        primary_x = self.pad + int(WIDTH * self.s) - int(PAD * self.s) - btn_w
        self.primary_rect = (primary_x, btn_y, primary_x + btn_w, btn_y + btn_h)
        self.secondary_rect = None
        if secondary:
            sx = primary_x - int(BTN_GAP * self.s) - btn_w
            self.secondary_rect = (sx, btn_y, sx + btn_w, btn_y + btn_h)

        self.hwnd = 0
        _ensure_class()
        hinst = kernel32.GetModuleHandleW(None)
        sw = user32.GetSystemMetrics(0)
        sh = user32.GetSystemMetrics(1)
        self.ox = max(0, (sw - self.w) // 2)
        self.oy = max(0, (sh - self.h) // 2)
        self.hwnd = user32.CreateWindowExW(
            WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_TOPMOST,
            _CLASS, "dialog", WS_POPUP,
            self.ox, self.oy, self.w, self.h, 0, 0, hinst, 0)
        if not self.hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        _dialogs[int(self.hwnd)] = self

    # -- 绘制
    def _render(self, hover: int) -> Image.Image:
        s, pad = self.s, self.pad
        img = Image.new("RGBA", (self.w, self.h), (0, 0, 0, 0))
        shadow = Image.new("L", img.size, 0)
        ImageDraw.Draw(shadow).rounded_rectangle(
            (pad, pad + int(3 * s), pad + int(WIDTH * s) - 1, pad + self.body_h - 1),
            radius=int(RADIUS * s), fill=58)
        shadow = shadow.filter(ImageFilter.GaussianBlur(int(6 * s)))
        img.paste(Image.new("RGBA", img.size, SHADOW_COLOR), (0, 0), shadow)

        d = ImageDraw.Draw(img)
        d.rounded_rectangle((pad, pad, pad + int(WIDTH * s) - 1, pad + self.body_h - 1),
                            radius=int(RADIUS * s), fill=BG,
                            outline=BORDER, width=max(1, round(s)))

        content_top = pad + int(PAD * s)
        logo = None
        if self.image is not None:
            try:
                logo = ImageOps.contain(self.image.convert("RGBA"),
                                        (self.logo_px, self.logo_px), Image.LANCZOS)
            except Exception:
                logo = None
        if logo is not None:
            img.alpha_composite(logo, (pad + int(PAD * s), content_top))
        else:
            d.rounded_rectangle((pad + int(PAD * s), content_top,
                                 pad + int(PAD * s) + self.logo_px,
                                 content_top + self.logo_px),
                                radius=int(12 * s), fill=(37, 99, 235, 255))
            d.text((pad + int(PAD * s) + self.logo_px / 2,
                    content_top + self.logo_px / 2), "DT",
                   font=menu._font(int(18 * s), True), fill=(255, 255, 255, 255),
                   anchor="mm")

        d.text((self.text_x, content_top), self.title, font=self.title_font,
               fill=TEXT, anchor="la")
        y = content_top + self.title_h + int(8 * s)
        for line in self.lines:
            d.text((self.text_x, y), line, font=self.body_font, fill=TEXT_MUTED,
                   anchor="la")
            y += self.line_h

        self._button(d, self.primary_rect, self.primary_text, primary=True,
                     hovered=(hover == 2))
        if self.secondary_rect is not None and self.secondary_text:
            self._button(d, self.secondary_rect, self.secondary_text, primary=False,
                         hovered=(hover == 1))
        return img

    def _button(self, d: ImageDraw.ImageDraw, rect, text: str, primary: bool,
                hovered: bool) -> None:
        left, top, right, bottom = rect
        if primary:
            fill = PRIMARY_HOVER if hovered else PRIMARY
            d.rounded_rectangle((left, top, right - 1, bottom - 1),
                                radius=int(6 * self.s), fill=fill)
            color = (255, 255, 255, 255)
        else:
            fill = SECONDARY_HOVER if hovered else SECONDARY
            d.rounded_rectangle((left, top, right - 1, bottom - 1),
                                radius=int(6 * self.s), fill=fill,
                                outline=SECONDARY_BORDER, width=1)
            color = TEXT
        d.text(((left + right) / 2, (top + bottom) / 2), text,
               font=menu._font(int(13 * self.s)), fill=color, anchor="mm")

    def _paint(self) -> None:
        img = self._render(self.hover)
        if self._hdc_screen is None:
            self._hdc_screen = user32.GetDC(0)
            self._hdc_mem = gdi32.CreateCompatibleDC(self._hdc_screen)
            bmi = menu.BITMAPINFO()
            bmi.bmiHeader.biSize = ctypes.sizeof(menu.BITMAPINFOHEADER)
            bmi.bmiHeader.biWidth = self.w
            bmi.bmiHeader.biHeight = -self.h
            bmi.bmiHeader.biPlanes = 1
            bmi.bmiHeader.biBitCount = 32
            bmi.bmiHeader.biCompression = 0
            bits = ctypes.c_void_p()
            self._hbmp = gdi32.CreateDIBSection(self._hdc_mem, ctypes.byref(bmi),
                                                0, ctypes.byref(bits), None, 0)
            self._bits = bits
            gdi32.SelectObject(self._hdc_mem, self._hbmp)
        ctypes.memmove(self._bits, img.tobytes("raw", "BGRA"), self.w * self.h * 4)
        blend = menu.BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        pt_dst = wintypes.POINT(self.ox, self.oy)
        size = wintypes.SIZE(self.w, self.h)
        pt_src = wintypes.POINT(0, 0)
        user32.UpdateLayeredWindow(self.hwnd, self._hdc_screen, ctypes.byref(pt_dst),
                                   ctypes.byref(size), self._hdc_mem, ctypes.byref(pt_src),
                                   0, ctypes.byref(blend), ULW_ALPHA)

    # -- 消息
    def _hit(self, x: int, y: int) -> int:
        if self.primary_rect and self.primary_rect[0] <= x < self.primary_rect[2] \
                and self.primary_rect[1] <= y < self.primary_rect[3]:
            return 2
        if self.secondary_rect and self.secondary_rect[0] <= x < self.secondary_rect[2] \
                and self.secondary_rect[1] <= y < self.secondary_rect[3]:
            return 1
        return 0

    def on_message(self, hwnd, msg, wparam, lparam):
        if msg == WM_MOUSEMOVE:
            x = ctypes.c_short(lparam & 0xFFFF).value
            y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
            hover = self._hit(x, y)
            if hover != self.hover:
                self.hover = hover
                self._paint()
            return 0
        if msg == WM_LBUTTONUP:
            x = ctypes.c_short(lparam & 0xFFFF).value
            y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
            hit = self._hit(x, y)
            if hit == 2:
                self.close(True)
            elif hit == 1:
                self.close(False)
            return 0
        if msg == WM_KEYDOWN:
            if wparam == VK_RETURN:
                self.close(True)
            elif wparam == VK_ESCAPE:
                self.close(False)
            return 0
        if msg == WM_CLOSE:
            self.close(False)
            return 0
        if msg == WM_DESTROY:
            _dialogs.pop(int(hwnd or 0), None)
            self.hwnd = 0
            self.alive = False
            user32.PostThreadMessageW(kernel32.GetCurrentThreadId(), WM_NULL, 0, 0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def close(self, result: bool) -> None:
        if not self.alive:
            return
        self.result = result
        self.alive = False
        if self.hwnd:
            user32.DestroyWindow(self.hwnd)
        user32.PostThreadMessageW(kernel32.GetCurrentThreadId(), WM_NULL, 0, 0)

    def show(self) -> bool:
        self._paint()
        user32.ShowWindow(self.hwnd, SW_SHOW)
        user32.SetForegroundWindow(self.hwnd)
        user32.SetFocus(self.hwnd)
        msg = wintypes.MSG()
        while self.alive and user32.GetMessageW(ctypes.byref(msg), 0, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        return self.result


def ask(title: str, text: str, image: Image.Image | None = None,
        primary: str = "确定", secondary: str = "取消") -> bool | None:
    """返回 True/False；窗口创建失败返回 None，调用方可回退到 MessageBox。"""
    menu.modal_open = True
    try:
        return _Dialog(title, text, image, primary, secondary).show()
    except Exception:
        menu._report_exc()
        return None
    finally:
        menu.modal_open = False


def alert(title: str, text: str, image: Image.Image | None = None,
          primary: str = "确定") -> bool:
    menu.modal_open = True
    try:
        _Dialog(title, text, image, primary, None).show()
        return True
    except Exception:
        menu._report_exc()
        return False
    finally:
        menu.modal_open = False
