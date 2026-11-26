"""本地编译 + 按判卷真实用例验证 C/C++ 答案。

用途：
  1. 自动修复时，先在本地跑通 `.in` / `.exp` 再入库、再交卷；
  2. 把「哪里不一样」回灌给模型，避免同一类逻辑错误反复拿 0 分。

注意：这里执行的是模型生成的用户自己的代码，仅在本地临时目录运行。
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path


def find_compiler(lang: str = "c") -> str | None:
    name = "g++" if lang == "cpp" else "gcc"
    exe = shutil.which(name)
    if exe:
        return exe
    # 普通 cmd 不继承 WorkBuddy 的 PATH，这里给几个本机/常见安装位置兜底
    candidates = [
        r"D:\mingw64\bin\gcc.exe",
        r"C:\MinGW\bin\gcc.exe",
        r"C:\Program Files\mingw-w64\bin\gcc.exe",
        r"D:\workbuddy\resources\vendor\PortableGit\mingw64\bin\gcc.exe",
    ]
    if lang == "cpp":
        candidates = [p.replace("gcc.exe", "g++.exe") for p in candidates]
    for p in candidates:
        if Path(p).exists():
            return p
    return None


def _norm(text: str) -> str:
    return (text or "").replace("\r\n", "\n").replace("\r", "\n")


def compare_output(actual: str, expected: str, mode: str = "exact") -> bool:
    """与平台 compareMode 对齐：exact 逐字符；token 忽略空白差异。"""
    a, e = _norm(actual), _norm(expected)
    if mode == "token":
        a = " ".join(a.split())
        e = " ".join(e.split())
    return a == e


def compile_code(code: str, lang: str = "c") -> tuple[Path | None, str, str]:
    """返回 (可执行文件路径, 错误信息, 临时目录)。调用方负责清理临时目录。"""
    exe = find_compiler(lang)
    if not exe:
        return None, "本机没有可用的 gcc/g++，跳过本地验证", ""
    td = tempfile.mkdtemp(prefix="trainer_verify_")
    suffix = ".cpp" if lang == "cpp" else ".c"
    src = Path(td) / ("ans" + suffix)
    out = Path(td) / "ans.exe"
    try:
        src.write_text(code or "", encoding="utf-8")
        r = subprocess.run(
            [exe, str(src), "-o", str(out)],
            capture_output=True,
            text=True,
            timeout=25,
        )
        if r.returncode != 0:
            return None, (r.stderr or r.stdout or "编译失败")[:1500], td
        return out, "", td
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}", td


def verify_code(code: str, lang: str, cases: list[dict]) -> tuple[bool, list[str]]:
    """跑所有判卷用例。cases: [{"input","expected","compareMode","label"}]。"""
    if not cases:
        return True, []
    exe, err, td = compile_code(code, lang)
    if exe is None:
        return False, [err or "编译失败"]
    details: list[str] = []
    try:
        for i, case in enumerate(cases, 1):
            label = case.get("label") or f"用例{i}"
            try:
                r = subprocess.run(
                    [str(exe)],
                    input=case.get("input") or "",
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=15,
                )
            except subprocess.TimeoutExpired:
                details.append(f"{label}：本地运行超时（可能死循环）")
                continue
            actual = r.stdout or ""
            expected = case.get("expected") or ""
            mode = case.get("compareMode") or "exact"
            if compare_output(actual, expected, mode):
                continue
            details.append(
                f"{label}（compareMode={mode}）\n"
                f"  输入: {case.get('input') or ''!r}\n"
                f"  期望: {expected[:800]!r}\n"
                f"  实际: {actual[:800]!r}\n"
                f"  错误: {(r.stderr or '')[:300]!r}"
            )
    finally:
        shutil.rmtree(td, ignore_errors=True)
    return not details, details


def cases_from_score(data, gid: str | None = None) -> list[dict]:
    """从 score_result 提取失败用例（含真实 input / expected / actual）。"""
    items = data if isinstance(data, list) else [data]
    cases: list[dict] = []
    for q in items:
        if not isinstance(q, dict):
            continue
        qgid = str(q.get("globalId") or "")
        if gid and qgid != gid:
            continue
        sr = q.get("scoreResult") or {}
        tc = sr.get("failedTestCase") or {}
        case = tc.get("testCase") or {}
        if not case:
            continue
        cases.append(
            {
                "gid": qgid,
                "input": case.get("input") or "",
                "expected": case.get("output") or "",
                "actual": tc.get("output") or "",
                "stderr": tc.get("stderr") or "",
                "compareMode": sr.get("compareMode") or "exact",
                "label": f"{qgid[:8]} · compareMode={sr.get('compareMode') or 'exact'}",
            }
        )
    return cases
