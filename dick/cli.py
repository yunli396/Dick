import json
from pathlib import Path
import sqlite3
import sys

from .cache import Cache
from .config import SOURCES, Settings
from .discovery import discover
from .index import Index
from .install import AUR_HELPER_HINT, Installer, aur_helper
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
              "sources": entries, "errors": errors, "hints": hints})
        return 1 if errors else 0
    print(f"系统：{settings.family}；安装优先级：{' → '.join(settings.priority)}")
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
                        create=action.name in {"source_enable", "source_disable", "web"})
    if action.name in {"updateme", "removeme"}:
        return run_self(settings, action, args)
    check_enabled(settings, args, action)
    if action.name in {"source_enable", "source_disable"}:
        return update_sources(settings, action, args)
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
