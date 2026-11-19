"""首次配置向导：交互式填写平台地址与账号，写入 config.json。

由「1-安装.bat」自动调用；也可以随时双击「0-重新配置.bat」重跑。
直接回车 = 保留当前值。密码明文保存在本机 config.json，请勿把整个文件夹再转发给别人。
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TEMPLATE = ROOT / "config.example.json"
TARGET = ROOT / "config.json"


def main() -> int:
    if not TARGET.exists():
        if not TEMPLATE.exists():
            print("[X] 找不到 config.example.json，安装包不完整")
            return 1
        shutil.copy(TEMPLATE, TARGET)
        print("[*] 已从模板生成 config.json")
    cfg = json.loads(TARGET.read_text(encoding="utf-8"))

    print()
    print("=" * 58)
    print(" 训练助手 · 首次配置（直接回车 = 保留原值）")
    print("=" * 58)
    print(" 说明：平台地址要写到你学校训练平台的根路径，")
    print("       形如 http://192.168.1.100/train/   （换成实际地址，结尾 / 会自动补）")
    print()

    cur_base = cfg.get("base_url", "")
    cur_code = (cfg.get("account") or {}).get("code", "")
    has_pwd = bool((cfg.get("account") or {}).get("pwd"))

    base = input(f"平台地址 [{cur_base}]: ").strip()
    code = input(f"学号/工号 [{cur_code or '未填'}]: ").strip()
    pwd = input(f"密码 [{'已设置，回车保留' if has_pwd else '未设置'}]: ").strip()

    if base:
        cfg["base_url"] = base.rstrip("/") + "/"
    acc = cfg.setdefault("account", {})
    if code:
        acc["code"] = code
    if pwd:
        acc["pwd"] = pwd

    TARGET.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    print()
    print("-" * 58)
    print(f" 平台地址：{cfg['base_url']}")
    print(f" 账号：    {acc.get('code') or '（未填，可以在控制台里登录）'}")
    print(f" 密码：    {'已设置' if acc.get('pwd') else '（未填）'}")
    print("-" * 58)
    if "YOUR-HOST" in cfg["base_url"]:
        print(" [!] 平台地址还是占位符，请重新运行本向导填入真实地址，")
        print("     否则控制台会连不上服务器。")
    print(" 配置已保存。接下来双击「2-启动控制台.bat」。")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
