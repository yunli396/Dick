import io
import tarfile
import xml.etree.ElementTree as ET

from .models import DickError, Package
from .network import decompress


def strip_nix_attribute(attribute):
    """把 Nix 属性路径缩成包名。

    `legacyPackages.x86_64-linux.python3Packages.requests` → `python3Packages.requests`；
    结果是 `nix profile install nixpkgs#<包名>` 能直接用的形式。
    """
    parts = attribute.strip().split(".")
    if len(parts) > 2 and parts[0] in {"legacyPackages", "packages"}:
        parts = parts[2:]
    return ".".join(parts)


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
