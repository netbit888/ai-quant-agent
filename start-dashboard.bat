@echo off
rem ============================================================
rem  AI Quant Agent - dashboard launcher
rem
rem  IMPORTANT: keep this file ASCII-only with CRLF line endings.
rem  A UTF-8 .bat with Chinese text plus "chcp 65001" makes
rem  cmd.exe lose its read position and run truncated lines
rem  ("on.exe", "cho", ...). Do not re-add chcp here.
rem ============================================================
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] venv not found: .venv\Scripts\python.exe
    echo Run the setup first.
    pause
    exit /b 1
)

echo ============================================
echo   AI Quant Agent - Dashboard
echo ============================================
echo Starting server on http://127.0.0.1:8765
echo Browser will open automatically.
echo Press Ctrl+C to stop.
echo.

.venv\Scripts\python.exe -u -m tools.dashboard
if errorlevel 1 (
    echo.
    echo [HINT] dashboard exited with an error.
    echo Open http://127.0.0.1:8765 in your browser to check.
    pause
)

endlocal
