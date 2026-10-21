@echo off
rem ============================================================
rem  AI Quant Agent  -  one-click commit + push
rem
rem  Double-click this file.
rem
rem  All logic lives in push.ps1. Two things this file must NOT do:
rem    1. contain Chinese  -> cmd.exe misreads its own lines and
rem       runs truncated commands (that is what broke the old
rem       start-dashboard.bat).
rem    2. call "chcp"      -> it swallows the child process stdin,
rem       so PowerShell's Read-Host would get an empty string and
rem       the commit message would silently be lost.
rem
rem  Encoding is handled inside push.ps1 instead: the text is read
rem  with the console's native code page and written to a UTF-8
rem  temp file, which is what git consumes.
rem
rem  Keep this file ASCII-only with CRLF line endings.
rem ============================================================
setlocal
cd /d "%~dp0"

where powershell >nul 2>nul
if errorlevel 1 (
    echo [ERROR] powershell.exe not found in PATH.
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0push.ps1"
