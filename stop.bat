@echo off
chcp 65001 >nul
cd /d "%~dp0"
setlocal enabledelayedexpansion

if exist .server.pid (
    set /p PID=<.server.pid
    taskkill /PID !PID! /F >nul 2>&1 && echo [OK] 推演agent画布已停止 (PID: !PID!) || echo [INFO] 进程 !PID! 不存在
    del .server.pid >nul 2>&1
) else (
    echo [INFO] 未找到 .server.pid，服务可能未在运行
)

endlocal
pause
