@echo off
rem ============================================================
rem  Publish the GitHub Release for the version in app_version.py.
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

rem ---- version (single source: app_version.py) ---------------------------
rem Tag and package name both derive from it, so they can never disagree.
rem Read via a temp file: for /f + nested parens/quotes is a cmd parsing minefield.
.venv\Scripts\python.exe -c "import app_version; print(app_version.APP_VERSION)" > "%TEMP%\aqa_ver.tmp"
set /p VER=<"%TEMP%\aqa_ver.tmp"
del "%TEMP%\aqa_ver.tmp" 2>nul
if "%VER%"=="" (
    echo [ERROR] cannot read APP_VERSION from app_version.py
    exit /b 1
)
set "PKG=dist\installer\AIQuant-Setup-%VER%.exe"
if not exist "%PKG%" (
    echo [ERROR] installer not found: %PKG%
    echo         Run tools\build_installer.bat first.
    exit /b 1
)

rem ---- release notes: only this version's section, not the whole file ----
rem The old script attached the entire CHANGELOG to every release page.
set "NOTES=dist\release_notes_%VER%.md"
.venv\Scripts\python.exe tools\release_notes.py CHANGELOG.md %VER% "%NOTES%" || exit /b 1

"%GH%" auth status >nul 2>&1
if errorlevel 1 (
    echo [INFO] not logged in. A browser window will open; approve it there.
    "%GH%" auth login --hostname github.com --git-protocol https --web
    if errorlevel 1 exit /b 1
)

echo.
echo [1/2] checking tag ...
"%GH%" release view v%VER% >nul 2>&1
if not errorlevel 1 (
    echo [WARN] release v%VER% already exists. Delete it on GitHub first, or bump the version.
    exit /b 1
)

echo [2/2] creating release ...

rem ---- delta asset (optional) ---------------------------------------------
rem Built by build_installer.bat step 4/4 when a baseline snapshot exists.
rem Not finding one is normal (first release after the feature, or a broken
rem baseline refused to generate) - the release just ships full installer.
set "DELTA="
for %%f in ("dist\installer\AIQuant-Delta-*-to-%VER%.exe") do set "DELTA=%%f"

if defined DELTA (
    echo [INFO] uploading with delta asset: %DELTA%
    "%GH%" release create v%VER% "%PKG%" "%DELTA%" ^
        --title "v%VER%" ^
        --notes-file "%NOTES%"
) else (
    echo [INFO] no delta asset for v%VER%, uploading full installer only
    "%GH%" release create v%VER% "%PKG%" ^
        --title "v%VER%" ^
        --notes-file "%NOTES%"
)

if errorlevel 1 ( echo [ERROR] release failed & exit /b 1 )

rem ---- record release baseline --------------------------------------------
rem ONLY after a successful upload: the snapshot must be exactly what users
rem can download right now, or the next delta would patch a tree nobody has.
.venv\Scripts\python.exe tools\make_delta.py record --dist dist\AIQuant --version %VER% || exit /b 1

echo.
echo ============================================
echo   Done. Release v%VER% published with:
echo   %PKG%
if defined DELTA echo   %DELTA%
echo ============================================
endlocal
