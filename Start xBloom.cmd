@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start-local.ps1" -OpenBrowser
if errorlevel 1 (
    echo.
    echo xBloom could not start. Review the error above or see README.md.
    pause
)
endlocal
