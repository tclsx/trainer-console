#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""reanswer：重开已交卷的卷子，用题库里的（更新后的）答案重做并交卷。"""
from __future__ import annotations

import sys
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from trainer import bank as bank_mod
from trainer import config as cfg_mod
from trainer.api import TrainerAPI
from trainer.captcha import CaptchaSolver
from trainer.runner import Runner
from trainer.solver import Solver


def main() -> int:
    sids = sys.argv[1:]
    if not sids:
        print("用法: python redo.py <sid> [sid ...]")
        return 2

    cfg = cfg_mod.load()
    api = TrainerAPI(cfg["base_url"], cfg_mod.SESSION_FILE)
    acc = cfg.get("account") or {}
    api.ensure_login(acc.get("code", ""), acc.get("pwd", ""))
    store = bank_mod.Bank(cfg_mod.DB_FILE)
    solver = Solver(cfg, store)
    captcha = CaptchaSolver(enabled=(cfg.get("safety") or {}).get("captcha_ocr", True), workdir=cfg_mod.STATE_DIR)
    runner = Runner(cfg, api, store, solver, captcha, dry_run=False)

    for sid in sids:
        print(f"== reanswer {sid}")
        try:
            r = api.reanswer(sid)
            print("  reanswer ->", r)
        except Exception as e:
            print("  reanswer 失败:", e)
            continue
        try:
            rec = api.record(sid)
            print("  state =", rec.get("state"))
            rows, stats = runner.solver.solve(rec)
            if stats.get("error"):
                print("  solve 错误:", stats["error"])
                continue
            for row in rows:
                print(f"  [{row.get('source')}] {str(row.get('preview'))[:60]}")
            result = runner._answer_and_submit(sid, rec, stats=stats, rows=rows)
            print("  result =", result)
        except Exception as e:
            print("  重做失败:", e)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
