@echo off
rem ============================================================
rem  Build the portable Windows folder (onedir).
rem
rem  IMPORTANT: keep this file ASCII-only with CRLF line endings.
rem  A UTF-8 .bat with Chinese text makes cmd.exe lose its read
rem  position and run truncated lines. Do not add Chinese here.
rem
rem  Output: dist\AIQuant\AIQuant.exe
rem ============================================================
setlocal
cd /d "%~dp0.."

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] venv not found: .venv\Scripts\python.exe
    exit /b 1
)

.venv\Scripts\python.exe -c "import PyInstaller" 2>nul
if errorlevel 1 (
    echo [INFO] installing pyinstaller ...
    .venv\Scripts\python.exe -m pip install "pyinstaller>=6.11" || exit /b 1
)

echo.
echo [1/2] cleaning previous build ...
if exist "build" rmdir /s /q "build"
if exist "dist\AIQuant" rmdir /s /q "dist\AIQuant"

echo [2/2] running pyinstaller ...
.venv\Scripts\pyinstaller.exe --noconfirm aqa.spec || exit /b 1

rem copy the user-facing readme next to the exe (kept out of _internal on purpose)
if exist "docs\PORTABLE.txt" copy /y "docs\PORTABLE.txt" "dist\AIQuant\README.txt" >nul

echo.
echo ============================================
echo   Done.
echo   Output : dist\AIQuant\AIQuant.exe
echo   Data   : will be created next to the exe
echo ============================================
endlocal
