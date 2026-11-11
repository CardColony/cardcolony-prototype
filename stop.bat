@echo off
rem [multi-process topology 2026-11-07] cluster stop:
rem   write logs\.manual_stop -> kill watchdog (else it revives the service
rem   within ~80s) -> kill all .server*.pid processes -> port sweep 5700-5708.
rem NOTE: keep this file pure ASCII - chcp races make non-ASCII lines flaky.
cd /d "%~dp0"
setlocal enabledelayedexpansion

set FOUND=0

rem ---- 0) manual-stop flag first: stray watchdogs stay passive ----
if not exist logs mkdir logs
echo manual stop> logs\.manual_stop

rem ---- 1) watchdog: pid file first, cmdline scan as fallback ----
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

rem ---- 2) service processes by pid file ----
for %%f in (.server.pid .server-runner*.pid .server-condense.pid) do (
    if exist %%f (
        set "PID="
        set /p PID=<%%f
        if defined PID (
            taskkill /PID !PID! /F /T >nul 2>&1 && ( echo [OK] %%f stopped ^(PID !PID!^) & set FOUND=1 ) || echo [INFO] %%f pid !PID! already gone
        )
        del %%f >nul 2>&1
    )
)

rem ---- 3) port sweep: 5700 + 5701..5710 (runners up to 8 + condense 5710) ----
for /l %%p in (5700,1,5710) do (
    for /f "tokens=5" %%a in ('netstat -ano ^| findstr LISTENING ^| findstr ":%%p "') do (
        if not "%%a"=="0" (
            taskkill /PID %%a /F >nul 2>&1 && echo [OK] killed PID %%a on port %%p
            set FOUND=1
        )
    )
)

if !FOUND!==0 echo [INFO] no running service processes found (watchdog + flag handled)

endlocal
pause
