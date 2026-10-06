"""可选的 AI 翻译：支持 OpenAI 兼容、OpenAI Responses 与 Anthropic 三种接口，带本地缓存。

这里同样不引入第三方依赖：请求用标准库 `urllib`，缓存是 `cache_dir/translations`
下的 JSON 文件，键由「模型 + 目标语言 + 原文」决定，因此同一个包描述只翻译一次。
调用方负责降级（Web GUI 会提示去设置页填写）。

两种「不可用」要分开说清楚，否则报错会把人带偏：
- `configured` 只看凭据是否齐全（接口地址 + 模型 + API Key），齐全就能测连通、拉模型；
- `enabled` 是「要不要自动翻译包描述」，没勾选时翻译功能会明确要求去勾选。
"""

import hashlib
import json
import re
import time
from pathlib import Path
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import urlsplit, urlunsplit

from .models import DickError


DEFAULT_PROMPT = (
    "你是 Linux 软件包仓库的本地化助手。把用户给出的软件包描述翻译成目标语言，"
    "保持简短、准确、面向普通用户，保留专有名词与软件名原文，不要添加解释或额外字段。"
)

CACHE_TTL = 30 * 86400
BATCH = 12
MAX_TEXT = 1500

#: 支持的三类接口形态；值分别是补全地址时要拼上去的操作路径
APIS = ("openai", "openai-responses", "anthropic")
OPERATIONS = {"openai": "chat/completions", "openai-responses": "responses", "anthropic": "messages"}
API_LABELS = {"openai": "OpenAI 兼容（chat/completions）",
              "openai-responses": "OpenAI Responses（responses）",
              "anthropic": "Anthropic Messages（messages）"}
VERSION_SEGMENT = re.compile(r"^v\d+[A-Za-z0-9._-]*$")


def operation_path(api):
    """这类接口的操作路径，认不出来就当 OpenAI 兼容。"""
    return OPERATIONS.get((api or "").strip(), OPERATIONS["openai"])


def endpoint(base_url, api="openai"):
    """把用户填的地址补成真正要请求的地址。

    - 只填到域名（`https://api.deepseek.com`）→ 补成 `/v1/chat/completions`；
    - 已经带了版本号（`https://api.deepseek.com/v1`）→ 只补操作名；
    - 路径里压根没有版本号（自建网关）→ 在末尾补 `/v1/<操作>`；
    - 本来就是完整地址（以 chat/completions、responses、messages 结尾）→ 原样返回。
    """
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return ""
    parts = urlsplit(base)
    if parts.path.rstrip("/").endswith(("chat/completions", "responses", "messages")):
        return base
    segments = [segment for segment in parts.path.split("/") if segment]
    suffix = operation_path(api)
    if any(VERSION_SEGMENT.match(segment) for segment in segments):
        path = f"{parts.path.rstrip('/')}/{suffix}"
    else:
        path = f"{parts.path.rstrip('/')}/v1/{suffix}"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


class Translator:
    """把包描述翻译成目标语言，结果按 (模型, 目标语言, 原文) 缓存在磁盘上。"""

    def __init__(self, settings):
        self.settings = settings
        self.config = settings.ai
        self.directory = Path(settings.cache_dir) / "translations"

    @property
    def configured(self):
        """凭据是否齐全——和「有没有启用」是两件事，测试连接只看这一项。"""
        return bool(self.config.get("configured"))

    @property
    def api(self):
        return (self.config.get("api") or "openai").strip() or "openai"

    @property
    def url(self):
        return endpoint(self.config.get("base_url", ""), self.api)

    def missing(self):
        """还缺哪些必填项，报错时逐项点名，别让人对着填好的表单猜。"""
        labels = (("base_url", "接口地址"), ("model", "模型"), ("api_key", "API Key"))
        return [label for key, label in labels if not (self.config.get(key) or "").strip()]

    def describe(self):
        """给前端的状态快照，绝不返回密钥本身。"""
        return {
            "enabled": bool(self.config.get("enabled")),
            "configured": self.configured,
            "api": self.api,
            "api_label": API_LABELS.get(self.api, API_LABELS["openai"]),
            "base_url": self.config.get("base_url", ""),
            "endpoint": self.url,
            "model": self.config.get("model", ""),
            "target": self.config.get("target", ""),
            "timeout": self.config.get("timeout", 60),
            "has_key": bool(self.config.get("api_key")),
            "prompt": self.config.get("prompt", ""),
        }

    def target(self, override=None):
        value = (override or self.config.get("target") or "中文").strip()
        return value or "中文"

    def _require_credentials(self):
        missing = self.missing()
        if missing:
            raise DickError("未配置 AI 接口：还缺 " + "、".join(missing)
                            + "（在设置页填写，或设置 DICK_AI_* 环境变量）")

    def translate(self, texts, target=None):
        """翻译一批文本，返回与输入等长的列表；失败时抛 DickError。"""
        if not isinstance(texts, list) or not texts:
            raise DickError("翻译需要非空的文本列表")
        cleaned = []
        for text in texts:
            if not isinstance(text, str):
                raise DickError("翻译的每一项都必须是字符串")
            cleaned.append(" ".join(text.split())[:MAX_TEXT])
        self._require_credentials()
        if not self.config.get("enabled"):
            raise DickError("AI 翻译还没有启用：在设置页勾选「启用 AI 翻译」后重试")
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
        """设置页的连通性测试，绕过缓存；只看凭据是否齐全，不要求已启用。"""
        self._require_credentials()
        answer = self._request([" ".join(str(text).split())[:MAX_TEXT]], self.target())[0]
        return self.readable(answer)

    @staticmethod
    def readable(answer):
        """把模型返回的 JSON（很多接口会把转义的中文塞进数组）还原成能直接看的文本。"""
        try:
            decoded = json.loads(answer)
        except (TypeError, ValueError):
            return answer
        if isinstance(decoded, list):
            return "、".join(str(item) for item in decoded)
        if isinstance(decoded, dict):
            return json.dumps(decoded, ensure_ascii=False)
        return str(decoded)

    def models(self):
        """列出接口提供的模型 ID（OpenAI 兼容与 Anthropic 都是 GET /v1/models）。"""
        self._require_credentials()
        return self._model_ids(self._get(self._models_url(), self._headers()))

    def _models_url(self):
        """模型列表挂在版本根上，而不是操作路径下面（/v1/chat/completions → /v1/models）。"""
        resolved = self.url
        operation = operation_path(self.api)
        if resolved.endswith("/" + operation):
            resolved = resolved[: -(len(operation) + 1)]
        return resolved.rstrip("/") + "/models"

    @staticmethod
    def _model_ids(payload):
        entries = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            raise DickError("接口没有返回模型列表：" + json.dumps(payload, ensure_ascii=False)[:300])
        names = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = entry.get("id") or entry.get("name")
            if isinstance(name, str) and name.strip():
                names.append(name.strip())
        if not names:
            raise DickError("模型列表是空的：这个地址或密钥可能不支持列出模型，手动填模型名即可")
        return sorted(dict.fromkeys(names))

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

    def _headers(self):
        headers = {"Content-Type": "application/json", "Accept": "application/json",
                   "User-Agent": "dick-web/0.1"}
        key = self.config.get("api_key", "")
        if self.api == "anthropic":
            headers["x-api-key"] = key
            headers["anthropic-version"] = "2023-06-01"
        else:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def _request(self, texts, language):
        api = self.api
        model = self.config.get("model")
        system = (self.config.get("prompt") or "").strip() or DEFAULT_PROMPT
        instruction = (
            f"目标语言：{language}\n"
            "输入是一个 JSON 数组，每一项是一段软件包描述。"
            "请只输出一个等长的 JSON 字符串数组，第 i 项是第 i 段的翻译，不要输出任何其他内容。\n"
            + json.dumps(texts, ensure_ascii=False)
        )
        if api == "anthropic":
            body = {"model": model, "max_tokens": 4096, "system": system,
                    "messages": [{"role": "user", "content": instruction}]}
        elif api == "openai-responses":
            body = {"model": model, "instructions": system, "input": instruction,
                    "max_output_tokens": 4096}
        else:
            body = {"model": model, "temperature": 0.2, "stream": False,
                    "messages": [{"role": "system", "content": system},
                                 {"role": "user", "content": instruction}]}
        payload = self._post(self.url, body, self._headers())
        return self._parse(payload, len(texts))

    def _send(self, request, url):
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

    def _post(self, url, body, headers):
        request = urlrequest.Request(url, data=json.dumps(body).encode("utf-8"),
                                     headers=headers, method="POST")
        return self._send(request, url)

    def _get(self, url, headers):
        return self._send(urlrequest.Request(url, headers=headers, method="GET"), url)

    def _content(self, payload):
        if isinstance(payload, dict) and isinstance(payload.get("choices"), list) and payload["choices"]:
            message = payload["choices"][0].get("message") or {}
            content = message.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                return "".join(part.get("text", "") for part in content if isinstance(part, dict))
        if isinstance(payload, dict) and isinstance(payload.get("output_text"), str):
            return payload["output_text"]
        if isinstance(payload, dict) and isinstance(payload.get("output"), list):
            # OpenAI Responses：output[].content[].text
            parts = []
            for item in payload["output"]:
                if not isinstance(item, dict):
                    continue
                for piece in item.get("content") or []:
                    if not isinstance(piece, dict):
                        continue
                    if piece.get("type") in (None, "output_text", "text") and isinstance(piece.get("text"), str):
                        parts.append(piece["text"])
            if parts:
                return "".join(parts)
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
