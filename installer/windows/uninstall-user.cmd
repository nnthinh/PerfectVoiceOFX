@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Uninstall-User.ps1" %*
exit /b %ERRORLEVEL%
