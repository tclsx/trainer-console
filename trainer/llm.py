"""OpenAI 兼容的 LLM 客户端（DeepSeek / 通义 / 智谱 / Kimi / 硅基流动 / Ollama 均可）。"""
from __future__ import annotations

import json
import time

import requests

PRESETS = {
    "deepseek": ("https://open.bigmodel.cn/api/paas/v4", "glm-4-flash"),
    "qwen": ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus"),
    "zhipu": ("https://open.bigmodel.cn/api/paas/v4", "glm-4-flash"),
    "kimi": ("https://api.moonshot.cn/v1", "moonshot-v1-8k"),
    "siliconflow": ("https://api.siliconflow.cn/v1", "Qwen/Qwen2.5-72B-Instruct"),
    "ollama": ("http://127.0.0.1:11434/v1", "qwen2.5-coder:7b"),
}


class LLMError(Exception):
    pass


class LLMClient:
    def __init__(self, cfg: dict):
        self.cfg = cfg or {}
        self.base = (self.cfg.get("base_url") or "").rstrip("/")
        self.key = self.cfg.get("api_key") or ""
        self.model = self.cfg.get("model") or "glm-4-flash"
        self.temperature = float(self.cfg.get("temperature", 0.2))
        self.timeout = int(self.cfg.get("timeout", 180))

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled")) and bool(self.base)

    def _proxies(self):
        """代理走 http://（requests 原生不支持 socks5，除非另装依赖）。本地模型不走代理。"""
        p = (self.cfg.get("proxy") or "").strip()
        if not p:
            return None
        if "127.0.0.1" in self.base or "localhost" in self.base:
            return None
        return {"http": p, "https": p}

    def test_connection(self, timeout: int = 10) -> tuple[bool, str]:
        """用一次极小的 chat 请求自检通路，返回 (是否连通, 人话结论)。不打印 key。"""
        if not self.base:
            return False, "还没填 base_url"
        url = f"{self.base}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 8,
            "stream": False,
        }
        try:
            r = requests.post(
                url, headers=headers, json=payload, timeout=timeout, proxies=self._proxies()
            )
        except requests.exceptions.ProxyError:
            return False, "代理连不上：代理要写成 http:// 开头（不支持 socks5）"
        except requests.exceptions.InvalidSchema:
            return False, "代理协议不支持：必须写成 http:// 开头（不支持 socks5）"
        except requests.exceptions.ConnectionError:
            return False, f"连不上 {url}：服务没启动 / 域名不可达 / 被墙"
        except requests.exceptions.Timeout:
            return False, f"连接超时（{timeout}s）：检查代理或换一个端点"
        except requests.RequestException as e:
            return False, f"网络错误：{type(e).__name__}: {str(e)[:120]}"
        if r.status_code == 200:
            try:
                content = r.json()["choices"][0]["message"]["content"]
                return True, f"连通 ✓ model={self.model}，回复 {str(content)[:40]!r}"
            except (KeyError, IndexError, ValueError):
                return True, "连通 ✓（HTTP 200，但返回不是标准 chat/completions 结构）"
        if r.status_code in (401, 403):
            return False, f"鉴权失败 HTTP {r.status_code}：api_key 不对或没权限"
        if r.status_code == 404:
            return False, "HTTP 404：base_url 一般要以 /v1 结尾，或模型名不存在"
        if r.status_code == 400:
            return False, f"HTTP 400：{r.text[:180]}"
        return False, f"HTTP {r.status_code}：{r.text[:180]}"

    def chat(self, messages: list[dict], retries: int = 2, max_tokens: int = 4096) -> str:
        if not self.enabled:
            raise LLMError("LLM 未启用")
        url = f"{self.base}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        last = None
        for attempt in range(retries + 1):
            try:
                r = requests.post(
                    url, headers=headers, json=payload,
                    timeout=self.timeout, proxies=self._proxies(),
                )
            except requests.RequestException as e:
                last = e
                time.sleep(2 + attempt * 3)
                continue
            if r.status_code == 200:
                data = r.json()
                try:
                    return data["choices"][0]["message"]["content"]
                except (KeyError, IndexError) as e:
                    raise LLMError(f"返回结构异常: {str(data)[:300]}") from e
            last = LLMError(f"HTTP {r.status_code}: {r.text[:300]}")
            if r.status_code in (400, 401, 403):
                break
            time.sleep(2 + attempt * 3)
        raise LLMError(str(last))

    def complete_json(self, system: str, user: str) -> dict:
        text = self.chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}]
        )
        return extract_json(text)


def extract_json(text: str) -> dict:
    """从模型回复里尽量抠出 JSON 对象。"""
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.lstrip().startswith("json"):
            text = text.lstrip()[4:]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise LLMError(f"模型未返回 JSON: {text[:200]}")
    blob = text[start : end + 1]
    try:
        return json.loads(blob)
    except json.JSONDecodeError:
        # 容错：去掉行尾逗号
        import re

        blob2 = re.sub(r",\s*([}\]])", r"\1", blob)
        return json.loads(blob2)


def extract_code(text: str) -> str:
    text = (text or "").strip()
    if "```" in text:
        blocks = text.split("```")
        # 取最长的代码块
        best = ""
        for i in range(1, len(blocks), 2):
            body = blocks[i]
            if body.startswith(("c", "cpp", "java", "python", "py", "csharp", "cs", "c++")):
                body = body.split("\n", 1)[1] if "\n" in body else ""
            if len(body) > len(best):
                best = body
        if best.strip():
            return best.strip("\n")
    return text
