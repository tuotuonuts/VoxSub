@echo off
setlocal
cd /d "%~dp0"

set "RUNNER=%~dp0scripts\run_source_test.ps1"
if not exist "%RUNNER%" (
    echo [错误] 找不到启动脚本：%RUNNER%
    pause
    exit /b 1
)

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%RUNNER%"
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
    echo.
    echo 测试版启动失败，详细日志已保存到诊断目录。
)
pause
exit /b %EXIT_CODE%
