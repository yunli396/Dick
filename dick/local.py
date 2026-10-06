"""读取各原生管理器里已安装的软件包。

这里只做“读”，卸载命令由 Installer 负责。AUR 没有独立的数据库，Arch 上
`pacman -Qm` 列出的是同步数据库之外的外来包（AUR 或手工构建），因此与
`pacman -Qn`（同步数据库内的原生包）分开，避免同一个包被列出两次。
"""

import json

from .discovery import native_output, rooted
from .models import DickError, LocalPackage
from .parsers import NIX_FLAKE_FLAGS, control_records, strip_nix_attribute


LIST_COMMANDS = {
    "pacman": ["pacman", "-Qn"],
    "aur": ["pacman", "-Qm"],
    "apt": ["dpkg-query", "-W", "-f=${binary:Package}\t${Version}\t${db:Status-Abbrev}\t${binary:Summary}\n"],
    "dnf": ["rpm", "-qa", "--qf", "%{NAME}\t%{VERSION}-%{RELEASE}\t%{SUMMARY}\n"],
    "flatpak": ["flatpak", "list", "--app", "--columns=application,version"],
    "snap": ["snap", "list"],
    "linyaps": ["ll-cli", "--json", "list", "--type=app"],
    "guix": ["guix", "package", "--list-installed"],
    "nixpkgs": ["nix", *NIX_FLAKE_FLAGS, "profile", "list", "--json"],
}

# 没有安装任何包时这些命令会以非零状态退出，属于正常情况。
EMPTY_IS_NORMAL = {"aur"}


def _parse_pacman(output, source):
    packages = []
    for line in output.splitlines():
        fields = line.split(None, 1)
        if fields and fields[0]:
            packages.append(LocalPackage(source, fields[0], fields[1].strip() if len(fields) > 1 else ""))
    return packages


def _parse_dpkg(output):
    packages = []
    for line in output.splitlines():
        fields = [field.strip() for field in line.split("\t")]
        if len(fields) < 4:
            continue
        # dpkg 的状态缩写为「期望动作 + 当前状态 + 错误标记」三段；
        # i 表示已安装，H 表示已解包但半安装（仍可用 purge 清理），rc 是只剩配置。
        if fields[2][:1] == "i" and fields[2][1:2] in {"i", "H"}:
            packages.append(LocalPackage("apt", fields[0], fields[1], fields[3]))
    return packages


def _parse_rpm(output):
    packages = []
    for line in output.splitlines():
        fields = [field.strip() for field in line.split("\t")]
        if len(fields) >= 3 and fields[0]:
            packages.append(LocalPackage("dnf", fields[0], fields[1], fields[2]))
    return packages


def _parse_flatpak(output):
    packages = []
    for line in output.splitlines():
        fields = [field.strip() for field in line.split("\t")]
        if len(fields) >= 2 and fields[0]:
            packages.append(LocalPackage("flatpak", fields[0], fields[1]))
    return packages


def _parse_snap(output):
    packages = []
    for index, line in enumerate(output.splitlines()):
        if index == 0 or line.startswith("No snaps are installed"):
            continue
        fields = line.split()
        if len(fields) >= 2:
            packages.append(LocalPackage("snap", fields[0], fields[1]))
    return packages


def _parse_linyaps(output):
    try:
        payload = json.loads(output)
    except ValueError as error:
        raise DickError(f"ll-cli 未返回 JSON 已安装列表：{error}") from error
    if not isinstance(payload, list):
        raise DickError("ll-cli list 返回了无效的已安装列表")
    packages = []
    for record in payload:
        if not isinstance(record, dict) or not isinstance(record.get("id"), str) or not record["id"]:
            raise DickError("ll-cli list 返回了无效的已安装记录")
        if record.get("kind") not in (None, "app"):
            continue
        packages.append(LocalPackage(
            "linyaps", record["id"],
            record["version"] if isinstance(record.get("version"), str) else "",
            record["description"] if isinstance(record.get("description"), str) else "",
        ))
    return packages


def _parse_apk_installed(text):
    """读 Alpine 的 /lib/apk/db/installed，字段格式和 APKINDEX 一样。"""
    packages = []
    for record in control_records(text):
        name = record.get("P", "")
        if not name:
            continue
        packages.append(LocalPackage("apk", name, record.get("V", ""), record.get("T", "")))
    return packages


def _parse_guix(output):
    """`guix package --list-installed` 输出四列：name version outputs location。"""
    packages = []
    for line in output.splitlines():
        if not line.strip():
            continue
        fields = line.split()
        if len(fields) < 2:
            raise DickError(f"guix 输出不是预期的四列表格：{line.strip()[:120]}")
        packages.append(LocalPackage("guix", fields[0], fields[1]))
    return packages


def _store_path_parts(store_paths):
    """store path 末段形如 `5z2yp3ysx8476c8g5w25b0smlgkjvaq3-hello-2.12.3`，去掉 32 位 hash 前缀后
    剩下 `<包名>-<版本>`。`nix profile list --json` 里没有 version 字段，版本只能从这里取。"""
    if not isinstance(store_paths, list):
        return "", ""
    for path in store_paths:
        if not isinstance(path, str) or not path.strip():
            continue
        base = path.strip().rstrip("/").rsplit("/", 1)[-1]
        head, _, tail = base.partition("-")
        if tail and len(head) == 32 and all(character in "0123456789abcdfghijklmnpqrsvwxyz" for character in head):
            base = tail
        name, separator, version = base.rpartition("-")
        if separator and name:
            # 版本号要以数字开头（`libfoo-unstable-2024-01-02` 这种取整个后缀）
            if version[:1].isdigit():
                return name, version
            return base, ""
    return "", ""


def _parse_nixpkgs(output):
    """`nix profile list --json` 里每个元素的属性路径去掉前缀就是包名。

    JSON 的形状（nix 2.35）是 `{"elements": {"<元素名>": {"attrPath": …, "storePaths": [...]}}}`：
    既没有 version 也没有 description，而且自己装自己的那个 `nix` 元素没有 attrPath——
    所以要退到字典键、再退到 store path 来取名字，版本从 store path 里解析。
    """
    try:
        payload = json.loads(output)
    except ValueError as error:
        raise DickError(f"nix profile list 未返回 JSON 已安装列表：{error}") from error
    if not isinstance(payload, dict):
        raise DickError("nix profile list 返回了无效的已安装列表")
    elements = payload.get("elements")
    if elements is None:
        elements = {}
    if isinstance(elements, dict):
        records = list(elements.items())
    elif isinstance(elements, list):
        records = [(None, record) for record in elements]
    else:
        raise DickError("nix profile list 返回了无效的已安装列表")
    packages = []
    for key, record in records:
        version = ""
        description = ""
        if isinstance(record, str):
            name = strip_nix_attribute(record)
        elif isinstance(record, dict):
            attribute = record.get("attrPath") or record.get("name") or (key if isinstance(key, str) else "")
            name = strip_nix_attribute(attribute) if isinstance(attribute, str) else ""
            if isinstance(record.get("version"), str):
                version = record["version"]
            if isinstance(record.get("description"), str):
                description = record["description"]
            stored_name, stored_version = _store_path_parts(record.get("storePaths"))
            name = name or stored_name
            version = version or stored_version
        else:
            continue
        if name:
            packages.append(LocalPackage("nixpkgs", name, version, description))
    return packages


PARSERS = {
    "pacman": lambda output: _parse_pacman(output, "pacman"),
    "aur": lambda output: _parse_pacman(output, "aur"),
    "apt": _parse_dpkg,
    "dnf": _parse_rpm,
    "flatpak": _parse_flatpak,
    "snap": _parse_snap,
    "linyaps": _parse_linyaps,
    "guix": _parse_guix,
    "nixpkgs": _parse_nixpkgs,
}

# 有些来源的已安装清单就是本地数据库文件：直接读比跑命令更稳，配 --root 也能用。
FILE_LISTS = {
    "apk": ("lib/apk/db/installed", _parse_apk_installed),
}


def read_installed(settings, source):
    """返回某个来源里已安装的包；命令失败时抛 DickError。"""
    if source in FILE_LISTS:
        relative, parser = FILE_LISTS[source]
        path = rooted(settings.root, relative)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return []
        except OSError as error:
            raise DickError(f"无法读取 {path}：{error}") from error
        return parser(text)
    try:
        output = native_output(LIST_COMMANDS[source], timeout=max(60, int(settings.timeout)))
    except DickError:
        if source in EMPTY_IS_NORMAL:
            return []
        raise
    return PARSERS[source](output)


def matches(package, lowered):
    """同名或相似名匹配：完整名称、ID 末段、或名称中包含关键词。"""
    name = package.name.casefold()
    return lowered in name or name.rsplit(".", 1)[-1] == lowered


def rank(package, lowered):
    name = package.name.casefold()
    if name == lowered:
        return 0
    if name.rsplit(".", 1)[-1] == lowered:
        return 1
    if name.startswith(lowered):
        return 2
    return 3
