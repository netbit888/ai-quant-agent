@echo off
rem ============================================================
rem  Run the panel straight from source. No PyInstaller, no Inno,
rem  no version bump, no GitHub release - edit code and rerun.
rem
rem  IMPORTANT: keep this file ASCII-only. A UTF-8 .bat with
rem  Chinese text makes cmd.exe lose its read position.
rem
rem  --native is NOT optional here. dashboard.py decides:
rem      want_window = (args.native or is_frozen()) and not (args.web or args.no_open)
rem  so WITHOUT it a source run goes headless and opens the page in
rem  a normal browser. No window.pywebview there -> the JS bridge is
rem  gone -> features that need a native dialog (import/export file
rem  pickers, fullscreen, quit) silently fall back or do nothing.
rem
rem  Data: by default this uses <repo>\data. To develop against
rem  the INSTALLED app's ledger, uncomment the line below. Just
rem  never run both panels at once - they share
rem  autotrade_state.json and will fight over it.
rem ============================================================
cd /d "%~dp0"

rem set AQA_DATA_DIR=D:\AIQuant\data

title AIQuant panel (source)

".venv\Scripts\python.exe" -m tools.dashboard --native
set RC=%ERRORLEVEL%

rem Keep the window open when it dies, otherwise the traceback is
rem gone before you can read it.
if not "%RC%"=="0" (
    echo.
    echo [panel exited with code %RC%]
    pause
)
