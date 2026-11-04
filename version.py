"""版本号单一真源。

整个项目里凡是出现版本号的地方——exe 文件名、PE 版本资源、User-Agent、
启动器页面页脚、README 引用——都从这里取值，不要在别处再写死一份。
以前版本号散在 6 个文件里，每次改版都要同步一堆地方，已经漏过好几次。

发布新版本只需要改这一行的数字，然后跑 build.bat。

关于当前版本号 0.9.0
-------------------
刻意停在 0.x，而不是 1.0：SemVer 里 1.0 表示"公共 API 已稳定、不会再有
破坏性变更"。但本启动器的"API"其实由上游 DeepTutor 定义——它依赖
`deeptutor start --detach`、`/api/partners/navigation`、`/ws/partners/{id}`
这些上游内部接口，上游一次重构就可能让本项目失效。签 1.0 等于替上游
做稳定性承诺，我们没有这个底气。

0.9.0 表示"功能完整、接近稳定，但仍跟随上游迭代"。等上游 1.8 稳定、
且本项目连续数月无破坏性变更之后，再切 1.0.0 那时承诺才兑现得了。
""" 

__version__ = "0.9.0"

# exe 产物名（含平台后缀）。PyInstaller --name 与 version_info.txt 都用它。
APP_NAME = f"DeepTutorTray-v{__version__}-win64"

# PE 版本资源要求四段式数字，例如 0.9.0 -> (0, 9, 0, 0)
VERSION_TUPLE = tuple(int(p) for p in (__version__.split("-")[0].split(".") + ["0"])[:4])
