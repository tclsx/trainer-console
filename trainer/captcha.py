"""人机验证（数字图）处理：优先 ddddocr 自动识别；
没有 OCR 时交给 manual_handler（控制台弹窗输入），命令行模式才用 input()。"""
from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import Callable


class CaptchaSolver:
    def __init__(self, enabled: bool = True, workdir: Path | str = "."):
        self.workdir = Path(workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)
        self._ocr = None
        # 由上层注入：接收图片字节，返回验证码字符串（None=放弃）。GUI 用这个避免 input() 卡死。
        self.manual_handler: Callable[[bytes], str | None] | None = None
        if enabled:
            try:
                import ddddocr  # type: ignore

                self._ocr = ddddocr.DdddOcr(show_ad=False)
            except Exception:
                self._ocr = None

    @property
    def auto(self) -> bool:
        return self._ocr is not None

    def solve(self, captcha: dict, allow_manual: bool = True) -> str | None:
        b64 = (captcha or {}).get("imageBase64") or ""
        if not b64:
            return None
        if b64.startswith("data:"):
            b64 = b64.split(",", 1)[-1]
        try:
            img = base64.b64decode(b64)
        except Exception:
            return None

        if self._ocr is not None:
            try:
                code = "".join(ch for ch in self._ocr.classification(img) if ch.isdigit())
                if 4 <= len(code) <= 6:
                    return code
            except Exception:
                pass

        if not allow_manual:
            return None

        if self.manual_handler is not None:
            # 控制台模式：把图交给 UI，等用户填（不碰 stdin）
            try:
                return self.manual_handler(img) or None
            except Exception:
                return None

        path = self.workdir / "captcha.png"
        path.write_bytes(img)
        print(f"\n[人机验证] 图片已保存：{path}")
        print("  打开图片看数字，然后输入（直接回车=放弃这份卷子）")
        try:
            code = input("  验证码：").strip()
        except EOFError:
            return None
        return code or None
