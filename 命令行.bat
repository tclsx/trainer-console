@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title 训练助手 - 命令行

if not exist ".venv\Scripts\python.exe" (
  echo [X] 还没安装，请先双击「1-安装.bat」。
  pause
  exit /b 1
)
echo 用法示例：
echo   run.py resume          续做未完成卷
echo   run.py once            刷一份
echo   run.py diagnose --sid XXX   查看判卷真实用例
echo   run.py seed --sid XXX --file ans.c   喂人工答案进题库
echo.
cmd /k ".venv\Scripts\python.exe run.py %*"
