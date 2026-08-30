@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

set "RUNNER=%~dp0scripts\run_source_test.ps1"
if not exist "%RUNNER%" (
    echo [ERROR] Source launcher not found: %RUNNER%
    pause
    exit /b 1
)

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%RUNNER%"
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
    echo.
    echo Source test startup failed. See the diagnostics directory for details.
)
pause
exit /b %EXIT_CODE%
