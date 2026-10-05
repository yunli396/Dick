import configparser
import glob
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess

from .models import DickError, Repository
from .parsers import control_records


def rooted(root, path):
    return root / str(path).lstrip("/")


def read_text(path):
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        raise DickError(f"无法读取源配置 {path}：{error}") from error


def pacman_repositories(settings):
    path = settings.root / "etc/pacman.conf"
    if not path.exists():
        return []
    servers = {}
    architecture = settings.architecture

    def parse(config, section="options", stack=()):
        nonlocal architecture
        resolved = config.resolve()
        if resolved in stack or len(stack) > 20:
            raise DickError(f"Pacman Include 循环或嵌套过深：{config}")
        for raw in read_text(config).splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1]
            elif "=" in line:
                key, value = [part.strip() for part in line.split("=", 1)]
                if key == "Include":
                    pattern = rooted(settings.root, value) if value.startswith("/") else config.parent / value
                    for included in sorted(glob.glob(str(pattern))):
                        section = parse(Path(included), section, (*stack, resolved))
                elif key == "Architecture" and section == "options" and value != "auto":
                    architecture = value.split()[0]
                elif key == "Server" and section != "options":
                    servers.setdefault(section, []).append(value)
        return section

    parse(path)
    return [Repository("pacman", name, tuple(dict.fromkeys(
        url.replace("$repo", name).replace("$arch", architecture).rstrip("/") + f"/{name}.db"
        for url in urls
    )), architecture) for name, urls in servers.items()]


def apt_repositories(settings):
    architecture = {"x86_64": "amd64", "aarch64": "arm64", "i686": "i386"}.get(
        settings.architecture, settings.architecture
    )
    directory = settings.root / "etc/apt/sources.list.d"
    paths = [settings.root / "etc/apt/sources.list", *sorted(directory.glob("*.list")),
             *sorted(directory.glob("*.sources"))]
    repositories = {}

    def add(uri, suite, components, architectures):
        if architectures and architecture not in architectures:
            return
        if suite.endswith("/"):
            components = [""]
        for component in components:
            base = uri.rstrip("/") + "/"
            if suite.endswith("/"):
                index = base + ("" if suite == "./" else suite) + "Packages"
            else:
                index = base + f"dists/{suite}/{component}/binary-{architecture}/Packages"
            digest = hashlib.sha256(index.encode()).hexdigest()[:12]
            name = f"{suite}/{component}@{digest}"
            repositories[name] = Repository("apt", name, tuple(index + suffix for suffix in (".xz", ".gz", "")),
                                             architecture, suite, component)

    for path in paths:
        if not path.exists():
            continue
        text = read_text(path)
        if path.suffix == ".sources":
            for record in control_records(text):
                if record.get("Enabled", "yes").lower() == "no" or "deb" not in record.get("Types", "").split():
                    continue
                for uri in record.get("URIs", "").split():
                    for suite in record.get("Suites", "").split():
                        add(uri, suite, record.get("Components", "").split(), record.get("Architectures", "").split())
        else:
            for line in text.splitlines():
                match = re.match(r"^\s*deb\s+(?:\[([^]]*)\]\s+)?(.*)", line)
                if not match:
                    continue
                options, rest = match.groups()
                parts = shlex.split(rest, comments=True)
                if len(parts) < 2:
                    continue
                architectures = []
                for option in (options or "").split():
                    if option.startswith("arch="):
                        architectures = option[5:].split(",")
                add(parts[0], parts[1], parts[2:], architectures)
    return list(repositories.values())


def dnf_repositories(settings):
    repositories = []
    variables = {"basearch": settings.architecture, "releasever": settings.releasever,
                 "arch": settings.architecture}

    def expand(value):
        for key, replacement in variables.items():
            value = value.replace("${" + key + "}", replacement).replace("$" + key, replacement)
        if "$" in value:
            raise DickError(f"DNF URL 含未解析变量：{value}")
        return value

    for path in sorted((settings.root / "etc/yum.repos.d").glob("*.repo")):
        parser = configparser.ConfigParser(interpolation=None, strict=False, inline_comment_prefixes=("#",))
        try:
            parser.read_string(read_text(path))
            for name in parser.sections():
                section = parser[name]
                if not section.getboolean("enabled", fallback=True):
                    continue
                urls = tuple(expand(url) for url in section.get("baseurl", "").split())
                mirrorlist = expand(section.get("mirrorlist", "").strip())
                metalink = expand(section.get("metalink", "").strip())
                if urls or mirrorlist or metalink:
                    repositories.append(Repository("dnf", name, urls, settings.architecture,
                                                   mirrorlist=mirrorlist, metalink=metalink))
        except (configparser.Error, ValueError) as error:
            raise DickError(f"DNF 配置无效 {path}：{error}") from error
    return repositories


def english_locale():
    """flatpak 这类命令的输出字段跟着 locale 走（连 JSON 键都会被翻译），解析前统一按 C 语言跑。"""
    env = dict(os.environ)
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    return env


def native_output(command, timeout=60, env=None):
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=timeout, env=env)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise DickError(f"命令失败 {command[0]}：{error}") from error
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise DickError(f"命令失败 {' '.join(command)}：{detail}")
    return result.stdout


def linyaps_repositories(settings):
    fallback = [Repository("linyaps", "linglong", ())]
    try:
        payload = json.loads(native_output(["ll-cli", "--json", "repo", "show"]))
    except (DickError, ValueError):
        return fallback
    entries = payload.get("repos") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return fallback
    repositories = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str) or not entry["name"].strip():
            continue
        url = entry.get("url")
        repository = Repository("linyaps", entry["name"].strip(),
                                (url.strip(),) if isinstance(url, str) and url.strip() else ())
        if repository not in repositories:
            repositories.append(repository)
    return repositories or fallback


def discover(settings, respect_enabled=True, native=True):
    """收集可用仓库。

    respect_enabled 为真时跳过被 `dick source disable` 关闭的来源；native 为假时
    不执行任何原生命令（供 `dick source list` 快速查看配置状态）。
    """

    def enabled(source):
        return settings.enabled(source) if respect_enabled else True

    repositories, errors = [], []
    for source, loader in (("pacman", pacman_repositories), ("apt", apt_repositories), ("dnf", dnf_repositories)):
        if not enabled(source):
            continue
        try:
            repositories.extend(loader(settings))
        except (DickError, ValueError) as error:
            errors.append(f"{source}：{error}")
    if enabled("aur") and settings.family == "arch":
        repositories.append(Repository("aur", "aur", ("https://aur.archlinux.org/rpc/v5",)))
    if native and enabled("flatpak") and settings.available("flatpak"):
        try:
            for line in native_output(["flatpak", "remotes", "--columns=name,url"]).splitlines():
                parts = line.split("\t")
                if len(parts) >= 2:
                    repository = Repository("flatpak", parts[0], (parts[1],))
                    if repository not in repositories:
                        repositories.append(repository)
        except DickError as error:
            errors.append(str(error))
    if enabled("snap") and settings.available("snap"):
        repositories.append(Repository("snap", "snap-store", ("https://api.snapcraft.io",)))
    if native and enabled("linyaps") and settings.available("linyaps"):
        repositories.extend(linyaps_repositories(settings))
    return repositories, errors
