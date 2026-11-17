@echo off
setlocal DisableDelayedExpansion
if not exist "%~dp0CodexQuota.pyw" (
    echo CodexQuota.pyw must be next to this launcher.
    pause
    exit /b 1
)
where.exe /q pythonw.exe
if not errorlevel 1 (
    start "" pythonw.exe "%~dp0CodexQuota.pyw"
    exit /b
)
where.exe /q pyw.exe
if not errorlevel 1 (
    start "" pyw.exe -3 "%~dp0CodexQuota.pyw"
    exit /b
)
echo Python 3 was not found. Install Python with Tkinter and add Python to PATH.
pause
exit /b 1
