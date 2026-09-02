@echo off
chcp 65001 >nul
rem ============================================================
rem  测试启动：valorant_hud.py（管理员权限运行）
rem  双击即可。首次会弹 UAC 确认（管理员权限是 Tab 注入所必需）
rem  解释器定位：%PYTHON% 环境变量 > py 启动器 > python（PATH）
rem  用 conda 环境前先设置，例如：
rem    set PYTHON=D:\anaconda\envs\ocr_hud_310\python.exe
rem  然后在本文件所在目录双击（或命令行运行本文件）。
rem ============================================================
setlocal
cd /d "%~dp0"

rem ---- 检查是否已是管理员，不是则用 RunAs 提权重启 ----
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting administrator privileges...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

rem ---- 管理员分支：定位 Python 解释器 ----
set "PYEXE="
if defined PYTHON set "PYEXE=%PYTHON%"
if not defined PYEXE (
    where py >nul 2>&1 && set "PYEXE=py"
)
if not defined PYEXE (
    where python >nul 2>&1 && set "PYEXE=python"
)
if not defined PYEXE (
    echo Python not found. Set PYTHON env var to your python.exe or add python to PATH.
    pause
    exit /b 1
)

echo Running valorant_hud.py as Administrator ...
"%PYEXE%" "%~dp0valorant_hud.py"
echo.
echo Exit code: %errorlevel%
pause
endlocal
