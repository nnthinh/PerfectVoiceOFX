@echo off
setlocal
rem User-space wrapper. Bypass is scoped to this file; we do not change machine ExecutionPolicy.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Install-User.ps1" %*
exit /b %ERRORLEVEL%
