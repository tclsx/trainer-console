@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title 训练助手 - 一键安装

echo.
echo ============================================================
echo   训练助手 · 一键安装
echo ============================================================
echo.

rem ---------- 1. 找 Python ----------
set "PY="
where python >nul 2>nul
if not errorlevel 1 set "PY=python"
if not defined PY (
  where py >nul 2>nul
  if not errorlevel 1 set "PY=py -3"
)
if not defined PY (
  echo [X] 没有找到 Python。
  echo.
  echo     请先安装 Python 3.10 或更高版本： https://www.python.org/downloads/
  echo     安装时务必勾选 "Add Python to PATH"，装完重新双击本文件。
  echo.
  pause
  exit /b 1
)

echo [1/4] 检查 Python ...
%PY% --version
if errorlevel 1 (
  echo [X] Python 无法运行，请重新安装。
  pause
  exit /b 1
)

rem ---------- 2. 独立虚拟环境 ----------
echo.
echo [2/4] 准备独立运行环境 .venv ...
if exist ".venv\Scripts\python.exe" (
  echo      已存在，跳过创建。
) else (
  %PY% -m venv .venv
  if errorlevel 1 (
    echo [X] 创建 .venv 失败。
    pause
    exit /b 1
  )
)

set "VPY=.venv\Scripts\python.exe"

rem ---------- 3. 装依赖 ----------
echo.
echo [3/4] 安装依赖（首次约 1-3 分钟，请耐心）...
"%VPY%" -m pip install --upgrade pip -q -i https://pypi.tuna.tsinghua.edu.cn/simple
"%VPY%" -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
if errorlevel 1 (
  echo.
  echo [!] 镜像源安装失败，改用官方源重试一次 ...
  "%VPY%" -m pip install -r requirements.txt
  if errorlevel 1 (
    echo [X] 依赖安装失败，请把上面的报错截图发给维护者。
    pause
    exit /b 1
  )
)

rem ---------- 4. 生成并填写配置 ----------
echo.
echo [4/4] 生成配置文件 ...
if not exist "config.json" (
  copy /y "config.example.json" "config.json" >nul
  echo      已生成 config.json
) else (
  echo      config.json 已存在，保留原配置。
)

echo.
call :setup
if errorlevel 1 (
  echo [!] 配置向导未正常完成，可以稍后双击「0-重新配置.bat」补填。
)

echo.
echo ============================================================
echo   安装完成！
echo   下一步：双击「2-启动控制台.bat」
echo   浏览器会打开 http://127.0.0.1:8791/
echo   详细教程见「安装与使用教程.md」
echo ============================================================
echo.
pause
exit /b 0

:setup
"%VPY%" setup.py
exit /b %errorlevel%
