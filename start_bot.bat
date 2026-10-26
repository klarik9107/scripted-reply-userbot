@echo off
rem Starts the scripted-reply userbot. Double-click it, or from a terminal:
rem   start_bot.bat            -> runs script "1"
rem   start_bot.bat my_scene   -> runs scripts\my_scene.json
rem   start_bot.bat 1 --dry-run -> only prints the scenario timeline
rem Close the window (or press Ctrl+C) to stop the bot.
chcp 65001 >nul
cd /d "%~dp0"

set "SCRIPT=%~1"
if "%SCRIPT%"=="" set "SCRIPT=1"
set "EXTRA=%~2 %~3"

rem Pick the venv's Python (this project's venv keeps it in venv\bin).
set "PY="
if exist "venv\bin\python.exe" set "PY=venv\bin\python.exe"
if not defined PY if exist "venv\Scripts\python.exe" set "PY=venv\Scripts\python.exe"
if not defined PY (
    echo [!] venv not found. Create it first:
    echo     python -m venv venv ^&^& venv\bin\python.exe -m pip install -r requirements.txt
    if not defined NOPAUSE pause
    exit /b 1
)

if not exist ".env" (
    echo [!] .env not found. Copy .env.example to .env and fill it in.
    if not defined NOPAUSE pause
    exit /b 1
)

rem Two instances on the same accounts would answer every message twice,
rem so stop any bot that is already running before starting a new one.
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'bot\.py\s+run' -and $_.ProcessId -ne $PID } | ForEach-Object { Write-Host ('Stopping already running bot (PID ' + $_.ProcessId + ')'); Stop-Process -Id $_.ProcessId -Force }"

:run
echo.
echo === Starting bot: script "%SCRIPT%" ===
"%PY%" bot.py run --script "%SCRIPT%" %EXTRA%
set "CODE=%ERRORLEVEL%"

if "%CODE%"=="0" (
    echo.
    echo Bot stopped.
    if not defined NOPAUSE pause
    exit /b 0
)

rem If ANOTHER bot instance is running (e.g. a second window), restarting
rem here would just fight it over the session file ("database is locked")
rem forever - so give up instead of looping.
powershell -NoProfile -Command "if (Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'bot\.py\s+run' -and $_.ProcessId -ne $PID }) { exit 3 } else { exit 0 }"
if "%ERRORLEVEL%"=="3" (
    echo.
    echo [!] Bot exited with code %CODE%, and another bot instance is already running - not restarting.
    if not defined NOPAUSE pause
    exit /b 2
)

echo.
echo [!] Bot crashed with code %CODE%. Restarting in 10 seconds (close this window to stop)...
timeout /t 10 /nobreak >nul
goto run
