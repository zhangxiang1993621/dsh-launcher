@echo off
rem ============================================================
rem  DeepSeek Harness Launcher - one-click build script
rem  Output : dist\dsh-launcher.exe (windowed, version 1.1.0)
rem  Usage  : double-click build.bat, or run from command line
rem ============================================================
setlocal
cd /d "%~dp0"

rem 1) Locate PyInstaller: prefer trae-agent python -m PyInstaller (tcl 8.6.15),
rem    else a pyinstaller on PATH (any python).
set "PYTHON="
set "PYINSTALLER="
if exist "D:\install\anaconda\envs\trae-agent\python.exe" (
    set "PYTHON=D:\install\anaconda\envs\trae-agent\python.exe"
) else (
    where py >nul 2>nul
    if not errorlevel 1 set "PYTHON=py -3"
)
if defined PYTHON (
    "%PYTHON%" -c "import PyInstaller" >nul 2>nul
    if not errorlevel 1 set "PYINSTALLER=%PYTHON% -m PyInstaller"
)
if not defined PYINSTALLER (
    where pyinstaller >nul 2>nul
    if not errorlevel 1 set "PYINSTALLER=pyinstaller"
)
if not defined PYINSTALLER (
    echo [ERROR] PyInstaller not found. Install it first: pip install pyinstaller
    pause
    exit /b 1
)

echo Using PyInstaller: %PYINSTALLER%
echo Building (1-3 minutes, please wait)...
%PYINSTALLER% --noconfirm dsh-launcher.spec
if errorlevel 1 (
    echo.
    echo [FAILED] Build error, see log above.
    pause
    exit /b 1
)

echo.
echo [OK] Output: %~dp0dist\dsh-launcher-1.1.0-*.exe
pause
