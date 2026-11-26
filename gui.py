#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""trainer-auto 控制台：本地 Web UI，随时控制刷题。

用法：
  .venv/Scripts/python.exe gui.py            # 默认 http://127.0.0.1:8791
  .venv/Scripts/python.exe gui.py 8888       # 自定义端口

功能：
  - 登录/额度/题库状态一览
  - 开始（连刷/单份/续做/试跑）与随时停止
  - 实时运行日志
  - 未完成卷子查看 + 手动喂答案入题库（无 LLM key 的手动路径）
  - LLM 配置快捷修改
  - 成绩表 + HTML 报告
"""
from __future__ import annotations

import json
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

for _s in (sys.stdout, sys.stderr):
    if _s and _s.encoding and _s.encoding.lower() != "utf-8":
        try:
            _s.reconfigure(encoding="utf-8", errors="backslashreplace")
        except Exception:
            pass

from trainer import bank as bank_mod
from trainer import config as cfg_mod
from trainer import report as report_mod
from trainer.api import ApiError, LoginError, TrainerAPI
from trainer.captcha import CaptchaSolver
from trainer.config import STATE_DIR
from trainer.llm import LLMClient, PRESETS as LLM_PRESETS
from trainer.runner import STOP, Runner, Stopped, log
from trainer.solver import TYPE_NAME, Solver, _slot_map, parse_questions

# ---------------- 日志缓冲（捕获 stdout，前端轮询） ----------------
class LogBuf:
    def __init__(self, cap: int = 1500):
        self.lock = threading.Lock()
        self.items: list[tuple[int, str]] = []
        self.next = 0
        self.cap = cap

    def add(self, line: str) -> None:
        with self.lock:
            self.items.append((self.next, line))
            self.next += 1
            if len(self.items) > self.cap:
                self.items = self.items[-self.cap // 2 :]

    def since(self, idx: int) -> tuple[list[tuple[int, str]], int]:
        with self.lock:
            return [x for x in self.items if x[0] >= idx], self.next


LOGBUF = LogBuf()


class _Tee:
    """把进程 stdout 同时送进日志缓冲（runner.log/solver 的 print 全都能收到）。"""

    def __init__(self, orig):
        self.orig = orig
        self._pending = ""

    def write(self, s: str):
        try:
            self.orig.write(s)
        except Exception:
            pass
        self._pending += s
        while "\n" in self._pending:
            line, self._pending = self._pending.split("\n", 1)
            LOGBUF.add(line)

    def flush(self):
        try:
            self.orig.flush()
        except Exception:
            pass

    def __getattr__(self, name):
        return getattr(self.orig, name)


sys.stdout = _Tee(sys.stdout)  # type: ignore[assignment]

# ---------------- 任务状态 ----------------
JOB_LOCK = threading.Lock()
JOB: dict = {
    "running": False,
    "kind": None,          # once / loop / resume
    "dry_run": False,
    "started": None,
    "done": 0,
    "total": None,
    "stop_requested": False,
    "error": None,
    "finished_at": None,
}
PROG_RE = re.compile(r"===== 第 (\d+)/(\d+) 份")

# ---------------- 人机验证：图交给浏览器，等用户填 ----------------
CAPTCHA_LOCK = threading.Lock()
CAPTCHA: dict = {"pending": False, "image": "", "created": None}
CAPTCHA_EVENT = threading.Event()
CAPTCHA_CODE: dict = {"value": None}


def captcha_handler(img: bytes) -> str | None:
    """由 Runner 的 CaptchaSolver 调用：把图推到控制台，阻塞等用户输入（最长 5 分钟）。"""
    import base64 as _b64

    path = STATE_DIR / "captcha.png"
    try:
        path.write_bytes(img)
    except Exception:
        pass
    with CAPTCHA_LOCK:
        CAPTCHA.update(
            pending=True,
            image=_b64.b64encode(img).decode(),
            created=time.time(),
        )
        CAPTCHA_CODE["value"] = None
    CAPTCHA_EVENT.clear()
    log("平台要求人机验证，请在窗口右下角输入验证码（5 分钟内）", "!")
    got = CAPTCHA_EVENT.wait(timeout=300)
    with CAPTCHA_LOCK:
        CAPTCHA["pending"] = False
        code = CAPTCHA_CODE["value"]
    return code if got else None


def job_set(**kw) -> None:
    with JOB_LOCK:
        JOB.update(kw)


def _job_thread(params: dict) -> None:
    kind = params.get("mode", "once")
    try:
        cfg = cfg_mod.load()
        api = TrainerAPI(cfg["base_url"], cfg_mod.SESSION_FILE)
        acc = cfg.get("account") or {}
        api.ensure_login(acc.get("code", ""), acc.get("pwd", ""))
        store = bank_mod.Bank(cfg_mod.DB_FILE)
        solver = Solver(cfg, store)
        if not solver.llm.enabled:
            log("LLM 未启用：只能命中本地题库，未命中的题会用兜底答案", "!")
        captcha = CaptchaSolver(
            enabled=(cfg.get("safety") or {}).get("captcha_ocr", True),
            workdir=STATE_DIR,
        )
        captcha.manual_handler = captcha_handler
        runner = Runner(cfg, api, store, solver, captcha, dry_run=params.get("dry_run", False))

        qtype = params.get("type")
        level = params.get("level")
        tag = params.get("tag")
        if kind == "loop":
            job_set(total=int(params.get("papers", 3)))
            r = runner.loop(int(params.get("papers", 3)), qtype, level, tag)
            job_set(done=r.get("done", 0))
        elif kind == "resume":
            runner.resume_pending()
            r = {"ok": True}
        elif kind == "redo":
            sids = params.get("sids") or []
            job_set(total=len(sids))
            done = 0
            for sid in sids:
                if STOP.is_set():
                    log("收到停止请求，剩余卷子不再重做", "■")
                    break
                try:
                    log(f"重做 {sid}")
                    api.reanswer(sid)
                    rec = api.record(sid)
                    if int(rec.get("state", 0)) != 0:
                        log(f"重开后状态={rec.get('state')}，跳过", "!")
                        continue
                    rows, stats = solver.solve(rec)
                    if stats.get("error"):
                        log(f"解题失败：{stats['error']}", "✗")
                        continue
                    for row in rows:
                        log(f"  编程题 [{row.get('source','?')}] {row.get('preview','')}")
                    res = runner._answer_and_submit(sid, rec, stats=stats, rows=rows)
                    log(f"重做结果：{res.get('score')}/{res.get('total')}", "★")
                except Exception as e:  # noqa: BLE001
                    log(f"重做 {sid} 失败：{type(e).__name__}: {e}", "✗")
                done += 1
                job_set(done=done)
            r = {"ok": True, "done": done}
        else:
            r = runner.run_one(qtype, level, tag)
        log(f"任务结束：{json.dumps(r, ensure_ascii=False, default=str)[:400]}", "★")
    except Stopped:
        log("任务已被用户停止", "■")
    except LoginError as e:
        log(f"登录失败：{e}", "✗")
        job_set(error=str(e))
    except Exception as e:  # noqa: BLE001
        log(f"任务异常：{type(e).__name__}: {e}", "✗")
        job_set(error=f"{type(e).__name__}: {e}")
    finally:
        STOP.clear()
        job_set(running=False, kind=None, finished_at=time.time(), stop_requested=False)
        try:
            store.close()
        except Exception:
            pass


def start_job(params: dict) -> tuple[bool, str]:
    with JOB_LOCK:
        if JOB["running"]:
            return False, "已有任务在运行，请先停止"
        JOB.update(
            running=True,
            kind=params.get("mode", "once"),
            dry_run=bool(params.get("dry_run")),
            started=time.time(),
            done=0,
            total=(
                int(params.get("papers", 3)) if params.get("mode") == "loop"
                else len(params.get("sids") or []) if params.get("mode") == "redo"
                else 1
            ),
            stop_requested=False,
            error=None,
            finished_at=None,
        )
    STOP.clear()
    LOGBUF.add(f"[{time.strftime('%H:%M:%S')}] ▶ 控制台启动任务：{json.dumps(params, ensure_ascii=False)}")
    threading.Thread(target=_job_thread, args=(params,), daemon=True).start()
    return True, "已启动"


def make_api() -> TrainerAPI:
    cfg = cfg_mod.load()
    api = TrainerAPI(cfg["base_url"], cfg_mod.SESSION_FILE)
    try:
        api.load_session()  # 恢复上次登录态，否则 record/seed 都会 401
    except Exception:
        pass
    return api


def ensure_live_session(api: TrainerAPI, cfg: dict) -> bool:
    """本地 sess 文件还在、但服务端会话已过期时，自动用 config 账号重登。

    背景：隔天打开控制台时 `load_session()` 仍返回 True（文件在），但 profile/quota
    会 401；status_payload 把异常吞掉，页面就显示一片空白。这里主动探一次活，
    过期就重登，用户无需手动点登录。
    """
    try:
        api.profile()
        return True
    except Exception:
        pass
    acc = cfg.get("account") or {}
    if not (acc.get("code") and acc.get("pwd")):
        return False
    try:
        api.login(acc["code"], acc["pwd"])
        log("服务端会话已过期，已用配置账号自动重新登录", "✓")
        return True
    except Exception as e:  # noqa: BLE001
        log(f"自动重登失败：{type(e).__name__}: {e}", "!")
        return False


def status_payload() -> dict:
    cfg = cfg_mod.load()
    api = make_api()
    logged_in = False
    user: dict = {}
    quota: dict = {}
    try:
        logged_in = bool(api.load_session())
    except Exception:
        pass
    if logged_in:
        logged_in = ensure_live_session(api, cfg)
    if logged_in:
        try:
            prof = api.profile()
            user = {k: prof.get(k) for k in ("name", "code", "gold", "exScore") if k in prof}
        except Exception:
            pass
        try:
            quota = api.quota()
        except Exception:
            pass
    llm = cfg.get("llm") or {}
    store = bank_mod.Bank(cfg_mod.DB_FILE)
    stats = store.stats()
    store.close()
    with JOB_LOCK:
        job = dict(JOB)
    with CAPTCHA_LOCK:
        cap = {"pending": CAPTCHA["pending"], "image": CAPTCHA["image"] if CAPTCHA["pending"] else ""}
    return {
        "logged_in": logged_in,
        "user": user,
        "quota": quota,
        "llm": {
            "enabled": bool(llm.get("enabled")),
            "key_set": bool(llm.get("api_key")),
            "model": llm.get("model", ""),
            "base_url": llm.get("base_url", ""),
            "proxy": mask_proxy(str(llm.get("proxy") or "")),
        },
        "bank": stats,
        "job": job,
        "captcha": cap,
        "learn": bool((cfg.get("safety") or {}).get("auto_learn_right_answer", False)),
        "skip_unanswered": bool((cfg.get("safety") or {}).get("skip_unanswered", False)),
        "practice": practice_payload(cfg),
    }


def do_login(code: str, pwd: str) -> tuple[bool, str]:
    api = make_api()
    try:
        user = api.login(code, pwd)
    except LoginError as e:
        return False, str(e)
    except Exception as e:  # noqa: BLE001
        # 网络抖动/服务端异常时别把 500 抛给前端，返回可读错误
        log(f"登录异常：{type(e).__name__}: {e}", "!")
        return False, f"{type(e).__name__}: {e}"
    # 把账号写回 config.json：否则会话过期后 ensure_login 会拿旧账号自动登录，切号等于没切
    try:
        cfg = cfg_mod.load()
        acc = cfg.setdefault("account", {})
        if code:
            acc["code"] = code
        if pwd:
            acc["pwd"] = pwd
        cfg_mod.save(cfg)
    except Exception as e:  # noqa: BLE001
        log(f"账号已登录，但写回 config.json 失败：{e}", "!")
    try:
        quota = api.quota()
    except Exception:
        quota = {}
    log(f"控制台登录成功：{user.get('name')}（{user.get('code')}）", "✓")
    return True, json.dumps({"user": user, "quota": quota}, ensure_ascii=False)


def pending_payload() -> dict:
    cfg = cfg_mod.load()
    api = make_api()
    ensure_live_session(api, cfg)
    store = bank_mod.Bank(cfg_mod.DB_FILE)
    sids = store.pending_papers(10)
    store.close()
    items = []
    for sid in sids:
        info: dict = {"sid": sid, "questions": [], "ok": False}
        try:
            rec = api.record(sid)
            try:
                time_left = int(rec.get("timeLeft", 1))
            except (TypeError, ValueError):
                time_left = 1
            if time_left <= 0:
                continue  # 服务端已超时作废，不再当成未完成卷展示
            if int(rec.get("state", 0)) != 0:
                continue
            qs = parse_questions(rec)
            for q in qs[:4]:
                info["questions"].append(
                    {
                        "globalId": q["globalId"],
                        "type_name": TYPE_NAME.get(q.get("qtype"), "?"),
                        "preview": (q["content"] or "")[:120].replace("\n", " "),
                    }
                )
            info["ok"] = True
        except Exception as e:
            info["error"] = f"{type(e).__name__}: {e}"
        items.append(info)
    return {"pending": items}


def do_seed(sid: str, code: str, qid: str | None) -> tuple[bool, str]:
    api = make_api()
    store = bank_mod.Bank(cfg_mod.DB_FILE)
    try:
        rec = api.record(sid)
        questions = parse_questions(rec)
        if not questions:
            return False, "卷子里没有题目"
        if qid:
            q = next((x for x in questions if x["globalId"] == qid), None)
        elif len(questions) == 1:
            q = questions[0]
        else:
            return False, "多题卷请指定 globalId"
        if q is None:
            return False, "没找到对应题目"
        slots = _slot_map(rec)
        hit = slots.get(str(q["globalId"]))
        bucket = hit[0] if hit else 4
        h = bank_mod.qhash(bucket, q["content"], q["options"])
        store.put_answer(h, bucket, q["content"], code, "manual")
        log(f"已把手动答案存入题库（q{bucket} 桶 / {q['globalId'][:8]}…）", "✓")
        return True, "答案已入题库，现在可以点「续做」交卷"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    finally:
        store.close()


def tags_payload(qtype: int, level: int) -> dict:
    """按题型/难度拉标签，供窗口下拉选择（-1 表示不限）。"""
    api = make_api()
    try:
        tags = api.tags(int(qtype), int(level))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "msg": f"{type(e).__name__}: {e}", "tags": []}
    items = []
    for t in tags or []:
        if not isinstance(t, dict):
            continue
        items.append(
            {
                "id": t.get("id"),
                "content": t.get("content") or f"标签 {t.get('id')}",
                "available": bool(t.get("available", True)),
            }
        )
    return {"ok": True, "tags": items}


def skip_cost() -> dict:
    """查「跳过本题」额度：numOfResetWeek / resetCost / resetLeft。"""
    api = make_api()
    try:
        r = api.get_reset()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}
    return {"ok": True, "reset": r}


def skip_paper(sid: str) -> tuple[bool, str]:
    """把一张未完成卷挂成「稍后再做」(state=4)，释放 pending 名额；会消耗跳过额度。"""
    api = make_api()
    store = bank_mod.Bank(cfg_mod.DB_FILE)
    try:
        try:
            ret = api.do_it_later(sid)
        except Exception as e:  # noqa: BLE001
            return False, f"{type(e).__name__}: {e}"
        if ret == 1:
            store.finish_paper(sid, state=4, note="do-it-later（耗跳过额度）")
            log(f"已把 {sid[:8]} 挂为稍后再做，释放 pending 名额", "✓")
            return True, "已释放 pending 名额"
        return False, f"平台拒绝（返回 {ret}）：0=金币/次数不足，其它=条件不满足（可能需开卷满 60 秒）"
    finally:
        store.close()


def save_llm(payload: dict) -> tuple[bool, str]:
    cfg = cfg_mod.load()
    llm = cfg.setdefault("llm", {})
    if "enabled" in payload:
        llm["enabled"] = bool(payload["enabled"])
    for k in ("base_url", "api_key", "model", "proxy"):
        if k in payload and payload[k] is not None:
            v = str(payload[k]).strip()
            # 密码框默认不回填，空串表示「不改已有的 key」
            if k == "api_key" and not v:
                continue
            llm[k] = v
    cfg_mod.save(cfg)
    log("LLM 配置已更新（config.json）", "✓")
    return True, "已保存"


def llm_presets() -> dict:
    """LLM 厂商预设。注意本文件另有同名 PRESETS（刷题节奏预设），别混用。"""
    return {k: {"base_url": v[0], "model": v[1]} for k, v in LLM_PRESETS.items()}


def mask_proxy(p: str) -> str:
    """代理地址可能带 user:pass@，界面上不回显凭据。"""
    if not p:
        return ""
    return re.sub(r"(//)[^/@]+@", r"\1***@", p)


def test_llm(payload: dict) -> tuple[bool, str]:
    """用界面当前填写的值（未保存也能测）自检模型通路。"""
    cfg = cfg_mod.load()
    llm = dict(cfg.get("llm") or {})
    for k in ("base_url", "api_key", "model", "proxy"):
        v = payload.get(k)
        if v is None:
            continue
        v = str(v).strip()
        if k == "api_key" and not v:
            continue  # 密码框空 = 沿用已保存的 key
        llm[k] = v
    ok, msg = LLMClient(llm).test_connection()
    log(f"LLM 连通性自检：{'成功' if ok else '失败'} · {msg}", "✓" if ok else "!")
    return ok, msg


def do_export(sid: str) -> tuple[bool, str]:
    """把某份卷子的完整题干导出成 Markdown，方便交给人/外部模型解答。"""
    # qType 是 11 种细分题型，和答题纸桶（6 个）不是一回事
    QTYPE_NAME = {
        0: "单选", 1: "多选", 2: "判断", 3: "填空", 4: "程序填空", 5: "编程",
        6: "实验", 7: "复杂", 8: "改错", 9: "附加改错", 10: "设计",
    }
    api = make_api()
    rec = api.record(sid)
    qs = parse_questions(rec)
    if not qs:
        return False, "这份卷子没有题目"
    lines = [
        f"# 卷子 {sid}",
        "",
        f"- 状态：{rec.get('state')}",
        f"- 开卷时间：{rec.get('startTime')}",
        "",
    ]
    for i, q in enumerate(qs, 1):
        lines += [
            f"## 第 {i} 题（qType={q.get('qtype')}，{QTYPE_NAME.get(q.get('qtype'), '?')}，"
            f"语言 {q.get('language')}，{q.get('point')} 分）",
            "",
            f"- globalId：`{q.get('globalId')}`",
            "",
            "```text",
            (q.get("content") or "").strip(),
            "```",
            "",
        ]
        if q.get("options"):
            lines.append("选项：")
            for o in q["options"]:
                lines.append(f"- `{o.get('globalId')}` {o.get('content')}")
            lines.append("")
    path = STATE_DIR / f"questions_{sid}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    log(f"题干已导出：{path}", "✓")
    return True, str(path)


def save_learn(enabled: bool) -> tuple[bool, str]:
    """保留路由兼容旧前端，但从 GUI 无法再开启。

    right-answer-paper 实测会让卷永久失去重做资格（0/11 那张废卷就是这么来的），
    所以这里硬拒；确实知情要用的人只能手动改 config.json。
    """
    if enabled:
        log("已拒绝开启「交卷后学习参考答案」：该接口会让卷永久无法重做", "!")
        return False, "已禁用：调用平台参考答案接口会让该卷永久无法重做；确需开启请手动改 config.json"
    cfg = cfg_mod.load()
    cfg.setdefault("safety", {})["auto_learn_right_answer"] = False
    cfg_mod.save(cfg)
    return True, "已保持关闭"


def save_skip(enabled: bool) -> tuple[bool, str]:
    cfg = cfg_mod.load()
    cfg.setdefault("safety", {})["skip_unanswered"] = bool(enabled)
    cfg_mod.save(cfg)
    log(f"无答案时跳过本题已{'开启（会消耗平台金币）' if enabled else '关闭'}（config.json）", "✓")
    return True, "已保存"


# ---------------- 刷题策略：控制「怎么刷」 ----------------
# 这些参数原先只能手改 config.json，现在做成窗口里随时可调、可一键套预设。
# 说明：任务在「开始时」读取配置，所以改完对下一次点「开始」生效；正在跑的当前任务不受影响。
PRACTICE_INT_FIELDS = {
    "min_dwell_seconds": ("practice", 0, 3600),
    "max_submit_retry": ("practice", 1, 50),
    "sleep_between_seconds": ("loop", 0, 3600),
    "jitter_seconds": ("loop", 0, 600),
    "max_papers_per_hour": ("safety", 0, 100),
    "auto_fix_attempts": ("practice", 0, 5),
    "verify_retries": ("practice", 1, 8),
}
PRACTICE_BOOL_FIELDS = {
    "stop_on_no_new": "loop",
    "captcha_ocr": "safety",
    "resume_pending": "practice",
    "auto_fix": "practice",
}

# 三档预设：动节奏与开关，不动题型与份数。套用后是一套完整状态。
_PRESET_BOOLS = {
    "stop_on_no_new": True, "captcha_ocr": True,
    "resume_pending": True, "auto_fix": True,
}
PRESETS = {
    "safe": {  # 稳妥：慢一点，风控最宽松
        "min_dwell_seconds": 180,
        "max_submit_retry": 15,
        "sleep_between_seconds": 90,
        "jitter_seconds": 30,
        "max_papers_per_hour": 5,
        **_PRESET_BOOLS,
    },
    "standard": {  # 标准：与工具默认值一致
        "min_dwell_seconds": 100,
        "max_submit_retry": 12,
        "sleep_between_seconds": 45,
        "jitter_seconds": 20,
        "max_papers_per_hour": 10,
        **_PRESET_BOOLS,
    },
    "fast": {  # 稍快：卷间更紧凑；作答时长低于平台要求时会被 429 拦下并自动重试
        "min_dwell_seconds": 90,
        "max_submit_retry": 12,
        "sleep_between_seconds": 20,
        "jitter_seconds": 10,
        "max_papers_per_hour": 15,
        **_PRESET_BOOLS,
    },
}
for _p in PRESETS.values():
    _p.setdefault("auto_fix_attempts", 2)
    _p.setdefault("verify_retries", 3)


def practice_payload(cfg: dict | None = None) -> dict:
    cfg = cfg if cfg is not None else cfg_mod.load()
    p = cfg.get("practice") or {}
    lp = cfg.get("loop") or {}
    sf = cfg.get("safety") or {}
    return {
        "min_dwell_seconds": int(p.get("min_dwell_seconds", 100) or 0),
        "max_submit_retry": int(p.get("max_submit_retry", 12) or 12),
        "sleep_between_seconds": int(lp.get("sleep_between_seconds", 45) or 0),
        "jitter_seconds": int(lp.get("jitter_seconds", 20) or 0),
        "max_papers_per_hour": int(sf.get("max_papers_per_hour", 10) or 0),
        "stop_on_no_new": bool(lp.get("stop_on_no_new", True)),
        "captcha_ocr": bool(sf.get("captcha_ocr", True)),
        "resume_pending": bool(p.get("resume_pending", True)),
        "auto_fix": bool(p.get("auto_fix", True)),
        "auto_fix_attempts": int(p.get("auto_fix_attempts", 2) or 0),
        "verify_retries": int(p.get("verify_retries", 3) or 1),
        # 上次的选择，窗口打开时回填
        "qbase_type": int(p.get("qbase_type", 4) if p.get("qbase_type") is not None else 4),
        "level": int(p.get("level", 0) if p.get("level") is not None else 0),
        "tag_id": int(p.get("tag_id", -1) if p.get("tag_id") is not None else -1),
        "papers": int(lp.get("papers", 3) if lp.get("papers") is not None else 3),
    }


def save_practice(payload: dict) -> tuple[bool, str]:
    cfg = cfg_mod.load()
    sections = {
        "practice": cfg.setdefault("practice", {}),
        "loop": cfg.setdefault("loop", {}),
        "safety": cfg.setdefault("safety", {}),
    }
    changed: list[str] = []
    for key, (sec, lo, hi) in PRACTICE_INT_FIELDS.items():
        if payload.get(key) is None:
            continue
        try:
            val = int(payload[key])
        except (TypeError, ValueError):
            return False, f"{key} 必须是整数"
        val = max(lo, min(hi, val))
        sections[sec][key] = val
        changed.append(f"{key}={val}")
    for key, sec in PRACTICE_BOOL_FIELDS.items():
        if payload.get(key) is None:
            continue
        val = bool(payload[key])
        sections[sec][key] = val
        changed.append(f"{key}={val}")
    if not changed:
        return False, "没有可保存的字段"
    cfg_mod.save(cfg)
    log("刷题策略已保存：" + "、".join(changed), "✓")
    return True, "已保存（连刷中从下一份起生效）"


def apply_preset(name: str) -> tuple[bool, str]:
    preset = PRESETS.get(name)
    if not preset:
        return False, f"未知预设：{name}"
    ok, msg = save_practice(preset)
    if ok:
        log(f"已套用「{ {'safe':'稳妥','standard':'标准','fast':'稍快'}.get(name, name) }」预设", "✓")
    return ok, msg


def remember_selection(body: dict) -> None:
    """记住本次的题型/难度/标签/份数，窗口重开或会话过期后仍延续上次选择。"""
    if body.get("mode") == "resume":
        return
    try:
        cfg = cfg_mod.load()
        p = cfg.setdefault("practice", {})
        lp = cfg.setdefault("loop", {})
        if body.get("type") is not None:
            p["qbase_type"] = int(body["type"])
        if body.get("level") is not None:
            p["level"] = int(body["level"])
        if body.get("tag") is not None:
            p["tag_id"] = int(body["tag"])
        if body.get("papers") is not None:
            lp["papers"] = max(1, int(body["papers"]))
        cfg_mod.save(cfg)
    except Exception:  # noqa: BLE001
        pass


def do_export_all() -> tuple[bool, str]:
    """把本地所有未完成卷的题干合并导出成一个 Markdown，批量解题用。"""
    store = bank_mod.Bank(cfg_mod.DB_FILE)
    try:
        sids = store.pending_papers(5)
    finally:
        store.close()
    if not sids:
        return False, "本地没有未完成卷子"
    chunks = []
    for sid in sids:
        ok, msg = do_export(sid)
        if ok:
            chunks.append(Path(msg).read_text(encoding="utf-8"))
    if not chunks:
        return False, "题干导出失败"
    path = STATE_DIR / "questions_batch.md"
    path.write_text("\n\n---\n\n".join(chunks), encoding="utf-8")
    log(f"已批量导出 {len(chunks)} 份题干：{path}", "✓")
    return True, str(path)


def do_diagnose(sid: str) -> dict:
    """拉判卷详情：每题得分 + 失败用例的真实输入/期望/实际输出，并落到 .state。"""
    api = make_api()
    data = api.score_result(sid)
    if not isinstance(data, list):
        data = [data]
    items: list[dict] = []
    for q in data:
        if not isinstance(q, dict):
            continue
        sr = q.get("scoreResult") or {}
        tc = sr.get("failedTestCase") or {}
        case = tc.get("testCase") or {}
        gid = str(q.get("globalId") or "")[:12]
        item = {
            "globalId": gid,
            "score": sr.get("score", q.get("score")),
            "state": sr.get("state"),
            "input": case.get("input") or "",
            "expected": case.get("output") or "",
            "actual": tc.get("output") or "",
            "stderr": tc.get("stderr"),
            "failed": bool(case),
        }
        if case:
            base = STATE_DIR / f"tc_{sid[:8]}_{gid}"
            base.with_suffix(".in").write_text(item["input"], encoding="utf-8")
            base.with_suffix(".exp").write_text(item["expected"], encoding="utf-8")
            base.with_suffix(".act").write_text(item["actual"], encoding="utf-8")
            item["files"] = f"{base.name}.in / .exp / .act"
            log(f"判卷用例已落盘：{base.name}.in/.exp/.act（{sid[:8]}）", "✓")
        items.append(item)
    return {"ok": True, "sid": sid, "items": items}


def do_report() -> tuple[bool, str]:
    cfg = cfg_mod.load()
    acc = (cfg.get("account") or {}).get("code", "")
    store = bank_mod.Bank(cfg_mod.DB_FILE)
    path = report_mod.build_html(store, ROOT / "report.html", acc)
    store.close()
    return True, str(path)


# ---------------- HTTP ----------------
PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>trainer-auto 控制台</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{
  --bg:#f3f5f8; --card:#ffffff; --line:#e3e8ef; --text:#1c2430; --muted:#6b7688;
  --blue:#2563eb; --blue-weak:#eff4ff; --green:#16a34a; --green-weak:#eaf7ef;
  --red:#dc2626; --red-weak:#fdeeee; --amber:#d97706; --amber-weak:#fdf5e6;
  --mono:Consolas,"Cascadia Mono",Menlo,monospace;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
  font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;padding:18px}
.wrap{max-width:1080px;margin:0 auto}
h1{font-size:18px;margin:0 0 4px}
.sub{color:var(--muted);font-size:12px;margin-bottom:14px}
.grid{display:grid;grid-template-columns:340px 1fr;gap:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
  padding:14px 16px;margin-bottom:14px;box-shadow:0 1px 2px rgba(16,24,40,.04)}
.card h2{font-size:14px;margin:0 0 10px;color:var(--text)}
.row{display:flex;align-items:center;gap:8px;margin-bottom:10px;flex-wrap:wrap}
label{font-size:13px;color:var(--muted)}
select,input[type=number],input[type=text],input[type=password]{
  border:1px solid var(--line);border-radius:6px;padding:6px 8px;font-size:13px;
  background:#fff;color:var(--text)}
input[type=number]{width:70px}
.full{width:100%}
button{border:none;border-radius:7px;padding:8px 16px;font-size:14px;cursor:pointer;
  color:#fff;background:var(--blue);font-weight:600}
button:hover{filter:brightness(1.08)}
button:disabled{background:#b9c2d0;cursor:not-allowed}
button.stop{background:var(--red)}
button.ghost{background:#fff;color:var(--text);border:1px solid var(--line);font-weight:500}
button.small{padding:4px 10px;font-size:12px}
.chips{display:flex;gap:8px;flex-wrap:wrap}
.chip{font-size:12px;padding:3px 10px;border-radius:20px;background:var(--blue-weak);
  color:var(--blue);font-weight:600}
.chip.ok{background:var(--green-weak);color:var(--green)}
.chip.warn{background:var(--amber-weak);color:var(--amber)}
.chip.bad{background:var(--red-weak);color:var(--red)}
.stats{display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:10px}
.stat{background:#f8fafc;border:1px solid var(--line);border-radius:8px;padding:10px}
.stat .k{font-size:12px;color:var(--muted)}
.stat .v{font-size:20px;font-weight:700;margin-top:2px}
.log{background:#f8fafc;border:1px solid var(--line);border-radius:8px;padding:10px;
  font:12px/1.5 var(--mono);height:300px;overflow-y:auto;white-space:pre-wrap;
  word-break:break-all;color:#334}
.log .lv{color:var(--muted)}
.log .ok{color:var(--green)} .log .err{color:var(--red)} .log .info{color:var(--blue)}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line)}
th{color:var(--muted);font-weight:600;font-size:12px}
.score-ok{color:var(--green);font-weight:700}
.score-bad{color:var(--red);font-weight:700}
.pending{border:1px solid var(--line);border-radius:8px;padding:10px;margin-bottom:10px}
.pending .sid{font:12px var(--mono);color:var(--muted);word-break:break-all}
textarea{width:100%;min-height:120px;border:1px solid var(--line);border-radius:6px;
  padding:8px;font:12px var(--mono);background:#fff;color:var(--text)}
pre.tc{margin:4px 0 10px;padding:8px;background:#fff;border:1px solid var(--line);
  border-radius:6px;font:12px/1.5 var(--mono);white-space:pre-wrap;word-break:break-all;
  max-height:220px;overflow:auto;color:var(--text)}
.jobbar{display:flex;align-items:center;gap:12px;background:var(--blue-weak);
  border-radius:8px;padding:8px 12px;font-size:13px;color:var(--blue);font-weight:600}
.jobbar.idle{background:#f1f4f9;color:var(--muted)}
.jobbar.error{background:var(--red-weak);color:var(--red)}
.dot{width:9px;height:9px;border-radius:50%;background:currentColor;flex:none}
.dot.live{animation:pulse 1.2s infinite}
@keyframes pulse{50%{opacity:.25}}
a{color:var(--blue)}
.muted{color:var(--muted);font-size:12px}
details summary{cursor:pointer;font-weight:600;font-size:13px}
details{margin-top:6px}
label.mode{display:flex;gap:6px;align-items:center;background:#f8fafc;border:1px solid var(--line);
  border-radius:7px;padding:7px 10px;cursor:pointer;font-size:13px;color:var(--text);flex:1}
label.mode input{accent-color:var(--blue)}
label.mode.on{border-color:var(--blue);background:var(--blue-weak)}
</style>
</head>
<body>
<div class="wrap">
  <h1>🧰 trainer-auto 控制台</h1>
  <div class="sub">HIT 程序设计能力训练平台 · 自动刷题 · <span id="endpoint"></span></div>

  <div class="grid">
    <div>
      <div class="card">
        <h2>刷题控制</h2>
        <div class="row" id="modes">
          <label class="mode"><input type="radio" name="mode" value="loop" checked>连刷多份</label>
          <label class="mode"><input type="radio" name="mode" value="once">单份</label>
        </div>
        <div class="row">
          <label class="mode"><input type="radio" name="mode" value="resume">续做未完成</label>
        </div>
        <div class="row" id="llm-warn" style="display:none;margin-top:4px">
          <span class="banner warn">⛔ 未启用 LLM：点「开始」开的卷若题库没有答案，会留成「未完成」。要全自动请先去下方「LLM 设置」填 key 启用；要手动解请开卷→粘代码→入题库→点「续做」。</span>
        </div>
        <div class="row">
          <label>题型</label>
          <select id="type">
            <option value="4" selected>编程题</option>
            <option value="0">单选题</option>
            <option value="1">多选题</option>
            <option value="2">判断题</option>
            <option value="3">填空题</option>
          </select>
          <label>难度</label>
          <select id="level">
            <option value="0" selected>随机</option>
            <option value="1">非常简单</option><option value="2">简单</option>
            <option value="3">普通</option><option value="4">困难</option>
            <option value="5">非常困难</option>
          </select>
        </div>
        <div class="row">
          <label>份数</label><input type="number" id="papers" value="3" min="1" max="100">
          <label title="只答题不交卷，验证流程"><input type="checkbox" id="dry"> 试跑</label>
        </div>
        <div class="row">
          <label>标签</label>
          <select id="tag" title="-1 表示不限；选具体标签就只刷这一类" style="flex:1;min-width:0">
            <option value="-1">不限（全部标签）</option>
          </select>
          <button class="ghost small" id="btn-tags" title="按当前题型/难度拉取标签">刷新</button>
        </div>
        <div class="row">
          <label title="无可用答案时，花一次跳过额度把卷挂成「已跳过」：不产生 0 分记录，也释放 pending 名额">
            <input type="checkbox" id="skip"> 无答案时跳过本题</label>
          <span class="muted">会消耗平台金币，默认关</span>
        </div>
        <div class="row">
          <span class="muted">⛔「交卷后学习参考答案」已禁用：调用该接口会让卷永久无法重做，那张 0/11 废卷就是这么来的</span>
        </div>
        <div class="row" style="margin-top:14px">
          <button id="btn-start">▶ 开始</button>
          <button id="btn-stop" class="stop" disabled>■ 停止</button>
          <button id="btn-report" class="ghost small">生成报告</button>
        </div>
        <div class="jobbar idle" id="jobbar"><span class="dot"></span><span id="jobtext">空闲</span></div>
        <div class="muted" style="margin-top:8px">停止会在当前卷答完、交完卷后生效；卷间隙与频控等待会立即中断。停止不会半途弃卷（避免占用未作答名额）。</div>
      </div>

      <div class="card">
        <h2>刷题策略 · 如何刷</h2>
        <div class="row">
          <label>一键预设</label>
          <button class="ghost small" data-preset="safe" title="慢一点、最保守，适合刚被限流或担心风控时">稳妥</button>
          <button class="ghost small" data-preset="standard" title="与工具默认值一致">标准</button>
          <button class="ghost small" data-preset="fast" title="卷间更紧凑；作答太快会被平台 429 拦下并自动重试">稍快</button>
        </div>
        <div class="row">
          <label>最短作答(秒)</label>
          <input type="number" id="p-dwell" min="0" max="3600" title="每份卷至少停留这么久才交，太短会被平台判定操作过快">
          <label>交卷重试</label>
          <input type="number" id="p-retry" min="1" max="50" title="被 429 / 验证码拦下时的最大重试次数">
        </div>
        <div class="row">
          <label>卷间休息(秒)</label>
          <input type="number" id="p-gap" min="0" max="3600">
          <label>随机抖动</label>
          <input type="number" id="p-jitter" min="0" max="600" title="在休息时长上再加 0~N 秒随机，避免机械节奏">
        </div>
        <div class="row">
          <label>每小时上限</label>
          <input type="number" id="p-perhour" min="0" max="100" title="0 表示不限">
          <span class="muted">0 = 不限</span>
        </div>
        <div class="row" style="gap:16px">
          <label title="遇到「没有新题/额度用完」就停，而不是继续空转"><input type="checkbox" id="p-stopnew"> 无新题即停</label>
          <label title="交卷遇到数字验证码时自动识别，识别失败会弹窗让你手输"><input type="checkbox" id="p-ocr"> 验证码自动识别</label>
          <label title="低分自动拿判卷真实用例改写、本地 gcc 验证后重做；这是压低 0 分的核心开关"><input type="checkbox" id="p-autofix"> 低分自动修复</label>
        </div>
        <div class="row">
          <label title="低分卷最多自动重做几次">重做次数</label>
          <input type="number" id="p-fixattempts" min="0" max="5" title="0 表示不自动重做">
          <label title="每次修复在本地按判卷用例跑通的最大次数">本地验证次数</label>
          <input type="number" id="p-verifyretries" min="1" max="8">
        </div>
        <div class="row" style="margin-top:6px">
          <button class="ghost small" id="btn-save-practice">保存策略</button>
          <span class="muted" id="p-msg">改完点保存；连刷中从下一份起生效</span>
        </div>
        <div class="muted" id="eta" style="margin-top:4px"></div>
      </div>

      <div class="card">
        <h2>账号与额度</h2>
        <div class="chips" id="acc-chips"><span class="chip warn">加载中…</span></div>
        <div class="stats" style="margin-top:10px">
          <div class="stat"><div class="k">本周剩余</div><div class="v" id="q-week">–</div></div>
          <div class="stat"><div class="k">今日剩余</div><div class="v" id="q-day">–</div></div>
          <div class="stat"><div class="k">未作答卷</div><div class="v" id="q-pend">–</div></div>
          <div class="stat"><div class="k">金币</div><div class="v" id="q-gold">–</div></div>
        </div>
        <details>
          <summary>LLM 设置（真·全自动的最后一步）</summary>
          <div class="row" style="margin-top:8px">
            <label><input type="checkbox" id="llm-on"> 启用</label>
            <select id="llm-preset" title="选服务商，自动填地址与模型">
              <option value="">快捷预设…</option>
              <option value="deepseek">DeepSeek</option>
              <option value="qwen">通义千问</option>
              <option value="zhipu">智谱 GLM</option>
              <option value="kimi">Kimi</option>
              <option value="siliconflow">硅基流动</option>
              <option value="ollama">Ollama 本地</option>
            </select>
          </div>
          <div class="row"><input type="text" id="llm-base" class="full" placeholder="https://open.bigmodel.cn/api/paas/v4"></div>
          <div class="row"><input type="text" id="llm-model" class="full" placeholder="glm-4-flash"></div>
          <div class="row"><input type="password" id="llm-key" class="full" placeholder="api_key（本地模型可留空）"></div>
          <div class="row"><input type="text" id="llm-proxy" class="full"
            placeholder="代理 http://127.0.0.1:7890（可选，须 http://，不支持 socks5）"></div>
          <div class="row">
            <button class="ghost small" id="btn-llm">保存</button>
            <button class="ghost small" id="btn-llm-test">测试连接</button>
          </div>
          <div id="llm-test-msg" class="muted" style="margin-top:6px">填好 key 先点「测试连接」，通过后再启用。</div>
        </details>
        <details>
          <summary>重新登录</summary>
          <div class="row" style="margin-top:8px">
            <input type="text" id="acc-code" placeholder="学号" style="flex:1">
            <input type="password" id="acc-pwd" placeholder="密码" style="flex:1">
            <button class="ghost small" id="btn-login">登录</button>
          </div>
        </details>
      </div>

      <div class="card">
        <h2>本地题库</h2>
        <div class="stats">
          <div class="stat"><div class="k">总卷数</div><div class="v" id="b-papers">–</div></div>
          <div class="stat"><div class="k">已完成</div><div class="v" id="b-done">–</div></div>
          <div class="stat"><div class="k">平均分</div><div class="v" id="b-avg">–</div></div>
          <div class="stat"><div class="k">缓存答案</div><div class="v" id="b-cache">–</div></div>
        </div>
      </div>
    </div>

    <div>
      <div class="card">
        <h2>运行日志</h2>
        <div class="log" id="log"></div>
      </div>

      <div class="card">
        <h2>未完成卷子 · 手动喂答案</h2>
        <div id="pending-warn" class="muted" style="color:var(--amber);margin-bottom:6px"></div>
        <div class="row" style="margin-bottom:8px">
          <button class="ghost small" id="btn-export-all">📚 批量导出全部题干</button>
          <span class="muted">一次拿到所有题，解完逐个入题库再「续做」</span>
        </div>
        <div id="pending" class="muted">无未完成卷子（或未登录）</div>
        <div class="muted" style="margin-top:6px">
          没配 LLM key 时的手动流程：开卷 → 在下方粘贴答案代码 → 入题库 → 点「续做」交卷。
        </div>
      </div>

      <div class="card">
        <h2>最近成绩</h2>
        <table id="tbl">
          <thead><tr><th>开卷时间</th><th>题型</th><th>状态</th><th>得分</th><th>备注</th><th>操作</th></tr></thead>
          <tbody id="tbl-body"></tbody>
        </table>
        <div class="muted" style="margin-top:6px">
          低分卷点「诊断」看判卷真实用例；改好答案入题库后点「重做」，会重开原卷再交一次。
        </div>
        <div id="diag" style="display:none;margin-top:12px;border:1px solid var(--line);
          border-radius:8px;padding:10px;background:#f8fafc">
          <div class="row" style="justify-content:space-between">
            <strong id="diag-title">判卷详情</strong>
            <button class="ghost small" id="btn-diag-close">关闭</button>
          </div>
          <div id="diag-body"></div>
        </div>
      </div>
    </div>
  </div>
</div>

<div id="captcha-bar" style="display:none;position:fixed;right:20px;bottom:20px;width:320px;
  background:#fff;border:1px solid #d97706;border-radius:10px;padding:12px;
  box-shadow:0 8px 28px rgba(16,24,40,.18);z-index:50">
  <div style="font-weight:700;color:#d97706;margin-bottom:8px">需要人机验证（交卷被拦）</div>
  <img id="captcha-img" alt="验证码" style="width:100%;border:1px solid #e3e8ef;border-radius:6px;background:#fff">
  <div class="row" style="margin-top:8px">
    <input type="text" id="captcha-input" placeholder="图中数字" style="flex:1">
    <button class="small" id="btn-captcha-ok">提交</button>
    <button class="ghost small" id="btn-captcha-skip">放弃</button>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);
let logCursor = 0;
let lastCaptcha = '';
let llmPresets = {};
let practiceLoaded = false;

document.querySelectorAll('label.mode').forEach(l => {
  l.querySelector('input').addEventListener('change', () => {
    document.querySelectorAll('label.mode').forEach(x => x.classList.toggle('on', x.querySelector('input').checked));
    const mode = document.querySelector('input[name=mode]:checked').value;
    $('papers').disabled = (mode !== 'loop');
    $('type').disabled = (mode === 'resume');
    $('level').disabled = (mode === 'resume');
    $('tag').disabled = (mode === 'resume');
  });
});
document.querySelector('label.mode').classList.add('on');
$('papers').disabled = false;

async function api(path, body) {
  const opt = body ? {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)} : {};
  const r = await fetch(path, opt);
  return r.json();
}

async function pollStatus() {
  try {
    const s = await api('/api/status');
    const chips = [];
    chips.push(s.logged_in
      ? `<span class="chip ok">已登录 ${s.user.name || ''} (${s.user.code || ''})</span>`
      : `<span class="chip bad">未登录</span>`);
    const L = s.llm;
    chips.push(L.enabled && L.key_set
      ? `<span class="chip ok">LLM: ${L.model}</span>`
      : `<span class="chip warn">LLM 未启用（仅题库命中）</span>`);
    $('skip').checked = s.skip_unanswered === true;
    $('llm-warn').style.display = (L.enabled && L.key_set) ? 'none' : 'flex';
    if (!practiceLoaded && s.practice) { fillPractice(s.practice); practiceLoaded = true; loadTags(); }
    $('acc-chips').innerHTML = chips.join(' ');
    $('q-week').textContent = s.quota.weekLimit ?? '–';
    $('q-day').textContent = (s.quota.dayLimit === 0 && !s.quota.dayExhausted) ? '不限' : (s.quota.dayLimit ?? '–');
    $('q-pend').textContent = s.quota.pendingCount ?? '–';
    $('q-gold').textContent = s.user.gold ?? '–';
    const pc = s.quota.pendingCount, pl = s.quota.pendingLimit;
    $('pending-warn').textContent = (pc != null && pl && pc >= pl - 1)
      ? `⚠ 未完成卷 ${pc}/${pl}，到 ${pl} 就无法再开新卷，先用「跳过」腾出名额` : '';
    $('b-papers').textContent = s.bank.papers ?? 0;
    $('b-done').textContent = s.bank.done ?? 0;
    $('b-avg').textContent = s.bank.avg_score ?? '–';
    $('b-cache').textContent = s.bank.cached_answers ?? 0;
    if (!document.activeElement || document.activeElement.id !== 'llm-key') {
      $('llm-on').checked = L.enabled;
      if (!$('llm-base').value) $('llm-base').value = L.base_url;
      if (document.activeElement && document.activeElement.id === 'llm-base') {} else if (!$('llm-model').value) $('llm-model').value = L.model;
      if (!$('llm-proxy').value) $('llm-proxy').value = L.proxy || '';
    }
    const j = s.job;
    const bar = $('jobbar'), txt = $('jobtext');
    $('btn-start').disabled = j.running;
    $('btn-stop').disabled = !j.running;
    if (j.running) {
      bar.className = 'jobbar';
      const kind = {loop:'连刷', once:'单份', resume:'续做', redo:'重做'}[j.kind] || j.kind;
      const prog = j.total ? ` ${j.done}/${j.total}` : '';
      txt.textContent = `运行中 · ${kind}${j.dry_run ? '（试跑）' : ''}${prog}${j.stop_requested ? ' · 停止请求已发出' : ''}`;
      bar.querySelector('.dot').classList.add('live');
    } else if (j.error) {
      bar.className = 'jobbar error'; txt.textContent = '出错：' + j.error;
    } else if (j.finished_at) {
      bar.className = 'jobbar idle'; txt.textContent = '空闲（上次任务已结束）';
    } else {
      bar.className = 'jobbar idle'; txt.textContent = '空闲';
    }
    if (j.running || j.finished_at) loadPapers();
    if (s.captcha && s.captcha.pending) {
      if (s.captcha.image && s.captcha.image !== lastCaptcha) {
        lastCaptcha = s.captcha.image;
        $('captcha-img').src = 'data:image/png;base64,' + s.captcha.image;
        $('captcha-input').value = '';
        $('captcha-bar').style.display = 'block';
        $('captcha-input').focus();
      }
    }
  } catch (e) { /* 服务器未启动 */ }
}

async function pollLog() {
  try {
    const d = await api('/api/log?since=' + logCursor);
    if (d.lines.length) {
      const el = $('log');
      const auto = el.scrollTop + el.clientHeight >= el.scrollHeight - 30;
      for (const [i, line] of d.lines) {
        const div = document.createElement('div');
        let cls = '';
        if (/✗|■/.test(line)) cls = 'err';
        else if (/✓|★/.test(line)) cls = 'ok';
        else if (/▶|!/.test(line)) cls = 'info';
        else cls = 'lv';
        div.className = cls;
        div.textContent = line;
        el.appendChild(div);
      }
      logCursor = d.next;
      while (el.childNodes.length > 600) el.removeChild(el.firstChild);
      if (auto) el.scrollTop = el.scrollHeight;
    }
  } catch (e) {}
}

async function loadPapers() {
  try {
    const d = await api('/api/papers');
    const TYPE = {0:'单选',1:'多选',2:'判断',3:'填空',4:'编程'};
    $('tbl-body').innerHTML = (d.papers || []).map(p => {
      let st, sc;
      if (p.state === 1) { st = '<span class="score-ok">已交卷</span>'; }
      else if (p.state === 0) { st = '<span style="color:var(--amber)">未完成</span>'; }
      else { st = p.state; }
      if (p.score == null) sc = '–';
      else if (p.total && p.score >= p.total) sc = `<span class="score-ok">${p.score}/${p.total}</span>`;
      else if (p.total) sc = `<span class="score-bad">${p.score}/${p.total}</span>`;
      else sc = p.score;
      const note = (p.note || '').replace(/</g, '&lt;');
      const sid = p.session_id || '';
      const ops = (p.state === 1 && sid)
        ? `<button class="ghost small" data-act="diag" data-sid="${sid}">🔍 诊断</button>
           <button class="ghost small" data-act="redo" data-sid="${sid}">🔄 重做</button>`
        : '<span class="muted">–</span>';
      return `<tr><td>${(p.created_at || '').slice(5, 16)}</td><td>${TYPE[p.qbase_type] || p.qbase_type}</td><td>${st}</td><td>${sc}</td><td class="muted">${note}</td><td>${ops}</td></tr>`;
    }).join('') || '<tr><td colspan="6" class="muted">暂无记录</td></tr>';
    $('tbl-body').querySelectorAll('button[data-act=diag]').forEach(btn => {
      btn.onclick = async () => {
        btn.disabled = true;
        const r = await api('/api/diagnose?sid=' + encodeURIComponent(btn.dataset.sid));
        btn.disabled = false;
        showDiag(r);
      };
    });
    $('tbl-body').querySelectorAll('button[data-act=redo]').forEach(btn => {
      btn.onclick = async () => {
        if (!confirm('用题库当前答案重开这份卷并重做交卷？\n（确认前请先改好答案并入题库）')) return;
        const r = await api('/api/redo', {sid: btn.dataset.sid});
        if (!r.ok) alert('失败：' + r.msg);
        pollStatus();
      };
    });
  } catch (e) {}
}

const esc = s => (s == null ? '' : String(s)).replace(/</g, '&lt;');
function showDiag(d) {
  const box = $('diag'), body = $('diag-body');
  if (!d.ok) {
    $('diag-title').textContent = '诊断失败：' + (d.msg || '');
    body.innerHTML = '';
  } else {
    $('diag-title').textContent = '判卷详情 · ' + (d.sid || '').slice(0, 12) + '…';
    body.innerHTML = (d.items || []).map(it => {
      if (!it.failed) return `<div class="muted">✔ ${it.globalId} 得分 ${it.score}，无失败用例</div>`;
      return `<div style="font-weight:600;margin-top:6px">✘ ${it.globalId} 得分 ${it.score}　用例已存 ${esc(it.files)}</div>
        <div class="muted">判卷输入</div><pre class="tc">${esc(it.input)}</pre>
        <div class="muted">期望输出</div><pre class="tc">${esc(it.expected)}</pre>
        <div class="muted">实际输出</div><pre class="tc">${esc(it.actual)}</pre>`;
    }).join('') || '<div class="muted">无详情</div>';
  }
  box.style.display = 'block';
  box.scrollIntoView({behavior:'smooth', block:'nearest'});
}

async function loadPending() {
  try {
    const d = await api('/api/pending');
    const host = $('pending');
    if (!d.pending || !d.pending.length) { host.innerHTML = '<span class="muted">无未完成卷子</span>'; return; }
    host.innerHTML = '';
    for (const p of d.pending) {
      if (!p.ok) continue;
      const div = document.createElement('div');
      div.className = 'pending';
      const qs = (p.questions || []).map((q, i) =>
        `<div class="muted">#${i + 1} ${q.type_name} ${q.globalId.slice(0, 8)}…</div>
         <div style="font-size:13px;margin:2px 0 6px">${q.preview.replace(/</g, '&lt;')}</div>`).join('');
      div.innerHTML = `<div class="sid">sessionId: ${p.sid}</div>${qs}
        <textarea placeholder="把该题答案代码粘贴到这里（单题卷直接贴，多题卷在最前面加一行 //qid:globalId）"></textarea>
        <div class="row" style="margin-top:8px">
          <button class="ghost small" data-act="seed" data-sid="${p.sid}">📥 入题库</button>
          <button class="ghost small" data-act="export" data-sid="${p.sid}">📄 导出题干</button>
          <button class="ghost small" data-act="skip" data-sid="${p.sid}">⏭ 跳过（耗金币）</button>
          <span class="muted">入题库后点左侧「续做」即可交卷</span>
        </div>`;
      host.appendChild(div);
    }
    host.querySelectorAll('button[data-act=seed]').forEach(btn => {
      btn.onclick = async () => {
        const box = btn.closest('.pending').querySelector('textarea');
        let code = box.value, qid = null;
        const m = code.match(/^\/\/\s*qid:\s*([0-9a-f]+)/i);
        if (m) { qid = m[1]; code = code.slice(m[0].length).trim(); }
        if (!code.trim()) { alert('先粘贴答案'); return; }
        btn.disabled = true; btn.textContent = '保存中…';
        const r = await api('/api/seed', {sid: btn.dataset.sid, code, qid});
        btn.disabled = false; btn.textContent = '📥 入题库';
        alert(r.ok ? r.msg : '失败：' + r.msg);
      };
    });
    host.querySelectorAll('button[data-act=export]').forEach(btn => {
      btn.onclick = async () => {
        const r = await api('/api/export', {sid: btn.dataset.sid});
        alert(r.ok ? '题干已导出到：\n' + r.msg : '失败：' + r.msg);
      };
    });
    host.querySelectorAll('button[data-act=skip]').forEach(btn => {
      btn.onclick = async () => {
        const c = await api('/api/skip-cost');
        const q = (c && c.reset) || {};
        const cost = q.resetCost != null ? q.resetCost : '?';
        const left = q.resetLeft != null ? q.resetLeft : (q.numOfResetWeek != null ? q.numOfResetWeek : '?');
        if (!confirm(`把这卷挂成「稍后再做」会释放 pending 名额，但消耗跳过额度：\n\n本次约 ${cost} 金币，本周剩余 ${left} 次。\n\n确定执行？`)) return;
        btn.disabled = true;
        const r = await api('/api/skip-paper', {sid: btn.dataset.sid});
        btn.disabled = false;
        alert(r.ok ? '已释放 pending 名额' : '失败：' + r.msg);
        loadPending(); pollStatus();
      };
    });
  } catch (e) {}
}

$('btn-start').onclick = async () => {
  const mode = document.querySelector('input[name=mode]:checked').value;
  const L = await api('/api/status');
  if ((mode === 'loop' || mode === 'once') && !(L.llm && L.llm.enabled && L.llm.key_set)) {
    if (!confirm('未启用 LLM、且题库可能没这道题的答案：\n点「开始」开的卷会变成「未完成」并占住未作答名额。\n\n确定继续？要全自动请先去「LLM 设置」填 key 启用；要手动解请开卷→粘代码→入题库→续做。')) {
      return;
    }
  }
  const body = {
    mode,
    type: parseInt($('type').value),
    level: parseInt($('level').value),
    tag: parseInt($('tag').value),
    papers: parseInt($('papers').value) || 1,
    dry: $('dry').checked,
  };
  const r = await api('/api/start', body);
  if (!r.ok) alert(r.msg);
};
$('btn-stop').onclick = async () => {
  if (!confirm('确认停止？当前卷会答完并交卷后才真正停止。')) return;
  await api('/api/stop', {});
  pollStatus();
};
    $('btn-report').onclick = async () => {
  const r = await api('/api/report', {});
  if (r.ok) window.open('/report', '_blank'); else alert('失败：' + r.msg);
};
$('btn-export-all').onclick = async () => {
  const r = await api('/api/export-all', {});
  alert(r.ok ? '题干已合并导出到：\n' + r.msg : '失败：' + r.msg);
};
$('btn-diag-close').onclick = () => { $('diag').style.display = 'none'; };
$('btn-captcha-ok').onclick = async () => {
  const code = $('captcha-input').value.trim();
  if (!code) { alert('先填验证码'); return; }
  await api('/api/captcha', {code});
  $('captcha-bar').style.display = 'none';
};
$('btn-captcha-skip').onclick = async () => {
  await api('/api/captcha', {code: ''});
  $('captcha-bar').style.display = 'none';
};
$('skip').onchange = async () => {
  await api('/api/skip', {enabled: $('skip').checked});
  pollStatus();
};
function llmForm() {
  return {
    enabled: $('llm-on').checked,
    base_url: $('llm-base').value.trim(),
    model: $('llm-model').value.trim(),
    api_key: $('llm-key').value.trim() || undefined,
    proxy: $('llm-proxy').value.trim(),
  };
}
$('llm-preset').onchange = async () => {
  const k = $('llm-preset').value;
  if (!k) return;
  if (!Object.keys(llmPresets).length) {
    const d = await api('/api/llm-presets');
    llmPresets = d.presets || {};
  }
  const p = llmPresets[k];
  if (!p) return;
  $('llm-base').value = p.base_url;
  $('llm-model').value = p.model;
  if (k === 'ollama') { $('llm-proxy').value = ''; $('llm-key').value = ''; }
  $('llm-test-msg').textContent = '已填入预设，填好 key 后点「测试连接」。';
};
$('btn-llm').onclick = async () => {
  const r = await api('/api/llm', llmForm());
  alert(r.ok ? '已保存到 config.json' : '失败：' + r.msg);
  pollStatus();
};
$('btn-llm-test').onclick = async () => {
  const box = $('llm-test-msg');
  box.textContent = '正在测试…（最多 10 秒）';
  box.style.color = 'var(--muted)';
  const r = await api('/api/llm-test', llmForm());
  box.textContent = (r.ok ? '✓ ' : '✗ ') + (r.msg || '');
  box.style.color = r.ok ? 'var(--green)' : 'var(--red)';
};
$('btn-login').onclick = async () => {
  const r = await api('/api/login', {code: $('acc-code').value.trim(), pwd: $('acc-pwd').value});
  alert(r.ok ? '登录成功' : '失败：' + r.msg);
  pollStatus(); loadPending(); loadPapers();
};

// ---- 刷题策略 ----
function updateEta() {
  const n = Math.max(1, parseInt($('papers').value) || 1);
  const dwell = Math.max(0, parseInt($('p-dwell').value) || 0);
  const gap = Math.max(0, parseInt($('p-gap').value) || 0);
  const jit = Math.max(0, parseInt($('p-jitter').value) || 0);
  const sec = n * dwell + Math.max(0, n - 1) * (gap + jit / 2);
  const min = Math.round(sec / 60);
  $('eta').textContent = `预计约 ${min} 分钟 · ${n} 份`;
  $('eta').title = '只计算最短作答时长与卷间休息，不含解题、重试和验证码耗时';
}
function ensureTagOption(v) {
  const sel = $('tag');
  if (!sel.querySelector(`option[value="${v}"]`)) {
    sel.insertAdjacentHTML('beforeend', `<option value="${v}">标签 ${v}（当前）</option>`);
  }
}
async function loadTags() {
  const t = $('type').value, l = $('level').value, sel = $('tag');
  const keep = sel.value || '-1';
  sel.innerHTML = '<option value="-1">不限（全部标签）</option>';
  const d = await api(`/api/tags?type=${encodeURIComponent(t)}&level=${encodeURIComponent(l)}`);
  const list = (d && d.ok && d.tags) || [];
  const esc = s => String(s).replace(/</g, '&lt;');
  for (const it of list) {
    sel.insertAdjacentHTML('beforeend',
      `<option value="${it.id}"${it.available ? '' : ' disabled'}>${esc(it.content)}${it.available ? '' : '（已做完）'}</option>`);
  }
  ensureTagOption(keep);
  sel.value = keep;
}
function fillPractice(p) {
  if (!p) return;
  $('p-dwell').value = p.min_dwell_seconds;
  $('p-retry').value = p.max_submit_retry;
  $('p-gap').value = p.sleep_between_seconds;
  $('p-jitter').value = p.jitter_seconds;
  $('p-perhour').value = p.max_papers_per_hour;
  $('p-stopnew').checked = !!p.stop_on_no_new;
  $('p-ocr').checked = !!p.captcha_ocr;
  $('p-autofix').checked = !!p.auto_fix;
  $('p-fixattempts').value = p.auto_fix_attempts;
  $('p-verifyretries').value = p.verify_retries;
  if (p.qbase_type != null) $('type').value = String(p.qbase_type);
  if (p.level != null) $('level').value = String(p.level);
  if (p.tag_id != null) { ensureTagOption(p.tag_id); $('tag').value = String(p.tag_id); }
  if (p.papers != null) $('papers').value = p.papers;
  updateEta();
}
function practiceMsg(r) {
  const box = $('p-msg');
  box.textContent = (r.ok ? '✓ ' : '✗ ') + (r.msg || '');
  box.style.color = r.ok ? 'var(--green)' : 'var(--red)';
  if (r.practice) fillPractice(r.practice);
  practiceLoaded = true;
}
$('btn-save-practice').onclick = async () => {
  const body = {
    min_dwell_seconds: parseInt($('p-dwell').value) || 0,
    max_submit_retry: parseInt($('p-retry').value) || 1,
    sleep_between_seconds: parseInt($('p-gap').value) || 0,
    jitter_seconds: parseInt($('p-jitter').value) || 0,
    max_papers_per_hour: parseInt($('p-perhour').value) || 0,
    stop_on_no_new: $('p-stopnew').checked,
    captcha_ocr: $('p-ocr').checked,
    auto_fix: $('p-autofix').checked,
    auto_fix_attempts: parseInt($('p-fixattempts').value) || 0,
    verify_retries: parseInt($('p-verifyretries').value) || 1,
  };
  practiceMsg(await api('/api/practice', body));
};
document.querySelectorAll('button[data-preset]').forEach(b => {
  b.onclick = async () => { practiceMsg(await api('/api/preset', {name: b.dataset.preset})); };
});
['papers', 'p-dwell', 'p-gap', 'p-jitter'].forEach(id => {
  $(id).addEventListener('input', updateEta);
});
$('type').addEventListener('change', loadTags);
$('level').addEventListener('change', loadTags);
$('btn-tags').onclick = loadTags;

$('endpoint').textContent = location.host;
pollStatus(); pollLog(); loadPapers(); loadPending();
setInterval(pollStatus, 5000);
setInterval(pollLog, 1500);
setInterval(loadPending, 30000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # 安静模式
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, text: str, code=200):
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    def do_GET(self):
        u = urlparse(self.path)
        p = u.path
        if p in ("/", "/index.html"):
            self._html(PAGE)
        elif p == "/api/status":
            try:
                self._json(status_payload())
            except Exception as e:
                self._json({"error": f"{type(e).__name__}: {e}"}, 500)
        elif p == "/api/log":
            qs = parse_qs_int(u.query)
            lines, nxt = LOGBUF.since(qs)
            self._json({"lines": lines, "next": nxt})
        elif p == "/api/papers":
            store = bank_mod.Bank(cfg_mod.DB_FILE)
            papers = store.recent_papers(20)
            store.close()
            self._json({"papers": papers})
        elif p == "/api/pending":
            try:
                self._json(pending_payload())
            except Exception as e:
                self._json({"pending": [], "error": str(e)})
        elif p == "/api/llm-presets":
            try:
                self._json({"presets": llm_presets()})
            except Exception as e:  # noqa: BLE001
                self._json({"presets": {}, "error": f"{type(e).__name__}: {e}"})
        elif p == "/api/skip-cost":
            self._json(skip_cost())
        elif p == "/api/tags":
            from urllib.parse import parse_qs

            qs = parse_qs(u.query)
            try:
                qtype = int(qs.get("type", ["4"])[0])
            except Exception:
                qtype = 4
            try:
                level = int(qs.get("level", ["0"])[0])
            except Exception:
                level = 0
            self._json(tags_payload(qtype, level))
        elif p == "/api/diagnose":
            from urllib.parse import parse_qs

            sid = parse_qs(u.query).get("sid", [""])[0]
            try:
                self._json(do_diagnose(sid))
            except Exception as e:
                self._json({"ok": False, "msg": f"{type(e).__name__}: {e}", "items": []}, 500)
        elif p == "/report":
            path = ROOT / "report.html"
            if path.exists():
                self._html(path.read_text(encoding="utf-8"))
            else:
                self._html("<h3>还没有报告，先点「生成报告」</h3>", 404)
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        p = u.path
        body = self._body()
        try:
            if p == "/api/start":
                ua = (self.headers.get("User-Agent") or "?")[:70]
                log(f"收到开始请求 mode={body.get('mode')} from {self.client_address[0]} UA={ua}")
                remember_selection(body)
                ok, msg = start_job(
                    {
                        "mode": body.get("mode", "once"),
                        "type": body.get("type"),
                        "level": body.get("level"),
                        "tag": body.get("tag"),
                        "papers": body.get("papers", 1),
                        "dry_run": bool(body.get("dry")),
                    }
                )
                self._json({"ok": ok, "msg": msg}, 200 if ok else 409)
            elif p == "/api/stop":
                with JOB_LOCK:
                    JOB["stop_requested"] = True
                STOP.set()
                log("控制台收到停止请求", "■")
                self._json({"ok": True, "msg": "停止请求已发出"})
            elif p == "/api/login":
                ok, msg = do_login(str(body.get("code", "")), str(body.get("pwd", "")))
                self._json({"ok": ok, "msg": msg})
            elif p == "/api/seed":
                ok, msg = do_seed(
                    str(body.get("sid", "")), str(body.get("code", "")), body.get("qid")
                )
                self._json({"ok": ok, "msg": msg})
            elif p == "/api/llm":
                ok, msg = save_llm(body)
                self._json({"ok": ok, "msg": msg})
            elif p == "/api/llm-test":
                ok, msg = test_llm(body)
                self._json({"ok": ok, "msg": msg})
            elif p == "/api/learn":
                ok, msg = save_learn(bool(body.get("enabled")))
                self._json({"ok": ok, "msg": msg})
            elif p == "/api/skip":
                ok, msg = save_skip(bool(body.get("enabled")))
                self._json({"ok": ok, "msg": msg})
            elif p == "/api/skip-paper":
                ok, msg = skip_paper(str(body.get("sid", "")))
                self._json({"ok": ok, "msg": msg})
            elif p == "/api/practice":
                ok, msg = save_practice(body)
                self._json({"ok": ok, "msg": msg, "practice": practice_payload()})
            elif p == "/api/preset":
                ok, msg = apply_preset(str(body.get("name", "")))
                self._json({"ok": ok, "msg": msg, "practice": practice_payload()})
            elif p == "/api/report":
                ok, msg = do_report()
                self._json({"ok": ok, "msg": msg})
            elif p == "/api/export":
                ok, msg = do_export(str(body.get("sid", "")))
                self._json({"ok": ok, "msg": msg})
            elif p == "/api/export-all":
                ok, msg = do_export_all()
                self._json({"ok": ok, "msg": msg})
            elif p == "/api/redo":
                sids = [str(x).strip() for x in (body.get("sids") or []) if str(x).strip()]
                if body.get("sid"):
                    sids.append(str(body["sid"]).strip())
                if not sids:
                    self._json({"ok": False, "msg": "没有指定 sessionId"}, 400)
                else:
                    ok, msg = start_job({"mode": "redo", "sids": sids})
                    self._json({"ok": ok, "msg": msg}, 200 if ok else 409)
            elif p == "/api/captcha":
                code = str(body.get("code", "")).strip()
                if code:
                    with CAPTCHA_LOCK:
                        CAPTCHA_CODE["value"] = code
                    CAPTCHA_EVENT.set()
                    log(f"已提交验证码：{code}", "✓")
                    self._json({"ok": True, "msg": "已提交"})
                else:
                    CAPTCHA_EVENT.set()  # 空值=放弃
                    log("已放弃本次人机验证", "!")
                    self._json({"ok": True, "msg": "已放弃"})
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:  # noqa: BLE001
            self._json({"ok": False, "msg": f"{type(e).__name__}: {e}"}, 500)


def parse_qs_int(query: str) -> int:
    from urllib.parse import parse_qs

    try:
        return int(parse_qs(query).get("since", ["0"])[0])
    except Exception:
        return 0


def _already_running(url: str) -> bool:
    """探测端口上是否已有同一个控制台在跑（避免 Windows 上重复绑定同一端口）。"""
    import urllib.request

    try:
        with urllib.request.urlopen(url + "api/status", timeout=1.2) as r:
            return r.status == 200
    except Exception:
        return False


def main():
    port = 8791
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    no_browser = "--no-browser" in sys.argv
    if args:
        try:
            port = int(args[0])
        except ValueError:
            pass
    cfg_mod.ensure_config()
    url = f"http://127.0.0.1:{port}/"
    if _already_running(url):
        log(f"控制台已经在运行：{url}（直接打开，不再重复启动）", "★")
        if not no_browser:
            try:
                import webbrowser

                webbrowser.open(url)
            except Exception:
                pass
        return
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    log(f"控制台已启动：{url} （Ctrl+C 退出）", "★")
    if not no_browser:
        try:
            import webbrowser

            threading.Timer(1.0, lambda: webbrowser.open(url)).start()
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("控制台退出", "■")


if __name__ == "__main__":
    main()
