@echo off
rem ---------------------------------------------------------------------------
rem  SixCoder - one-click start (Windows)
rem
rem  NOTE: this file must stay ASCII-only with CRLF line endings.
rem  cmd.exe parses .bat with the active code page (GBK on zh-CN) and needs CRLF;
rem  UTF-8 Chinese text + LF-only would be read as garbage commands.
rem  All Chinese messages live in start.ps1 (UTF-8 with BOM), not here.
rem ---------------------------------------------------------------------------
chcp 65001 >nul 2>&1
setlocal
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
if errorlevel 1 (
  echo.
  echo Start failed. See messages above.
  echo Press any key to close this window...
  pause >nul
)

endlocal
