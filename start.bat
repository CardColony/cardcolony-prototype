@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
title 推演agent画布

cd /d "%~dp0"

:: 检查是否已在运行
if exist .server.pid (
    set /p PID=<.server.pid
    tasklist /FI "PID eq !PID!" 2>nul | findstr "!PID!" >nul
    if not errorlevel 1 (
        echo [错误] 服务已在运行 (PID: !PID!)
        echo 如需重启请先执行 stop.bat
        pause
        exit /b 1
    )
    del .server.pid 2>nul
)

:: 激活虚拟环境并启动（PID 由 main.py 自己写入 .server.pid）
call .venv\Scripts\activate.bat
start "推演agent画布" python main.py

echo 服务正在启动...
timeout /t 3 /nobreak >nul

if exist .server.pid (
    set /p PID=<.server.pid
    echo [成功] 服务已启动 (PID: !PID!)
    echo 访问 http://localhost:5700
) else (
    echo [失败] 服务未能启动，请检查 .env 和依赖
)

endlocal
pause
