@echo off
rem ---------------------------------------------------------------------------
rem  SixCoder - one-click stop (Windows)
rem  ASCII-only + CRLF on purpose (see start.bat header).
rem ---------------------------------------------------------------------------
chcp 65001 >nul 2>&1
setlocal
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop.ps1" %*
echo.
echo Press any key to close this window...
pause >nul

endlocal
