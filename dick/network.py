import bz2
import gzip
import io
import lzma
import shutil
import subprocess
import zlib
from pathlib import PurePosixPath
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .models import DickError


class HTTPClient:
    def __init__(self, timeout=20, max_bytes=64 * 1024 * 1024):
        self.timeout = timeout
        self.max_bytes = max_bytes

    def get(self, url):
        if urlparse(url).scheme not in {"http", "https", "file"}:
            raise DickError(f"不支持的索引 URL：{url}")
        request = Request(url, headers={"User-Agent": "DICK/0.1", "Accept-Encoding": "identity"})
        try:
            with urlopen(request, timeout=self.timeout) as response:
                content = response.read(self.max_bytes + 1)
        except (HTTPError, URLError, OSError, ValueError) as error:
            raise DickError(f"下载失败 {url}：{error}") from error
        if len(content) > self.max_bytes:
            raise DickError(f"索引超过下载限制：{url}")
        return content


def decompress(content, url="", limit=256 * 1024 * 1024):
    suffix = PurePosixPath(urlparse(url).path).suffix
    try:
        if content.startswith(b"\x1f\x8b"):
            handle = gzip.GzipFile(fileobj=io.BytesIO(content))
        elif content.startswith(b"\xfd7zXZ\x00"):
            handle = lzma.LZMAFile(io.BytesIO(content))
        elif content.startswith(b"BZh"):
            handle = bz2.BZ2File(io.BytesIO(content))
        elif suffix in {".zst", ".zstd"} or content.startswith(b"\x28\xb5\x2f\xfd"):
            return decompress_zstd(content, limit)
        else:
            handle = io.BytesIO(content)
        with handle:
            result = handle.read(limit + 1)
        if len(result) > limit:
            raise DickError("解压索引超过大小限制")
        return result
    except (OSError, EOFError, lzma.LZMAError, zlib.error) as error:
        raise DickError(f"损坏的压缩索引：{error}") from error


def decompress_zstd(content, limit=256 * 1024 * 1024):
    try:
        import compression.zstd as zstd
    except ImportError:
        zstd = None
    if zstd is not None:
        try:
            decompressor = zstd.ZstdDecompressor()
            result = decompressor.decompress(content, max_length=limit + 1)
            if len(result) > limit:
                raise DickError("解压索引超过大小限制")
            if not decompressor.eof:
                raise DickError("损坏的 zstd 索引：帧不完整")
            return result
        except DickError:
            raise
        except (EOFError, OSError, ValueError, zstd.ZstdError) as error:
            raise DickError(f"损坏的 zstd 索引：{error}") from error

    executable = shutil.which("zstd")
    if executable is None:
        raise DickError("当前 Python 没有 zstd 支持，且未找到 zstd 命令")
    try:
        result = subprocess.run([executable, "-q", "-d", "-c"], input=content,
                                capture_output=True, check=False)
    except OSError as error:
        raise DickError(f"无法执行 zstd：{error}") from error
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise DickError(f"损坏的 zstd 索引：{detail or '解压失败'}")
    if len(result.stdout) > limit:
        raise DickError("解压索引超过大小限制")
    return result.stdout
