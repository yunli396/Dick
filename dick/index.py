import hashlib
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
from urllib.parse import quote, urlencode, urljoin
import xml.etree.ElementTree as ET

from .discovery import native_output
from .models import DickError, Package
from .parsers import apt_packages, dnf_packages, pacman_packages


# Sources without a stable public index: queried on demand and kept in the TTL query cache.
QUERY_SOURCES = ("aur", "snap", "linyaps")


class Index:
    def __init__(self, settings, cache, client, repositories, report):
        self.settings = settings
        self.cache = cache
        self.client = client
        self.repositories = repositories
        self.report = report
        self.report_lock = threading.Lock()

    def _report(self, message):
        with self.report_lock:
            self.report(message)

    def _dnf_urls(self, repository):
        urls = list(repository.urls)
        if repository.mirrorlist:
            text = self.client.get(repository.mirrorlist).decode("utf-8", errors="replace")
            urls.extend(line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#"))
        elif repository.metalink:
            try:
                document = ET.fromstring(self.client.get(repository.metalink))
                for element in document.iter():
                    if element.tag.rsplit("}", 1)[-1] == "url" and element.text:
                        url = element.text.strip()
                        if url.endswith("repodata/repomd.xml"):
                            urls.append(url[:-len("repodata/repomd.xml")])
            except ET.ParseError as error:
                raise DickError(f"DNF metalink 格式错误：{error}") from error
        return list(dict.fromkeys(urls))

    def _dnf_primary(self, base):
        base = base.rstrip("/") + "/"
        try:
            document = ET.fromstring(self.client.get(urljoin(base, "repodata/repomd.xml")))
        except ET.ParseError as error:
            raise DickError(f"DNF repomd 格式错误：{error}") from error
        for entry in document:
            if entry.attrib.get("type") in {"modules", "modulemd"}:
                self._report("警告：DNF 仓库含模块流；搜索展示原始索引，安装由 dnf 检查启用流。")
        for entry in document:
            if entry.attrib.get("type") != "primary":
                continue
            fields = {child.tag.rsplit("}", 1)[-1]: child for child in entry}
            location = fields.get("location")
            if location is None or not location.attrib.get("href"):
                raise DickError("DNF primary 缺少 location")
            url = urljoin(base, location.attrib["href"])
            content = self.client.get(url)
            checksum = fields.get("checksum")
            if checksum is not None and checksum.text:
                algorithm = checksum.attrib.get("type", "sha256")
                if algorithm == "sha":
                    algorithm = "sha1"
                try:
                    digest = hashlib.new(algorithm, content).hexdigest()
                except ValueError as error:
                    raise DickError(f"不支持的 DNF 校验算法：{algorithm}") from error
                if digest != checksum.text.strip():
                    raise DickError("DNF primary 校验失败")
            return content
        raise DickError("DNF 仓库没有可用 primary XML 元数据")

    def refresh_repository(self, repository):
        if repository.source == "flatpak":
            output = native_output(["flatpak", "remote-ls", "--app", "--columns=application,name,description,version",
                                    repository.name], timeout=max(60, self.settings.timeout))
            packages = []
            for line in output.splitlines():
                parts = line.split("\t")
                if len(parts) >= 4 and parts[0]:
                    packages.append(Package(parts[0], "flatpak", parts[2] or parts[1], parts[3], repository.name))
                elif line.strip():
                    raise DickError("Flatpak 输出不是预期的四列 TSV")
            return self.cache.replace(repository, packages)
        parsers = {"pacman": pacman_packages, "apt": apt_packages, "dnf": dnf_packages}
        failures = []
        urls = self._dnf_urls(repository) if repository.source == "dnf" else repository.urls
        for url in urls:
            try:
                content = self._dnf_primary(url) if repository.source == "dnf" else self.client.get(url)
                return self.cache.replace(repository, parsers[repository.source](content, repository))
            except DickError as error:
                failures.append(str(error))
        raise DickError(f"{repository.key} 所有镜像失败：" + "；".join(failures or ["没有可用 URL"]))

    def refresh(self, sources, progress=None, workers=None):
        results, failures = [], []
        selected = [repository for repository in self.repositories if repository.source in sources]
        if progress is not None:
            progress.start()

        def refresh_one(repository):
            try:
                if repository.source in QUERY_SOURCES:
                    self.cache.invalidate_queries([repository.source])
                    result = {"source": repository.source, "repository": repository.name, "on_demand": True}
                    if progress is None or not progress.enabled:
                        self._report(f"{repository.key}：已清空按需查询缓存")
                else:
                    count = self.refresh_repository(repository)
                    result = {"source": repository.source, "repository": repository.name, "count": count}
                    if progress is None or not progress.enabled:
                        self._report(f"{repository.key}：{count} 个包")
                return result, None
            except DickError as error:
                return None, str(error)

        if selected:
            max_workers = min(len(selected), workers or self.settings.workers)
            with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="dick-index") as executor:
                futures = {executor.submit(refresh_one, repository): repository for repository in selected}
                for future in as_completed(futures):
                    repository = futures[future]
                    try:
                        result, failure = future.result()
                    except Exception as error:
                        result, failure = None, f"{repository.key}：{error}"
                    if result is not None:
                        results.append(result)
                        if progress is not None:
                            progress.update(repository.name, True, f"{result.get('count', '按需')}")
                    else:
                        failures.append(failure)
                        self._report(f"警告：{failure}；保留上次成功索引")
                        if progress is not None:
                            progress.update(repository.name, False)
        if progress is not None:
            progress.finish()
        return results, failures

    def _aur(self, query, exact):
        key = ("info:" if exact else "search:") + query
        cached = self.cache.query_get("aur", key, self.settings.ttl)
        if cached is not None:
            return cached
        base = "https://aur.archlinux.org/rpc/v5/"
        url = base + ("info?" + urlencode({"arg[]": query}) if exact else
                      "search/" + quote(query, safe="") + "?" + urlencode({"by": "name-desc"}))
        try:
            payload = json.loads(self.client.get(url))
            if not isinstance(payload, dict) or payload.get("type") == "error":
                raise DickError(f"AUR RPC 错误：{payload.get('error', '无效响应') if isinstance(payload, dict) else '无效响应'}")
            if not isinstance(payload.get("results"), list) or any(
                not isinstance(value, dict) or not isinstance(value.get("Name"), str)
                or not isinstance(value.get("Description") or "", str)
                or not isinstance(value.get("Version", ""), str) for value in payload.get("results", [])
            ):
                raise DickError("AUR RPC 返回无效包记录")
            packages = [Package(value["Name"], "aur", value.get("Description") or "",
                                value.get("Version", ""), "aur") for value in payload["results"]]
        except (ValueError, TypeError, KeyError) as error:
            raise DickError(f"AUR RPC 响应无效：{error}") from error
        self.cache.query_put("aur", key, packages)
        return packages

    def _snap(self, query, exact):
        key = ("info:" if exact else "search:") + query
        cached = self.cache.query_get("snap", key, self.settings.ttl)
        if cached is not None:
            return cached
        output = native_output(["snap", "find", query])
        packages = []
        for line in output.splitlines()[1:]:
            parts = line.split(None, 4)
            if len(parts) >= 5 and (not exact or parts[0] == query):
                packages.append(Package(parts[0], "snap", parts[4], parts[1], "snap-store"))
        self.cache.query_put("snap", key, packages)
        return packages

    def _linyaps(self, query, exact):
        key = ("info:" if exact else "search:") + query
        cached = self.cache.query_get("linyaps", key, self.settings.ttl)
        if cached is not None:
            return cached
        output = native_output(["ll-cli", "--json", "search", query],
                               timeout=max(60, self.settings.timeout))
        try:
            payload = json.loads(output)
        except ValueError as error:
            raise DickError(f"ll-cli 未返回 JSON 搜索结果：{error}") from error
        if isinstance(payload, dict):
            records = [(repository, value) for repository, values in payload.items()
                       if isinstance(values, list) for value in values]
        elif isinstance(payload, list):
            records = [("", value) for value in payload]
        else:
            raise DickError("ll-cli search 返回了无效的搜索结果")
        lowered = query.casefold()
        packages = []
        for repository, record in records:
            if not isinstance(record, dict) or not isinstance(record.get("id"), str) or not record["id"]:
                raise DickError("ll-cli search 返回了无效的应用记录")
            if exact and not self._linyaps_matches(record, lowered):
                continue
            architecture = record.get("arch")
            if isinstance(architecture, list):
                architecture = ",".join(value for value in architecture if isinstance(value, str))
            packages.append(Package(
                record["id"], "linyaps",
                record["description"] if isinstance(record.get("description"), str) else "",
                record["version"] if isinstance(record.get("version"), str) else "",
                repository if isinstance(repository, str) else "",
                architecture if isinstance(architecture, str) else "",
            ))
        self.cache.query_put("linyaps", key, packages)
        return packages

    @staticmethod
    def _linyaps_matches(record, lowered):
        """Exact matches accept the full app id, its last segment, or the display name."""
        candidates = {record["id"].casefold(), record["id"].rsplit(".", 1)[-1].casefold()}
        if isinstance(record.get("name"), str):
            candidates.add(record["name"].casefold())
        return lowered in candidates

    def search(self, query, sources, exact=False):
        selected = [repository for repository in self.repositories if repository.source in sources]
        active = {(repository.source, repository.name) for repository in selected}
        failures = []
        for repository in selected:
            if repository.source not in QUERY_SOURCES and self.cache.snapshot(repository) is None:
                try:
                    self.refresh_repository(repository)
                except DickError as error:
                    failures.append(str(error))
        packages = [package for package in self.cache.search(query, sources, exact)
                    if (package.source, package.repository) in active]
        if exact and "flatpak" in sources:
            aliases = [package for package in self.cache.search(query, ["flatpak"])
                       if (package.source, package.repository) in active
                       and package.name.rsplit(".", 1)[-1].casefold() == query.casefold()]
            packages = list(dict.fromkeys([*packages, *aliases]))
        for source, loader in (("aur", self._aur), ("snap", self._snap), ("linyaps", self._linyaps)):
            if source not in sources or not any(repository.source == source for repository in selected):
                continue
            try:
                packages.extend(loader(query, exact))
            except DickError as error:
                failures.append(str(error))
        for failure in failures:
            self._report(f"警告：{failure}")
        order = {source: position for position, source in enumerate(self.settings.priority)}
        repository_order = {(repository.source, repository.name): position
                            for position, repository in enumerate(self.repositories)}
        packages.sort(key=lambda package: (package.name.lower() != query.lower(),
                                          package.name.lower(), order.get(package.source, 99),
                                          repository_order.get((package.source, package.repository), 99)))
        return packages, failures
