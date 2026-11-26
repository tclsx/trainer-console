# 训练助手（TrainerConsole）v1.1

一个**本地运行**的网页控制台，用于校内 C 语言训练平台「自主练习」模式的自动答题：
点一下「开始」，自动完成选题 → 取卷 → 解题 → 保存 → 按平台要求停留 → 交卷 → 记录成绩。

> 仅支持自主练习模式；仅面向能访问该训练平台网络的本人账号。
> 使用前请先读「使用边界与免责.md」和「安装与使用教程.md」。

---

## 快速开始

```
1. 解压到 D:\TrainerConsole\（路径别带中文/空格）
2. 双击 1-安装.bat        → 自动装 Python 依赖 + 填平台地址/账号
3. 双击 2-启动控制台.bat  → 浏览器打开 http://127.0.0.1:8791/
4. 控制台「LLM 设置」填入自己的 API key → 测试连接 → 启用
5. 点「▶ 开始」
```

**自动解题需要一个大模型 API key**（控制台内置智谱/DeepSeek/通义/Kimi/硅基流动/Ollama 预设，
默认智谱 `glm-4-flash` 免费档）。没配 key 时控制台可用，但新题不会自动生成答案。
包内不含任何账号、密码、API key 或题库答案。

---

## 功能

| 能力 | 说明 |
|---|---|
| 本地 Web 控制台 | 纯标准库 HTTP 服务，绑定 `127.0.0.1:8791`，不对外暴露 |
| 题库优先 | 做过的题按题干哈希记住答案，下次直接命中，不经过模型 |
| 模型解题 | OpenAI 兼容端点，支持 6 家预设；生成完整 C 源码 |
| 交卷前自检 | 拦截会让服务端崩溃的写法、本机 gcc 语法检查，失败自动重写一次 |
| 合规节奏 | 按平台要求控制最短作答时长、卷间间隔、每小时上限 |
| 未完成卷管理 | pending 卷列出题干，可手动粘代码「📥 入题库」后续做 |
| 成绩与诊断 | 成绩表逐卷得分；「诊断」看判卷真实输入/期望/实际输出，「重做」重交 |
| 会话自愈 | 服务端登录态过期时自动用配置账号重登 |
| 报告导出 | 一键生成成绩 HTML 报告 |

所有数据保存在本地 `.state/`（SQLite + 会话文件），不上传任何第三方。

---

## 命令行（可选）

命令行入口 `命令行.bat`，或直接：

```bat
.venv\Scripts\python.exe run.py resume                      :: 续做未完成卷
.venv\Scripts\python.exe run.py once                        :: 刷一份
.venv\Scripts\python.exe run.py diagnose --sid <sessionId>  :: 查看判卷真实用例
.venv\Scripts\python.exe run.py seed --sid <sessionId> --file ans.c   :: 喂人工答案进题库
.venv\Scripts\python.exe run.py redo <sessionId>            :: 改完答案重做某卷
.venv\Scripts\python.exe run.py quota                       :: 查看额度
```

---

## 环境要求

- Windows 10 / 11
- Python 3.10+（安装时勾选 "Add Python to PATH"）
- 能访问训练平台的网络（通常为校园网 / VPN）
- 依赖：`requests`（`requirements.txt`，安装脚本自动装）
- 可选：MinGW gcc（启用交卷前 C 语法自检，没有会自动跳过）
- 可选：`ddddocr`（自动识别人机验证数字图，不装则手动输入）

---

## 目录结构

```
TrainerConsole/
├─ 1-安装.bat  2-启动控制台.bat  0-重新配置.bat  3-停止控制台.bat
├─ setup.py                 首次配置向导
├─ config.example.json       干净配置模板
├─ config.json               （安装后生成，含你的账号与 key）
├─ gui.py  run.py  redo.py   程序入口
├─ trainer/                  核心模块（api / bank / solver / runner / llm / captcha / config / keys / report）
├─ .venv/                    （安装后生成）独立 Python 环境
└─ .state/                   （运行后生成）题库、成绩、会话、日志
```

---

## 已知边界

- 自动解题质量受模型能力和平台题目质量影响，**不承诺满分**。平台存在题面与判卷期望不一致、单次动态用例、部分改错题静态匹配等情况；低分卷请用「诊断 / 重做」修复。
- 平台有作答时长、按键序列、人机验证等风控。工具默认采用保守节奏，**不建议调快**。
- 本包不含任何题库答案与历史成绩，全部从零积累。

详细步骤见「安装与使用教程.md」，合规说明见「使用边界与免责.md」。
