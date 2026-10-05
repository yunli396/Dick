"""可选的 AI 翻译：兼容 OpenAI 与 Anthropic 的 HTTP 接口，带本地缓存。

这里同样不引入第三方依赖：请求用标准库 `urllib`，缓存是 `cache_dir/translations`
下的 JSON 文件，键由「模型 + 目标语言 + 原文」决定，因此同一个包描述只翻译一次。
没有配置 API key 时一切返回 DickError，调用方负责降级（Web GUI 会提示去设置页填写）。
"""

import hashlib
import json
import time
from pathlib import Path
from urllib import error as urlerror
from urllib import request as urlrequest

from .models import DickError


DEFAULT_PROMPT = (
    "你是 Linux 软件包仓库的本地化助手。把用户给出的软件包描述翻译成目标语言，"
    "保持简短、准确、面向普通用户，保留专有名词与软件名原文，不要添加解释或额外字段。"
)

CACHE_TTL = 30 * 86400
BATCH = 12
MAX_TEXT = 1500


class Translator:
    """把包描述翻译成目标语言，结果按 (模型, 目标语言, 原文) 缓存在磁盘上。"""

    def __init__(self, settings):
        self.settings = settings
        self.config = settings.ai
        self.directory = Path(settings.cache_dir) / "translations"

    @property
    def configured(self):
        return bool(self.config.get("configured"))

    def describe(self):
        """给前端的状态快照，绝不返回密钥本身。"""
        return {
            "enabled": bool(self.config.get("enabled")),
            "configured": self.configured,
            "base_url": self.config.get("base_url", ""),
            "model": self.config.get("model", ""),
            "target": self.config.get("target", ""),
            "timeout": self.config.get("timeout", 60),
            "has_key": bool(self.config.get("api_key")),
            "prompt": self.config.get("prompt", ""),
        }

    def target(self, override=None):
        value = (override or self.config.get("target") or "中文").strip()
        return value or "中文"

    def translate(self, texts, target=None):
        """翻译一批文本，返回与输入等长的列表；失败时抛 DickError。"""
        if not isinstance(texts, list) or not texts:
            raise DickError("翻译需要非空的文本列表")
        cleaned = []
        for text in texts:
            if not isinstance(text, str):
                raise DickError("翻译的每一项都必须是字符串")
            cleaned.append(" ".join(text.split())[:MAX_TEXT])
        if not self.configured:
            raise DickError("未配置 AI 接口：在设置里填写 base_url、model 与 API key，"
                            "或设置 DICK_AI_API_KEY 环境变量")
        language = self.target(target)
        results, pending = [None] * len(cleaned), []
        for index, text in enumerate(cleaned):
            if not text:
                results[index] = ""
                continue
            cached = self._read_cache(text, language)
            if cached is None:
                pending.append(index)
            else:
                results[index] = cached
        for start in range(0, len(pending), BATCH):
            chunk = pending[start:start + BATCH]
            translations = self._request([cleaned[index] for index in chunk], language)
            if len(translations) != len(chunk):
                raise DickError(f"AI 返回了 {len(translations)} 条翻译，期望 {len(chunk)} 条")
            for index, translation in zip(chunk, translations):
                results[index] = translation
                self._write_cache(cleaned[index], language, translation)
        return results

    def test(self, text="Firefox is a fast, private and safe web browser."):
        """设置页的连通性测试，绕过缓存。"""
        if not self.configured:
            raise DickError("未配置 AI 接口：先填写 base_url、model 与 API key")
        translations = self._request([" ".join(str(text).split())[:MAX_TEXT]], self.target())
        return translations[0]

    def _cache_file(self, text, language):
        key = "\n".join((self.config.get("model", ""), language, text))
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.directory / f"{digest}.json"

    def _read_cache(self, text, language):
        path = self._cache_file(text, language)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict) or not isinstance(payload.get("translation"), str):
            return None
        if time.time() - float(payload.get("time", 0)) > CACHE_TTL:
            return None
        return payload["translation"]

    def _write_cache(self, text, language, translation):
        path = self._cache_file(text, language)
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + ".tmp")
            temporary.write_text(json.dumps({
                "text": text, "target": language, "model": self.config.get("model", ""),
                "translation": translation, "time": time.time(),
            }, ensure_ascii=False), encoding="utf-8")
            temporary.replace(path)
        except OSError:
            pass  # 缓存写不进去不影响本次翻译

    def _headers(self, anthropic):
        headers = {"Content-Type": "application/json", "Accept": "application/json",
                   "User-Agent": "dick-web/0.1"}
        if anthropic:
            headers["x-api-key"] = self.config.get("api_key", "")
            headers["anthropic-version"] = "2023-06-01"
        else:
            headers["Authorization"] = f"Bearer {self.config.get('api_key', '')}"
        return headers

    def _request(self, texts, language):
        base_url = self.config.get("base_url", "").rstrip("/")
        anthropic = "anthropic" in base_url
        system = (self.config.get("prompt") or "").strip() or DEFAULT_PROMPT
        instruction = (
            f"目标语言：{language}\n"
            "输入是一个 JSON 数组，每一项是一段软件包描述。"
            "请只输出一个等长的 JSON 字符串数组，第 i 项是第 i 段的翻译，不要输出任何其他内容。\n"
            + json.dumps(texts, ensure_ascii=False)
        )
        if anthropic:
            url = base_url + "/messages"
            body = {"model": self.config.get("model"), "max_tokens": 4096, "system": system,
                    "messages": [{"role": "user", "content": instruction}]}
        else:
            url = base_url + "/chat/completions"
            body = {"model": self.config.get("model"), "temperature": 0.2, "stream": False,
                    "messages": [{"role": "system", "content": system},
                                 {"role": "user", "content": instruction}]}
        payload = self._post(url, body, self._headers(anthropic))
        return self._parse(payload, len(texts))

    def _post(self, url, body, headers):
        request = urlrequest.Request(url, data=json.dumps(body).encode("utf-8"),
                                     headers=headers, method="POST")
        timeout = float(self.config.get("timeout") or 60)
        try:
            with urlrequest.urlopen(request, timeout=timeout) as response:
                raw = response.read()
        except urlerror.HTTPError as error:
            detail = ""
            try:
                detail = error.read().decode("utf-8", "replace")[:400]
            except OSError:
                pass
            raise DickError(f"AI 接口返回 {error.code}：{detail or error.reason}") from error
        except (urlerror.URLError, OSError, TimeoutError) as error:
            reason = getattr(error, "reason", error)
            raise DickError(f"无法连接 AI 接口 {url}：{reason}") from error
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except ValueError as error:
            raise DickError(f"AI 接口返回的不是 JSON：{error}") from error

    def _content(self, payload):
        if isinstance(payload, dict) and isinstance(payload.get("choices"), list) and payload["choices"]:
            message = payload["choices"][0].get("message") or {}
            content = message.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                return "".join(part.get("text", "") for part in content if isinstance(part, dict))
        if isinstance(payload, dict) and isinstance(payload.get("content"), list):
            return "".join(part.get("text", "") for part in payload["content"] if isinstance(part, dict))
        if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
            raise DickError("AI 接口报错：" + str(payload["error"].get("message", payload["error"]))[:300])
        raise DickError("无法从 AI 响应中取出文本：" + json.dumps(payload, ensure_ascii=False)[:300])

    def _parse(self, payload, expected):
        content = self._content(payload).strip()
        block = None
        if content.startswith("```"):
            parts = content.split("```")
            if len(parts) >= 3:
                block = parts[1]
                if block.startswith("json"):
                    block = block[4:]
        candidate = block if block is not None else content
        for text in (candidate, candidate[candidate.find("["):candidate.rfind("]") + 1]):
            if not text.strip():
                continue
            try:
                parsed = json.loads(text)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                for key in ("translations", "result", "items", "texts"):
                    if isinstance(parsed.get(key), list):
                        parsed = parsed[key]
                        break
            if isinstance(parsed, list):
                values = [item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
                          for item in parsed]
                if len(values) == expected:
                    return values
        if expected == 1:
            return [candidate.strip()]
        raise DickError("AI 没有返回等长的 JSON 数组，请检查模型或提示词：" + content[:300])
