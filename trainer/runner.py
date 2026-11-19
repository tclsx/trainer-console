"""刷题主循环：选题 → 取卷 → 作答 → 遵守最短作答时间 → 交卷 → 等分 → 学习。"""
from __future__ import annotations

import json
import random
import threading
import time
from datetime import datetime

from . import bank as bank_mod
from .api import ApiError, NeedCaptcha, TooFast, TrainerAPI
from .captcha import CaptchaSolver
from .config import CHOOSE_Q, POST_ANSWER, QBASE_TYPES
from .solver import Solver, TYPE_NAME, _slot_map, parse_questions


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def log(msg: str, level: str = "·"):
    print(f"[{ts()}] {level} {msg}", flush=True)


# 控制台（gui.py）用来请求停止当前任务
STOP = threading.Event()


class Stopped(Exception):
    """用户在控制台点了停止。"""


class Runner:
    def __init__(
        self,
        cfg: dict,
        api: TrainerAPI,
        store: bank_mod.Bank,
        solver: Solver,
        captcha: CaptchaSolver,
        dry_run: bool = False,
    ):
        self.cfg = cfg
        self.api = api
        self.store = store
        self.solver = solver
        self.captcha = captcha
        self.dry_run = dry_run
        self.practice = cfg.get("practice") or {}
        self._cfg_path = cfg.get("_path")

    def _reload_live_config(self) -> None:
        """连刷时每份开始前重读配置：窗口里改的节奏参数从下一份起生效。

        只影响 Runner 自己读取的 practice/loop/safety；Solver 的 LLM 配置仍在任务启动时固定。
        读失败（例如配置文件正写到一半）就沿用旧值，不打断任务。
        """
        try:
            from . import config as _cfg

            fresh = _cfg.load(self._cfg_path or None)
        except Exception:  # noqa: BLE001
            return
        self.cfg = fresh
        self.practice = fresh.get("practice") or {}

    # ---------------- 可中断等待 ----------------
    def _sleep(self, seconds: float, interrupt: bool = False) -> None:
        """分段 sleep。interrupt=True 时响应控制台停止请求（卷间隙/频控等待）；
        卷内守时、交卷重试不中断——中断会导致交白卷或交卷失败。"""
        end = time.time() + max(0.0, float(seconds))
        while True:
            left = end - time.time()
            if left <= 0:
                return
            if interrupt and STOP.is_set():
                raise Stopped("用户停止")
            time.sleep(min(left, 1.0))

    # ---------------- 单份 ----------------
    def run_one(
        self,
        qbase_type: int | None = None,
        level: int | None = None,
        tag_id: int | None = None,
    ) -> dict:
        p = self.practice
        if STOP.is_set():
            raise Stopped("用户停止")
        qbase_type = p.get("qbase_type", 4) if qbase_type is None else qbase_type
        level = p.get("level", 0) if level is None else level
        tag_id = p.get("tag_id", -1) if tag_id is None else tag_id

        log(f"选题：{QBASE_TYPES.get(qbase_type, qbase_type)} / 难度 {level} / 标签 {tag_id}")
        code, data = self.api.choose_q(qbase_type, level, tag_id)
        if code != 0:
            if code == 3 and data.get("waitSeconds"):
                wait = int(data["waitSeconds"]) + 5
                log(f"太频繁，等待 {wait}s（{CHOOSE_Q[3]}）", "!")
                self._sleep(wait, interrupt=True)
                code, data = self.api.choose_q(qbase_type, level, tag_id)
                if code != 0:
                    return {"ok": False, "reason": CHOOSE_Q.get(code, code), "code": code}
            elif code == 4 and p.get("resume_pending", True):
                log("未完成卷子已达上限，先清理本地记账的未完成卷", "!")
                if self.resume_pending():
                    return {"ok": False, "reason": "已清理未完成卷，可重跑", "code": code}
                return {"ok": False, "reason": CHOOSE_Q[4], "code": code}
            else:
                return {"ok": False, "reason": CHOOSE_Q.get(code, f"code={code}"), "code": code}

        sid = data.get("sessionId")
        if not sid:
            return {"ok": False, "reason": f"没拿到 sessionId: {data}", "code": -1}
        self.store.add_paper(sid, qbase_type, tag_id, level)
        log(f"开卷成功 sessionId={sid}，开始取卷", "✓")
        return self.process_paper(sid, qbase_type)

    def resume_pending(self) -> bool:
        sids = self.store.pending_papers(5)
        if not sids:
            log("本地没有未完成记录（服务端的 pending 可能是网页上手动开的）", "!")
            return False
        for sid in sids:
            if STOP.is_set():
                log("收到停止请求，不再续做剩余卷子", "■")
                break
            try:
                log(f"续做 {sid}")
                rec = self.api.record(sid)
                # 服务端已超时作废的卷（timeLeft<=0）永远存不进答案，
                # 留着只会每次 resume 都失败一次；直接清掉本地记账。
                try:
                    time_left = int(rec.get("timeLeft", 1))
                except (TypeError, ValueError):
                    time_left = 1
                if time_left <= 0:
                    self.store.finish_paper(sid, state=3, note="服务端已超时作废，自动清理")
                    log(f"{sid[:8]}… 服务端已超时作废，清理本地记账", "!")
                    continue
                if int(rec.get("state", 0)) != 0:
                    self.store.finish_paper(sid, state=int(rec.get("state", 1)), note="状态已变更")
                    continue
                # 关键：续做也必须先答题，否则会把空答题纸直接交上去
                rows, stats = self.solver.solve(rec)
                for r in rows:
                    log(f"  {r.get('type_name','?')}题 [{r.get('source','?')}] {r.get('preview','')}")
                if stats.get("error"):
                    self._dump(rec, "record_unknown.json")
                    continue
                self._answer_and_submit(sid, rec, stats=stats, rows=rows)
            except Exception as e:  # noqa: BLE001
                log(f"续做 {sid} 失败：{e}", "!")
        return True

    # ---------------- 处理一份卷 ----------------
    def process_paper(self, sid, qbase_type: int) -> dict:
        started = time.time()
        rec = self.api.record(sid)
        state = int(rec.get("state", 0))
        if state != 0:
            self.store.finish_paper(sid, state=state, note="开卷时状态非未完成")
            return {"ok": False, "reason": f"卷子状态={state}（非未完成）"}

        if rec.get("canViewCode") is False:
            log("平台标记该卷 canViewCode=false，无法作答，跳过", "!")
            self.store.finish_paper(sid, note="canViewCode=false")
            return {"ok": False, "reason": "canViewCode=false"}

        rows, stats = self.solver.solve(rec)
        if stats.get("error"):
            self._dump(rec, "record_unknown.json")
            return {"ok": False, "reason": stats["error"]}
        for r in rows:
            log(f"  {r.get('type_name','?')}题 [{r.get('source','?')}] {r.get('preview','')}")
        if stats.get("llm_error"):
            log(f"模型调用失败：{stats['llm_error']}", "!")

        if self.dry_run:
            self._dump(rec, f"dryrun_{sid}.json")
            log("dry-run：不提交，已把卷子和答案写到 dryrun 文件", "✓")
            return {"ok": True, "dry_run": True, "stats": stats, "rows": rows}

        return self._answer_and_submit(sid, rec, started, stats, rows)

    def _answer_and_submit(self, sid, rec, started=None, stats=None, rows=None) -> dict:
        started = started or self._server_started_epoch(rec) or time.time()
        # 安全闸：一条有效答案都没有（LLM 未启用 + 题库未命中）就别交卷，
        # 交空卷会白占一次额度，且退出后还得不回来。
        if not self.dry_run and not self._has_answers(rec):
            log("所有题目都没有可用答案（LLM 未启用或题库未命中），不提交空卷", "✗")
            if (self.cfg.get("safety") or {}).get("skip_unanswered"):
                # 花一次「跳过本题」额度把卷挂成 SKIPPED：不留 0 分记录，也释放 pending 名额。
                # 默认关，见 config.json safety.skip_unanswered。
                try:
                    ret = self.api.skip_this(sid)
                except ApiError as e:
                    log(f"跳过本题失败：{e}", "!")
                else:
                    if ret == 1:
                        self.store.finish_paper(sid, state=3, note="skip-this（消耗跳过额度）")
                        log("已跳过本题：无 0 分记录，pending 名额已释放", "✓")
                        return {"ok": True, "skipped": True, "session_id": sid}
                    log(f"跳过被拒（返回 {ret}）：0=金币不足，其它=条件不满足；卷子保留", "!")
            log("这份卷保留在未完成里：配好 LLM 或手动喂答案后点「续做」", "!")
            return {"ok": False, "reason": "无可用答案，已保留为未完成卷"}
        code = self.api.post_answer(sid, rec.get("answerPaper") or {})
        if code == 11:  # 还有题目未作答 -> 空答案兜底再存一次
            log("平台提示还有题目未作答，用兜底答案补齐后再存", "!")
            self._fill_blank_slots(rec)
            code = self.api.post_answer(sid, rec.get("answerPaper") or {})
        if code == 8:
            log("校验码被拒（本地时间与服务端偏差），重新同步时钟后重试", "!")
            time.sleep(1.2)
            code = self.api.post_answer(sid, rec.get("answerPaper") or {})

        if code != 0:
            log(f"保存答案返回 {code}：{POST_ANSWER.get(code, '未知')}", "!")
            if code in (2, 6, 7):
                # 卷子已经交过/超时/看过答案，直接去等成绩
                self.store.finish_paper(sid, note=f"post-answer={code}")
                score, total = self._read_score(sid)
                return {"ok": True, "session_id": sid, "score": score, "total": total,
                        "note": POST_ANSWER.get(code)}
            # 答案没存上就不要交卷，否则等于交白卷
            self.store.finish_paper(sid, note=f"post-answer={code}")
            return {"ok": False, "reason": f"答案保存失败（{POST_ANSWER.get(code, code)}）"}
        log("答案已保存", "✓")

        self._dwell(started)
        submitted = self._submit_with_retry(sid)
        if not submitted:
            self.store.finish_paper(sid, note="submit 失败")
            return {"ok": False, "reason": "交卷失败"}

        state = self.api.wait_scored(sid, timeout=300)
        score, total = self._read_score(sid)
        note = f"state={state}"
        self.store.finish_paper(sid, state=state, score=score, total=total, note=note)
        log(f"已交卷：状态={state} 得分={score}/{total}", "★")
        if score is not None and total and score < total:
            log(f"低分卷：run.py diagnose --sid {sid} 看判卷真实用例，改答案入库后 run.py redo {sid}", "!")
            self._warn_empty_expected(sid)

        # 默认不学：实测**任何时机**调用 right-answer-paper 都会让该卷无法 reanswer
        # （未交卷→post-answer=6；已交卷→reanswer=0）。只在明确开启且本卷满分时才学。
        if (
            (self.cfg.get("safety") or {}).get("auto_learn_right_answer")
            and score is not None
            and total
            and score >= total
        ):
            self._learn(sid, rec)
        return {"ok": True, "session_id": sid, "score": score, "total": total, "stats": stats, "rows": rows}

    # ---------------- 交卷 ----------------
    def _server_started_epoch(self, rec: dict):
        """按服务端开卷时间换算本机 epoch，resume 老卷时不再重复等最短作答时长。"""
        s = (rec.get("startTime") or "").strip()
        if not s:
            return None
        try:
            dt = datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return None
        elapsed = max(0.0, (self.api.now_server() - dt).total_seconds())
        return time.time() - elapsed

    def _dwell(self, started: float) -> None:
        need = int(self.practice.get("min_dwell_seconds", 100) or 0)
        elapsed = time.time() - started
        if elapsed < need:
            wait = need - elapsed
            log(f"合规等待 {wait:.0f}s（平台要求作答时间，太早交卷会被 429 拦下）")
            time.sleep(wait)

    def _submit_with_retry(self, sid) -> bool:
        max_retry = int(self.practice.get("max_submit_retry", 12) or 12)
        for attempt in range(max_retry):
            try:
                self.api.submit(sid)
                return True
            except TooFast as e:
                wait = max(5, int(e.wait_seconds) + random.randint(2, 6))
                log(f"交卷太早，平台要求再等 {e.wait_seconds}s（第 {attempt + 1} 次）", "!")
                time.sleep(wait)
            except NeedCaptcha as e:
                log("平台要求人机验证", "!")
                code = self.captcha.solve(e.captcha)
                if not code:
                    log("没有拿到验证码，放弃这份卷（答案已保存，可稍后手动交卷）", "!")
                    return False
                cid = (e.captcha or {}).get("captchaId")
                try:
                    self.api.submit(sid, {"captchaId": cid, "captchaCode": code})
                    return True
                except TooFast as e2:
                    time.sleep(max(5, int(e2.wait_seconds) + 3))
                except NeedCaptcha:
                    log("验证码不正确", "!")
                    continue
            except ApiError as e:
                log(f"交卷出错：{e}", "!")
                time.sleep(3)
        return False

    # ---------------- 分数 ----------------
    def _warn_empty_expected(self, sid) -> None:
        """0 分且判卷期望输出为空串：大概率是平台要求「零输出」程序。

        实测（2026-11-11 图书管理题）：compareMode=token 且 testCase.output=""
        不是平台缺标准答案，而是期望程序完全不打印提示符——只读输入、写文件，
        stdout 必须为空。按题面示例加 printf 提示符反而必 0 分。
        正确做法：去掉所有 printf（含结果输出），改成零输出版本再 redo。
        """
        try:
            data = self.api.score_result(sid)
        except ApiError:
            return
        for q in data if isinstance(data, list) else [data]:
            if not isinstance(q, dict):
                continue
            sr = q.get("scoreResult") or {}
            case = ((sr.get("failedTestCase") or {}).get("testCase") or {})
            actual = (sr.get("failedTestCase") or {}).get("output")
            expected = case.get("output")
            # 只有「期望输出被平台明确给出、且是空串」才算平台缺标准答案；
            # compareMode=none（改错题等非标准比对题型）的 expected 为空是正常的，不能误报。
            if sr.get("compareMode") in (None, "none"):
                continue
            if "output" in case and not (expected or "").strip() and (actual or "").strip():
                log(
                    "判卷期望输出是空串：平台多半要求「零输出」程序——"
                    "把所有 printf（包括提示符和结果）都去掉，只读输入/写文件，再 redo",
                    "!",
                )
                return

    def _read_score(self, sid):
        score = total = None
        try:
            data = self.api.score_result(sid)
            score, total = self._parse_score(data)
        except ApiError:
            pass
        if score is None:
            try:
                rec = self.api.record(sid)
                for k in ("score", "totalScore", "gotScore"):
                    if k in rec:
                        score = rec[k]
                        break
                for k in ("totalScore", "fullScore", "paperScore"):
                    if k in rec and k != "totalScore":
                        pass
                total = rec.get("fullScore") or rec.get("paperScore") or total
            except ApiError:
                pass
        return score, total

    @staticmethod
    def _parse_score(data):
        if isinstance(data, dict):
            score = None
            for k in ("score", "totalScore", "gotScore", "point"):
                if k in data and isinstance(data[k], (int, float)):
                    score = data[k]
                    break
            total = None
            for k in ("fullScore", "paperScore", "totalPoint", "maxScore"):
                if k in data and isinstance(data[k], (int, float)):
                    total = data[k]
                    break
            if score is None and isinstance(data.get("questions"), list):
                s = 0.0
                t = 0.0
                for q in data["questions"]:
                    if not isinstance(q, dict):
                        continue
                    s += float(q.get("score") or q.get("gotScore") or 0)
                    t += float(q.get("point") or q.get("fullScore") or 0)
                if t:
                    score, total = round(s, 1), round(t, 1)
            return score, total
        return None, None

    # ---------------- 学习参考答案（仅在交卷之后） ----------------
    def _learn(self, sid, rec=None) -> None:
        """交卷后把平台参考答案存入本地题库，同题下次直接命中。

        注意：**调用 right-answer-paper 会让这份卷永久失去重做资格**——
        未交卷时调用会被标记「已查看答案」，之后 post-answer 一律返回 code 6；
        已交卷时调用，之后 reanswer 返回 0（对照组未调用过的卷返回 1）。
        reanswer / half-redo 都清不掉，`do-it-later` 只能把废卷挂起释放 pending 名额。
        所以这个功能默认关闭，且只在满分卷上、用户明确开启时才执行。
        """
        log("⚠ 即将调用 right-answer-paper：本卷此后将永久无法重做（reanswer=0）", "!")
        try:
            paper = self.api.right_answer_paper(sid, allow=True)
        except ApiError as e:
            log(f"取参考答案失败（可能金币不足或被护栏拦截）：{e}", "!")
            return
        if not isinstance(paper, dict):
            return
        official: dict[str, str] = {}
        for b in range(6):
            for item in paper.get(f"q{b}") or []:
                if not isinstance(item, dict):
                    continue
                gid = str(item.get("globalId") or "")
                ra = item.get("rightAnswer")
                val = (
                    "\n".join(str(x) for x in ra if x is not None)
                    if isinstance(ra, list)
                    else str(ra or "")
                )
                if gid and val.strip():
                    official[gid] = val
        if not official:
            return
        try:
            rec = rec or self.api.record(sid)
        except ApiError:
            return
        slots = _slot_map(rec)
        learned = 0
        for q in parse_questions(rec):
            gid = str(q["globalId"])
            val = official.get(gid)
            hit = slots.get(gid)
            if not val or not hit:
                continue
            bucket = hit[0]
            h = bank_mod.qhash(bucket, q["content"], q["options"])
            if self.store.get_answer(h):
                continue  # 题库已有答案（可能是实测验证过的），不覆盖
            self.store.put_answer(h, bucket, q["content"], val, "right-answer")
            learned += 1
        log(f"已从参考答案学习 {learned} 题进本地题库", "✓")

    # ---------------- 工具 ----------------
    @staticmethod
    def _has_answers(rec: dict) -> bool:
        """答题纸里是否存在至少一条有内容的答案（判断题 0/1 也算已答）。"""
        paper = rec.get("answerPaper") or {}
        for i in range(6):
            for item in paper.get(f"q{i}") or []:
                ans = item.get("answer")
                if isinstance(ans, str) and ans.strip():
                    return True
                if isinstance(ans, list) and any(str(x).strip() for x in ans):
                    return True
                if isinstance(ans, int) and ans in (0, 1):
                    return True
        return False

    @staticmethod
    def _fill_blank_slots(rec: dict) -> None:
        paper = rec.get("answerPaper") or {}
        for i in range(6):
            for item in paper.get(f"q{i}") or []:
                ans = item.get("answer")
                empty = ans in (None, "", []) or (i == 2 and ans == 2)
                if empty:
                    if i == 2:
                        item["answer"] = 1
                    elif i in (1, 3):
                        item["answer"] = [""]
                    else:
                        item["answer"] = ""

    @staticmethod
    def _dump(obj, name: str) -> None:
        from .config import STATE_DIR

        STATE_DIR.mkdir(parents=True, exist_ok=True)
        path = STATE_DIR / name
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        log(f"原始数据已保存：{path}")

    # ---------------- 批量 ----------------
    def loop(self, papers: int, qbase_type=None, level=None, tag_id=None) -> dict:
        loop_cfg = self.cfg.get("loop") or {}
        max_per_hour = int((self.cfg.get("safety") or {}).get("max_papers_per_hour", 10) or 0)
        hour_start = time.strftime("%Y-%m-%d %H:00:00")
        done = 0
        results = []
        for i in range(papers):
            # 每份开始前重读配置：控制台里改的策略从这一份就生效（不必停任务重开）
            self._reload_live_config()
            loop_cfg = self.cfg.get("loop") or {}
            max_per_hour = int((self.cfg.get("safety") or {}).get("max_papers_per_hour", 10) or 0)
            if STOP.is_set():
                log("收到停止请求，结束本轮", "■")
                break
            if max_per_hour and self.store.papers_since(hour_start) >= max_per_hour:
                log(f"本小时已达自设上限 {max_per_hour} 份，停止", "!")
                break
            quota = {}
            try:
                quota = self.api.quota()
            except ApiError:
                pass
            if quota:
                log(
                    "额度：本周剩 {} / 今日剩 {} / 未作答 {}".format(
                        quota.get("weekLimit", "?"),
                        quota.get("dayLimit", "?"),
                        quota.get("pending", "?"),
                    )
                )
            log(f"===== 第 {i + 1}/{papers} 份 =====", "▶")
            try:
                r = self.run_one(qbase_type, level, tag_id)
            except Stopped:
                log("收到停止请求，结束本轮", "■")
                break
            except ApiError as e:
                log(f"接口异常：{e}", "✗")
                r = {"ok": False, "reason": str(e)}
            results.append(r)
            if r.get("ok"):
                done += 1
            else:
                reason = str(r.get("reason") or "")
                # 「无可用答案」必须停：否则每轮都会新开一张卷并留成 pending，
                # 几轮就把平台的未作答名额（上限 5）顶满，反而刷不动。
                if any(
                    k in reason
                    for k in ("额度", "没有新题", "所有题目", "未作答的选题", "无可用答案", "没有可用答案", "保留为未完成卷")
                ):
                    if loop_cfg.get("stop_on_no_new", True):
                        log(f"终止循环：{reason}", "!")
                        break
            if i < papers - 1:
                gap = int(loop_cfg.get("sleep_between_seconds", 45)) + random.randint(
                    0, int(loop_cfg.get("jitter_seconds", 20))
                )
                log(f"休息 {gap}s 再开下一份")
                try:
                    self._sleep(gap, interrupt=True)
                except Stopped:
                    log("收到停止请求，结束本轮", "■")
                    break
        return {"done": done, "results": results}
