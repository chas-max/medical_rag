@echo off
rem Windows cmd / double-click entry point for start-ui.ps1.
rem Do not rely on the .ps1 file association: it may open the file in Notepad.
setlocal
cd /d "%~dp0"
where pwsh >nul 2>nul
if %errorlevel%==0 (
  pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-ui.ps1" %*
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-ui.ps1" %*
)
if errorlevel 1 pause
endlocal
