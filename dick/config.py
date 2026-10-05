import json
import os
import math
from pathlib import Path
import platform
import shutil
import tomllib

from .models import DickError


SOURCES = ("pacman", "aur", "apt", "dnf", "flatpak", "linyaps", "snap")
# snap 永远排在最后（见 Settings 里的强制重排）：包体积大、首次启动慢、桌面集成最差，
# 只在其它来源都没有的时候才兜底。
LAST_SOURCE = "snap"
PRIORITIES = {
    "arch": ["pacman", "aur", "flatpak", "linyaps", "snap"],
    "debian": ["apt", "flatpak", "linyaps", "snap"],
    "fedora": ["dnf", "flatpak", "linyaps", "snap"],
    "other": ["apt", "dnf", "pacman", "flatpak", "linyaps", "snap"],
}
EXECUTABLES = {"apt": "apt-get", "linyaps": "ll-cli"}


def system_family(root):
    try:
        values = {}
        for line in (root / "etc/os-release").read_text().splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                values[key] = value.strip().strip('"\'')
        identifiers = {values.get("ID", ""), *values.get("ID_LIKE", "").split()}
    except OSError:
        identifiers = set()
    if identifiers & {"arch", "manjaro", "endeavouros"}:
        return "arch"
    if identifiers & {"debian", "ubuntu"}:
        return "debian"
    if identifiers & {"fedora", "rhel", "centos", "rocky", "almalinux"}:
        return "fedora"
    return "other"


class Settings:
    def __init__(self, config_path=None, root="/", cache_dir=None, workers=None, create=False):
        self.root = Path(root)
        self.family = system_family(self.root)
        self.architecture = platform.machine() or "x86_64"
        config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        self.config_path = Path(config_path) if config_path else config_home / "dick/config.toml"
        try:
            with self.config_path.open("rb") as handle:
                self.data = tomllib.load(handle)
        except FileNotFoundError:
            if config_path and not create:
                raise DickError(f"配置文件不存在：{self.config_path}") from None
            self.data = {}
        except (OSError, tomllib.TOMLDecodeError) as error:
            raise DickError(f"无法读取配置：{error}") from error
        try:
            self.prefer = self.data.get("syntax", {}).get("prefer", "auto")
            cache = self.data.get("cache", {})
            network = self.data.get("network", {})
            self.ttl = int(cache.get("ttl", 900))
            self.timeout = float(network.get("timeout", 20))
            self.max_bytes = int(network.get("max_bytes", 64 * 1024 * 1024))
            configured_workers = network.get("workers", 4)
            self.workers = int(workers if workers is not None else configured_workers)
            self.releasever = str(self.data.get("dnf", {}).get("releasever", self._releasever()))
            self.priority = self.data.get("priority", {}).get(self.family, {}).get(
                "order", PRIORITIES[self.family]
            )
            configured_sources = self.data.get("sources", {})
            ai = self.data.get("ai", {})
            web = self.data.get("web", {})
        except (TypeError, ValueError, AttributeError) as error:
            raise DickError(f"配置字段类型错误：{error}") from error
        if not isinstance(self.prefer, str) or self.prefer not in {"auto", "pacman", "apt"}:
            raise DickError("syntax.prefer 必须为 auto、pacman 或 apt")
        enabled = configured_sources.get("enabled")
        if enabled is not None and (not isinstance(enabled, list) or any(
                not isinstance(source, str) or source not in SOURCES for source in enabled)):
            raise DickError("sources.enabled 必须为来源列表：" + ", ".join(SOURCES))
        self.enabled_sources = tuple(source for source in SOURCES
                                     if enabled is None or source in set(enabled))
        if not isinstance(self.priority, list) or not self.priority or any(
            not isinstance(source, str) or source not in SOURCES for source in self.priority
        ):
            raise DickError("priority.order 必须为非空来源列表：" + ", ".join(SOURCES))
        if len(set(self.priority)) != len(self.priority):
            raise DickError("priority.order 不能包含重复来源")
        # 无论配置怎么写，snap 都被挪到最后一名；配置里漏掉它也会补上，
        # 这样「其它来源都没有」时仍然有一个兜底，而它永远不会抢先。
        self.priority = [source for source in self.priority if source != LAST_SOURCE] + [LAST_SOURCE]
        if self.ttl < 0 or not math.isfinite(self.timeout) or self.timeout <= 0 or self.max_bytes <= 0 or self.workers <= 0:
            raise DickError("缓存 TTL 不能为负数，网络限制和并发数必须大于零")
        self.ai = self._ai_settings(ai)
        (self.web_host, self.web_port, self.web_tls,
         self.web_cert, self.web_key, self.web_token) = self._web_settings(web)
        cache_home = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
        self.cache_dir = Path(cache_dir) if cache_dir else cache_home / "dick"

    def _ai_settings(self, ai):
        """解析 [ai] 段，环境变量优先于配置文件；未启用或没有密钥时 configured 为假。"""
        if not isinstance(ai, dict):
            raise DickError("ai 配置必须是表")
        enabled = ai.get("enabled", False)
        if not isinstance(enabled, bool):
            raise DickError("ai.enabled 必须为布尔值")
        environment = os.environ
        base_url = environment.get("DICK_AI_BASE_URL", ai.get("base_url", "https://api.deepseek.com/v1"))
        model = environment.get("DICK_AI_MODEL", ai.get("model", "deepseek-chat"))
        api_key = environment.get("DICK_AI_API_KEY", ai.get("api_key", ""))
        target = environment.get("DICK_AI_TARGET", ai.get("target", "中文"))
        prompt = ai.get("prompt", "")
        try:
            timeout = float(ai.get("timeout", 60))
        except (TypeError, ValueError) as error:
            raise DickError(f"ai.timeout 必须是数字：{error}") from error
        for name, value in (("base_url", base_url), ("model", model), ("target", target), ("prompt", prompt)):
            if not isinstance(value, str):
                raise DickError(f"ai.{name} 必须为字符串")
        if not base_url.startswith(("http://", "https://")):
            raise DickError("ai.base_url 必须以 http:// 或 https:// 开头")
        if not model.strip() or not target.strip():
            raise DickError("ai.model 和 ai.target 不能为空")
        if not isinstance(api_key, str):
            raise DickError("ai.api_key 必须为字符串")
        if not math.isfinite(timeout) or timeout <= 0:
            raise DickError("ai.timeout 必须大于零")
        enabled = enabled or bool(environment.get("DICK_AI_ENABLED") or environment.get("DICK_AI_API_KEY"))
        base_url = base_url.rstrip("/")
        return {"enabled": enabled, "base_url": base_url, "model": model, "api_key": api_key,
                "target": target, "timeout": timeout, "prompt": prompt,
                "configured": bool(enabled and api_key and model and base_url)}

    def _web_settings(self, web):
        if not isinstance(web, dict):
            raise DickError("web 配置必须是表")
        host = web.get("host", "127.0.0.1")
        port = web.get("port", 3907)
        tls = web.get("tls", False)
        cert = web.get("cert", "")
        key = web.get("key", "")
        token = web.get("token", "")
        if not isinstance(host, str) or not host.strip():
            raise DickError("web.host 必须为非空字符串")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise DickError("web.port 必须是 1-65535 之间的整数")
        if not isinstance(tls, bool):
            raise DickError("web.tls 必须为布尔值")
        for name, value in (("cert", cert), ("key", key), ("token", token)):
            if not isinstance(value, str):
                raise DickError(f"web.{name} 必须为字符串")
        if bool(cert) != bool(key):
            raise DickError("web.cert 和 web.key 必须同时提供")
        return host, port, tls, cert, key, token

    def _releasever(self):
        try:
            for line in (self.root / "etc/os-release").read_text().splitlines():
                if line.startswith("VERSION_ID="):
                    return line.split("=", 1)[1].strip('"\'').split(".")[0]
        except OSError:
            pass
        return ""

    def available(self, source):
        if source == "aur":
            return self.family == "arch"
        return shutil.which(EXECUTABLES.get(source, source)) is not None

    def enabled(self, source):
        return source in self.enabled_sources

    def _config_lines(self):
        path = self.config_path
        try:
            return path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        except OSError as error:
            raise DickError(f"无法读取配置 {path}：{error}") from error

    def _write_lines(self, lines):
        path = self.config_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + ".tmp")
            temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
            os.replace(temporary, path)
        except OSError as error:
            raise DickError(f"无法写入配置 {path}：{error}") from error

    def write_section(self, section, values):
        """外科式写回 [section] 里的若干键，保留文件中的其他内容与注释。

        values 的值必须是已经格式化好的 TOML 字面量（例如 '["pacman"]' 或 'true'）。
        """
        lines = self._config_lines()
        header, end = None, len(lines)
        for index, raw in enumerate(lines):
            stripped = raw.strip()
            if not (stripped.startswith("[") and stripped.endswith("]")):
                continue
            if stripped == f"[{section}]":
                header = index
            elif header is not None:
                end = index
                break
        remaining = dict(values)
        if header is not None:
            for index in range(header + 1, end):
                key = lines[index].strip().split("=", 1)[0].strip()
                if key in remaining:
                    lines[index] = f"{key} = {remaining.pop(key)}"
        additions = [f"{key} = {value}" for key, value in remaining.items()]
        if additions:
            if header is None:
                if lines and lines[-1].strip():
                    lines.append("")
                lines.append(f"[{section}]")
                lines.extend(additions)
            else:
                lines[header + 1:header + 1] = additions
        self._write_lines(lines)

    def set_enabled(self, sources):
        """把启用来源列表写回配置文件，保留文件中的其他内容与注释。"""
        ordered = [source for source in SOURCES if source in set(sources)]
        self.write_section("sources", {"enabled": "[" + ", ".join(f'"{source}"' for source in ordered) + "]"})
        self.enabled_sources = tuple(ordered)

    def set_ai(self, values):
        """把 AI 设置写回 [ai] 段并刷新内存状态。"""
        literals = {}
        for key, value in values.items():
            if isinstance(value, bool):
                literals[key] = "true" if value else "false"
            elif isinstance(value, (int, float)):
                literals[key] = repr(value)
            else:
                literals[key] = json.dumps(str(value), ensure_ascii=False)
        self.write_section("ai", literals)
        stored = self.data.get("ai")
        if not isinstance(stored, dict):
            stored = {}
        stored.update(values)
        self.data["ai"] = stored
        self.ai = self._ai_settings(stored)
        return self.ai
