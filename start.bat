@echo off
rem [multi-process topology 2026-11-07] cluster start:
rem   kill watchdog -> already-running check -> runner 0..N-1 (127.0.0.1:5701+i)
rem   -> web (5700, public) -> watchdog + clear .manual_stop.
rem Runner count: ECO_RUNNER_COUNT (default 4). runner_main.py self-sets
rem ECO_ROLE=runner and ECO_RUNNER_INDEX from argv.
rem NOTE: keep this file pure ASCII - chcp 65001 mid-batch races with the
rem cmd parser and non-ASCII lines can misfire nondeterministically.
setlocal enabledelayedexpansion
title CardColony canvas

cd /d "%~dp0"

if not defined ECO_RUNNER_COUNT set ECO_RUNNER_COUNT=4
if %ECO_RUNNER_COUNT% lss 1 set ECO_RUNNER_COUNT=1
set BASE_PORT=5701
set ECO_RUNNER_BASE_PORT=%BASE_PORT%
set /a LAST=%ECO_RUNNER_COUNT%-1

if not exist logs mkdir logs

rem ---- 0) kill watchdog first: it would otherwise see the dead service and
rem    resurrect a SINGLE-process main.py during our startup window ----
if exist logs\.watchdog.pid (
    set /p WDPID=<logs\.watchdog.pid
    if defined WDPID (
        taskkill /PID !WDPID! /F >nul 2>&1 && echo [OK] watchdog stopped ^(PID !WDPID!^)
    )
    del logs\.watchdog.pid >nul 2>&1
)
for /f "delims=" %%a in ('powershell -NoProfile -ExecutionPolicy Bypass -File wdpid.ps1 2^>nul') do (
    taskkill /PID %%a /F >nul 2>&1 && echo [OK] watchdog stopped ^(PID %%a^)
)

rem ---- already-running check: refuse if any pid file's process is alive ----
set ALIVE=0
for %%f in (.server.pid .server-runner*.pid .server-condense.pid) do (
    if exist %%f (
        set "PID="
        set /p PID=<%%f
        if defined PID (
            tasklist /FI "PID eq !PID!" 2>nul | findstr "!PID!" >nul
            if not errorlevel 1 (
                echo [ERROR] service already running: %%f ^(PID !PID!^)
                set ALIVE=1
            ) else (
                del %%f 2>nul
            )
        ) else (
            del %%f 2>nul
        )
    )
)
if !ALIVE!==1 (
    echo Run stop.bat first.
    pause
    exit /b 1
)

call .venv\Scripts\activate.bat

rem ---- 1) runners first: minimized windows, stdout -> logs\runnerN_stdout.log ----
rem    ECO_RUNNER_URL/URLS/ECO_PORT cleared so stale forwarding config can't skew topology.
for /l %%i in (0,1,%LAST%) do (
    start "CardColony-runner%%i" /min cmd /c "set ECO_ROLE=runner&& set ECO_RUNNER_COUNT=%ECO_RUNNER_COUNT%&& set ECO_RUNNER_INDEX=%%i&& set ECO_RUNNER_URL=&& set ECO_RUNNER_URLS=&& set ECO_PORT=&& python runner_main.py %%i >> logs\runner%%i_stdout.log 2>&1"
    echo starting runner%%i ^(port %BASE_PORT%+%%i^)...
)

rem ---- 2) wait for all runner ports (max ~60s); proceed anyway - web proxies per-request ----
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
echo [WARN] runner ports not all up within 60s, starting web anyway.
:runners_up

rem ---- 2b) condense dedicated process (ECO_ROLE=condense, sole owner of
rem    condensation; bare health probe on 127.0.0.1:5710) ----
start "CardColony-condense" /min cmd /c "set ECO_ROLE=condense&& set ECO_RUNNER_COUNT=%ECO_RUNNER_COUNT%&& set ECO_RUNNER_URL=&& set ECO_RUNNER_URLS=&& set ECO_PORT=&& python condense_main.py >> logs\condense_stdout.log 2>&1"
echo starting condense (probe port 5710)...

rem ---- 3) web on 5700 (visible console) ----
start "CardColony-web" cmd /c "set ECO_ROLE=web&& set ECO_RUNNER_COUNT=%ECO_RUNNER_COUNT%&& set ECO_RUNNER_URL=&& set ECO_RUNNER_URLS=&& set ECO_PORT=&& python main.py"

echo web starting...
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
    set /a LPORT=%BASE_PORT%+%LAST%
    echo [OK] web up ^(PID !PID!^), runner x%ECO_RUNNER_COUNT% ^(5701-!LPORT!^)
    echo http://localhost:5700
) else (
    echo [FAIL] web did not start - see logs\service_stdout.log and runnerN_stdout.log
)

rem ---- 4) watchdog + clear manual-stop flag ----
start "CardColony-watchdog" /min cmd /c "python watchdog.py >> logs\watchdog_stdout.log 2>&1"
del logs\.manual_stop >nul 2>&1
echo watchdog started

endlocal
pause
