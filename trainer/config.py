"""配置加载与路径管理。"""
from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config.json"
EXAMPLE_CONFIG = ROOT / "config.example.json"
STATE_DIR = ROOT / ".state"
SESSION_FILE = STATE_DIR / "session.json"
DB_FILE = STATE_DIR / "trainer.db"

DEFAULTS = {
    "base_url": "http://YOUR-HOST/train/",
    "account": {"code": "", "pwd": ""},
    "practice": {
        "qbase_type": 4,
        "level": 0,
        "tag_id": -1,
        "min_dwell_seconds": 100,
        "max_submit_retry": 12,
        "resume_pending": True,
    },
    "loop": {
        "papers": 3,
        "sleep_between_seconds": 45,
        "jitter_seconds": 20,
        "stop_on_no_new": True,
    },
    "llm": {
        "enabled": False,
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "api_key": "",
        "model": "glm-4-flash",
        "temperature": 0.2,
        "timeout": 180,
    },
    "safety": {
        # 危险：调用 right-answer-paper 会让该卷永久无法重做，GUI 不提供开启入口，
        # 只保留给手动改 config.json + 明确知情的情况。
        "auto_learn_right_answer": False,
        # 无可用答案时花一次跳过额度把卷挂成 SKIPPED，不产生 0 分记录、不占 pending。
        # 默认关：会真实消耗平台金币（resetCost），且需满足 60 秒倒计时等条件。
        "skip_unanswered": False,
        "captcha_ocr": True,
        "max_papers_per_hour": 10,
    },
}

# 题型编号（与服务端 choose-q 的 qBaseType 一致）
QBASE_TYPES = {0: "单选题", 1: "多选题", 2: "判断题", 3: "填空题", 4: "编程题"}
LEVELS = {0: "随机", 1: "非常简单", 2: "简单", 3: "普通", 4: "困难", 5: "非常困难"}

# choose-q 返回码
CHOOSE_Q = {
    0: "OK",
    1: "没有新题了（该标签已全部做完）",
    2: "本周额度已用完",
    3: "操作太频繁",
    4: "未作答的选题过多（上限 5 道）",
    5: "今日额度已用完",
}

# post-answer 返回码
POST_ANSWER = {
    0: "保存成功",
    1: "记录不存在",
    2: "记录已提交",
    6: "已查看答案，无法保存",
    7: "已超时，无法保存",
    8: "校验失败（本地时间与服务端差异过大）",
    11: "还有题目未作答",
}

LANGUAGE_BY_ID = {1: "c", 2: "cpp", 3: "java", 4: "python", 5: "csharp"}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load(path: Path | str | None = None) -> dict:
    p = Path(path) if path else DEFAULT_CONFIG
    if not p.exists():
        raise SystemExit(
            f"找不到配置文件 {p}\n请复制 config.example.json 为 config.json，并填入学号密码。"
        )
    with open(p, "r", encoding="utf-8") as f:
        raw = json.load(f)
    cfg = _deep_merge(DEFAULTS, raw)
    cfg["_path"] = str(p)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return cfg


def save(cfg: dict, path: Path | str | None = None) -> None:
    p = Path(path) if path else Path(cfg.get("_path") or DEFAULT_CONFIG)
    data = {k: v for k, v in cfg.items() if not k.startswith("_")}
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def ensure_config() -> Path:
    if not DEFAULT_CONFIG.exists() and EXAMPLE_CONFIG.exists():
        import shutil

        shutil.copy(EXAMPLE_CONFIG, DEFAULT_CONFIG)
    return DEFAULT_CONFIG


def env(name: str, fallback: str = "") -> str:
    return os.environ.get(name, fallback)
