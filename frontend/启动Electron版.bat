@echo off
REM ===========================================================================
REM  VoxSub Electron - one-click dev launcher
REM
REM  ASCII-ONLY, INCLUDING PATHS IN THE CONTENT:
REM    This file is double-clicked from Explorer, where the console codepage is
REM    CP936. cmd.exe reads .bat files with that codepage, so a Chinese path
REM    written inside the file is mis-decoded into mojibake and every
REM    `if exist` check fails - the window flashes and nothing happens.
REM    Keep the content ASCII; the file NAME may be Chinese (FS is Unicode).
REM
REM  The real logic lives in scripts\run-launcher.bat, next to the launcher it
REM  drives. This file only forwards, so there is a single implementation shared
REM  by every entry point (repo root, this folder, and the old checkout).
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

set "IMPL=%~dp0scripts\run-launcher.bat"

if not exist "%IMPL%" (
  echo.
  echo [ERROR] Launcher implementation not found:
  echo         %IMPL%
  echo.
  pause
  exit /b 1
)

call "%IMPL%" %*
set "RC=%ERRORLEVEL%"

endlocal & exit /b %RC%
