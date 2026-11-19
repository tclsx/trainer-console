#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""命令行入口。

常用：
  python run.py init                          # 生成 config.json
  python run.py login                         # 用 config 里的学号密码登录
  python run.py quota                         # 看练习额度
  python run.py tags --type 4                 # 看可选题目标签
  python run.py once --type 4 --dry-run       # 取一份卷、答题、但不交（先验流程）
  python run.py once --type 4                 # 刷一份编程题
  python run.py loop --papers 3 --type 3      # 连刷 3 份填空题
  python run.py inspect --sid 12345           # 打印某份卷子的原始结构
  python run.py report                        # 生成 HTML 报告
"""
from __future__ import annotations

import argparse
import json
import shutil
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
from trainer import report as report_mod
from trainer.api import ApiError, LoginError, TrainerAPI
from trainer.captcha import CaptchaSolver
from trainer.config import QBASE_TYPES, STATE_DIR
from trainer.runner import Runner, log
from trainer.solver import Solver, parse_questions


def build(args) -> tuple[dict, TrainerAPI, bank_mod.Bank]:
    cfg_mod.ensure_config()
    cfg = cfg_mod.load(args.config)
    api = TrainerAPI(cfg["base_url"], cfg_mod.SESSION_FILE)
    store = bank_mod.Bank(cfg_mod.DB_FILE)
    return cfg, api, store


def need_login(cfg, api) -> dict:
    acc = cfg.get("account") or {}
    try:
        return api.ensure_login(acc.get("code", ""), acc.get("pwd", ""))
    except LoginError as e:
        log(str(e), "✗")
        log("请把学号/密码填进 config.json 的 account 字段，再执行 python run.py login", "!")
        sys.exit(2)


# ---------------- 各命令 ----------------
def cmd_init(args):
    cfg_mod.ensure_config()
    if not cfg_mod.DEFAULT_CONFIG.exists():
        shutil.copy(cfg_mod.EXAMPLE_CONFIG, cfg_mod.DEFAULT_CONFIG)
    cfg_mod.STATE_DIR.mkdir(parents=True, exist_ok=True)
    print(f"配置已就绪：{cfg_mod.DEFAULT_CONFIG}")
    print("打开它，填 account.code / account.pwd，以及 llm.api_key（要让模型答题的话）")


def cmd_login(args):
    cfg, api, store = build(args)
    acc = cfg.get("account") or {}
    code, pwd = args.code or acc.get("code"), args.pwd or acc.get("pwd")
    if not code or not pwd:
        log("缺少学号或密码（config.json 的 account，或 --code/--pwd）", "✗")
        return 2
    try:
        user = api.login(code, pwd)
    except LoginError as e:
        log(str(e), "✗")
        return 2
    log(f"登录成功：{user.get('name')}（{user.get('code')}）", "✓")
    q = api.quota()
    log(f"额度：{json.dumps(q, ensure_ascii=False)}")
    return 0


def cmd_quota(args):
    cfg, api, store = build(args)
    need_login(cfg, api)
    print(json.dumps(api.quota(), ensure_ascii=False, indent=2))
    try:
        print(json.dumps(api.gate(), ensure_ascii=False, indent=2))
    except ApiError:
        pass
    try:
        prof = api.profile()
        keep = {k: prof.get(k) for k in ("name", "code", "gold", "exScore") if k in prof}
        if keep:
            print("账号：", json.dumps(keep, ensure_ascii=False))
    except ApiError:
        pass
    return 0


def cmd_tags(args):
    cfg, api, store = build(args)
    need_login(cfg, api)
    tags = api.tags(args.type, args.level)
    log(f"{QBASE_TYPES.get(args.type, args.type)} / 难度 {args.level} 共 {len(tags)} 个标签")
    for t in tags:
        flag = "可用" if t.get("available", True) else "已做完/不可用"
        print(f"  id={t.get('id'):<5} {t.get('content')}  [{flag}]")
    return 0


def cmd_once(args):
    cfg, api, store = build(args)
    need_login(cfg, api)
    runner = make_runner(cfg, api, store, dry_run=args.dry_run)
    r = runner.run_one(args.type, args.level, args.tag)
    print(json.dumps(r, ensure_ascii=False, indent=2, default=str))
    return 0 if r.get("ok") else 1


def cmd_loop(args):
    cfg, api, store = build(args)
    need_login(cfg, api)
    runner = make_runner(cfg, api, store, dry_run=args.dry_run)
    r = runner.loop(args.papers, args.type, args.level, args.tag)
    log(f"本轮完成 {r['done']} 份", "★")
    print(report_mod.console_table(store, 10))
    return 0


def cmd_resume(args):
    cfg, api, store = build(args)
    need_login(cfg, api)
    runner = make_runner(cfg, api, store, dry_run=args.dry_run)
    runner.resume_pending()
    return 0


def cmd_inspect(args):
    cfg, api, store = build(args)
    need_login(cfg, api)
    sid = args.sid
    if not sid:
        code, data = api.choose_q(args.type, args.level, args.tag)
        if code != 0:
            log(f"选题失败：{code} {data}", "✗")
            return 1
        sid = data.get("sessionId")
        log(f"新开一份，sessionId={sid}")
        store.add_paper(sid, args.type, args.tag, args.level)
    rec = api.record(sid)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = STATE_DIR / f"record_{sid}.json"
    path.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    qs = parse_questions(rec)
    print(f"顶层字段：{list(rec.keys())}")
    print(f"题目数量：{len(qs)}（原始 JSON 已存 {path}）")
    for i, q in enumerate(qs, 1):
        print(f"  #{i} type={q['qtype']} point={q['point']} lang={q['language']}")
        print(f"     题干：{(q['content'] or '')[:80].replace(chr(10), ' ')}")
        for o in q["options"]:
            print(f"     选项 {o['globalId']}: {(o['content'] or '')[:50]}")
    ap = rec.get("answerPaper") or {}
    print("答题纸结构：" + json.dumps({k: len(v or []) for k, v in ap.items()}, ensure_ascii=False))
    if ap.get("q0"):
        print("答题纸样例：" + json.dumps(ap["q0"][0], ensure_ascii=False)[:400])
    return 0


def cmd_diagnose(args):
    """拉取判卷详情：每题得分 + 失败用例的输入/期望/实际输出，落到 .state 方便对照修答案。"""
    cfg, api, store = build(args)
    need_login(cfg, api)
    sid = args.sid
    data = api.score_result(sid)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if not isinstance(data, list):
        data = [data]
    total_score = 0.0
    failed = 0
    for q in data:
        if not isinstance(q, dict):
            continue
        gid = str(q.get("globalId") or "")[:8]
        sr = q.get("scoreResult") or {}
        score = sr.get("score", q.get("score"))
        total_score += float(score or 0)
        print(f"题 {gid}：{score} 分  state={sr.get('state')}")
        tc = (sr.get("failedTestCase") or {})
        tcc = tc.get("testCase") or {}
        if tcc:
            failed += 1
            base = STATE_DIR / f"tc_{sid[:8]}_{gid}"
            base.with_suffix(".in").write_text(tcc.get("input") or "", encoding="utf-8")
            base.with_suffix(".exp").write_text(tcc.get("output") or "", encoding="utf-8")
            base.with_suffix(".act").write_text(tc.get("output") or "", encoding="utf-8")
            print(f"  失败用例已存：{base}.in / .exp（期望） / .act（实际）")
            print(f"  期望首行：{(tcc.get('output') or '').splitlines()[:2]}")
            print(f"  实际首行：{(tc.get('output') or '').splitlines()[:2]}")
    print(f"合计：{round(total_score, 1)} 分")
    if failed:
        print("修好答案后：run.py seed --sid ... --file ... && run.py redo <sid>")
    return 0


def cmd_redo(args):
    """reanswer 重开已交卷的卷子，用题库当前答案重做并交卷（修完答案后跑）。"""
    cfg, api, store = build(args)
    need_login(cfg, api)
    runner = make_runner(cfg, api, store)
    for sid in args.sids:
        try:
            api.reanswer(sid)
            rec = api.record(sid)
            if int(rec.get("state", 0)) != 0:
                log(f"{sid} 重开后状态={rec.get('state')}，跳过", "!")
                continue
            rows, stats = runner.solver.solve(rec)
            if stats.get("error"):
                log(f"{sid} 解题失败：{stats['error']}", "!")
                continue
            for r in rows:
                log(f"  编程题 [{r.get('source','?')}] {r.get('preview','')}")
            result = runner._answer_and_submit(sid, rec, stats=stats, rows=rows)
            log(f"重做结果：{result.get('score')}/{result.get('total')}", "★")
        except Exception as e:  # noqa: BLE001
            log(f"{sid} 重做失败：{e}", "!")
    return 0


def cmd_report(args):
    cfg, api, store = build(args)
    out = Path(args.out) if args.out else cfg_mod.ROOT / "report.html"
    acc = (cfg.get("account") or {}).get("code", "")
    path = report_mod.build_html(store, out, acc)
    print(report_mod.console_table(store, 15))
    print(f"\n报告已生成：{path}")
    return 0


def cmd_bank(args):
    cfg, api, store = build(args)
    print(json.dumps(store.stats(), ensure_ascii=False, indent=2))
    return 0


def cmd_seed(args):
    """把人工写好的答案喂进本地题库（配合 resume 使用：题库命中即可交卷）。"""
    cfg, api, store = build(args)
    need_login(cfg, api)
    rec = api.record(args.sid)
    questions = parse_questions(rec)
    if not questions:
        log("卷子里没有题目", "✗")
        return 1
    if args.qid:
        q = next((x for x in questions if x["globalId"] == args.qid), None)
    elif len(questions) == 1:
        q = questions[0]
    else:
        log(f"卷里有 {len(questions)} 题，请用 --qid 指定 globalId：", "!")
        for i, x in enumerate(questions, 1):
            print(f"  {i}. {x['globalId']}  {(x['content'] or '')[:40]}")
        return 1
    if q is None:
        log("没找到 --qid 对应的题目", "✗")
        return 1
    from trainer.solver import _slot_map

    slots = _slot_map(rec)
    hit = slots.get(str(q["globalId"]))
    bucket = hit[0] if hit else 4
    code = Path(args.file).read_text(encoding="utf-8")
    h = bank_mod.qhash(bucket, q["content"], q["options"])
    store.put_answer(h, bucket, q["content"], code, "manual")
    log(f"已把 {args.file} 存为该题答案（q{bucket} 桶 / {q['globalId'][:8]}…）", "✓")
    return 0


def make_runner(cfg, api, store, dry_run=False) -> Runner:
    solver = Solver(cfg, store)
    captcha = CaptchaSolver(
        enabled=(cfg.get("safety") or {}).get("captcha_ocr", True),
        workdir=STATE_DIR,
    )
    if not solver.llm.enabled:
        log("LLM 未启用：只能命中本地题库，未命中的题会用兜底答案（分数不会好看）", "!")
    elif not solver.llm.key and "localhost" not in solver.llm.base and "127.0.0.1" not in solver.llm.base:
        log("LLM 已启用但 api_key 为空", "!")
    return Runner(cfg, api, store, solver, captcha, dry_run=dry_run)


def main(argv=None):
    p = argparse.ArgumentParser(description="HIT 训练平台自动刷题工具")
    p.add_argument("--config", default=None, help="配置文件路径，默认 config.json")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="生成 config.json").set_defaults(func=cmd_init)

    sp = sub.add_parser("login", help="登录并保存会话")
    sp.add_argument("--code")
    sp.add_argument("--pwd")
    sp.set_defaults(func=cmd_login)

    sub.add_parser("quota", help="查看额度与账号状态").set_defaults(func=cmd_quota)

    sp = sub.add_parser("tags", help="列出可选题目标签")
    sp.add_argument("--type", type=int, default=4)
    sp.add_argument("--level", type=int, default=0)
    sp.set_defaults(func=cmd_tags)

    sp = sub.add_parser("once", help="刷一份")
    sp.add_argument("--type", type=int, default=None, help="0单选 1多选 2判断 3填空 4编程")
    sp.add_argument("--level", type=int, default=None, help="0随机 1非常简单 2简单 3普通 4困难 5非常困难")
    sp.add_argument("--tag", type=int, default=None, help="标签 id，-1 表示不限")
    sp.add_argument("--dry-run", action="store_true", help="只答题不提交")
    sp.set_defaults(func=cmd_once)

    sp = sub.add_parser("loop", help="连刷多份")
    sp.add_argument("--papers", type=int, default=3)
    sp.add_argument("--type", type=int, default=None)
    sp.add_argument("--level", type=int, default=None)
    sp.add_argument("--tag", type=int, default=None)
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=cmd_loop)

    sp = sub.add_parser("resume", help="续做本地记账里未交卷的卷子")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=cmd_resume)

    sp = sub.add_parser("inspect", help="打印卷子原始结构（排查用）")
    sp.add_argument("--sid", default=None)
    sp.add_argument("--type", type=int, default=4)
    sp.add_argument("--level", type=int, default=0)
    sp.add_argument("--tag", type=int, default=-1)
    sp.set_defaults(func=cmd_inspect)

    sp = sub.add_parser("report", help="生成 HTML 报告")
    sp.add_argument("--out", default=None)
    sp.set_defaults(func=cmd_report)

    sub.add_parser("bank", help="本地题库统计").set_defaults(func=cmd_bank)

    sp = sub.add_parser("seed", help="给某份卷子的题目喂人工答案进题库")
    sp.add_argument("--sid", required=True, help="sessionId")
    sp.add_argument("--file", required=True, help="答案文件（代码/文本）")
    sp.add_argument("--qid", default=None, help="题目 globalId（单题卷可省）")
    sp.set_defaults(func=cmd_seed)

    sp = sub.add_parser("diagnose", help="查看判卷详情：失败用例的输入/期望/实际输出")
    sp.add_argument("--sid", required=True, help="sessionId")
    sp.set_defaults(func=cmd_diagnose)

    sp = sub.add_parser("redo", help="重开已交卷的卷子重做（配合 seed 修答案）")
    sp.add_argument("sids", nargs="+", help="sessionId 列表")
    sp.set_defaults(func=cmd_redo)

    args = p.parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
