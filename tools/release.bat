@echo off
rem ============================================================
rem  Publish the GitHub Release for v1.0.0.
rem
rem  Prerequisite (one time only):
rem    gh auth login
rem
rem  IMPORTANT: keep this file ASCII-only with CRLF line endings.
rem  A UTF-8 .bat with Chinese text makes cmd.exe lose its read
rem  position and run truncated lines. Do not add Chinese here.
rem ============================================================
setlocal
cd /d "%~dp0.."

set "GH=%USERPROFILE%\Downloads\ghcli\bin\gh.exe"
if not exist "%GH%" (
    echo [ERROR] gh.exe not found at %GH%
    echo         Download it from https://cli.github.com and adjust this path.
    exit /b 1
)

set "PKG=dist\installer\AIQuant-Setup-1.0.0.exe"
if not exist "%PKG%" (
    echo [ERROR] installer not found: %PKG%
    echo         Run tools\build_installer.bat first.
    exit /b 1
)

"%GH%" auth status >nul 2>&1
if errorlevel 1 (
    echo [INFO] not logged in. A browser window will open; approve it there.
    "%GH%" auth login --hostname github.com --git-protocol https --web
    if errorlevel 1 exit /b 1
)

echo.
echo [1/2] checking tag ...
"%GH%" release view v1.0.0 >nul 2>&1
if not errorlevel 1 (
    echo [WARN] release v1.0.0 already exists. Delete it on GitHub first, or bump the version.
    exit /b 1
)

echo [2/2] creating release ...
"%GH%" release create v1.0.0 "%PKG%" ^
    --title "v1.0.0" ^
    --notes-file CHANGELOG.md

if errorlevel 1 ( echo [ERROR] release failed & exit /b 1 )

echo.
echo ============================================
echo   Done. Release v1.0.0 published with:
echo   %PKG%
echo ============================================
endlocal
