@echo off
REM ===========================================================================
REM  VoxSub Electron - one-click launcher (repository root)
REM
REM  Why this file is here:
REM    People look for a start script at the repository root. The real launcher
REM    lives next to the code it drives (frontend\scripts\launch.mjs), so this
REM    file only forwards to it. One implementation, several entry points.
REM
REM  ASCII-ONLY, INCLUDING PATHS IN THE CONTENT:
REM    cmd.exe reads .bat files with the OEM codepage (CP936 here), not UTF-8.
REM    A Chinese path written inside the file becomes mojibake and the launcher
REM    silently does nothing. Keep content ASCII; file NAMES may be Chinese.
REM
REM  Usage:
REM    double-click          -> incremental build + launch
REM    launch.bat --clean    -> force full rebuild
REM    launch.bat --no-build -> skip build, launch as-is
REM    launch.bat --dev      -> open DevTools after start
REM    launch.bat --debug    -> enable remote debugging on port 9222
REM    launch.bat --silent   -> no window appears (automation)
REM    launch.bat --keep     -> do not stop running instances first
REM ===========================================================================
setlocal

set "IMPL=%~dp0frontend\scripts\run-launcher.bat"

if not exist "%IMPL%" (
  echo.
  echo [ERROR] Launcher not found:
  echo         %IMPL%
  echo.
  echo         The frontend folder may be missing or moved.
  pause
  exit /b 1
)

call "%IMPL%" %*
set "RC=%ERRORLEVEL%"

endlocal & exit /b %RC%
