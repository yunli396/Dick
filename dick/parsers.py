import io
import tarfile
import xml.etree.ElementTree as ET

from .models import DickError, Package
from .network import decompress

# AppStream 里带 xml:lang 的条目是译文；不带的那一份才是上游写给所有人看的源语言文本。
XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"


def strip_nix_attribute(attribute):
    """把 Nix 属性路径缩成包名。

    `legacyPackages.x86_64-linux.python3Packages.requests` → `python3Packages.requests`；
    结果是 `nix profile install nixpkgs#<包名>` 能直接用的形式。
    """
    parts = attribute.strip().split(".")
    if len(parts) > 2 and parts[0] in {"legacyPackages", "packages"}:
        parts = parts[2:]
    return ".".join(parts)


# `nix search` 与 `nix profile` 都是实验性命令。带上这两个全局开关（必须写在子命令之前），
# 用户不用先改 nix.conf 就能用；开关只是本次调用生效，不落盘。
NIX_FLAKE_FLAGS = ("--extra-experimental-features", "nix-command flakes")


def control_records(text):
    record = {}
    field = None
    for line in [*text.splitlines(), ""]:
        if not line.strip():
            if record:
                yield record
            record, field = {}, None
        elif line[:1].isspace() and field:
            record[field] += "\n" + line[1:]
        elif ":" in line:
            field, value = line.split(":", 1)
            record[field] = value.strip()


def apt_packages(content, repository):
    text = decompress(content).decode("utf-8", errors="replace")
    found = False
    for record in control_records(text):
        if "Package" in record:
            found = True
            yield Package(record["Package"], "apt", record.get("Description", ""),
                          record.get("Version", ""), repository.name,
                          record.get("Architecture", repository.architecture))
    if text.strip() and not found:
        raise DickError("APT 索引中没有 Package 字段")


def pacman_packages(content, repository):
    try:
        with tarfile.open(fileobj=io.BytesIO(decompress(content)), mode="r:") as archive:
            total = 0
            for member in archive:
                if not member.isfile() or member.name.rsplit("/", 1)[-1] != "desc":
                    continue
                total += member.size
                if member.size > 1024 * 1024 or total > 256 * 1024 * 1024:
                    raise DickError("Pacman desc 超过大小限制")
                handle = archive.extractfile(member)
                if handle is None:
                    continue
                with handle:
                    lines = handle.read().decode("utf-8", errors="replace").splitlines()
                fields = {}
                field = None
                for line in lines:
                    if line.startswith("%") and line.endswith("%"):
                        field = line.strip("%")
                        fields[field] = []
                    elif line and field:
                        fields[field].append(line)
                values = {key: "\n".join(value) for key, value in fields.items()}
                if values.get("NAME"):
                    yield Package(values["NAME"], "pacman", values.get("DESC", ""),
                                  values.get("VERSION", ""), repository.name,
                                  values.get("ARCH", repository.architecture))
    except (tarfile.TarError, OSError) as error:
        raise DickError(f"损坏的 Pacman 数据库：{error}") from error


def dnf_packages(content, repository):
    try:
        root_checked = False
        for event, element in ET.iterparse(io.BytesIO(decompress(content)), events=("start", "end")):
            if not root_checked:
                if element.tag.rsplit("}", 1)[-1] != "metadata":
                    raise DickError("DNF primary 的根节点不是 metadata")
                root_checked = True
            if event != "end":
                continue
            if element.tag.rsplit("}", 1)[-1] != "package":
                continue
            fields = {child.tag.rsplit("}", 1)[-1]: child for child in element}
            name = fields.get("name")
            version = fields.get("version")
            architecture = fields.get("arch")
            summary = fields.get("summary")
            if name is not None and name.text:
                attributes = version.attrib if version is not None else {}
                epoch = attributes.get("epoch", "0")
                rendered = attributes.get("ver", "")
                if attributes.get("rel"):
                    rendered += "-" + attributes["rel"]
                if epoch != "0":
                    rendered = epoch + ":" + rendered
                package_arch = architecture.text if architecture is not None else ""
                if package_arch in {repository.architecture, "noarch", ""}:
                    yield Package(name.text, "dnf", summary.text or "" if summary is not None else "",
                                  rendered, repository.name, package_arch or "")
            element.clear()
    except ET.ParseError as error:
        raise DickError(f"损坏的 DNF XML：{error}") from error


def apk_packages(content, repository):
    """解析 Alpine 的 APKINDEX.tar.gz。

    压缩包里有一个纯文本 APKINDEX：记录之间用空行分隔，字段是单字母（`P:` 名称、
    `V:` 版本、`T:` 描述、`A:` 架构、`D:` 依赖……），字段顺序不固定，`P:` 也不在首行。
    """
    try:
        with tarfile.open(fileobj=io.BytesIO(decompress(content)), mode="r:") as archive:
            for member in archive:
                if not member.isfile() or member.name.rsplit("/", 1)[-1] != "APKINDEX":
                    continue
                if member.size > 64 * 1024 * 1024:
                    raise DickError("APKINDEX 超过大小限制")
                handle = archive.extractfile(member)
                if handle is None:
                    continue
                with handle:
                    text = handle.read().decode("utf-8", errors="replace")
                found = False
                for record in control_records(text):
                    if not record.get("P"):
                        continue
                    found = True
                    yield Package(record["P"], "apk", record.get("T", ""),
                                  record.get("V", ""), repository.name,
                                  record.get("A", repository.architecture))
                if not found:
                    raise DickError("APKINDEX 中没有 P 字段")
                return
            raise DickError("APKINDEX.tar.gz 里没有 APKINDEX 文件")
    except (tarfile.TarError, OSError) as error:
        raise DickError(f"损坏的 APKINDEX.tar.gz：{error}") from error


def _appstream_version(releases):
    """AppStream 的版本号在 `<releases>` 里，一条一个 `<release version= date=>`。

    取日期最新的一条：上游是按日期倒序写的，但顺序并不保证，所以按 date 自己挑。
    """
    newest, newest_date = "", ""
    for release in releases:
        version = (release.attrib.get("version") or "").strip()
        if not version:
            continue
        date = (release.attrib.get("date") or "").strip()
        if not newest or date > newest_date:
            newest, newest_date = version, date
    return newest


def appstream_metadata(content):
    """解析 Flatpak 远程的 AppStream 目录（appstream.xml），返回 {组件 id: 字段}。

    `flatpak remote-ls --json` 只给 id、名字、分支和来源：版本永远是空串，更没有描述。
    这些信息只存在于远程仓库的 AppStream 里，所以 `dick update --source flatpak` 会拉一份
    来合并（大约 10 MB 压缩、50 MB 展开、解析一秒以内）。

    同一个 name/summary 会有一串 xml:lang 译文，只认不带 lang 的源语言版本，结果才不会随
    构建机的语言变化；实在没有源语言条目才退回第一条译文。

    categories 也从这里带出来（IDE / Development 用来识别开发工具，见 install.py）。
    """
    metadata = {}
    for _event, component in ET.iterparse(io.BytesIO(content), events=("end",)):
        if component.tag.rsplit("}", 1)[-1] != "component":
            continue
        fields = {"name": "", "summary": "", "version": "", "categories": ""}
        fallback = {}
        categories = []
        component_id = ""
        for child in component:
            tag = child.tag.rsplit("}", 1)[-1]
            if tag == "id":
                component_id = (child.text or "").strip()
            elif tag in {"name", "summary"}:
                text = " ".join((child.text or "").split())
                if not text:
                    continue
                if XML_LANG in child.attrib:
                    fallback.setdefault(tag, text)
                else:
                    fields[tag] = fields[tag] or text
            elif tag == "releases":
                fields["version"] = _appstream_version(child)
            elif tag == "categories":
                for category in child:
                    if category.tag.rsplit("}", 1)[-1] != "category":
                        continue
                    text = " ".join((category.text or "").split())
                    if text and text not in categories:
                        categories.append(text)
        if component_id:
            for tag in ("name", "summary"):
                fields[tag] = fields[tag] or fallback.get(tag, "")
            fields["categories"] = ",".join(categories)
            metadata.setdefault(component_id, fields)
        component.clear()
    return metadata
