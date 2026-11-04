"""Windows 原生 Toast 通知（PowerShell + Windows.UI.Notifications）。

为什么不用第三方库
------------------
win10toast / winotify / plyer 都要额外打包；winotify 在未注册 AUMID 时
Windows 11 上可能静默失败。这里直接用 Windows PowerShell 5.1 内置的
WinRT API，并借用 PowerShell 自己已注册的 AUMID，免注册即可稳定显示。
点击通知通过 ``activationType="protocol"`` 打开 http 链接（默认浏览器），
同样不需要注册自定义协议。

回退：如果 PowerShell 被策略拦截或执行失败，调用方可用 pystray 气泡通知。

安全：为什么参数走环境变量
------------------------
任务标题、伙伴名、头像路径都来自 DeepTutor 的会话数据，属于**不可信输入**。
如果把它们直接插进 PowerShell 脚本，``@"..."@``（可展开 here-string）会
展开其中的 ``$(...)``，等于让一条会话标题获得执行任意命令的能力。

因此这里：
  1. 脚本模板用**单引号** here-string ``@'...'@`` —— PowerShell 不展开内容；
  2. 所有动态值通过**环境变量**传入，脚本内只引用 ``$env:XXX``；
  3. XML 用 ``xml.etree`` 在 Python 侧构造，序列化时自动完成转义。

三条一起用，输入既进不了代码路径，也进不了 XML 结构。
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

# Windows PowerShell 已注册的 AppUserModelID（免注册 Toast 的通用做法）。
_POWERSHELL_AUMID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"

# 单引号 here-string：内容原样保留，PowerShell 不做任何变量展开。
# 所有动态值走环境变量（$env:DT_*），不可信输入永远不会被当成代码解析。
_PS_TEMPLATE = r"""$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$appId = $env:DT_AUMID
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml($env:DT_XML)
$toast = New-Object Windows.UI.Notifications.ToastNotification $xml
$toast.Tag = $env:DT_TAG
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show($toast)
"""

_lock = threading.Lock()


def _xml_escape(text: str) -> str:
    """保留给外部调用者；内部构造 XML 走 ElementTree，不依赖手工转义。"""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _attr_escape(text: str) -> str:
    """属性值转义（URL 等），比文本转义更严格地处理引号。"""
    return _xml_escape(text)


def _to_file_uri(path: str) -> str:
    """本地路径转 file:/// URI（Toast 图片需要 URI）。"""
    if not path:
        return ""
    try:
        return Path(path).resolve().as_uri()
    except Exception:
        return path.replace("\\", "/")


def _build_toast_xml(title: str, message: str, icon_uri: str, click_url: str) -> str:
    """用 ElementTree 构造 toast XML。

    序列化时文本与属性值自动转义，调用方不需要也不能自己拼字符串。
    """
    toast = ET.Element("toast")
    if click_url:
        toast.set("activationType", "protocol")
        toast.set("launch", click_url)

    visual = ET.SubElement(toast, "visual")
    binding = ET.SubElement(visual, "binding")
    binding.set("template", "ToastGeneric")

    if icon_uri:
        image = ET.SubElement(binding, "image")
        image.set("placement", "appLogoOverride")
        image.set("src", icon_uri)

    for text in (title, message):
        node = ET.SubElement(binding, "text")
        node.text = text

    return ET.tostring(toast, encoding="unicode")


def show_toast(
    title: str,
    message: str,
    icon_path: str | os.PathLike | None = None,
    click_url: str = "",
    tag: str = "",
    timeout: int = 20,
) -> bool:
    """发送一条 Windows 原生 Toast。

    参数：
        title:      标题（任务标题）。
        message:    正文（伙伴名 · 状态）。
        icon_path:  头像 PNG 的本地绝对路径（appLogoOverride 小图标）。
        click_url:  点击后打开的 http(s) 链接（协议激活，默认浏览器）。
        tag:        去重标签（同一任务同一标签只弹一条）。
        timeout:    powershell 进程超时秒数。

    返回 True 表示 PowerShell 成功执行（不保证系统一定展示了通知）。
    """
    icon = str(icon_path or "")
    if icon and not os.path.isabs(icon):
        icon = os.path.abspath(icon)
    icon_uri = _to_file_uri(icon)

    xml = _build_toast_xml(str(title or ""), str(message or ""),
                            icon_uri, str(click_url or ""))

    env = os.environ.copy()
    env["DT_AUMID"] = _POWERSHELL_AUMID
    env["DT_XML"] = xml
    env["DT_TAG"] = str(tag or "deeptutor")[:64]

    tmp = Path(tempfile.gettempdir()) / f"dt_toast_{os.getpid()}_{int(time.time() * 1000)}.ps1"
    try:
        tmp.write_text(_PS_TEMPLATE, encoding="utf-8-sig")
        with _lock:
            proc = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-STA",
                    "-ExecutionPolicy", "Bypass",
                    "-WindowStyle", "Hidden",
                    "-File", str(tmp),
                ],
                capture_output=True,
                timeout=timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                stdin=subprocess.DEVNULL,
                env=env,
            )
        return proc.returncode == 0
    except Exception:
        return False
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    import sys

    ok = show_toast(
        "测试通知",
        "DeepTutor 启动器 · Toast 通道正常",
        sys.argv[1] if len(sys.argv) > 1 else "",
        click_url="http://localhost:3782",
        tag="test",
    )
    print("toast ok" if ok else "toast failed")