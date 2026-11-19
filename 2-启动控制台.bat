@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title 训练助手 - 启动控制台

if not exist ".venv\Scripts\python.exe" (
  echo [X] 还没安装，请先双击「1-安装.bat」。
  echo.
  pause
  exit /b 1
)
if not exist "config.json" (
  echo [X] 还没有 config.json，请先双击「1-安装.bat」或「0-重新配置.bat」。
  echo.
  pause
  exit /b 1
)

echo 正在启动控制台 ...
start "" /min ".venv\Scripts\python.exe" gui.py 8791 --no-browser
rem 等服务起来再开浏览器（重复运行安全：已有实例会自动退出）
timeout /t 2 >nul
start "" http://127.0.0.1:8791/

echo.
echo 控制台地址： http://127.0.0.1:8791/
echo 如果浏览器没自动打开，手动复制上面的地址到浏览器即可。
echo 关闭控制台：在任务管理器结束 python.exe，或运行「3-停止控制台.bat」。
echo.
pause
