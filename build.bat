@echo off
REM Build the versioned DeepTutor tray launcher. Requires Python 3.11+ (64-bit) on PATH.
REM NOTE: keep this file ASCII-only. cmd.exe reads .bat files using the system
REM       codepage (GBK on zh-CN), so UTF-8 Chinese comments turn into garbage
REM       that can break the command line.
REM The version number and artifact name come from version.py - never hardcode here.
setlocal

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [1/4] creating build venv...
    python -m venv .venv || goto :fail
)

echo [2/4] installing build deps...
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
".venv\Scripts\python.exe" -m pip install -r requirements-dev.txt || goto :fail

echo [3/4] generating version_info.txt...
".venv\Scripts\python.exe" gen_version_info.py --emit || goto :fail

set "APP_NAME="
set /p APP_NAME=<.app_name
if not defined APP_NAME (
    echo Failed to read APP_NAME from version.py
    goto :fail
)

echo [4/4] building exe...
".venv\Scripts\pyinstaller.exe" --onefile --noconsole ^
    --icon icon.ico ^
    --version-file version_info.txt ^
    --add-data "logo.png;." ^
    --add-data "web/index.html;web" ^
    --name "%APP_NAME%" --distpath dist --workpath build --specpath . launcher.py || goto :fail

echo.
echo Done: dist\%APP_NAME%.exe
echo Copy it next to start.bat in your DeepTutor install directory.
exit /b 0

:fail
echo.
echo Build failed.
exit /b 1
