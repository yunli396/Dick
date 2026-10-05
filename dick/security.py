"""Web 界面的安全设施：自签证书与访问令牌。

为什么不在前端「自己加密」再走明文 HTTP：页面本身、加密函数和密钥都经过同一根网线，
同网段的人可以拿到密钥解密，甚至可以改掉页面直接偷密码。缺的是「证明页面没被改过」，
那只能靠 TLS。所以这里生成自签证书（首次访问时在浏览器里确认一次），再叠一层访问令牌，
让同网段的人连已安装列表都翻不到。
"""

import json
import os
import secrets
import shutil
import socket
import ssl
import subprocess
from pathlib import Path

from .models import DickError

TOKEN_BYTES = 6  # 12 个十六进制字符：手机上手输一次不至于太痛苦
CERT_NAME = "server.crt"
KEY_NAME = "server.key"
META_NAME = "server.json"
OPENSSL_DAYS = "365"


def interface_addresses():
    """本机各网卡的 IPv4 地址（Linux 上用 ioctl 读，拿不到就返回空）。

    只靠「默认路由探测」是不够的：装了 VPN / 虚拟网卡时，内核挑中的可能是
    198.18.0.1 这类隧道地址，手机访问的局域网地址反而不在证书里。
    """
    try:
        import fcntl
        import struct
    except ImportError:  # pragma: no cover - 非 Unix 平台
        return []
    found = []
    try:
        names = [name for _, name in socket.if_nameindex()]
    except (OSError, AttributeError):  # pragma: no cover - 极端环境
        return found
    for name in names:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                packed = fcntl.ioctl(sock.fileno(), 0x8915,  # SIOCGIFADDR
                                     struct.pack("256s", name.encode("utf-8")[:15]))
            address = socket.inet_ntoa(packed[20:24])
        except OSError:
            continue
        if address and address != "0.0.0.0":
            found.append(address)
    return found


def certificate_names(host=None):
    """证书要覆盖的主机名与 IP，免得浏览器因为名字不匹配而根本不给放行。"""
    dns = ["localhost"]
    addresses = ["127.0.0.1"]
    if host and host not in {"0.0.0.0", "::", ""}:
        (addresses if host.replace(".", "").isdigit() else dns).append(host)
    try:
        name = socket.gethostname()
    except OSError:  # pragma: no cover - 极端环境
        name = ""
    if name and name not in dns:
        dns.append(name)
    try:
        for info in socket.getaddrinfo(name or "localhost", None, socket.AF_INET):
            addresses.append(info[4][0])
    except OSError:
        pass
    for address in interface_addresses():
        if address not in addresses:
            addresses.append(address)
    try:  # 让内核挑一张默认网卡，得到局域网地址（不真的发包）
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("192.0.2.1", 9))
            addresses.append(probe.getsockname()[0])
        finally:
            probe.close()
    except OSError:
        pass
    return {"dns": list(dict.fromkeys(dns)), "ip": list(dict.fromkeys(addresses))}


def ensure_certificate(directory, host=None):
    """生成（或复用）自签证书；局域网地址变了就重新生成，返回 (证书, 私钥)。"""
    directory = Path(directory)
    cert_path, key_path, meta_path = directory / CERT_NAME, directory / KEY_NAME, directory / META_NAME
    names = certificate_names(host)
    if cert_path.exists() and key_path.exists():
        try:
            if json.loads(meta_path.read_text(encoding="utf-8")) == names:
                return cert_path, key_path
        except (OSError, ValueError):
            pass
    openssl = shutil.which("openssl")
    if openssl is None:
        raise DickError("--tls 需要用 openssl 生成自签证书，请先安装 openssl")
    try:
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
    except OSError as error:
        raise DickError(f"无法创建证书目录 {directory}：{error}") from error
    subject = ",".join([*(f"DNS:{item}" for item in names["dns"]), *(f"IP:{item}" for item in names["ip"])])
    command = [openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", OPENSSL_DAYS,
               "-keyout", str(key_path), "-out", str(cert_path), "-subj", "/CN=dick",
               "-addext", f"subjectAltName={subject}"]
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise DickError(f"生成自签证书失败：{result.stderr.strip() or f'退出码 {result.returncode}'}")
    try:
        os.chmod(key_path, 0o600)
    except OSError:
        pass
    meta_path.write_text(json.dumps(names, ensure_ascii=False), encoding="utf-8")
    return cert_path, key_path


def ssl_context(cert, key):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        context.load_cert_chain(str(cert), str(key))
    except (OSError, ssl.SSLError) as error:
        raise DickError(f"无法加载证书 {cert}：{error}") from error
    return context


def token_path(settings):
    return Path(settings.config_path).parent / "web-token"


def resolve_token(settings, token=None, disabled=False):
    """访问令牌：命令行 > 配置 > 令牌文件 > 新生成并落盘。返回 (令牌, 是否刚生成)。

    空字符串表示不校验（`dick web --no-token`），只在明确要求时才这样。
    """
    if disabled:
        return "", False
    if token:
        return token.strip(), False
    configured = (getattr(settings, "web_token", "") or "").strip()
    if configured:
        return configured, False
    path = token_path(settings)
    try:
        existing = path.read_text(encoding="utf-8").strip()
    except OSError:
        existing = ""
    if existing:
        return existing, False
    fresh = secrets.token_hex(TOKEN_BYTES)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(fresh + "\n", encoding="utf-8")
        os.chmod(path, 0o600)
    except OSError as error:
        raise DickError(f"无法写入访问令牌 {path}：{error}") from error
    return fresh, True
