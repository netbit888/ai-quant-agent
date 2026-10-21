@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================
echo   AI Quant Agent - Dashboard
echo ============================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] venv not found: .venv\Scripts\python.exe
    echo Run the setup first.
    pause
    exit /b 1
)

echo Starting server on http://127.0.0.1:8765
echo Browser will open automatically.
echo Press Ctrl+C to stop.
echo.

rem -u：不缓冲 stdout，否则后台同步线程的日志要等进程退出才看得见
.venv\Scripts\python.exe -u -m tools.dashboard
if errorlevel 1 (
    echo.
    echo [HINT] Port 8765 might already be in use.
    echo Try opening http://127.0.0.1:8765 directly in your browser.
    pause
)
