@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title 训练助手 - 停止控制台

set "FOUND="
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8791" ^| findstr "LISTENING"') do (
  taskkill /PID %%a /F >nul 2>nul
  if not errorlevel 1 set "FOUND=1"
)
if defined FOUND (
  echo 控制台已停止。
) else (
  echo 没有发现正在运行的控制台（端口 8791 空闲）。
)
echo.
pause
