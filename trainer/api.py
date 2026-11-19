"""HIT 程序设计能力训练平台（/train）HTTP 客户端。

所有接口均以服务端相对路径 api/student/... 调用，登录态靠查询参数 ?sid=<会话id> 维持
（auth 成功后返回 body.sid；实测 cookie / header 方式服务端一律 401）。
关键的坑：
  1) post-answer 需要 verificationCode = md5("YYYY-MM-DD HH:mm <sessionId>")，
     时间取本地系统时间，服务端会校验（返回 8 = 本地时间不对）。这里用服务端
     Date 头校正时钟偏移，避免本机时间漂移导致保存失败。
  2) submit 可能返回 429（作答时间不足，body.waitSeconds）或 428（需要人机验证，
     body.captcha）。
"""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests


class ApiError(Exception):
    pass


class LoginError(ApiError):
    pass


class TooFast(ApiError):
    def __init__(self, wait_seconds: int, raw: str = ""):
        super().__init__(f"作答/操作过快，需等待 {wait_seconds} 秒")
        self.wait_seconds = wait_seconds
        self.raw = raw


class NeedCaptcha(ApiError):
    def __init__(self, captcha: dict):
        super().__init__("需要人机验证")
        self.captcha = captcha or {}


class TrainerAPI:
    def __init__(self, base_url: str, session_file: Path | str | None = None, timeout: int = 40):
        self.base = base_url.rstrip("/") + "/"
        self.timeout = timeout
        self.session_file = Path(session_file) if session_file else None
        self.s = requests.Session()
        self.s.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "Origin": self.base.rstrip("/"),
                "Referer": self.base,
                "Accept-Language": "zh-CN,zh;q=0.9",
            }
        )
        self.user: dict = {}
        self.clock_offset: float = 0.0  # 服务端时间 - 本地时间（秒）

    # ---------------- 基础 ----------------
    def _url(self, path: str) -> str:
        return self.base + path.lstrip("/")

    def _sync_clock(self, resp: requests.Response) -> None:
        date_header = resp.headers.get("Date")
        if not date_header:
            return
        try:
            server_dt = parsedate_to_datetime(date_header)
        except Exception:
            return
        # HTTP 的 Date 一律是 GMT；parsedate 对 GMT 可能返回 naive，统一按 UTC 处理
        if server_dt.tzinfo is None:
            server_dt = server_dt.replace(tzinfo=timezone.utc)
        server_local = server_dt.astimezone().replace(tzinfo=None)
        self.clock_offset = (server_local - datetime.now()).total_seconds()

    def now_server(self) -> datetime:
        return datetime.now() + timedelta(seconds=self.clock_offset)

    def _request(self, method: str, path: str, *, params=None, json_body=None, allow=(200,)):
        url = self._url(path)
        merged = dict(params or {})
        # 会话校验：服务端通过查询参数 sid 识别登录态（实测 header/cookie 均无效）
        sid = (self.user or {}).get("sid")
        if sid and "sid" not in merged and "api/student/auth" not in path:
            merged["sid"] = sid
        try:
            r = self.s.request(
                method, url, params=merged, json=json_body, timeout=self.timeout, allow_redirects=True
            )
        except requests.RequestException as e:
            raise ApiError(f"网络错误 {method} {path}: {e}") from e
        self._sync_clock(r)
        if r.status_code in (428, 429):
            body = {}
            try:
                body = r.json() or {}
            except Exception:
                pass
            if r.status_code == 429:
                raise TooFast(int(body.get("waitSeconds", 60) or 60), r.text[:300])
            raise NeedCaptcha(body.get("captcha") or body)
        if r.status_code == 401:
            raise ApiError("未登录或登录已过期，请先执行 login")
        if r.status_code not in allow:
            raise ApiError(f"HTTP {r.status_code} {method} {path}: {r.text[:300]}")
        if not r.text:
            return None
        try:
            return r.json()
        except ValueError:
            return r.text

    # ---------------- 登录态 ----------------
    def login(self, code: str, pwd: str) -> dict:
        resp = self.s.post(
            self._url("api/student/auth"),
            json={"code": code, "pwd": pwd},
            timeout=self.timeout,
        )
        self._sync_clock(resp)
        if resp.status_code != 200:
            raise LoginError(f"登录请求失败 HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json() or {}
        if not data.get("id"):
            raise LoginError("学号或密码错误（或账号被锁）：" + json.dumps(data, ensure_ascii=False))
        self.user = data
        self.save_session()
        return data

    def save_session(self) -> None:
        if not self.session_file:
            return
        self.session_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "user": self.user,
            "cookies": requests.utils.dict_from_cookiejar(self.s.cookies),
            "base": self.base,
            "saved_at": datetime.now().isoformat(timespec="seconds"),
        }
        with open(self.session_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    def load_session(self) -> bool:
        if not self.session_file or not self.session_file.exists():
            return False
        try:
            payload = json.load(open(self.session_file, "r", encoding="utf-8"))
        except Exception:
            return False
        if payload.get("base") != self.base:
            return False
        self.user = payload.get("user") or {}
        for k, v in (payload.get("cookies") or {}).items():
            self.s.cookies.set(k, v)
        return bool(self.user.get("id"))

    def ensure_login(self, code: str = "", pwd: str = "") -> dict:
        if self.load_session():
            try:
                self.quota()
                return self.user
            except ApiError:
                pass
        if not code or not pwd:
            raise LoginError("没有可用的登录态，且配置里没填学号/密码")
        return self.login(code, pwd)

    # ---------------- 业务接口 ----------------
    def quota(self) -> dict:
        return self._request("GET", "api/student/quota") or {}

    def gate(self) -> dict:
        return self._request("GET", "api/student/gate") or {}

    def tags(self, qbase_type: int = 4, level: int = 0) -> list:
        data = self._request(
            "GET", "api/student/list-tag", params={"qBaseType": qbase_type, "level": level}
        )
        return data or []

    def choose_q(self, qbase_type: int = 4, level: int = 0, tag_id: int = -1, number: int | None = None):
        """开一份卷子。返回 (code, data)。code 见 config.CHOOSE_Q。"""
        if number is None:
            number = 5 if qbase_type < 4 else 1
        body = [{"qBaseType": qbase_type, "number": number, "level": level, "tagId": tag_id}]
        try:
            data = self._request("POST", "api/student/choose-q", json_body=body)
        except TooFast as e:
            return 3, {"waitSeconds": e.wait_seconds}
        if not isinstance(data, dict):
            return -1, {"raw": data}
        return int(data.get("code", -1)), data

    def record(self, session_id) -> dict:
        return self._request("GET", f"api/student/record/{session_id}") or {}

    def verification_code(self, session_id) -> str:
        now = self.now_server()
        stamp = f"{now.year}-{now.month:02d}-{now.day:02d} {now.hour:02d}:{now.minute:02d}"
        return hashlib.md5(f"{stamp} {session_id}".encode("utf-8")).hexdigest()

    def post_answer(self, session_id, answer_paper: dict) -> int:
        body = {
            "sessionId": session_id,
            "answerPaper": answer_paper,
            "verificationCode": self.verification_code(session_id),
        }
        data = self._request("POST", "api/student/post-answer", json_body=body)
        if isinstance(data, dict):
            return int(data.get("code", -1))
        return data if isinstance(data, int) else -1

    def submit(self, session_id, captcha: dict | None = None):
        body = captcha or None
        data = self._request(
            "POST", "api/student/submit", params={"sessionId": session_id}, json_body=body
        )
        return data

    def score_result(self, session_id):
        return self._request("GET", f"api/student/score-result/{session_id}")

    def right_answer_paper(self, session_id, *, allow: bool = False):
        """取平台参考答案。默认硬拦，必须显式 allow=True 才能发请求。

        实测（真实账号对照实验）：对某个 sessionId **任何时机**调用此接口，
        这份卷都会永久失去重做资格——
          未交卷时调用 -> 被标记「已查看答案」，之后 post-answer 一律 code 6；
          已交卷时调用 -> 之后 reanswer 返回 0（没调用过的对照组返回 1）。
        half-redo 清不掉，do-it-later 只能把废卷挂成 state=4 释放 pending 名额。
        前端 36 个接口里也没有「花重置额度重开卷子」的入口。
        """
        if not allow:
            raise ApiError(
                "right-answer-paper 已被护栏拦截：调用它会让该卷永久无法重做；"
                "确需调用请显式传 allow=True"
            )
        return self._request("GET", f"api/student/right-answer-paper/{session_id}")

    def captcha(self) -> dict:
        return self._request("GET", "api/student/captcha") or {}

    def run_code(self, code: str, language: int = 2, stdin: str = ""):
        body = {"code": code, "language": language - 1, "input": [stdin]}
        return self._request("POST", "api/student/run-code", json_body=body) or []

    def history(self, tag_id: str = "", body: dict | None = None):
        return self._request(
            "POST", "api/student/history", params={"tagId": tag_id}, json_body=body or {}
        )

    def get_reset(self):
        """查询「跳过本题」周额度：numOfResetWeek / resetCost / resetLeft。

        别被名字骗了：这不是重置已交卷卷子的额度。前端只在单题卷(isQ4)调用它，
        用来驱动 skip-this / do-it-later 的 60 秒倒计时，失败文案 q.skipNotEnoughGold。
        """
        return self._request("GET", "api/student/get-reset") or {}

    def skip_this(self, session_id):
        """跳过当前卷（消耗金币额度，成功返回 1，0=额度不足，其它=被拒）。"""
        return self._request("POST", f"api/student/skip-this?sessionId={session_id}", json_body=None)

    def do_it_later(self, session_id):
        """挂成「稍后再做」state=4，释放 pending 名额，同样消耗跳过额度。"""
        return self._request("POST", f"api/student/do-it-later?sessionId={session_id}", json_body=None)

    def half_redo(self, session_id):
        return self._request("POST", f"api/student/half-redo?sessionId={session_id}", json_body=None)

    def cancel_skip(self, session_id):
        return self._request("POST", f"api/student/cancel-skip?sessionId={session_id}", json_body=None)

    def reanswer(self, session_id):
        return self._request("POST", f"api/student/reanswer?sessionId={session_id}", json_body=None)

    def rescore(self, session_id):
        """重新触发判卷流水线，仅用于判卷服务失败/卡在 state=2 时重试。

        前端链路是 retry() -> rescore() -> pollScore()，不是「按新答案重判」；
        已判完的低分卷调它不会改分。
        """
        return self._request("POST", f"api/student/rescore?sessionId={session_id}", json_body=None)

    def profile(self) -> dict:
        return self._request("GET", "api/student/profile") or {}

    # ---------------- 轮询 ----------------
    def wait_scored(self, session_id, timeout: int = 240, interval: float = 5.0) -> int:
        """等待计分完成，返回最终 state（1=已完成）。"""
        deadline = time.time() + timeout
        state = 2
        while time.time() < deadline:
            try:
                rec = self.record(session_id)
            except ApiError:
                rec = {}
            state = rec.get("state", 2) if isinstance(rec, dict) else 2
            if state != 2:
                return state
            time.sleep(interval)
        return state
