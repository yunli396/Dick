import json
import os
from pathlib import Path
import select
import sqlite3
import sys

from .cache import Cache
from .config import SOURCES, Settings
from .discovery import discover
from .index import Index
from .install import AUR_HELPER_HINT, Installer, aur_helper, is_development_tool
from .local import matches, rank, read_installed
from .models import DickError
from .network import HTTPClient
from .progress import Progress
from .syntax import HELP, normalize, options


SOURCE_VIEWS = {"source_list", "source_scan"}
MUTATING = {"install", "remove", "upgrade"}


def report(message):
    print(message, file=sys.stderr)


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def print_packages(packages):
    for package in packages:
        description = " ".join(package.description.split())
        print(f"[{package.source}/{package.repository}] {package.name} {package.version}\n  {description}")
        if is_development_tool(package):
            print("  ⚠ IDE/开发工具：flatpak 沙箱访问不到系统级工具链（编译器、SDK、容器），做开发建议用原生源安装。")


def check_enabled(settings, args, action):
    if not args.source or action.name in SOURCE_VIEWS:
        return
    disabled = [source for source in args.source if not settings.enabled(source)]
    if disabled:
        raise DickError("来源已禁用：" + "、".join(disabled)
                        + "；先运行 dick source enable " + " ".join(disabled))


def update_sources(settings, action, args):
    current = set(settings.enabled_sources)
    changed = set(action.targets)
    if action.name == "source_enable":
        current |= changed
    else:
        current -= changed
    wanted = sorted(current, key=SOURCES.index)
    settings.set_enabled(wanted)
    if args.json:
        emit({"enabled": wanted, "config": str(settings.config_path)})
        return 0
    for target in action.targets:
        print(f"{target}：{'已启用' if target in wanted else '已禁用'}")
        if target in wanted and not settings.available(target):
            report(f"提示：未检测到 {target} 所需的原生命令，该来源暂时不可用")
    print(f"配置已更新：{settings.config_path}")
    return 0


def run_autorank(settings, action, args):
    """`source autorank [enable|disable]`：只看状态或开关自动来源排序。"""
    if not action.targets:
        state = "已开启" if settings.autorank else "已关闭"
        if args.json:
            emit({"autorank": settings.autorank, "config": str(settings.config_path)})
            return 0
        print(f"自动来源排序：{state}")
        print("  开启时：安装 IDE/开发类的 flatpak 候选会自动排到原生源之后"
              "（沙箱访问不到系统级工具链）。")
        print("  关闭后：安装完全按配置文件里的顺序来，可用 dick source ranking 手工排序。")
        return 0
    enabled = action.targets[0] == "enable"
    settings.set_autorank(enabled)
    if args.json:
        emit({"autorank": settings.autorank, "config": str(settings.config_path)})
        return 0
    print(f"自动来源排序：{'已开启' if enabled else '已关闭'}（已写入 {settings.config_path}）")
    if enabled:
        print("安装时会自动把 IDE/开发类的 flatpak 候选排到原生源之后。")
    else:
        print("安装时不再自动重排来源；可用 dick source ranking 手工调整顺序。")
    return 0


# 终端发来的转义序列（CSI / SS3）到按键名的映射。带修饰键的箭头是 CSI 1;2A 这种形式，
# 所以不能只看最后两个字节。
SEQUENCE_KEYS = {
    "[A": "up", "[B": "down", "[C": "right", "[D": "left",
    "OA": "up", "OB": "down", "OC": "right", "OD": "left",
    "[1;2A": "shift-up", "[1;2B": "shift-down",
}


def read_key(descriptor):
    """读一个按键，把转义序列还原成 up / down / shift-up 这类按键名。

    直接读文件描述符（而不是 sys.stdin）是必要的：raw 模式下 STDIN 已经禁用了行缓冲，
    但 Python 的文本层还会自己预读，真用 sys.stdin.read 的话转义序列的后续字节会留在
    Python 的缓冲区里，select 就再也等不到它们了。
    """
    character = os.read(descriptor, 1).decode("utf-8", "ignore")
    if character != "\x1b":
        return character
    sequence = b""
    while len(sequence) < 8 and select.select([descriptor], [], [], 0.05)[0]:
        sequence += os.read(descriptor, 1)
        if sequence[:1] == b"[":  # CSI：一直读到结束字节（0x40-0x7e）
            if len(sequence) > 1 and 0x40 <= sequence[-1] <= 0x7E:
                break
        elif sequence[:1] == b"O":  # SS3：ESC O 后面只跟一个字节
            if len(sequence) > 1:
                break
        else:  # Esc 后面不是 CSI / SS3（Alt+字母之类）：就此打住
            break
    if not sequence:
        return "escape"  # 单独的 Esc：用户按的是取消，不是方向键
    return SEQUENCE_KEYS.get(sequence.decode("utf-8", "ignore"), "escape")


def ranking_key(order, position, key):
    """处理一次按键，返回 (来源顺序, 高亮位置, 动作)；动作是 None / "save" / "cancel"。

    方向键只挪高亮，挪来源是另外的键（J/K 或 Shift+↑/↓）。两者分开才不会出现
    「按住 ↓ 永远在搬同一个来源、想去碰别的却够不着」这种使不上劲的手感。
    """
    last = len(order) - 1
    if key in {"down", "j"}:
        return order, min(position + 1, last), None
    if key in {"up", "k"}:
        return order, max(position - 1, 0), None
    if key in {"J", "shift-down"} and position < last:
        order[position], order[position + 1] = order[position + 1], order[position]
        return order, position + 1, None
    if key in {"K", "shift-up"} and position > 0:
        order[position], order[position - 1] = order[position - 1], order[position]
        return order, position - 1, None
    if key in {"\r", "\n"}:
        return order, position, "save"
    if key in {"q", "escape", "\x03"}:
        return order, position, "cancel"
    return order, position, None


def draw_ranking(order, position):
    """重排界面：清屏重画，免去记行数和处理 raw 模式下的换行。"""
    lines = [
        "↑/↓（或 k/j）移动高亮，J / K（或 Shift+↑/↓）把高亮的来源上下挪，Enter 保存，q 取消：",
        "",
    ]
    for index, source in enumerate(order):
        lines.append(f" {'▶' if index == position else ' '} {index + 1}. {source}")
    lines.append("")
    lines.append("只有本机可用的来源列在这里；不可用的来源会保留在配置里、排在最后（snap 恒垫底）。")
    sys.stdout.write("\x1b[2J\x1b[H" + "\r\n".join(lines) + "\r\n")
    sys.stdout.flush()


def rank_sources(sources):
    """方向键 TUI：返回排好的来源顺序，用户取消时返回 None。"""
    try:
        import termios
        import tty
    except ImportError:  # pragma: no cover - 只在非类 Unix 系统上走到
        raise DickError("source ranking 的交互界面只在类 Unix 终端上可用") from None
    descriptor = sys.stdin.fileno()
    order = list(sources)
    position = 0
    saved = termios.tcgetattr(descriptor)
    try:
        tty.setraw(descriptor)
        while True:
            draw_ranking(order, position)
            order, position, action = ranking_key(order, position, read_key(descriptor))
            if action == "save":
                return order
            if action == "cancel":
                return None
    finally:
        termios.tcsetattr(descriptor, termios.TCSADRAIN, saved)
        sys.stdout.write("\x1b[2J\x1b[H")
        sys.stdout.flush()


def run_ranking(settings, args):
    """`source ranking`：关掉自动排序后手工排来源顺序，方向键界面。"""
    if settings.autorank:
        raise DickError("自动来源排序已开启；先运行 dick source autorank disable，"
                        "再用 dick source ranking 手工排序")
    sources = settings.available_sources()
    if len(sources) < 2:
        raise DickError("可排序的可用来源不足两个，无需排序")
    if args.json:
        emit({"autorank": False, "priority": list(settings.priority),
              "rankable": sources, "config": str(settings.config_path)})
        return 0
    if not sys.stdin.isatty():
        raise DickError("source ranking 需要交互式终端（TTY）；"
                        "也可以直接编辑配置文件里的 [priority." + settings.family + "] order")
    ordered = rank_sources(sources)
    if ordered is None:
        report("已取消，来源顺序未改动。")
        return 0
    # 不可用的来源不在界面里，但它们得留在配置里（以后装了 flatpak / nix 就轮得到），
    # 于是按原来的相对顺序接在排好的列表后面；snap 由 set_priority 强制垫底。
    merged = ordered + [source for source in settings.priority if source not in ordered]
    saved = settings.set_priority(merged)
    print("安装优先级已更新：" + " → ".join(saved))
    return 0


def source_hints(settings):
    """与仓库配置无关、但用户会关心的环境提示。

    Arch 系默认带 aur 来源，可 AUR 自己不会构建包：得先有一个 AUR 助手（paru/yay）。
    缺少时不报错，只在 source list / scan 里点一句怎么装。
    """
    if settings.family == "arch" and settings.enabled("aur") and aur_helper() is None:
        return [AUR_HELPER_HINT]
    return []


def show_sources(settings, action, args, repositories, errors):
    hints = source_hints(settings)
    entries = []
    for source in args.source or list(SOURCES):
        found = [repository for repository in repositories if repository.source == source]
        entries.append({
            "source": source,
            "enabled": settings.enabled(source),
            "available": settings.available(source),
            "repositories": [{"repository": repository.name, "urls": list(repository.urls),
                              "mirrorlist": repository.mirrorlist, "metalink": repository.metalink}
                             for repository in found],
        })
    if args.json:
        emit({"family": settings.family, "priority": list(settings.priority),
              "autorank": settings.autorank,
              "sources": entries, "errors": errors, "hints": hints})
        return 1 if errors else 0
    print(f"系统：{settings.family}；安装优先级：{' → '.join(settings.priority)}")
    if settings.autorank:
        print("自动来源排序：已开启（flatpak 的 IDE/开发类候选会自动排到原生源之后）")
    else:
        print("自动来源排序：已关闭（安装完全按上面的顺序；可用 dick source ranking 调整）")
    for entry in entries:
        states = ["已启用" if entry["enabled"] else "已禁用", "可用" if entry["available"] else "不可用"]
        print(f"[{entry['source']}] {'，'.join(states)}")
        for repository in entry["repositories"]:
            urls = [url for url in [*repository["urls"], repository["mirrorlist"], repository["metalink"]] if url]
            shown = ", ".join(urls[:3]) + (f" 等 {len(urls)} 个" if len(urls) > 3 else "")
            print(f"  {repository['repository']}: {shown}")
        if not entry["repositories"]:
            print("  未识别到仓库配置。")
    for hint in hints:
        print(f"提示：{hint}")
    for error in errors:
        report(f"错误：{error}")
    if action.name == "source_list":
        print("提示：source list 只读取配置文件；执行 dick source scan 会让原生命令重新探测。")
    return 1 if errors else 0


def read_sources(settings, sources):
    """按来源读取本机已安装的包，返回 (packages, errors)。"""
    packages, errors = [], []
    for source in sources:
        if not settings.available(source):
            errors.append(f"{source}：原生管理器不可用")
            continue
        try:
            packages.extend(read_installed(settings, source))
        except DickError as error:
            errors.append(f"{source}：{error}")
    return packages, errors


def run_list(settings, action, args, sources):
    packages, errors = read_sources(settings, sources)
    if action.targets:
        lowered = action.targets[0].casefold()
        packages = [package for package in packages if lowered in package.name.casefold()]
    grouped = {}
    for package in packages:
        grouped.setdefault(package.source, []).append(package)
    shown, truncated = [], []
    for source in sources:
        found = sorted(grouped.get(source, []), key=lambda package: package.name.casefold())
        shown.extend(found[:args.limit])
        if len(found) > args.limit:
            truncated.append(f"{source} 共 {len(found)} 个")
    if args.json:
        emit({"packages": [package.to_dict() for package in shown], "total": len(packages), "errors": errors})
    else:
        for package in shown:
            print(f"[{package.source}] {package.name} {package.version}")
        if not packages:
            print("没有找到已安装的包。")
        for message in truncated:
            report(f"{message}，每个来源显示前 {args.limit} 个；使用 --limit 调整")
    for error in errors:
        report(f"警告：{error}")
    return 1 if errors else 0


def find_candidates(settings, target, sources):
    packages, errors = read_sources(settings, sources)
    lowered = target.casefold()
    found = [package for package in packages if matches(package, lowered)]
    found.sort(key=lambda package: (rank(package, lowered), package.source, package.name.casefold()))
    return found, errors


def confirm(message):
    try:
        answer = input(message)
    except EOFError:
        return False
    return answer.strip().casefold() in {"y", "yes"}


def choose_candidates(target, candidates, args):
    if len(candidates) == 1:
        return candidates
    if args.json or not sys.stdin.isatty():
        names = "、".join(f"{package.source}/{package.name}" for package in candidates)
        raise DickError(f"{target} 匹配到多个已安装的包，请用 --source 或完整名称指定：{names}")
    print(f"{target} 匹配到 {len(candidates)} 个已安装的包：")
    for index, package in enumerate(candidates, 1):
        print(f"  [{index}] {package.source:<8} {package.name} {package.version}")
    while True:
        try:
            answer = input("选择要卸载的编号（多个用逗号分隔，直接回车取消）：")
        except EOFError:
            return []
        answer = answer.strip()
        if not answer:
            return []
        try:
            indexes = [int(part) for part in answer.replace("，", ",").split(",") if part.strip()]
        except ValueError:
            print("请输入编号，例如 1 或 1,3。")
            continue
        if not indexes or any(index < 1 or index > len(candidates) for index in indexes):
            print(f"编号范围是 1-{len(candidates)}。")
            continue
        return [candidates[index - 1] for index in dict.fromkeys(indexes)]


def run_remove(settings, action, args, sources):
    interactive = sys.stdin.isatty() and not args.json
    if not interactive and not args.yes and not args.dry_run:
        raise DickError("非交互卸载需要 --yes，或先用 --dry-run 预览要执行的命令")
    installer = Installer(settings, None, report, args.dry_run, args.yes, sys.stderr if args.json else None)
    results = []
    for target in action.targets:
        candidates, errors = find_candidates(settings, target, sources)
        for error in errors:
            report(f"警告：{error}")
        if not candidates:
            report(f"没有找到已安装的 {target}；使用 dick list 查看本机已安装的包")
            results.append({"name": target, "success": False, "candidates": [], "error": "未找到已安装的包"})
            continue
        if len(candidates) == 1:
            package = candidates[0]
            if interactive and not args.yes and not args.dry_run and not confirm(
                    f"确认卸载 [{package.source}] {package.name} {package.version}？[y/N] "):
                report(f"已取消 {target}。")
                continue
            chosen = candidates
        elif args.dry_run and not interactive:
            report(f"{target} 匹配到多个已安装的包，--dry-run 逐个预览：")
            chosen = candidates
        else:
            chosen = choose_candidates(target, candidates, args)
            if not chosen:
                report(f"已取消 {target}。")
                continue
        for package in chosen:
            command = installer.uninstall_command(package, args.deep)
            code = installer.execute(command)
            record = {"name": target, "source": package.source, "package": package.name,
                      "version": package.version, "command": command,
                      "returncode": code, "success": code == 0, "dry_run": args.dry_run}
            if code != 0:  # 报告里带上原生命令的原因，而不是光秃秃一个退出码
                record["error"] = installer.failure_reason(code)
                installer.report_uninstall_hint(package)
            results.append(record)
    if args.json:
        emit({"results": results})
    return 0 if all(result["success"] for result in results) else 1


def run_upgrade(settings, args):
    installer = Installer(settings, None, report, args.dry_run, args.yes, sys.stderr if args.json else None)
    command = installer.upgrade_command(args.source)
    code = installer.execute(command)
    if args.json:
        emit({"command": command, "returncode": code, "dry_run": args.dry_run})
    return 0 if code == 0 else 1


def report_self(payload):
    """把 updateme / removeme 的结果讲清楚：先回显命令，再回显输出，最后给结论。"""
    label = "计划：" if payload["dry_run"] else "执行："
    for command in payload.get("commands", ()):
        report(label + " ".join(str(part) for part in command))
    for line in payload.get("lines", ()):
        report("  " + line)
    for path in payload.get("removed", ()):
        report(("将删除：" if payload["dry_run"] else "已删除：") + path)
    if payload["message"]:
        report(payload["message"])


def run_self(settings, action, args):
    from .selfmanage import run_remove, run_update
    payload = run_update(settings, args) if action.name == "updateme" else run_remove(settings, args)
    if args.json:
        emit(payload)
    else:
        report_self(payload)
    return 0 if payload["success"] else 1


def run(argv):
    args, remaining = options(argv)
    if args.help or not argv:
        print(HELP)
        return 0
    action = normalize(remaining)
    settings = Settings(args.config, args.root, args.cache_dir, args.jobs,
                        create=action.name in {"source_enable", "source_disable",
                                               "source_autorank", "source_ranking", "web"})
    if action.name in {"updateme", "removeme"}:
        return run_self(settings, action, args)
    check_enabled(settings, args, action)
    if action.name in {"source_enable", "source_disable"}:
        return update_sources(settings, action, args)
    if action.name == "source_autorank":
        return run_autorank(settings, action, args)
    if action.name == "source_ranking":
        return run_ranking(settings, args)
    if action.name == "web":
        from .web import serve
        return serve(settings, args.host or settings.web_host, args.port or settings.web_port,
                     open_browser=args.open_browser, tls=args.tls, token=args.token,
                     no_token=args.no_token)
    if action.name in MUTATING and not args.dry_run and settings.root.resolve() != Path("/").resolve():
        raise DickError("--root 只用于读取源配置；系统变更请在真实根目录下执行，或使用 --dry-run")
    repositories, errors = discover(settings, respect_enabled=action.name not in SOURCE_VIEWS,
                                    native=action.name != "source_list")
    if action.name in SOURCE_VIEWS:
        return show_sources(settings, action, args, repositories, errors)
    for error in errors:
        report(f"警告：{error}")
    sources = args.source or list(dict.fromkeys(repository.source for repository in repositories))
    if action.name == "remove":
        return run_remove(settings, action, args, sources)
    if action.name == "upgrade":
        return run_upgrade(settings, args)
    if action.name == "list":
        return run_list(settings, action, args, sources)
    cache = Cache(settings.cache_dir)
    try:
        client = HTTPClient(settings.timeout, settings.max_bytes)
        index = Index(settings, cache, client, repositories, report)
        if action.name == "update":
            if not any(repository.source in sources for repository in repositories):
                raise DickError("没有可刷新的源；使用 dick source scan 检查配置")
            selected = [repository for repository in repositories if repository.source in sources]
            progress = Progress(settings.family, len(selected), sys.stderr, enabled=not args.json)
            results, failures = index.refresh(sources, progress=progress, workers=settings.workers)
            failures.extend(errors)
            if not errors:
                cache.prune(repositories, sources)
            if args.json:
                emit({"refreshed": results, "errors": failures})
            return 1 if failures else 0
        if action.name == "search":
            packages, failures = index.search(action.targets[0], sources, exact=args.exact)
            failures = [*errors, *failures]
            if args.json:
                emit({"packages": [package.to_dict() for package in packages[:args.limit]],
                      "total": len(packages), "errors": failures})
            else:
                print_packages(packages[:args.limit])
                if not packages:
                    print("没有找到目标包。")
                elif len(packages) > args.limit:
                    report(f"共 {len(packages)} 个结果，显示前 {args.limit} 个；使用 --limit 调整")
            return 1 if (not packages and failures) else 0
        if action.name == "install":
            installer = Installer(settings, index, report, args.dry_run, args.yes,
                                  sys.stderr if args.json else None)
            results = []
            for target in action.targets:
                result = installer.install(target, sources)
                results.append(result)
                if not result["success"]:
                    report(f"未能安装 {target}：所有候选来源已失败")
                    for attempt in result["attempts"]:
                        if "error" in attempt:
                            report(f"  {attempt['source']}：{attempt['error']}")
            if args.json:
                emit({"results": results})
            return 0 if all(result["success"] for result in results) else 1
        raise DickError(f"未实现动作：{action.name}")
    finally:
        cache.close()


def main(argv=None):
    try:
        return run(sys.argv[1:] if argv is None else argv)
    except KeyboardInterrupt:
        report("操作已取消。")
        return 130
    except (DickError, OSError, sqlite3.Error) as error:
        report(f"DICK：{error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
