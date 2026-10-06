"""DICK 的自管理：把 DICK 自己更新到新版本，或者把 DICK 自己卸载干净。

只对 install.sh 装出来的目录结构动手：

    <前缀>/share/dick/venv   虚拟环境
    <前缀>/share/dick/src    源码（git 克隆或解开的 tarball）
    <前缀>/bin/dick          → <前缀>/share/dick/venv/bin/dick

源码工作区（开发时的 git 检出）只做说明、不删；系统 Python / 发行版包管理器装的
DICK 也只告诉用户该用哪个命令——删错别人的东西比少帮一次忙糟得多。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from . import __version__
from .models import DickError


DEFAULT_REF = "main"
REPO_URL = "https://github.com/yunli396/Dick.git"


@dataclass
class Installation:
    """一处 DICK 安装。kind 决定 updateme / removeme 能不能动手。"""

    kind: str
    prefix: Path | None = None
    share: Path | None = None
    venv: Path | None = None
    source: Path | None = None
    command: Path | None = None
    hint: str = ""

    def describe(self) -> str:
        if self.kind == "script":
            return f"install.sh 安装：{self.share}（命令 {self.command or self.prefix / 'bin/dick'}）"
        if self.kind == "checkout":
            return f"源码工作区：{self.source}"
        if self.kind == "system":
            return f"系统 Python：{self.source}"
        return "未识别的安装位置"


def run_command(command, cwd=None):
    """执行外部命令，返回 (退出码, 合并后的输出)。测试里替换这一个函数即可。"""
    try:
        completed = subprocess.run([str(part) for part in command],
                                   cwd=str(cwd) if cwd else None,
                                   stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT,
                                   text=True,
                                   errors="replace")
    except OSError as error:  # 命令不存在、不可执行……都当成一次失败的命令
        return 127, f"无法执行 {command[0]}：{error}"
    return completed.returncode, completed.stdout or ""


def _prefix_below(path):
    """<前缀>/share/dick/… → <前缀>；别的形状返回 None。

    认两种形状：<前缀>/share/dick/venv/bin/<命令>（venv 里的解释器与控制台脚本）
    和 <前缀>/share/dick/src/dick/<模块>.py（venv 里 import 的那份源码）。
    """
    parents = list(Path(path).parents)
    for index in range(len(parents) - 1):
        if parents[index].name == "dick" and parents[index + 1].name == "share":
            return parents[index + 2] if index + 2 < len(parents) else None
    return None


def candidate_prefixes():
    """候选前缀：环境变量 → 正在运行的解释器/命令 → 常见位置。"""
    found = []
    env = os.environ.get("DICK_PREFIX", "").strip()
    if env:
        found.append(Path(env).expanduser())
    probes = [Path(sys.executable).resolve()]
    if sys.argv and sys.argv[0]:
        probes.append(Path(sys.argv[0]).resolve())
    for probe in probes:
        prefix = _prefix_below(probe)
        if prefix is not None:
            found.append(prefix)
    running = _prefix_below(Path(__file__).resolve())  # 优先认「正在跑的这份代码」
    if running is not None:
        found.append(running)
    found.extend([Path.home() / ".local", Path("/usr/local"), Path("/usr")])
    unique = []
    for path in found:
        path = Path(path)
        if path not in unique:
            unique.append(path)
    return unique


def script_layout(prefix):
    """识别 install.sh 的安装目录结构。"""
    prefix = Path(prefix)
    share = prefix / "share" / "dick"
    venv = share / "venv"
    source = share / "src"
    command = prefix / "bin" / "dick"
    if not (venv.is_dir() or source.is_dir() or command.exists() or command.is_symlink()):
        return None
    return Installation(
        kind="script",
        prefix=prefix,
        share=share,
        venv=venv if venv.is_dir() else None,
        source=source if (source / "pyproject.toml").is_file() else None,
        command=command if (command.exists() or command.is_symlink()) else None,
    )


def checkout_layout():
    """正在从源码工作区里跑（开发模式）。"""
    root = Path(__file__).resolve().parent.parent
    if (root / "pyproject.toml").is_file() and (root / ".git").is_dir():
        return Installation(kind="checkout", source=root)
    return None


def system_layout():
    """装在系统 Python 的 site-packages 里（多半来自发行版包）。"""
    module = Path(__file__).resolve()
    if not {"site-packages", "dist-packages"} & set(module.parts):
        return None
    for root in (Path("/usr"), Path("/usr/local")):
        try:
            module.relative_to(root)
        except ValueError:
            continue
        return Installation(
            kind="system",
            prefix=root,
            source=module.parent,
            hint=f"DICK 由系统 Python（{module.parent}）提供，多半来自发行版包；"
                 "更新和卸载请用装它的包管理器（pacman / apt / dnf / apk / nix…）。",
        )
    return None


def running_layout():
    """正在运行的这份代码来自哪里：装在前缀里就是那个前缀，源码工作区就是工作区。"""
    prefix = _prefix_below(Path(__file__).resolve())
    if prefix is not None:
        found = script_layout(prefix)
        if found is not None:
            return found
    return checkout_layout()


def detect(prefix=None):
    if prefix is not None:
        found = script_layout(Path(prefix).expanduser())
        if found is None:
            raise DickError(f"{prefix} 下没有 install.sh 装出来的 DICK（没找到 share/dick）")
        return found
    env = os.environ.get("DICK_PREFIX", "").strip()
    if env:
        found = script_layout(Path(env).expanduser())
        if found is not None:
            return found
    # 正在跑的那份代码优先于 ~/.local 之类的默认位置：在源码工作区里跑 web 时，
    # 「更新 DICK」要更新工作区，而不是另一个碰巧装好的 ~/.local。
    found = running_layout()
    if found is not None:
        return found
    for candidate in candidate_prefixes():
        found = script_layout(candidate)
        if found is not None:
            return found
    found = system_layout()
    if found is not None:
        return found
    return Installation(
        kind="unknown",
        hint="没找到 DICK 的安装位置；如果还没装，先按 README 的「一键安装」跑一次 install.sh。",
    )


def version_of(installation, dry_run=False):
    """读安装目录里的版本号，读不到就退回当前进程的版本。"""
    if dry_run:
        return __version__
    if installation.kind == "checkout":
        return __version__
    if installation.venv is not None:
        code, output = run_command([installation.venv / "bin" / "python", "-c",
                                    "import dick; print(dick.__version__)"])
        if code == 0 and output.strip():
            return output.strip().splitlines()[-1].strip()
    return __version__


def update_commands(installation, ref):
    """updateme 要按顺序跑的命令。"""
    commands = []
    if installation.kind == "checkout":
        return [["git", "-C", str(installation.source), "pull", "--ff-only"]]
    if installation.kind == "script":
        source = installation.source
        if source is not None and (source / ".git").is_dir():
            commands.append(["git", "-C", str(source), "fetch", "--depth", "1", "origin", ref])
            commands.append(["git", "-C", str(source), "checkout", "-q", "-f", "FETCH_HEAD"])
        if installation.venv is not None:
            pip = installation.venv / "bin" / "pip"
            if source is not None:
                commands.append([str(pip), "install", "--quiet", "--upgrade", str(source)])
            else:  # 当初是用 tarball 装的，没有源码目录，就让 pip 直接取仓库
                commands.append([str(pip), "install", "--quiet", "--upgrade",
                                 f"git+{REPO_URL}@{ref}"])
        return commands
    return commands


def run_update(settings, args):
    """dick updateme：更新 DICK 自己，返回结果字典。"""
    installation = detect(getattr(args, "prefix", None))
    ref = (getattr(args, "ref", None) or os.environ.get("DICK_REF") or DEFAULT_REF).strip() or DEFAULT_REF
    if installation.kind in {"unknown", "system"}:
        raise DickError(installation.hint)
    payload = {
        "action": "updateme",
        "kind": installation.kind,
        "location": installation.describe(),
        "ref": ref,
        "before": version_of(installation, dry_run=args.dry_run),
        "after": None,
        "commands": update_commands(installation, ref),
        "lines": [],
        "returncode": 0,
        "dry_run": args.dry_run,
        "success": True,
        "message": "",
    }
    if not payload["commands"]:
        raise DickError(f"{installation.prefix} 里既没有 venv 也没有源码目录可更新；"
                        "建议重新跑一次 install.sh。")
    _execute(payload, args)
    if installation.kind == "checkout":
        # 源码工作区就地更新，重新导入才看得到新版本
        payload["after"] = payload["before"]
        payload["message"] = (f"源码工作区已更新到 {ref}；DICK 直接跑源码，不用重新安装。"
                              "重启 DICK 后新版本生效。")
        if not payload["success"]:
            payload["message"] = ("更新失败；如果 git pull --ff-only 被本地改动挡住，"
                                  "先看看 git status 再决定怎么处理。")
        return payload
    payload["after"] = version_of(installation, dry_run=args.dry_run)
    if not payload["success"]:
        payload["message"] = f"更新失败（退出码 {payload['returncode']}）；上面的输出里有原生命令的原因。"
    elif payload["dry_run"]:
        payload["message"] = "以上是更新 DICK 会执行的命令（演练，没有真的执行）。"
    elif payload["after"] != payload["before"]:
        payload["message"] = f"DICK 已更新：{payload['before']} → {payload['after']}"
    else:
        payload["message"] = f"DICK 已经是最新版本（{payload['after']}）。"
    return payload


def _execute(payload, args):
    """按顺序执行 payload['commands']，把输出与失败原因记进 payload。"""
    for command in payload["commands"]:
        if args.dry_run:
            continue
        code, output = run_command(command)
        payload["lines"].extend(line.rstrip() for line in output.splitlines() if line.strip())
        if code != 0:
            payload.update(returncode=code, success=False,
                           error=f"命令失败（退出码 {code}）：{' '.join(str(part) for part in command)}")
            return
    return None


def removal_targets(installation, settings, purge):
    """removeme 要删的路径，加上（purge 时）配置与缓存。"""
    targets = []
    if installation.share is not None:
        targets.append(Path(installation.share))
    if installation.command is not None:
        targets.append(Path(installation.command))
    kept = []
    if purge:
        targets.append(Path(settings.config_path).parent)
        targets.append(Path(settings.cache_dir))
    else:
        config_dir = Path(settings.config_path).parent
        if config_dir.exists():
            kept.append(config_dir)
    return targets, kept


def run_remove(settings, args):
    """dick removeme：卸载 DICK 自己，返回结果字典。"""
    installation = detect(getattr(args, "prefix", None))
    purge = bool(getattr(args, "purge", False))
    dry_run = bool(args.dry_run)
    if installation.kind == "unknown":
        raise DickError(installation.hint)
    if installation.kind == "system":
        raise DickError(installation.hint)
    if installation.kind == "checkout":
        raise DickError(f"{installation.source} 是 DICK 的源码工作区，不是 install.sh 装出来的；"
                        "删掉它会连你的改动一起丢。确实不要了请手动删除，"
                        "或者用 pip uninstall dick（如果是 pip 装的）。")
    targets, kept = removal_targets(installation, settings, purge)
    if not dry_run:
        if not args.yes and not sys.stdin.isatty():
            raise DickError("非交互卸载 DICK 需要 --yes；想先看看删什么就用 --dry-run")
        if not args.yes and not _confirm(targets):
            raise DickError("已取消卸载。")
        blocked = [path for path in targets if path.exists() and not _writable(path)]
        if blocked:
            raise DickError("没有写权限：" + "、".join(str(path) for path in blocked)
                            + "；这些属于系统目录，请用 sudo 或发行版包管理器处理。")
    removed = []
    for path in targets:
        if not (path.exists() or path.is_symlink()):
            continue
        if not dry_run:
            if path.is_symlink() or path.is_file():
                path.unlink()
            else:
                shutil.rmtree(path)
        removed.append(str(path))
    if not dry_run:
        _prune_empty_dirs(installation)
    payload = {
        "action": "removeme",
        "kind": installation.kind,
        "location": installation.describe(),
        "removed": removed,
        "kept": [str(path) for path in kept],
        "purge": purge,
        "returncode": 0,
        "dry_run": dry_run,
        "success": True,
        "message": "",
    }
    if dry_run:
        payload["message"] = "以上是卸载 DICK 会删除的内容（演练，没有真的删）。"
    else:
        payload["message"] = "已卸载 DICK。"
        if kept:
            payload["message"] += " 保留配置：" + "、".join(str(path) for path in kept) + "（要一起删就加 --purge）"
    return payload


def _prune_empty_dirs(installation):
    """<前缀>/bin 与 <前缀>/share 空了就收走（前缀本身与非空目录一律不动）。"""
    prefix = Path(installation.prefix) if installation.prefix is not None else None
    parents = []
    if installation.command is not None:
        parents.append(Path(installation.command).parent)
    if installation.share is not None:
        parents.append(Path(installation.share).parent)
    for path in parents:
        try:
            if path.is_dir() and path != prefix and not any(path.iterdir()):
                path.rmdir()
        except OSError:
            pass


def _confirm(targets):
    listed = "、".join(str(path) for path in targets)
    try:
        answer = input(f"确认删除 {listed}？[y/N] ")
    except EOFError:
        return False
    return answer.strip().casefold() in {"y", "yes"}


def _writable(path):
    """路径本身或它可删除的父目录是否可写。"""
    probe = path if path.is_dir() and not path.is_symlink() else path.parent
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return os.access(probe, os.W_OK)
