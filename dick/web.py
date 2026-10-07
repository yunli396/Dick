"""DICK 的本地 Web GUI：标准库 http.server + 单页应用（应用商店式界面）。

设计取舍：
- 只用标准库，`ThreadingHTTPServer` 每个请求一个线程；SQLite 连接不能跨线程，
  所以每个请求/任务自己建一个 `Cache`，用完就关。
- 系统变更（install/remove/upgrade）必须显式 `dry_run` 或 `confirm`；默认只监听
  127.0.0.1。要开给局域网（例如手机）时配 `--tls` + 访问令牌：`/api/*` 都要带令牌，
  提权则靠网页密码框里现填的 sudo 密码（只在任务内存里用一次，不落盘）。
- 图标按需抓取 Flathub / Snapcraft，抓不到就用本地生成的字母头像，因此界面永远
  有图标可显示，且离线也能工作。
"""

import hashlib
import hmac
import html
import json
import math
import os
import re
import ssl
import sys
import threading
import time
import uuid
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import parse_qs, quote, urlparse
from datetime import date

from . import __version__, catalog, selfmanage
from .ai import APIS as AI_APIS, Translator
from .ai import endpoint as ai_endpoint
from .cache import Cache
from .config import SOURCES, Settings
from .discovery import discover
from .index import Index, QUERY_SOURCES
from .install import Installer
from .local import matches, rank, read_installed
from .models import DickError, LocalPackage, Package
from .network import HTTPClient
from .progress import Progress
from .security import ensure_certificate, resolve_token, ssl_context


WEBUI = Path(__file__).with_name("webui")
USER_AGENT = f"dick-web/{__version__}"
# 每次启动换一个编号：网页靠它确认「重启之后确实是新进程」。
BOOT_ID = uuid.uuid4().hex[:8]
# 重启前留一点时间把响应写完（下限可在测试里调小）。
RESTART_MIN_DELAY = 0.2
RESTART_DELAY = 0.8
FLATHUB_ICON = "https://dl.flathub.org/repo/appstream/x86_64/icons/128x128/{name}.png"
FLATHUB_SEARCH = "https://flathub.org/api/v2/search"
SNAPCRAFT_DETAILS = "https://api.snapcraft.io/api/v1/snaps/details/{name}?fields=icon_url"
SNAPCRAFT_SEARCH = "https://api.snapcraft.io/api/v1/snaps/search?q={name}&fields=icon_url,title"
SNAP_HEADERS = {"X-Ubuntu-Series": "16"}
ICON_TTL = 30 * 86400
ICON_MISS_TTL = 6 * 3600
AVATAR_COLORS = ("#D97757", "#C96442", "#B08968", "#8A7F6B", "#7D8B72",
                 "#9A6A5B", "#6F6E69", "#5F6B6D")
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/assets/app.css": ("app.css", "text/css; charset=utf-8"),
    "/assets/app.js": ("app.js", "text/javascript; charset=utf-8"),
}
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def http_get(url, headers=None, timeout=12, data=None):
    request = urlrequest.Request(
        url, data=data, method="POST" if data is not None else "GET",
        headers={"User-Agent": USER_AGENT, "Accept": "*/*", **(headers or {})},
    )
    with urlrequest.urlopen(request, timeout=timeout) as response:
        return response.read(), response.headers.get_content_type()


def avatar_label(name):
    segment = str(name).rstrip("/").rsplit("/", 1)[-1].rsplit(".", 1)[-1] or str(name)
    letters = "".join(character for character in segment if character.isalnum())[:2]
    return (letters or "?").upper() if letters[:1].isalpha() else (letters or "?")


def avatar_svg(source, name):
    """抓不到图标时的字母头像：暖色系、按名字稳定取色，离线可用。"""
    key = f"{source}|{name}".encode("utf-8")
    color = AVATAR_COLORS[int(hashlib.sha1(key).hexdigest(), 16) % len(AVATAR_COLORS)]
    label = html.escape(avatar_label(name))
    title = html.escape(str(name))
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 96 96" width="96" height="96" '
            f'role="img" aria-label="{title}"><title>{title}</title>'
            f'<rect width="96" height="96" rx="24" fill="{color}"/>'
            f'<text x="48" y="50" text-anchor="middle" dominant-baseline="central" '
            f'font-family="Georgia, \'Noto Serif SC\', serif" font-size="38" fill="#FFF9F3">{label}</text>'
            f'</svg>').encode("utf-8")


class IconStore:
    """按需抓取远端图标并落盘缓存；失败时回退到本地字母头像。"""

    def __init__(self, directory, offline=False):
        self.directory = Path(directory) / "icons"
        self.offline = offline
        self.lock = threading.Lock()

    def key(self, source, name):
        return hashlib.sha1(f"{source}|{name}".encode("utf-8")).hexdigest()

    def files(self, source, name):
        base = self.directory / self.key(source, name)
        return base.with_suffix(".bin"), base.with_suffix(".json")

    def get(self, source, name):
        """返回 (bytes, content_type)。"""
        with self.lock:
            cached = self._cached(source, name)
        if cached is not None:
            return cached
        result = None
        if not self.offline:
            try:
                result = self._fetch(source, name)
            except Exception as error:  # 图标永远不该让接口失败
                print(f"[web] 图标抓取失败 {source}/{name}：{error}", file=sys.stderr)
        with self.lock:
            self._remember(source, name, result)
        return result or (avatar_svg(source, name), "image/svg+xml")

    def _cached(self, source, name):
        binary, meta_path = self.files(source, name)
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        age = time.time() - float(meta.get("time", 0))
        if not meta.get("ok"):
            if age < ICON_MISS_TTL:
                return avatar_svg(source, name), "image/svg+xml"
            return None
        if age > ICON_TTL:
            return None
        try:
            return binary.read_bytes(), meta.get("type") or "image/png"
        except OSError:
            return None

    def _remember(self, source, name, result):
        binary, meta_path = self.files(source, name)
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            if result is None:
                meta_path.write_text(json.dumps({"ok": False, "time": time.time()}), encoding="utf-8")
                return
            content, content_type = result
            binary.write_bytes(content)
            meta_path.write_text(json.dumps(
                {"ok": True, "type": content_type, "time": time.time(), "bytes": len(content)}),
                encoding="utf-8")
        except OSError:
            pass

    def _fetch(self, source, name):
        for fetcher in self._fetchers(source, name):
            result = fetcher()
            if result:
                return result
        return None

    def _fetchers(self, source, name):
        if source == "snap":
            return (lambda: self._snapcraft_details(name),
                    lambda: self._snapcraft_search(name),
                    lambda: self._flathub_direct(name),
                    lambda: self._flathub_search(name))
        if source in {"flatpak", "linyaps"} or "." in name:
            return (lambda: self._flathub_direct(name),
                    lambda: self._flathub_search(name),
                    lambda: self._snapcraft_search(name))
        return (lambda: self._flathub_search(name),
                lambda: self._snapcraft_search(name),
                lambda: self._flathub_direct(name))

    def _flathub_direct(self, name):
        if "." not in name:
            return None
        try:
            content, content_type = http_get(FLATHUB_ICON.format(name=name))
        except urlerror.HTTPError:
            return None
        if not content_type.startswith("image/"):
            return None
        return content, content_type

    def _flathub_search(self, name):
        body = json.dumps({"query": name}).encode("utf-8")
        try:
            payload, _ = http_get(FLATHUB_SEARCH, {"Content-Type": "application/json"}, data=body)
            data = json.loads(payload.decode("utf-8", "replace"))
        except (urlerror.URLError, OSError, ValueError):
            return None
        for hit in (data.get("hits") or [])[:3]:
            if not isinstance(hit, dict):
                continue
            url = hit.get("icon")
            if not isinstance(url, str) or not url.startswith("http"):
                app_id = hit.get("app_id")
                if isinstance(app_id, str) and app_id:
                    url = FLATHUB_ICON.format(name=app_id)
                else:
                    continue
            try:
                content, content_type = http_get(url)
            except (urlerror.URLError, OSError):
                continue
            if content_type.startswith("image/"):
                return content, content_type
        return None

    def _snapcraft_details(self, name):
        try:
            payload, _ = http_get(SNAPCRAFT_DETAILS.format(name=name), SNAP_HEADERS)
            data = json.loads(payload.decode("utf-8", "replace"))
            url = data.get("icon_url")
        except (urlerror.URLError, OSError, ValueError):
            return None
        if not isinstance(url, str) or not url.startswith("http"):
            return None
        try:
            content, content_type = http_get(url)
        except (urlerror.URLError, OSError):
            return None
        return (content, content_type) if content_type.startswith("image/") else None

    def _snapcraft_search(self, name):
        try:
            payload, _ = http_get(SNAPCRAFT_SEARCH.format(name=name), SNAP_HEADERS)
            data = json.loads(payload.decode("utf-8", "replace"))
            hits = (data.get("_embedded") or {}).get("clickindex:package") or []
        except (urlerror.URLError, OSError, ValueError):
            return None
        for hit in hits[:2]:
            url = hit.get("icon_url") if isinstance(hit, dict) else None
            if not isinstance(url, str) or not url.startswith("http"):
                continue
            try:
                content, content_type = http_get(url)
            except (urlerror.URLError, OSError):
                continue
            if content_type.startswith("image/"):
                return content, content_type
        return None


class Job:
    """一次长耗时操作（安装、卸载、升级、刷新）的日志与结果。"""

    def __init__(self, kind, title):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.title = title
        self.status = "running"
        self.lines = []
        self.result = None
        self.error = None
        self.started = time.time()
        self.finished = None
        self.lock = threading.Lock()

    def log(self, message):
        text = ANSI.sub("", str(message)).strip()
        if not text:
            return
        with self.lock:
            self.lines.append(text)
            if len(self.lines) > 4000:
                del self.lines[:1000]

    def write(self, text):
        for line in ANSI.sub("", str(text)).splitlines():
            self.log(line)

    def flush(self):
        pass

    def isatty(self):
        return True  # Progress 会按「真终端」渲染成一行行文本，正好适合日志面板

    def finish(self, result=None, error=None):
        with self.lock:
            self.result = result
            self.error = error
            self.status = "failed" if error else "done"
            self.finished = time.time()

    def snapshot(self, since=0):
        with self.lock:
            lines = self.lines[since:]
            total = len(self.lines)
            return {"id": self.id, "kind": self.kind, "title": self.title, "status": self.status,
                    "lines": lines, "offset": total, "result": self.result, "error": self.error,
                    "elapsed": round((self.finished or time.time()) - self.started, 2)}


class WebApp:
    """路由与业务逻辑；HTTP 层只负责解析请求和落 JSON。"""

    def __init__(self, settings, offline=False):
        self.settings = settings
        self.icons = IconStore(settings.cache_dir, offline=offline)
        self.translator = Translator(settings)
        self.jobs = {}
        self.lock = threading.Lock()
        self.privilege_failures = 0
        self.privilege_until = 0.0
        self.routes = {
            ("GET", "/api/status"): self.status,
            ("GET", "/api/featured"): self.featured,
            ("GET", "/api/sources"): self.sources,
            ("POST", "/api/sources/enable"): self.enable_sources,
            ("POST", "/api/sources/disable"): self.disable_sources,
            ("POST", "/api/settings/autorank"): self.save_autorank,
            ("POST", "/api/settings/priority"): self.save_priority,
            ("GET", "/api/search"): self.search,
            ("GET", "/api/installed"): self.installed,
            ("GET", "/api/candidates"): self.candidates,
            ("GET", "/api/package"): self.package,
            ("POST", "/api/action"): self.action,
            ("POST", "/api/self/restart"): self.restart,
            ("GET", "/api/ai"): self.ai_status,
            ("POST", "/api/ai"): self.ai_save,
            ("POST", "/api/ai/test"): self.ai_test,
            ("POST", "/api/ai/models"): self.ai_models,
            ("POST", "/api/translate"): self.translate,
        }

    # ---------------------------------------------------------------- 基础设施

    def new_job(self, kind, title, worker):
        job = Job(kind, title)
        with self.lock:
            self.jobs[job.id] = job
            if len(self.jobs) > 60:
                for old in sorted(self.jobs.values(), key=lambda item: item.started)[:20]:
                    if old.status != "running":
                        self.jobs.pop(old.id, None)

        def run():
            try:
                job.finish(result=worker(job))
            except DickError as error:
                job.log(f"错误：{error}")
                job.finish(error=str(error))
            except Exception as error:  # noqa: BLE001 - 任务线程必须兜住一切
                job.log(f"内部错误：{error!r}")
                job.finish(error=f"{type(error).__name__}: {error}")

        threading.Thread(target=run, daemon=True, name=f"dick-job-{job.id}").start()
        return job

    def job_by_id(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
        if job is None:
            raise DickError(f"未知任务：{job_id}")
        return job

    def sources_param(self, query, required=False, installed=False):
        raw = query.get("sources") or query.get("source") or ""
        values = [item.strip() for item in raw.split(",") if item.strip()]
        unknown = [value for value in values if value not in SOURCES]
        if unknown:
            raise DickError("未知来源：" + "、".join(unknown) + "；可选：" + "、".join(SOURCES))
        if not values:
            if required:
                raise DickError("需要 sources 参数")
            if installed:
                # 读取本机已安装列表不需要仓库信息，直接用可用且已启用的来源
                return [source for source in SOURCES
                        if self.settings.enabled(source) and self.settings.available(source)]
            repositories, _ = discover(self.settings, respect_enabled=True, native=True)
            values = list(dict.fromkeys(repository.source for repository in repositories))
        return values

    def limit_param(self, query, default=60, ceiling=500):
        try:
            value = int(query.get("limit", default))
        except (TypeError, ValueError):
            raise DickError("limit 必须是整数") from None
        return max(1, min(ceiling, value))

    def index_for(self, report, repositories=None):
        if repositories is None:
            repositories, _ = discover(self.settings, respect_enabled=True, native=True)
        cache = Cache(self.settings.cache_dir)
        client = HTTPClient(self.settings.timeout, self.settings.max_bytes)
        return cache, Index(self.settings, cache, client, repositories, report)

    def collect_installed(self, sources):
        """读取各来源的已安装列表（与 cli.read_sources 行为一致，但模块内自足）。"""
        packages, errors = [], []
        for source in sources:
            if not self.settings.available(source):
                continue
            try:
                packages.extend(read_installed(self.settings, source))
            except DickError as error:
                errors.append(f"{source}：{error}")
        return packages, errors

    def installed_keys(self, sources):
        try:
            packages, _ = self.collect_installed(sources)
        except DickError:
            return set()
        return {(package.source, package.name.casefold()) for package in packages}

    # ---------------------------------------------------------------- 查询接口

    def status(self, query, body):
        repositories, errors = discover(self.settings, respect_enabled=True, native=False)
        entries = []
        for source in SOURCES:
            entries.append({
                "source": source,
                "enabled": self.settings.enabled(source),
                "available": self.settings.available(source),
                "repositories": sum(1 for repository in repositories if repository.source == source),
            })
        return {"family": self.settings.family, "architecture": self.settings.architecture,
                "priority": list(self.settings.priority), "autorank": self.settings.autorank,
                "sources": entries, "errors": errors,
                "version": __version__, "config_path": str(self.settings.config_path),
                "cache_dir": str(self.settings.cache_dir), "root": str(self.settings.root),
                "ai": self.translator.describe(), "offline": self.icons.offline,
                "boot": BOOT_ID, "pid": os.getpid(), "self": self.describe_self()}

    def describe_self(self):
        """设置页要显示的「DICK 自己装在哪、能不能自己更新」。"""
        try:
            installation = selfmanage.detect()
        except DickError as error:
            return {"kind": "unknown", "location": "", "hint": str(error), "can_update": False}
        except Exception as error:  # noqa: BLE001 - 探测失败不该拖垮整个设置页
            return {"kind": "unknown", "location": "",
                    "hint": f"无法判断安装位置：{type(error).__name__}: {error}", "can_update": False}
        ref = os.environ.get("DICK_REF") or selfmanage.DEFAULT_REF
        try:
            can_update = bool(selfmanage.update_commands(installation, ref))
        except Exception:  # noqa: BLE001
            can_update = False
        return {"kind": installation.kind, "location": installation.describe(),
                "hint": installation.hint, "can_update": can_update}

    # ---- 首页精选 -----------------------------------------------------------

    def featured(self, query, body):
        """把静态精选目录解析到本机索引，返回「今日精选 + 分类货架」结构。"""
        sources = self.sources_param(query)
        order = {source: position for position, source in enumerate(self.settings.priority)}
        local = [source for source in sources if source not in QUERY_SOURCES]
        notes = []
        cache, index = self.index_for(notes.append)
        try:
            packages, failures = index.search("", local) if local else ([], [])
            by_name = {}
            for package in packages:
                by_name.setdefault(package.name.casefold(), []).append(package)
            resolved = {}
            for app in catalog.APPS:
                package = self._resolve_app(app, by_name, order)
                if package is None and app.on_demand:
                    package = self._query_app(index, app, sources)
                resolved[app.key] = package
        finally:
            cache.close()

        installed = self.installed_keys(sources) if sources else set()
        records = {app.key: self._app_record(app, resolved.get(app.key), installed)
                   for app in catalog.APPS}
        sections = []
        for category in catalog.CATEGORIES:
            apps = catalog.in_category(category.id)
            sections.append({
                "id": category.id, "title": category.title, "subtitle": category.subtitle,
                "icon": category.icon, "apps": [records[app.key] for app in apps],
            })
        pool = [records[app.key] for app in catalog.featured_apps() if records[app.key]["resolved"]]
        if not pool:
            pool = [records[app.key] for app in catalog.featured_apps()]
        hero = pool[date.today().toordinal() % len(pool)] if pool else None
        return {"hero": hero, "sections": sections,
                "categories": [{"id": category.id, "title": category.title,
                                "subtitle": category.subtitle, "icon": category.icon,
                                "count": len(catalog.in_category(category.id))}
                               for category in catalog.CATEGORIES],
                "sources": sources, "errors": [*failures, *notes],
                "total": len(records), "resolved": sum(1 for record in records.values()
                                                       if record["resolved"])}

    @staticmethod
    def _resolve_app(app, by_name, order):
        """按候选包名在本地索引里找最合适的一条；限定了来源就只在那些来源里找。"""
        for candidate in app.names():
            found = by_name.get(candidate.casefold())
            if not found:
                continue
            if app.sources:
                preferred = [package for package in found if package.source in app.sources]
                if not preferred:
                    continue
                found = preferred
            return sorted(found, key=lambda package: order.get(package.source, 99))[0]
        return None

    @staticmethod
    def _query_app(index, app, sources):
        """本地索引里没有时，才向按需来源（AUR / snap / 玲珑）真正查一次。

        只查用户这次请求范围内的来源：筛选到 pacman 时不该偷偷去问玲珑。
        """
        wanted = [source for source in app.on_demand if source in sources]
        if not wanted:
            return None
        for candidate in app.names():
            try:
                packages, _ = index.search(candidate, wanted, exact=True)
            except DickError:
                continue
            if packages:
                return packages[0]
        return None

    @staticmethod
    def _app_record(app, package, installed):
        record = {"key": app.key, "name": app.name, "tagline": app.tagline,
                  "category": app.category, "category_title": catalog.category_title(app.category),
                  "featured": app.featured, "resolved": package is not None}
        if package is None:
            record.update({"source": None, "package": None, "version": "", "description": "",
                           "repository": "", "architecture": "", "installed": False,
                           "icon": f"/api/icon?source=other&name={quote(app.name)}"})
            return record
        record.update({
            "source": package.source, "package": package.name, "version": package.version,
            "description": package.description, "repository": package.repository,
            "architecture": package.architecture,
            "installed": (package.source, package.name.casefold()) in installed,
            "icon": f"/api/icon?source={package.source}&name={quote(package.name)}",
        })
        return record

    def sources(self, query, body):
        native = query.get("scan") in {"1", "true", "yes"}
        repositories, errors = discover(self.settings, respect_enabled=True, native=native)
        entries = []
        for source in SOURCES:
            found = [repository for repository in repositories if repository.source == source]
            entries.append({
                "source": source,
                "enabled": self.settings.enabled(source),
                "available": self.settings.available(source),
                "repositories": [{"name": repository.name, "urls": list(repository.urls),
                                  "mirrorlist": repository.mirrorlist, "metalink": repository.metalink}
                                 for repository in found],
            })
        return {"scanned": native, "sources": entries, "errors": errors,
                "config_path": str(self.settings.config_path)}

    def enable_sources(self, query, body):
        return self._set_sources(body.get("sources"), True)

    def disable_sources(self, query, body):
        return self._set_sources(body.get("sources"), False)

    def _set_sources(self, values, enable):
        if not isinstance(values, list) or not values:
            raise DickError("需要 sources 列表")
        for value in values:
            if value not in SOURCES:
                raise DickError(f"未知来源：{value}；可选：" + "、".join(SOURCES))
        current = set(self.settings.enabled_sources)
        if enable:
            current.update(values)
        else:
            current.difference_update(values)
        ordered = [source for source in SOURCES if source in current]
        self.settings.set_enabled(ordered)
        return {"enabled": list(self.settings.enabled_sources),
                "config_path": str(self.settings.config_path)}

    def save_autorank(self, query, body):
        """设置页的「自动来源排序」开关，写回 [install] autorank。"""
        enabled = body.get("enabled")
        if not isinstance(enabled, bool):
            raise DickError("需要 enabled 布尔值")
        self.settings.set_autorank(enabled)
        return {"autorank": self.settings.autorank, "config_path": str(self.settings.config_path)}

    def save_priority(self, query, body):
        """设置页拖拽出来的安装优先级，写回 [priority.<家族>] order。

        与 CLI 的 `source ranking` 同一规矩：自动来源排序开着时不许手工排——否则
        「谁先谁后」有两个主人，用户改了顺序却还在自动微调，很难解释。
        """
        order = body.get("order")
        if not isinstance(order, list) or not order:
            raise DickError("需要 order 来源列表")
        values = [str(source) for source in order]
        for source in values:
            if source not in SOURCES:
                raise DickError(f"未知来源：{source}；可选：" + "、".join(SOURCES))
        if self.settings.autorank:
            raise DickError("自动来源排序已开启：先在设置页关掉它，再手工排序来源")
        saved = self.settings.set_priority(values)
        return {"priority": saved, "config_path": str(self.settings.config_path)}

    def search(self, query, body):
        text = (query.get("q") or "").strip()
        if not text:
            raise DickError("缺少搜索关键词 q")
        sources = self.sources_param(query)
        exact = query.get("exact") in {"1", "true", "yes"}
        limit = self.limit_param(query)
        notes = []
        cache, index = self.index_for(notes.append)
        try:
            packages, failures = index.search(text, sources, exact=exact)
        finally:
            cache.close()
        installed = self.installed_keys(sources)
        results = []
        for package in packages[:limit]:
            record = package.to_dict()
            record["installed"] = (package.source, package.name.casefold()) in installed
            record["icon"] = f"/api/icon?source={package.source}&name={package.name}"
            results.append(record)
        return {"query": text, "total": len(packages), "packages": results,
                "errors": [*failures, *notes], "sources": sources}

    def installed(self, query, body):
        sources = self.sources_param(query, installed=True)
        text = (query.get("q") or "").strip().casefold()
        limit = self.limit_param(query, default=300, ceiling=2000)
        packages, errors = self.collect_installed(sources)
        if text:
            packages = [package for package in packages if text in package.name.casefold()]
        packages.sort(key=lambda package: (package.source, package.name.casefold()))
        results = []
        for package in packages[:limit]:
            record = package.to_dict()
            record["icon"] = f"/api/icon?source={package.source}&name={package.name}"
            results.append(record)
        return {"total": len(packages), "packages": results, "errors": errors, "sources": sources}

    def candidates(self, query, body):
        target = (query.get("target") or "").strip()
        if not target:
            raise DickError("缺少 target")
        sources = self.sources_param(query, installed=True)
        lowered = target.casefold()
        packages, errors = self.collect_installed(sources)
        found = [package for package in packages if matches(package, lowered)]
        found.sort(key=lambda package: (rank(package, lowered), package.source, package.name.casefold()))
        results = []
        for package in found:
            record = package.to_dict()
            record["rank"] = rank(package, lowered)
            record["icon"] = f"/api/icon?source={package.source}&name={package.name}"
            results.append(record)
        return {"target": target, "total": len(found), "candidates": results, "errors": errors}

    def package(self, query, body):
        source = (query.get("source") or "").strip()
        name = (query.get("name") or "").strip()
        if source not in SOURCES:
            raise DickError("package 需要合法的 source 参数")
        if not name:
            raise DickError("package 需要 name 参数")
        record = Package(name, source, query.get("description", ""), query.get("version", ""),
                         query.get("repository", ""), query.get("architecture", ""),
                         query.get("categories", ""))
        detail = record.to_dict()
        detail["icon"] = f"/api/icon?source={source}&name={name}"
        detail["installed"] = (source, name.casefold()) in self.installed_keys([source])
        installer = Installer(self.settings, None, lambda message: None, dry_run=True, yes=False)
        try:
            detail["install_command"] = installer.install_command(record)
        except DickError as error:
            detail["install_command"] = None
            detail["install_note"] = str(error)
        if detail["installed"]:
            try:
                detail["remove_command"] = installer.uninstall_command(LocalPackage(source, name))
            except DickError:
                detail["remove_command"] = None
        return detail

    def icon(self, query, body):
        source = (query.get("source") or "").strip()
        name = (query.get("name") or "").strip()
        if not name:
            raise DickError("icon 需要 name 参数")
        content, content_type = self.icons.get(source if source in SOURCES else "other", name)
        return content, content_type

    # ---------------------------------------------------------------- 系统变更

    def action(self, query, body):
        kind = (body.get("action") or "").strip()
        if kind not in {"install", "remove", "upgrade", "update", "updateme"}:
            raise DickError("action 必须是 install、remove、upgrade、update 或 updateme")
        dry_run = bool(body.get("dry_run"))
        confirm = bool(body.get("confirm"))
        if not dry_run and not confirm:
            raise DickError("真正的系统变更需要 confirm=true；请先用演练模式预览命令")
        if not dry_run and kind != "updateme" and self.settings.root.resolve() != Path("/").resolve():
            raise DickError("--root 只用于读取源配置；系统变更请在真实根目录下执行，或使用演练模式")
        sources = body.get("sources")
        if sources is None and kind != "updateme":
            sources = self.sources_param(query, installed=kind == "remove")
        elif sources is None:
            sources = []
        elif not isinstance(sources, list) or any(item not in SOURCES for item in sources):
            raise DickError("sources 必须是来源列表")
        targets = body.get("targets") or []
        if not isinstance(targets, list) or any(
                not isinstance(item, str) or not item.strip() or item.startswith("-") for item in targets):
            raise DickError("targets 必须是包名列表，且不能以 - 开头")
        deep = bool(body.get("deep"))
        yes = True if not dry_run else bool(body.get("yes"))
        packages = body.get("packages") or []
        if not isinstance(packages, list) or any(
                not isinstance(item, dict) or not isinstance(item.get("name"), str)
                or not item["name"].strip() or item["name"].startswith("-") for item in packages):
            raise DickError("packages 必须是 [{source, name}] 形式的列表")
        if kind == "install" and not targets:
            raise DickError("install 需要 targets")
        if kind == "remove" and not targets and not packages:
            raise DickError("remove 需要 targets 或 packages")
        if kind in {"upgrade", "update", "updateme"} and targets:
            raise DickError(f"{kind} 不接受 targets")
        ref = (body.get("ref") or "").strip()
        if ref and (ref.startswith("-") or not re.fullmatch(r"[A-Za-z0-9._/-]+", ref)):
            raise DickError("ref 只能是分支或标签名")
        title = {"install": "安装", "remove": "卸载", "upgrade": "整机升级", "update": "刷新索引",
                 "updateme": "更新 DICK"}[kind]
        if kind == "remove":
            title = "卸载 " + "、".join(targets[:3] or [item["name"] for item in packages[:3]])
        elif kind == "install":
            title = "安装 " + "、".join(targets[:3])
        # 网页密码框里现填的 sudo 密码：只在这次任务的内存里活着，用完就清，绝不落盘。
        password = body.pop("password", None)
        if password is not None and (not isinstance(password, str) or len(password) > 512):
            raise DickError("password 必须是长度不超过 512 的字符串")
        password = password or ""
        privileged = kind in {"install", "remove", "upgrade"} and not dry_run
        if privileged:
            wait = self.privilege_wait()
            if wait:
                raise DickError(f"sudo 密码连续输错，请 {wait} 秒后再试（手动安装可在宿主终端直接执行）")
        payload = {"action": kind, "targets": targets, "sources": sources, "deep": deep,
                   "packages": packages, "dry_run": dry_run, "confirm": confirm,
                   "password": bool(password), "ref": ref or None}

        def worker(job):
            nonlocal password
            try:
                if kind == "updateme":
                    job.log(f"{title}：{'演练' if dry_run else '执行'}"
                            + (f"（分支 {ref}）" if ref else ""))
                    return self.job_self_update(job, ref or None, dry_run)
                job.log(f"{title}：{'演练' if dry_run else '执行'}；来源 " + "、".join(sources))
                if kind == "install":
                    result = self.job_install(job, targets, sources, dry_run, yes, password)
                elif kind == "remove":
                    result = self.job_remove(job, targets, sources, deep, dry_run, yes, packages, password)
                elif kind == "upgrade":
                    result = self.job_upgrade(job, sources, dry_run, yes, password)
                else:
                    result = self.job_update(job, sources)
                if privileged:
                    self.note_privilege(not (isinstance(result, dict) and result.get("auth_failed")))
                return result
            finally:
                password = ""  # 让密码字符串尽早变成垃圾回收的候选

        job = self.new_job(kind, title, worker)
        return {"job": job.snapshot(), "request": payload}

    def privilege_wait(self):
        """连续输错密码后的冷却：返回剩余秒数，未冷却时返回 0。"""
        with self.lock:
            return max(0, int(self.privilege_until - time.time()))

    def note_privilege(self, ok):
        with self.lock:
            if ok:
                self.privilege_failures = 0
                self.privilege_until = 0.0
                return
            self.privilege_failures += 1
            if self.privilege_failures >= 5:
                self.privilege_failures = 0
                self.privilege_until = time.time() + 60

    def job_install(self, job, targets, sources, dry_run, yes, password=""):
        cache, index = self.index_for(job.log)
        try:
            installer = Installer(self.settings, index, job.log, dry_run, yes, stream=job.log,
                                  password=password)
            results = [installer.install(target, sources) for target in targets]
            auth_failed = installer.auth_failed
        finally:
            cache.close()
        for result in results:
            if not result["success"]:
                job.log(f"未能安装 {result['name']}：所有候选来源都已失败")
                for attempt in result["attempts"]:
                    if "error" in attempt:
                        job.log(f"  {attempt['source']}：{attempt['error']}")
        return {"results": results, "dry_run": dry_run, "auth_failed": auth_failed}

    def job_remove(self, job, targets, sources, deep, dry_run, yes, packages=None, password=""):
        installer = Installer(self.settings, None, job.log, dry_run, yes, stream=job.log,
                              password=password)
        installed, errors = self.collect_installed(sources)
        for error in errors:
            job.log(f"警告：{error}")
        known = {(package.source, package.name) for package in installed}
        explicit = [LocalPackage(item.get("source") or "", item["name"], item.get("version", ""),
                                 item.get("description", "")) for item in (packages or [])]
        work, results = [], []
        if explicit:  # 前端已经在候选列表里选好了具体包，不再做模糊匹配
            work = [(package.name, [package]) for package in explicit]
        else:
            for target in targets:
                lowered = target.casefold()
                found = sorted([package for package in installed if matches(package, lowered)],
                               key=lambda item: (rank(item, lowered), item.source, item.name.casefold()))
                if not found:
                    job.log(f"没有找到已安装的 {target}")
                    results.append({"name": target, "success": False, "error": "未找到已安装的包"})
                    continue
                if len(found) > 1:
                    job.log(f"{target} 匹配到 {len(found)} 个已安装的包："
                            + "、".join(f"{package.source}/{package.name}" for package in found))
                    if not dry_run:
                        job.log("为避免误删，请先在候选列表里选择要卸载的具体包")
                        results.append({"name": target, "success": False,
                                        "error": "匹配到多个包，需要先选择"})
                        continue
                work.append((target, found))
        for label, chosen in work:
            for package in chosen:
                if package.source and (package.source, package.name) not in known:
                    job.log(f"提示：{package.source}/{package.name} 不在已安装列表中，仍按该名称执行")
                try:
                    command = installer.uninstall_command(package, deep)
                    code = installer.execute(command)
                    record = {"name": label, "source": package.source, "package": package.name,
                              "version": package.version, "command": command,
                              "returncode": code, "success": code == 0, "dry_run": dry_run}
                    if code != 0:  # 报告里带上原生命令的原因，而不是光秃秃一个退出码
                        record["error"] = installer.failure_reason(code)
                        installer.report_uninstall_hint(package)
                    results.append(record)
                except DickError as error:
                    job.log(f"{package.source}/{package.name}：{error}")
                    results.append({"name": label, "source": package.source, "package": package.name,
                                    "success": False, "error": str(error)})
        return {"results": results, "dry_run": dry_run, "auth_failed": installer.auth_failed}

    def job_upgrade(self, job, sources, dry_run, yes, password=""):
        installer = Installer(self.settings, None, job.log, dry_run, yes, stream=job.log,
                              password=password)
        command = installer.upgrade_command(sources)
        code = installer.execute(command)
        return {"command": command, "returncode": code, "success": code == 0, "dry_run": dry_run,
                "auth_failed": installer.auth_failed}

    def job_update(self, job, sources):
        repositories, errors = discover(self.settings, respect_enabled=True, native=True)
        cache, index = self.index_for(job.log, repositories)
        try:
            selected = [repository for repository in repositories if repository.source in sources]
            progress = Progress(self.settings.family, len(selected), job, enabled=True)
            progress.start()
            results, failures = index.refresh(sources, progress=progress, workers=self.settings.workers)
            progress.finish()
            failures.extend(errors)
            if not failures:
                cache.prune(repositories, sources)
        finally:
            cache.close()
        job.log(f"刷新完成：成功 {len(results)} 个仓库，失败 {len(failures)} 个")
        return {"refreshed": results, "errors": failures}

    # ---------------------------------------------------------------- 自管理

    def job_self_update(self, job, ref=None, dry_run=False):
        """网页里的「更新 DICK」：直接复用 CLI 的 updateme。"""
        args = SimpleNamespace(prefix=None, ref=ref, dry_run=dry_run, yes=True)
        payload = selfmanage.run_update(self.settings, args)
        job.log(f"安装位置：{payload['location']}")
        for command in payload["commands"]:
            job.log(("将执行：" if dry_run else "执行：") + " ".join(str(part) for part in command))
        if not payload["success"]:
            raise DickError(payload.get("error") or payload["message"])
        job.log(payload["message"])
        if not dry_run and payload["commands"]:
            payload["restart_required"] = True
            job.log("提示：重启 DICK 服务后新版本才会生效（设置页的「重启服务」）。")
        return payload

    def restart(self, query, body):
        """重启服务进程：先把响应写完，再原地重起（新进程会重新加载代码）。"""
        if not body.get("confirm"):
            raise DickError("重启服务需要 confirm=true")
        try:
            delay = float(body.get("delay", RESTART_DELAY))
        except (TypeError, ValueError):
            raise DickError("delay 必须是秒数")
        delay = min(max(delay, RESTART_MIN_DELAY), 30.0)
        argv = list(getattr(sys, "orig_argv", None) or [sys.executable, "-m", "dick", *sys.argv[1:]])
        app = self

        def reboot():
            time.sleep(delay)
            try:
                os.execv(sys.executable, argv)
            except OSError as error:  # 起不来就留着旧进程，至少页面还能回话
                app.restart_error = str(error)
                print(f"重启失败：{error}", file=sys.stderr, flush=True)

        self.restart_error = None
        threading.Thread(target=reboot, daemon=True, name="dick-restart").start()
        return {"ok": True, "pid": os.getpid(), "boot": BOOT_ID, "command": argv,
                "delay": delay, "message": f"{delay:.1f} 秒后重启服务，页面稍后自动恢复。"}

    # ---------------------------------------------------------------- AI 翻译

    def ai_status(self, query, body):
        return self.translator.describe()

    def ai_save(self, query, body):
        allowed = {"enabled", "api", "base_url", "model", "api_key", "target", "prompt", "timeout"}
        values = {}
        for key, value in body.items():
            if key not in allowed:
                raise DickError(f"未知设置项：{key}")
            if key == "enabled":
                if not isinstance(value, bool):
                    raise DickError("enabled 必须是布尔值")
                values[key] = value
            elif key == "api":
                if not isinstance(value, str) or value not in AI_APIS:
                    raise DickError("api 只能是 " + "、".join(AI_APIS) + " 之一")
                values[key] = value
            elif key == "timeout":
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    raise DickError("timeout 必须是数字") from None
                if not math.isfinite(number) or number <= 0:
                    raise DickError("timeout 必须大于零")
                values[key] = int(number) if number.is_integer() else number
            elif not isinstance(value, str):
                raise DickError(f"{key} 必须是字符串")
            elif key == "api_key" and value == "":
                continue  # 留空表示不改动已保存的密钥
            else:
                values[key] = value
        if not values:
            raise DickError("没有要保存的设置")
        self.settings.set_ai(values)
        self.translator = Translator(self.settings)
        return self.translator.describe()

    def ai_test(self, query, body):
        text = body.get("text") or "Firefox is a fast, private and safe web browser."
        if not isinstance(text, str):
            raise DickError("text 必须是字符串")
        return {"translation": self.translator.test(text), "target": self.translator.target()}

    def ai_models(self, query, body):
        """按当前（或表单里临时填的）地址拉一份模型列表，方便设置页直接挑。"""
        ai = dict(self.settings.ai)
        for key in ("base_url", "api", "api_key"):
            value = body.get(key)
            if not isinstance(value, str):
                continue
            if key == "api":
                value = value.strip().lower()
                if value not in AI_APIS:
                    raise DickError("api 只能是 " + "、".join(AI_APIS) + " 之一")
            if key == "api_key" and not value.strip():
                continue  # 留空表示用已保存的密钥
            ai[key] = value.strip() if key != "api_key" else value
        probe = SimpleNamespace(ai=ai, cache_dir=self.settings.cache_dir)
        translator = Translator(probe)
        return {"models": translator.models(), "endpoint": ai_endpoint(ai.get("base_url", ""),
                                                                      ai.get("api") or "openai")}

    def translate(self, query, body):
        texts = body.get("texts")
        if not isinstance(texts, list) or not texts:
            raise DickError("translate 需要 texts 列表")
        target = body.get("target")
        if target is not None and not isinstance(target, str):
            raise DickError("target 必须是字符串")
        return {"translations": self.translator.translate(texts, target),
                "target": self.translator.target(target)}


class Handler(BaseHTTPRequestHandler):
    server_version = f"DICK/{__version__}"
    protocol_version = "HTTP/1.1"

    @property
    def app(self):
        return self.server.app

    def log_message(self, fmt, *args):
        print(f"[web] {self.address_string()} {fmt % args}", file=sys.stderr)

    def handle_one_request(self):
        try:
            return super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError):
            # 手机/浏览器刷新或关页面时会直接掐断连接，这不值得打一整串 traceback。
            self.close_connection = True

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def authorized(self, query):
        """访问令牌：`X-Dick-Token` 头或 `?token=`；服务器没配令牌就不校验。"""
        expected = getattr(self.server, "token", "") or ""
        if not expected:
            return True
        presented = self.headers.get("X-Dick-Token") or query.get("token") or ""
        return hmac.compare_digest(presented, expected)

    def dispatch(self, method):
        parsed = urlparse(self.path)
        path = parsed.path
        if len(path) > 1 and path.endswith("/"):
            path = path.rstrip("/")
        query = {key: values[-1] for key, values in parse_qs(parsed.query, keep_blank_values=True).items()}
        try:
            if method == "GET" and path in STATIC:  # 界面骨架不带数据，允许先加载再要令牌
                return self.serve_static(path)
            if not self.authorized(query):
                return self.respond_json({"error": "需要访问令牌：在宿主终端的启动横幅里查看",
                                          "code": "token"}, HTTPStatus.UNAUTHORIZED)
            body = self.read_body() if method == "POST" else {}
            handler = self.app.routes.get((method, path))
            if handler is None and method == "GET" and path.startswith("/api/job/"):
                try:
                    since = int(query.get("since", 0))
                except (TypeError, ValueError):
                    since = 0
                return self.respond_json(self.app.job_by_id(path.rsplit("/", 1)[-1]).snapshot(since))
            if method == "GET" and path == "/api/icon":  # 图标可以长缓存
                content, content_type = self.app.icon(query, {})
                return self.respond_bytes(content, content_type, cache="public, max-age=604800")
            if handler is None:
                return self.respond_json({"error": f"未知接口：{method} {path}"}, HTTPStatus.NOT_FOUND)
            result = handler(query, body)
            return self.respond_json(result)
        except DickError as error:
            return self.respond_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        except Exception as error:  # noqa: BLE001 - 一个请求出错不该拖垮服务
            import traceback
            traceback.print_exc()
            return self.respond_json({"error": f"内部错误：{type(error).__name__}: {error}"},
                                     HTTPStatus.INTERNAL_SERVER_ERROR)

    def read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise DickError("Content-Length 非法") from None
        if length > 2 * 1024 * 1024:
            raise DickError("请求体过大")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        if content_type and content_type != "application/json":
            raise DickError("请求体需要 Content-Type: application/json")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as error:
            raise DickError(f"请求体不是合法 JSON：{error}") from error
        if not isinstance(payload, dict):
            raise DickError("请求体必须是 JSON 对象")
        return payload

    def serve_static(self, path):
        name, content_type = STATIC[path]
        try:
            content = (WEBUI / name).read_bytes()
        except OSError as error:
            return self.respond_json({"error": f"界面资源缺失：{error}"}, HTTPStatus.INTERNAL_SERVER_ERROR)
        return self.respond_bytes(content, content_type, cache="no-store")

    def respond_json(self, payload, status=HTTPStatus.OK):
        content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return self.respond_bytes(content, "application/json; charset=utf-8", status)

    def respond_bytes(self, content, content_type, status=HTTPStatus.OK, cache=None):
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", cache or "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(content)
        except (BrokenPipeError, ConnectionResetError):
            pass


def serve(settings, host=None, port=None, open_browser=False, offline=False,
          tls=None, token=None, no_token=False):
    host = host or settings.web_host
    port = port or settings.web_port
    tls = settings.web_tls if tls is None else tls
    app = WebApp(settings, offline=offline)

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    try:
        server = Server((host, port), Handler)
    except OSError as error:
        raise DickError(f"无法监听 {host}:{port}：{error}") from error
    server.app = app
    scheme = "http"
    if tls:
        cert, key = (Path(settings.web_cert), Path(settings.web_key)) if settings.web_cert else (None, None)
        if cert is None:  # 默认把自签证书放在配置文件旁边的 tls/ 目录
            cert, key = ensure_certificate(Path(settings.config_path).parent / "tls", host)
        try:
            server.socket = ssl_context(cert, key).wrap_socket(server.socket, server_side=True)
        except (OSError, ssl.SSLError) as error:
            server.server_close()
            raise DickError(f"无法启用 HTTPS：{error}") from error
        scheme = "https"
    access, fresh = resolve_token(settings, token, no_token)
    server.token = access
    display = host if ":" not in host else f"[{host}]"
    print(f"DICK Web GUI：{scheme}://{display}:{port}", file=sys.stderr)
    if access:
        where = f"，已写入 {Path(settings.config_path).parent / 'web-token'}" if fresh else ""
        print(f"访问令牌：{access}{where}", file=sys.stderr)
        print("别的设备第一次打开会要求输入一次这个令牌；本机浏览器只需输一次。", file=sys.stderr)
    else:
        print("访问令牌：已关闭（--no-token）；请只在本机回环上这样用。", file=sys.stderr)
    if tls:
        print("HTTPS 用的是自签证书：浏览器会警告一次，选择继续访问即可。", file=sys.stderr)
    print(f"配置：{settings.config_path}；缓存：{settings.cache_dir}", file=sys.stderr)
    print("按 Ctrl+C 停止。", file=sys.stderr)
    if open_browser:
        url = f"{scheme}://{display if host not in {'0.0.0.0', '::'} else '127.0.0.1'}:{port}"
        if access:
            url += f"/?token={quote(access)}"
        threading.Thread(target=lambda: webbrowser.open(url), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。", file=sys.stderr)
    finally:
        server.server_close()
    return 0
