@echo off
rem Start Mesh-Spy on Windows. Double-click this, or run it from a terminal.
rem
rem Everything real happens in bootstrap.py, which is shared with Linux and
rem macOS: it creates the virtualenv and config on first run, so this works
rem straight out of a clone with nothing installed but Python.
setlocal
cd /d "%~dp0"

rem The py launcher is preferred because a bare "python" on Windows often
rem resolves to the Microsoft Store stub, which cannot build a usable venv.
py -3 --version >nul 2>nul
if %errorlevel% equ 0 (
    py -3 bootstrap.py %*
) else (
    python bootstrap.py %*
)

rem Double-clicked windows close the instant the process ends, taking any
rem error message with them. Pausing only on failure keeps that readable
rem without making a normal Ctrl+C shutdown wait for a keypress.
if %errorlevel% neq 0 (
    echo.
    echo Mesh-Spy exited with error %errorlevel%.
    pause
)
endlocal
