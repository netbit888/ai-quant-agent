@echo off
rem ============================================================
rem  Build the Windows installer (PyInstaller + Inno Setup).
rem
rem  IMPORTANT: keep this file ASCII-only with CRLF line endings.
rem  A UTF-8 .bat with Chinese text makes cmd.exe lose its read
rem  position and run truncated lines. Do not add Chinese here.
rem
rem  Output: dist\installer\AIQuant-Setup-<version>.exe
rem ============================================================
setlocal
cd /d "%~dp0.."

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] venv not found: .venv\Scripts\python.exe
    exit /b 1
)

rem ---- version (single source: app_version.py) ---------------------------
rem app_version.py is THE only place the version lives. iss no longer hardcodes it.
for /f "usebackq delims=" %%i in (`".venv\Scripts\python.exe -c "import app_version; print(app_version.APP_VERSION)""`) do set "VER=%%i"
if "%VER%"=="" (
    echo [ERROR] cannot read APP_VERSION from app_version.py
    exit /b 1
)
if "%VER%"=="0.0.0" (
    echo [ERROR] APP_VERSION is 0.0.0, fix app_version.py first
    exit /b 1
)
echo [INFO] building version %VER% ...

rem ---- 1/3 dependencies -------------------------------------------------
.venv\Scripts\python.exe -c "import PyInstaller" 2>nul
if errorlevel 1 (
    echo [INFO] installing pyinstaller ...
    .venv\Scripts\python.exe -m pip install "pyinstaller>=6.11" || exit /b 1
)
.venv\Scripts\python.exe -c "import webview" 2>nul
if errorlevel 1 (
    echo [INFO] installing pywebview ...
    .venv\Scripts\python.exe -m pip install "pywebview>=6.0" || exit /b 1
)

rem ---- 2/3 freeze -------------------------------------------------------
echo.
echo [1/2] cleaning previous build ...
if exist "build" rmdir /s /q "build"
if exist "dist\AIQuant" rmdir /s /q "dist\AIQuant"

echo [2/2] running pyinstaller ...
.venv\Scripts\pyinstaller.exe --noconfirm aqa.spec || exit /b 1
if not exist "dist\AIQuant\AIQuant.exe" (
    echo [ERROR] build failed: dist\AIQuant\AIQuant.exe missing
    exit /b 1
)

rem ---- 3/3 installer ----------------------------------------------------
set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" (
    echo.
    echo [ERROR] Inno Setup 6 not found.
    echo         Install it once from https://jrsoftware.org/isdl.php
    echo         then run this script again. The app itself is already
    echo         built at dist\AIQuant\AIQuant.exe and can be run as-is.
    exit /b 1
)

echo.
echo [3/3] compiling installer ...
"%ISCC%" /DMyAppVersion=%VER% installer\aqa.iss || exit /b 1

echo.
echo ============================================
echo   Done.
echo   Version   : %VER%
echo   App       : dist\AIQuant\AIQuant.exe
echo   Installer : dist\installer\AIQuant-Setup-%VER%.exe
echo ============================================
endlocal
