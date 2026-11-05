@echo off
rem [自动任务 2026-11-04] 计划任务「推演agent-日重启5700」每日 07:00 调用。
rem 与 start.bat/stop.bat 的区别：无 pause、无窗口标题、结果落
rem logs\service_restart.log——计划任务在后台跑，不等人按键。
chcp 65001 >nul
cd /d "%~dp0"
setlocal enabledelayedexpansion

set LOG=logs\service_restart.log
if not exist logs mkdir logs
echo [%date% %time%] ===== 日重启开始 ===== >> %LOG%

rem ---- 1) 停旧进程：先按 .server.pid，再按端口兜底（pid 文件失效时）----
if exist .server.pid (
    set /p PID=<.server.pid
    tasklist /FI "PID eq !PID!" 2>nul | findstr "!PID!" >nul
    if not errorlevel 1 (
        taskkill /PID !PID! /F >> %LOG% 2>&1
        echo 按 pid 文件停了 !PID! >> %LOG%
    )
    del .server.pid >nul 2>&1
)
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":5700" ^| findstr LISTENING') do (
    taskkill /PID %%a /F >> %LOG% 2>&1
    echo 按端口兜底停了 %%a >> %LOG%
)
ping -n 3 127.0.0.1 >nul

rem ---- 2) 启动：venv 激活后另起最小化窗口，stdout 落文件 ----
call .venv\Scripts\activate.bat
start "" /min cmd /c "python main.py >> logs\service_stdout.log 2>&1"

rem ---- 3) 验证：最多等 ~30s，.server.pid 出现且 5700 在听才算成 ----
set OK=0
for /l %%i in (1,1,15) do (
    ping -n 2 127.0.0.1 >nul
    if exist .server.pid (
        netstat -ano | findstr ":5700" | findstr LISTENING >nul
        if not errorlevel 1 ( set OK=1 & goto :done )
    )
)
:done
if %OK%==1 (
    set /p PID=<.server.pid
    echo [%date% %time%] 重启成功 PID=!PID! >> %LOG%
) else (
    echo [%date% %time%] 重启失败：未见 .server.pid 或 5700 未监听 >> %LOG%
)
endlocal
