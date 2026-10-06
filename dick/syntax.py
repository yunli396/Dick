import argparse
from dataclasses import dataclass

from . import __version__
from .config import SOURCES
from .models import DickError


HELP = """DICK — Detective Index Collection Kit

用法：dick [选项] <命令> [参数 ...]

命令：
  source list              列出已知来源及其启用/可用状态（不执行原生命令）
  source scan              重新扫描本机来源配置并列出识别到的仓库
  source enable  来源...   启用来源，写入配置文件
  source disable 来源...   禁用来源，写入配置文件
  install 包名...          安装，按来源优先级自动降级
  remove  包名...          卸载，扫描已安装的同名/相似包后询问卸载哪个
  list    [关键词...]      列出本机已安装的包
  search  关键词...        跨来源搜索
  update  [--source 来源]  刷新索引缓存
  upgrade [--source 来源]  整机升级（相当于 pacman -Syu）
  updateme                 更新 DICK 自己（install.sh 装的走 git + pip）
  removeme                 卸载 DICK 自己（默认保留配置，--purge 一起删）
  web                      启动本地 Web GUI（应用商店界面，默认 http://127.0.0.1:3907）

来源：pacman / aur / apt / dnf / apk / flatpak / linyaps
      guix / nixpkgs / snap

选项：--source 来源    限定来源，可重复
      --json           输出 JSON
      --dry-run        显示原生命令，不执行安装、卸载或升级
      -y, --yes        原生命令非交互确认，并跳过 remove 的确认
      --exact          搜索只做精确匹配（同时匹配 ID 别名）
      --deep           卸载时同时清理配置与孤立依赖
      --limit 数量     每个来源的列出上限（默认 50）
      --jobs 数量      索引并发数（默认配置为 4）
      --prefix 路径    updateme / removeme 操作的前缀（默认自动探测 ~/.local 等）
      --ref 分支       updateme 更新到哪个分支或标签（默认 main）
      --purge          removeme 时连配置与缓存一起删除
      --host 地址      web 监听地址（默认 127.0.0.1）
      --port 端口      web 监听端口（默认 3907）
      --open           启动 web 后尝试打开浏览器
      --tls            web 用自签证书开启 HTTPS（首次访问时在浏览器里确认一次）
      --token 令牌     web 访问令牌（默认读配置或 ~/.config/dick/web-token，首次自动生成）
      --no-token       web 不校验访问令牌（仅限本机回环使用）
      --config 路径    TOML 配置文件（source enable/disable 与 web 设置界面写入它）
      --cache-dir 路径 SQLite 缓存目录
      --root 路径      源配置读取根目录；此模式禁止执行系统变更
      --version        显示版本

示例：dick source list; dick search firefox; dick install firefox --dry-run; dick web
"""


@dataclass(frozen=True)
class Action:
    name: str
    targets: tuple[str, ...] = ()


def options(argv):
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--source", action="append", choices=SOURCES)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("-y", "--yes", "--noconfirm", action="store_true", dest="yes")
    parser.add_argument("--exact", action="store_true")
    parser.add_argument("--deep", action="store_true")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--jobs", type=int)
    parser.add_argument("--prefix")
    parser.add_argument("--ref")
    parser.add_argument("--purge", action="store_true")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--open", action="store_true", dest="open_browser")
    parser.add_argument("--tls", action="store_true", help="web 用自签证书开启 HTTPS")
    parser.add_argument("--token", help="web 访问令牌（默认读取配置或 ~/.config/dick/web-token）")
    parser.add_argument("--no-token", action="store_true", dest="no_token", help="web 不校验访问令牌")
    parser.add_argument("--config")
    parser.add_argument("--cache-dir")
    parser.add_argument("--root", default="/")
    parser.add_argument("--version", action="version", version=f"DICK {__version__}")
    parser.add_argument("-h", "--help", action="store_true")
    parsed, remaining = parser.parse_known_args(argv)
    if parsed.limit <= 0 or (parsed.jobs is not None and parsed.jobs <= 0):
        raise DickError("--limit 和 --jobs 必须大于零")
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise DickError("--port 必须在 1-65535 之间")
    return parsed, remaining


def normalize(arguments):
    if not arguments:
        raise DickError("缺少命令；使用 dick --help 查看用法")
    command, *rest = arguments
    if command == "source":
        return source_action(rest)
    if command == "web":
        if rest:
            raise DickError("web 不接受参数；使用 --host/--port 调整监听地址")
        return Action("web")
    if command not in {"install", "remove", "list", "search", "update", "upgrade", "updateme", "removeme"}:
        raise DickError(f"未知命令：{command}；使用 dick --help 查看用法")
    if any(target.startswith("-") or not target.strip() for target in rest):
        raise DickError("包名/关键词不能为空或以 - 开头；不支持的原生参数不会透传")
    if command in {"search", "list"}:
        if command == "search" and not rest:
            raise DickError("search 需要关键词")
        return Action(command, (" ".join(rest),) if rest else ())
    if command in {"update", "upgrade", "updateme", "removeme"}:
        if rest:
            raise DickError(f"{command} 不接受包名")
        return Action(command)
    if not rest:
        raise DickError(f"{command} 需要包名")
    return Action(command, tuple(rest))


def source_action(rest):
    if not rest:
        raise DickError("source 需要子命令：list / scan / enable / disable")
    subcommand, *targets = rest
    if subcommand in {"list", "scan"}:
        if targets:
            raise DickError(f"source {subcommand} 不接受参数")
        return Action(f"source_{subcommand}")
    if subcommand in {"enable", "disable"}:
        if not targets:
            raise DickError(f"source {subcommand} 需要来源名称：" + ", ".join(SOURCES))
        for target in targets:
            if target not in SOURCES:
                raise DickError(f"未知来源：{target}；可选：" + ", ".join(SOURCES))
        return Action(f"source_{subcommand}", tuple(targets))
    raise DickError(f"未知 source 子命令：{subcommand}；可选：list / scan / enable / disable")
