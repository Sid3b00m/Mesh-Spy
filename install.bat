@echo off
rem Mesh-Spy installer for Windows. Double-click this.
rem
rem It only exists to get past PowerShell's execution policy, which on a
rem default Windows install refuses to run install.ps1 and reports it as a
rem security error rather than something you can fix. Bypass applies to this
rem one invocation and changes no machine setting.
setlocal
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*

if %errorlevel% neq 0 (
    echo.
    echo Install failed with error %errorlevel%.
)
echo.
pause
endlocal
