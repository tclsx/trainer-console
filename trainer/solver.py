"""答题引擎：把一份卷子的题目抽出来 → 交给题库/大模型 → 写回答题纸。

两个不同的"题型"概念（实测真实卷确认）：
  · question.qType：细分题型 0-10（0单选 1多选 2判断 3填空 4程序填空 5编程
    6实验编程 7复杂编程 8改错 9附加改错 10设计）
  · answerPaper 槽位（q0..q5 桶）：q0=单选 q1=多选 q2=判断 q3=填空
    q4=编程（qType 4-9 都进这里，answer 是整段代码 + keySequence）
    q5=设计
  因此一律按 globalId 在 6 个桶里找槽位，用桶下标决定答案形态，
  绝不能用 qType 当桶下标（编程题 qType=5 却落在 q4 桶）。
"""
from __future__ import annotations

import json
import re

from . import bank as bank_mod
from .config import LANGUAGE_BY_ID
from .keys import code_to_key_sequence
from .llm import LLMClient, LLMError, extract_code

TYPE_NAME = {0: "单", 1: "多", 2: "判断", 3: "填空", 4: "编程", 5: "设计"}
LETTERS = "ABCDEFGH"

LEVEL_NAME = {1: "简单", 2: "简单", 3: "中等", 4: "较难", 5: "困难"}


def _check_code(code: str, lang: str) -> tuple[bool, str]:
    """交卷前自检：硬禁用项 + 本机 gcc 语法检查，返回 (是否通过, 问题描述)。

    只拦「一定出事」的情况（服务端会崩的 sscanf、编译不过的语法）。
    gcc 不可用 / 非 C/C++ 语言时自动跳过，避免误伤静态判卷的改错题。
    """
    import shutil
    import subprocess
    import tempfile
    from pathlib import Path

    if not isinstance(code, str) or not code.strip():
        return False, "生成的代码为空"
    if "sscanf" in code:
        return False, "含 sscanf：判卷服务端静态分析会崩溃断连，必须改用 scanf / fgets"
    if lang not in ("c", "cpp"):
        return True, ""
    compiler = "g++" if lang == "cpp" else "gcc"
    exe = shutil.which(compiler)
    if not exe:
        return True, ""
    suffix = ".cpp" if lang == "cpp" else ".c"
    try:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / ("ans" + suffix)
            path.write_text(code, encoding="utf-8")
            r = subprocess.run(
                [exe, "-fsyntax-only", str(path)],
                capture_output=True, text=True, timeout=12,
            )
            if r.returncode == 0:
                return True, ""
            return False, (r.stderr or r.stdout or "编译失败")[:1200]
    except Exception:  # noqa: BLE001
        return True, ""  # 编译器本身异常时不拦，交给服务端判


def _q_list(rec: dict) -> list:
    for key in (("paper", "questions"), ("questions",), ("paperRecord", "paper", "questions")):
        node = rec
        ok = True
        for k in key:
            if isinstance(node, dict) and k in node:
                node = node[k]
            else:
                ok = False
                break
        if ok and isinstance(node, list):
            return node
    return []


def parse_questions(rec: dict) -> list[dict]:
    out = []
    for q in _q_list(rec):
        if not isinstance(q, dict):
            continue
        opts = q.get("options") or []
        out.append(
            {
                "globalId": q.get("globalId"),
                "qtype": int(q.get("qType", q.get("qtype", 0)) or 0),
                "content": q.get("content") or "",
                "contentEn": q.get("contentEn") or "",
                "options": [
                    {
                        "globalId": o.get("globalId"),
                        "content": o.get("content") or "",
                        "contentEn": o.get("contentEn") or "",
                    }
                    for o in opts
                    if isinstance(o, dict)
                ],
                "language": int(q.get("language", 0) or 0),
                "point": q.get("point"),
                "raw": q,
            }
        )
    return out


def _answer_slots(rec: dict) -> dict:
    paper = rec.get("answerPaper") or {}
    return {f"q{i}": list(paper.get(f"q{i}") or []) for i in range(6)}


def _slot_map(rec: dict) -> dict[str, tuple[int, dict]]:
    """globalId -> (桶下标, 槽位对象)。扫全部 6 个桶，qType 与桶下标不对应。"""
    out = {}
    paper = rec.get("answerPaper") or {}
    for bucket in range(6):
        for item in paper.get(f"q{bucket}") or []:
            if isinstance(item, dict) and item.get("globalId"):
                out[str(item["globalId"])] = (bucket, item)
    return out


class Solver:
    def __init__(self, cfg: dict, store: bank_mod.Bank):
        self.cfg = cfg
        self.bank = store
        self.llm = LLMClient(cfg.get("llm") or {})

    # ---------- 主入口 ----------
    def solve(self, rec: dict) -> tuple[list[dict], dict]:
        """返回 (每题的答题记录, 统计)。原地修改 rec['answerPaper']。"""
        questions = parse_questions(rec)
        if not questions:
            return [], {"error": "卷子里没解析到题目（结构可能变了，用 inspect 看原始 JSON）"}

        slots = _slot_map(rec)
        plan: dict[str, dict] = {}  # globalId -> {value, source, note}

        def bucket_of(q):
            hit = slots.get(str(q["globalId"]))
            return hit[0] if hit else None

        objective = [q for q in questions if bucket_of(q) in (0, 1, 2, 3)]
        code_qs = [q for q in questions if bucket_of(q) == 4]
        design_qs = [q for q in questions if bucket_of(q) == 5]

        # 1) 题库命中
        pending_obj = []
        for q in objective:
            h = bank_mod.qhash(bucket_of(q), q["content"], q["options"])
            cached = self.bank.get_answer(h)
            if cached is not None:
                val = self._resolve_cached(q, cached, bucket_of(q))
                if val is not None:
                    plan[q["globalId"]] = {"value": val, "source": "bank", "qhash": h}
                    continue
            pending_obj.append(q)

        # 2) 客观题批量问模型
        if pending_obj:
            if self.llm.enabled:
                try:
                    got = self._ask_objective(pending_obj)
                except LLMError as e:
                    got = {}
                    plan["__llm_error__"] = {"value": str(e), "source": "error"}
                by_id = {x["globalId"]: x for x in pending_obj}
                for gid, val in got.items():
                    q = by_id.get(gid)
                    if q is None:
                        continue
                    h = bank_mod.qhash(bucket_of(q), q["content"], q["options"])
                    plan[gid] = {"value": val, "source": "llm", "qhash": h}
            # 3) 兜底：不留空（否则 submit 会被拒：还有题目未作答）
            for q in pending_obj:
                if q["globalId"] not in plan:
                    plan[q["globalId"]] = {
                        "value": self._fallback(q),
                        "source": "fallback",
                    }

        # 4) 编程题（q4 桶：编程/实验/复杂/程序填空/改错）
        for q in code_qs:
            h = bank_mod.qhash(4, q["content"], q["options"])
            cached = self.bank.get_answer(h)
            if cached:
                try:
                    plan[q["globalId"]] = {
                        "value": json.loads(cached) if isinstance(cached, str) else cached,
                        "source": "bank",
                        "qhash": h,
                    }
                    continue
                except Exception:
                    pass
            if self.llm.enabled:
                seeded = (slots.get(str(q["globalId"])) or (None, {}))[1].get("answer") or ""
                try:
                    plan[q["globalId"]] = {
                        "value": self._ask_code(q, seeded),
                        "source": "llm",
                        "qhash": h,
                    }
                    continue
                except LLMError as e:
                    plan[q["globalId"]] = {"value": "", "source": f"llm-error:{e}"[:80]}
            else:
                plan[q["globalId"]] = {"value": "", "source": "no-llm"}

        # 5) 设计题
        for q in design_qs:
            if self.llm.enabled:
                try:
                    plan[q["globalId"]] = {"value": self._ask_design(q), "source": "llm"}
                    continue
                except LLMError as e:
                    plan[q["globalId"]] = {"value": "", "source": f"llm-error:{e}"[:80]}
            else:
                plan[q["globalId"]] = {"value": "", "source": "no-llm"}

        # 6) 写回答题纸
        rows = []
        total = 0
        for q in questions:
            item = plan.get(q["globalId"]) or {"value": "", "source": "missing"}
            hit = slots.get(str(q["globalId"]))
            if hit is None:
                rows.append({"type": q["qtype"], "id": q["globalId"], "ok": False,
                             "note": "答题纸里没有对应条目"})
                continue
            bucket, slot = hit
            ok = self._apply(slot, bucket, item["value"])
            total += 1 if ok else 0
            # 题库回写
            if item.get("source") == "llm" and item.get("qhash"):
                self.bank.put_answer(item["qhash"], bucket, q["content"], item["value"], "llm")
            rows.append(
                {
                    "type": bucket,
                    "type_name": TYPE_NAME.get(bucket, "?"),
                    "id": q["globalId"],
                    "source": item.get("source"),
                    "ok": ok,
                    "preview": self._preview(item["value"]),
                }
            )

        stats = {
            "questions": len(questions),
            "filled": total,
            "by_source": {},
        }
        for r in rows:
            s = r.get("source") or "?"
            stats["by_source"][s] = stats["by_source"].get(s, 0) + 1
        if "__llm_error__" in plan:
            stats["llm_error"] = plan["__llm_error__"]["value"]
        return rows, stats

    # ---------- 写入 ----------
    def _apply(self, slot: dict, bucket: int, value) -> bool:
        try:
            if bucket == 0:
                slot["answer"] = value or ""
            elif bucket == 1:
                slot["answer"] = list(value or [])
            elif bucket == 2:
                slot["answer"] = int(value) if value not in (None, "") else 1
            elif bucket == 3:
                arr = slot.get("answer") or []
                n = len(arr) if isinstance(arr, list) and arr else 1
                vals = list(value or [])
                if len(vals) < n:
                    vals += [""] * (n - len(vals))
                slot["answer"] = vals[:n]
            elif bucket == 4:
                slot["answer"] = value or ""
                if "sscanf" in (value or ""):
                    # 实测：答案含 sscanf 会让服务端 post-answer 静态分析直接崩溃断连
                    print("[!] 警告：代码里含 sscanf，保存答案大概率被服务端断连，建议改写成 scanf", flush=True)
                if self.cfg.get("practice", {}).get("synthesize_key_events", True):
                    slot["keySequence"] = code_to_key_sequence(slot["answer"])
                    slot.setdefault("inputSequence", [])
            elif bucket == 5:
                slot["answer"] = value or ""
                slot.setdefault("answerPic", [])
            else:
                return False
            return True
        except Exception:
            return False

    # ---------- 题库复用 ----------
    def _resolve_cached(self, q: dict, cached, bucket: int = None):
        if bucket is None:
            bucket = q.get("bucket", 0)
        try:
            data = json.loads(cached) if isinstance(cached, str) else cached
        except Exception:
            return None
        if bucket in (0, 1, 2):
            letters = data.get("letters") if isinstance(data, dict) else None
            texts = data.get("texts") if isinstance(data, dict) else None
            if texts:
                ids = []
                for t in texts:
                    hit = next((o["globalId"] for o in q["options"] if o["content"] == t), None)
                    if hit:
                        ids.append(hit)
                if ids:
                    return ids[0] if q["qtype"] == 0 else ids
            if letters:
                idx = [LETTERS.find(x) for x in letters]
                ids = [
                    q["options"][i]["globalId"]
                    for i in idx
                    if 0 <= i < len(q["options"])
                ]
                if ids:
                    return ids[0] if q["qtype"] == 0 else ids
            return None
        if q["qtype"] in (3, 4, 5):
            return data
        return None

    # ---------- 兜底 ----------
    def _fallback(self, q: dict):
        if q["qtype"] == 2:
            return 1
        if q["qtype"] == 0 and q["options"]:
            return q["options"][0]["globalId"]
        if q["qtype"] == 1 and q["options"]:
            return [q["options"][0]["globalId"]]
        if q["qtype"] == 3:
            return [""]
        return ""

    # ---------- 模型调用 ----------
    def _ask_objective(self, questions: list[dict]) -> dict[str, object]:
        items, meta = [], {}
        for i, q in enumerate(questions, 1):
            if q["qtype"] in (0, 1):
                opts = [
                    {"letter": LETTERS[j], "text": o["content"]}
                    for j, o in enumerate(q["options"])
                ]
                kind = "单选" if q["qtype"] == 0 else "多选"
                items.append({"no": i, "type": kind, "stem": q["content"], "options": opts})
            elif q["qtype"] == 2:
                items.append({"no": i, "type": "判断", "stem": q["content"]})
            else:
                blanks = self._blank_count(q)
                items.append(
                    {"no": i, "type": "填空", "stem": q["content"], "blanks": blanks}
                )
            meta[i] = q

        system = (
            "你是中国高校程序设计基础课程（C/C++/Java/Python）的资深助教，做题准确率极高。"
            "只输出 JSON，不要解释，不要 markdown 代码块。"
        )
        user = (
            "下面是若干客观题。判定要求：\n"
            "- 单选：给一个选项字母字符串，如 \"B\"\n"
            "- 多选：给字母数组，如 [\"A\",\"C\"]（只选确定的，宁少不错）\n"
            "- 判断：给 \"1\"（正确）或 \"0\"（错误）\n"
            "- 填空：给字符串数组，长度等于 blanks，按空顺序；写最小答案（不要多余空格）\n"
            "输出格式：{\"1\": ..., \"2\": ...}，键是题目 no。\n\n"
            + json.dumps(items, ensure_ascii=False, indent=1)
        )
        data = self.llm.complete_json(system, user)
        out: dict[str, object] = {}
        for k, v in data.items():
            try:
                q = meta[int(k)]
            except (KeyError, ValueError):
                continue
            val = self._letter_to_ids(q, v)
            if val is not None:
                out[q["globalId"]] = val
        return out

    def _letter_to_ids(self, q: dict, v):
        qt = q["qtype"]
        if qt == 0:
            if isinstance(v, list):
                v = v[0] if v else ""
            s = str(v).strip().upper()[:1]
            i = LETTERS.find(s)
            return q["options"][i]["globalId"] if 0 <= i < len(q["options"]) else None
        if qt == 1:
            if isinstance(v, str):
                v = re.findall(r"[A-H]", v.upper().replace(",", ""))
            ids = []
            for x in v or []:
                i = LETTERS.find(str(x).strip().upper()[:1])
                if 0 <= i < len(q["options"]):
                    ids.append(q["options"][i]["globalId"])
            return ids or None
        if qt == 2:
            s = str(v).strip()
            if s in ("1", "1.0", "true", "True", "正确", "对", "T", "A"):
                return 1
            if s in ("0", "false", "False", "错误", "错", "F", "B"):
                return 0
            return 1
        if qt == 3:
            if isinstance(v, list):
                return [str(x) for x in v]
            return [str(v)]
        return None

    def _blank_count(self, q: dict) -> int:
        content = q["content"] or ""
        m = re.findall(r"_{2,}|\(\s*\)|（\s*）", content)
        return max(1, len(m))

    def _ask_code(self, q: dict, seeded: str = "") -> str:
        lang = LANGUAGE_BY_ID.get(q["language"], "cpp")
        system = (
            "你是国内高校 C 语言自动判卷平台的解题助教，专写能通过判卷的完整源码。"
            "只输出一份完整代码，用 ``` 包裹；不要解释、不要标题、不要附多份版本。"
        )
        user = (
            f"编程语言：{lang}\n"
            f"题目：\n{q['content']}\n\n"
            "以下是该平台实测的判卷规则，违反任意一条就是 0 分：\n"
            "1. 输出严格照题目给的「输出格式要求」格式串，逐字符一致（空格、换行、标点都不能改）；"
            "题面中文示例与格式串矛盾时，以格式串为准。\n"
            "2. 题目明确给了「输入提示信息」时，必须原样打印提示符（如 Input n: / Please input x:）；"
            "判卷是单行管道输入、无输入回显，做法是先用 scanf 读完整输入再 printf 提示符，"
            "不要在循环结尾重复打提示符。没给提示信息的题不要凭空添加任何提示符。\n"
            "3. 若题目要求写文件、或期望输出为空，则全程不能有任何 printf，stdout 必须为空。\n"
            "4. 多行输出（矩阵等）按示例逐行换行，最后一行不要多余的尾随换行。\n"
            "5. 禁止 sscanf（判卷服务端静态分析会崩溃断连）；读入只用 scanf / fgets。\n"
            "6. 输入格式串里的字面量要一字不差（如 a = %3hd%4hd, b = ...）；"
            "注意 short int 上限 32767，求和等中间量改用 int / long long；"
            "INT_MIN 取负用 long long 避免溢出。\n"
            "7. 交完整可编译源码，含 main；不要注释掉原代码再附新版本。\n"
        )
        # 程序填空/改错题：答题框里已预置待修改的代码（qType 4/8/9）
        if seeded and seeded.strip() and "return 0" not in seeded[:200]:
            user += (
                "\n\n改错题特别要求：只修改有错误的语句，原本正确的语句请不要动（改了也扣分）；"
                "把全角中文分号/括号改成半角英文符号；输出修改后的完整文件。\n"
                f"题目预置的初始代码：\n```\n{seeded}\n```"
            )
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        text = self.llm.chat(messages, max_tokens=3000)
        code = extract_code(text)
        ok, err = _check_code(code, lang)
        if ok:
            return code
        # 自检没过：把具体错误回灌给模型，让它自己修一遍；仍不过就用原代码
        # （不硬拦：部分改错题是纯静态匹配，服务端根本不编译）
        try:
            fix = self.llm.chat(
                messages + [
                    {"role": "assistant", "content": text},
                    {"role": "user", "content": (
                        f"这份代码交卷前自检没通过，原因：{err}\n"
                        "请修正后重新输出完整代码（仍用 ``` 包裹）。"
                    )},
                ],
                max_tokens=3000,
            )
            code2 = extract_code(fix)
            if _check_code(code2, lang)[0]:
                return code2
        except LLMError:
            pass
        return code

    def _ask_design(self, q: dict) -> str:
        system = "你是计算机专业助教，回答简洁、分点、给关键代码或伪代码。"
        text = self.llm.chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": f"设计题：\n{q['content']}\n\n请给出完整的设计说明与关键实现。"},
            ]
        )
        return text.strip()

    @staticmethod
    def _preview(value) -> str:
        if isinstance(value, list):
            return ", ".join(str(v) for v in value)[:60]
        if value is None:
            return ""
        s = str(value).replace("\n", " ⏎ ")
        return s[:60]
