@echo off
rem [scheduled task 2026-11-04] "CardColony daily restart 5700" runs at 07:00.
rem [multi-process topology 2026-11-07] restarts the whole cluster:
rem   kill watchdog -> .manual_stop -> kill group -> runners -> web -> watchdog -> clear flag.
rem No pause, all minimized, output -> logs\service_restart.log.
rem NOTE: keep this file pure ASCII - chcp races make non-ASCII lines flaky.
cd /d "%~dp0"
setlocal enabledelayedexpansion

if not defined ECO_RUNNER_COUNT set ECO_RUNNER_COUNT=4
if %ECO_RUNNER_COUNT% lss 1 set ECO_RUNNER_COUNT=1
set BASE_PORT=5701
set ECO_RUNNER_BASE_PORT=%BASE_PORT%
set /a LAST=%ECO_RUNNER_COUNT%-1

set LOG=logs\service_restart.log
if not exist logs mkdir logs
echo manual stop> logs\.manual_stop
echo [%date% %time%] ===== daily restart: web + %ECO_RUNNER_COUNT% runners ===== >> %LOG%

rem ---- 0) watchdog: pid file first, cmdline scan as fallback ----
if exist logs\.watchdog.pid (
    set /p WDPID=<logs\.watchdog.pid
    if defined WDPID (
        taskkill /PID !WDPID! /F >> %LOG% 2>&1
        echo watchdog killed via pid file !WDPID! >> %LOG%
    )
    del logs\.watchdog.pid >nul 2>&1
)
for /f "delims=" %%a in ('powershell -NoProfile -ExecutionPolicy Bypass -File wdpid.ps1 2^>nul') do (
    taskkill /PID %%a /F >> %LOG% 2>&1
    echo watchdog killed via cmdline %%a >> %LOG%
)

rem ---- 1) stop service group: pid files, then port sweep 5700-5708 ----
for %%f in (.server.pid .server-runner*.pid .server-condense.pid) do (
    if exist %%f (
        set "PID="
        set /p PID=<%%f
        if defined PID (
            tasklist /FI "PID eq !PID!" 2>nul | findstr "!PID!" >nul
            if not errorlevel 1 (
                taskkill /PID !PID! /F /T >> %LOG% 2>&1
                echo killed !PID! via %%f >> %LOG%
            )
        )
        del %%f >nul 2>&1
    )
)
for /l %%p in (5700,1,5710) do (
    for /f "tokens=5" %%a in ('netstat -ano ^| findstr LISTENING ^| findstr ":%%p "') do (
        if not "%%a"=="0" (
            taskkill /PID %%a /F >> %LOG% 2>&1
            echo port-sweep killed %%a on %%p >> %LOG%
        )
    )
)
ping -n 3 127.0.0.1 >nul

rem ---- 2) start: venv, runners first then web, all minimized + stdout to logs ----
call .venv\Scripts\activate.bat

for /l %%i in (0,1,%LAST%) do (
    start "CardColony-runner%%i" /min cmd /c "set ECO_ROLE=runner&& set ECO_RUNNER_COUNT=%ECO_RUNNER_COUNT%&& set ECO_RUNNER_INDEX=%%i&& set ECO_RUNNER_URL=&& set ECO_RUNNER_URLS=&& set ECO_PORT=&& python runner_main.py %%i >> logs\runner%%i_stdout.log 2>&1"
    echo started runner%%i >> %LOG%
)

rem wait for runner ports (max ~60s); continue anyway - web proxies per-request
set READY=0
for /l %%t in (1,1,30) do (
    ping -n 2 127.0.0.1 >nul
    set READY=0
    for /l %%i in (0,1,%LAST%) do (
        set /a PORT=%BASE_PORT%+%%i
        netstat -ano | findstr "LISTENING" | findstr ":!PORT! " >nul && set /a READY+=1
    )
    if !READY! geq %ECO_RUNNER_COUNT% goto :runners_up
)
echo [%date% %time%] WARN: runner ports not all up, continuing >> %LOG%
:runners_up

rem condense dedicated process: sole owner of condensation (probe 5710)
start "CardColony-condense" /min cmd /c "set ECO_ROLE=condense&& set ECO_RUNNER_COUNT=%ECO_RUNNER_COUNT%&& set ECO_RUNNER_URL=&& set ECO_RUNNER_URLS=&& set ECO_PORT=&& python condense_main.py >> logs\condense_stdout.log 2>&1"
echo started condense >> %LOG%

start "CardColony-web" /min cmd /c "set ECO_ROLE=web&& set ECO_RUNNER_COUNT=%ECO_RUNNER_COUNT%&& set ECO_RUNNER_URL=&& set ECO_RUNNER_URLS=&& set ECO_PORT=&& python main.py >> logs\service_stdout.log 2>&1"

rem ---- 3) verify: max ~30s, .server.pid present + 5700 listening ----
set OK=0
for /l %%i in (1,1,15) do (
    ping -n 2 127.0.0.1 >nul
    if exist .server.pid (
        netstat -ano | findstr "LISTENING" | findstr ":5700 " >nul
        if not errorlevel 1 ( set OK=1 & goto :done )
    )
)
:done
if %OK%==1 (
    set /p PID=<.server.pid
    echo [%date% %time%] restart ok: web PID=!PID! runner x%ECO_RUNNER_COUNT% >> %LOG%
) else (
    echo [%date% %time%] restart FAILED: no .server.pid or 5700 not listening >> %LOG%
)

rem ---- 4) watchdog back up + clear flag ----
start "CardColony-watchdog" /min cmd /c "python watchdog.py >> logs\watchdog_stdout.log 2>&1"
del logs\.manual_stop >nul 2>&1
echo [%date% %time%] watchdog restarted, flag cleared >> %LOG%
endlocal
