@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title 训练助手 - 重新配置

if not exist ".venv\Scripts\python.exe" (
  echo [X] 还没安装，请先双击「1-安装.bat」。
  pause
  exit /b 1
)
".venv\Scripts\python.exe" setup.py
echo.
pause
