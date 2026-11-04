"""生成 PE 版本资源文件（version_info.txt）。

PyInstaller 的 --version-file 需要一个具体的 Python 脚本，手写的话版本号
会和 version.py 各维护一份。改成从 version.py 生成，杜绝漂移。

用法：
    python gen_version_info.py              # 打印 version_info.txt 内容到 stdout
    python gen_version_info.py --emit       # 同时写出 version_info.txt 和 .app_name
"""

import sys
from pathlib import Path

from version import APP_NAME, VERSION_TUPLE, __version__

# FixedFileInfo 用元组 (0, 9, 0, 0)；StringStruct 里的字符串版本必须是点分 "0.9.0.0"。
quad = ", ".join(str(n) for n in VERSION_TUPLE)
dotted = ".".join(str(n) for n in VERSION_TUPLE)

TEMPLATE = """VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({quad}),
    prodvers=({quad}),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '080404B0',
        [StringStruct('FileDescription', 'DeepTutor 托盘启动器 / Windows Tray Launcher'),
         StringStruct('FileVersion', '{dotted}'),
         StringStruct('InternalName', 'DeepTutorTray'),
         StringStruct('LegalCopyright', 'Copyright (c) 2026 DeepTutor Tray Launcher contributors'),
         StringStruct('OriginalFilename', '{app}.exe'),
         StringStruct('ProductName', 'DeepTutor Tray Launcher'),
         StringStruct('ProductVersion', '{dotted}'),
         StringStruct('Comments', 'Unofficial Windows tray launcher for DeepTutor 1.7.x. Requires a local DeepTutor installation; not affiliated with the HKUDS DeepTutor project.')])
    ]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
"""

if __name__ == "__main__":
    text = TEMPLATE.format(quad=quad, dotted=dotted, app=APP_NAME)
    if "--emit" in sys.argv:
        # build.bat 用这个模式：直接落盘，避开 for /f 里的引号嵌套。
        Path("version_info.txt").write_text(text, encoding="utf-8")
        Path(".app_name").write_text(APP_NAME, encoding="ascii")
    else:
        print(text, end="")
