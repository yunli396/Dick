import os
import re
import shlex
import shutil
import subprocess
import sys

from .models import DickError

# 原生包管理器的索引过期时，本地记的版本号在镜像上已经不存在——下载会 404（pacman 报
# 「无法从 … 获取文件」/「无法提交处理」，apt 报 Hash Sum mismatch / 404）。这里给出
# 各来源刷新索引的命令，安装失败一次后会自动刷新再重试一次。
REFRESH_COMMANDS = {
    "pacman": ["pacman", "-Sy"],
    "apt": ["apt-get", "update"],
}

STALE_INDEX = re.compile(
    r"无法获取|无法下载|无法从|下载失败|无法提交|Failed to retrieve|Failed to fetch|"
    r"Cannot retrieve|Hash Sum mismatch|校验和不匹配|returned error: 404|404\s+Not Found",
    re.IGNORECASE,
)

# 玲珑（linyaps）的安装/卸载由系统 D-Bus 上的 PackageManager 服务执行，它用 polkit 的
# org.deepin.linglong.PackageManager1.install|uninstall 规则把关，三条默认值都是 auth_admin。
# 普通用户调用时 polkitd 会去用户桌面会话里找认证代理弹框，网页/手机这种没人守着桌面的
# 场景只会等到「Error 9: not authorized」——所以这里也走 sudo（root 调用不需要 polkit 授权）。
NOT_AUTHORIZED = re.compile(r"not authorized|未授权|权限不足", re.IGNORECASE)


class Installer:
    def __init__(self, settings, index, report, dry_run=False, yes=False, stream=None, password=None):
        self.settings = settings
        self.index = index
        self.report = report
        self.dry_run = dry_run
        self.yes = yes
        self.stream = stream
        # 网页密码框里填的 sudo 密码：只在任务内存里活着，不落盘（由调用方负责用完清掉）。
        self.password = password.rstrip("\r\n") if password else None
        self.auth_failed = False
        self._sudo_ready = None
        # 上一次执行的输出（Web 任务能全量拿到；CLI 至少能拿到 stderr），
        # 用来判断失败是不是「本地索引过期」这种可以自动补救的情况。
        self.last_output = []

    def privileged(self, command):
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            return command
        if self.dry_run or shutil.which("sudo"):
            return ["sudo", *command]
        raise DickError("系统安装需要 root 或 sudo")

    def sudo_ready(self):
        """sudo 能否在无人值守的情况下直接提权（即已配置 NOPASSWD 或已缓存凭据）。

        Web 任务没有终端，sudo 会直接报「需要密码」并退出——先问一次，失败就给出人话提示，
        而不是让用户对着一句「退出码 1」猜原因。结果缓存在实例上，避免每个包都问一遍。
        """
        if self._sudo_ready is None:
            try:
                self._sudo_ready = subprocess.run(["sudo", "-n", "true"], check=False,
                                                  stdout=subprocess.DEVNULL,
                                                  stderr=subprocess.DEVNULL).returncode == 0
            except OSError:
                self._sudo_ready = False
        return self._sudo_ready

    def sudo_command(self, command):
        """有密码时把 sudo 换成从标准输入读密码的形式；密码本身永远不进命令行。"""
        if self.password and command and command[0] == "sudo":
            return ["sudo", "-S", "-p", "", *command[1:]]
        return command

    @staticmethod
    def looks_like_auth_failure(text):
        return bool(re.search(r"incorrect password|Sorry, try again|Authentication failure|"
                              r"密码不正确|密码尝试错误|认证失败|对不起", text, re.IGNORECASE))

    def looks_like_stale_index(self):
        return bool(self.last_output) and bool(STALE_INDEX.search("\n".join(self.last_output)))

    def looks_like_not_authorized(self):
        return bool(self.last_output) and bool(NOT_AUTHORIZED.search("\n".join(self.last_output)))

    def refresh_command(self, source):
        """该来源刷新索引的命令；没有（比如 dnf 会自己更新元数据）就返回 None。"""
        command = REFRESH_COMMANDS.get(source)
        if not command or not self.settings.available(source):
            return None
        return self.privileged(command)

    def _stream_run(self, command):
        """非交互场景（Web 任务）：把子进程的 stdout/stderr 实时回灌到任务日志。"""
        authenticated = bool(self.password) and command[0] == "sudo"
        # 没有密码、也没有终端时才替 sudo 做主：CLI 的 --json 仍有 tty，sudo 自己会弹密码提示。
        if command[0] == "sudo" and not authenticated and not sys.stdin.isatty() and not self.sudo_ready():
            binary = shutil.which(command[1]) or command[1]
            # /usr/sbin 在多数发行版里是 /usr/bin 的软链，sudo 的匹配规则跟实际解析结果有关，
            # 两个路径都给出来最省事。
            paths = ", ".join(dict.fromkeys([binary, os.path.realpath(binary)]))
            self.report("sudo 需要输入密码，而当前任务没有终端，无法提权。")
            self.report("解决办法（三选一）：")
            self.report("  1) 在网页的安装/卸载确认框里填上 sudo 密码（只在任务内存里用一次，不保存）")
            self.report(f"  2) 在宿主终端里给 {command[1]} 配置免密（之后网页里的安装就能直接用）："
                        f"echo \"$USER ALL=(root) NOPASSWD: {paths}\" | "
                        "sudo tee /etc/sudoers.d/dick >/dev/null && sudo chmod 440 /etc/sudoers.d/dick")
            self.report("  3) 在宿主终端里自己执行同一条命令：" + shlex.join(command))
            self.last_output = []
            return 1
        try:
            process = subprocess.Popen(self.sudo_command(command),
                                       stdin=subprocess.PIPE if authenticated else None,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0)
        except OSError as error:
            raise DickError(f"无法执行 {command[0]}：{error}") from error
        if authenticated:
            try:
                process.stdin.write((self.password + "\n").encode("utf-8"))
                process.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
            finally:
                try:
                    process.stdin.close()
                except OSError:
                    pass
        pending, seen = "", []
        with process.stdout:
            while True:
                chunk = process.stdout.read(4096)
                if not chunk:
                    break
                pending += chunk.decode("utf-8", "replace")
                while True:
                    breaks = [index for index in (pending.find("\n"), pending.find("\r")) if index >= 0]
                    if not breaks:
                        break
                    index = min(breaks)
                    line, pending = pending[:index].strip(), pending[index + 1:]
                    if line:
                        self.report(line)
                        seen.append(line)
        if pending.strip():
            self.report(pending.strip())
            seen.append(pending.strip())
        code = process.wait()
        self.last_output = seen
        if authenticated and self.looks_like_auth_failure("\n".join(seen)):
            self.auth_failed = True
            self.report("sudo 密码不正确：网页密码框里填的密码没通过验证，请重新输入（没有保存过它）。")
        return code

    def execute(self, command):
        self.report(("计划：" if self.dry_run else "执行：") + shlex.join(command))
        if self.dry_run:
            return 0
        if self.settings.root.resolve() != self.settings.root.__class__("/").resolve():
            raise DickError("--root 只用于读取源配置；系统变更请在真实根目录下执行")
        if callable(self.stream):  # Web 任务：无终端，输出回灌到任务日志
            return self._stream_run(command)
        try:
            if self.stream is None:
                # CLI 直连终端：stdout 照旧继承（颜色与进度条不变，sudo 还是从 /dev/tty 读密码），
                # stderr 收下来既能原样转出，也能用来识别「索引过期」这类可以自动重试的失败。
                completed = subprocess.run(command, check=False, stderr=subprocess.PIPE)
                raw = completed.stderr or b""
                text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
                if text:
                    sys.stderr.write(text)
                    sys.stderr.flush()
                self.last_output = text.splitlines()
                return completed.returncode
            # --json 等带文件 sink 的场景：stderr 跟着 stdout 走，别弄脏 stdout。
            self.last_output = []
            return subprocess.run(command, check=False, stdout=self.stream,
                                  stderr=subprocess.STDOUT).returncode
        except OSError as error:
            raise DickError(f"无法执行 {command[0]}：{error}") from error

    def install_command(self, package):
        source, name = package.source, package.name
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9+_.:@-]*", name):
            raise DickError(f"索引中的包名无效：{name!r}")
        confirmation = ["--noconfirm"] if self.yes else []
        if source == "pacman":
            target = f"{package.repository}/{name}" if package.repository else name
            return self.privileged(["pacman", "-S", *confirmation, "--", target])
        if source == "aur":
            if hasattr(os, "geteuid") and os.geteuid() == 0 and not self.dry_run:
                raise DickError("AUR 构建必须以普通用户运行")
            helper = next((candidate for candidate in ("paru", "yay") if shutil.which(candidate)), None)
            if helper is None:
                raise DickError("AUR 安装需要已安装的 paru 或 yay")
            return [helper, "-S", *confirmation, "--", f"aur/{name}"]
        if source == "flatpak":
            return ["flatpak", "install", *(["-y"] if self.yes else []), "--", package.repository, name]
        if source == "snap":
            return self.privileged(["snap", "install", "--", name])
        if source == "linyaps":
            # 玲珑的服务要 polkit 授权（auth_admin），普通用户调用得靠桌面会话里的认证框；
            # 走 sudo 后由 root 调用，不需要 polkit，也复用网页里的提权密码框。
            return self.privileged(["ll-cli", "install", *(["-y"] if self.yes else []), name])
        if source == "apt":
            return self.privileged(["apt-get", "install", *(["-y"] if self.yes else []), "--", name])
        if source == "dnf":
            return self.privileged(["dnf", "install", *(["-y"] if self.yes else []), "--", name])
        raise DickError(f"不支持安装来源：{source}")

    def install(self, target, sources):
        attempts = []
        refreshed = set()
        source_order = [source for source in self.settings.priority if source in sources]
        source_order.extend(source for source in sources if source not in source_order)
        for source in source_order:
            if not self.settings.available(source):
                attempts.append({"source": source, "error": "原生管理器不可用"})
                continue
            packages, failures = self.index.search(target, [source], exact=True)
            if not packages:
                attempts.append({"source": source, "error": "；".join(failures) or "未找到同名包"})
                continue
            try:
                if len({package.name for package in packages}) > 1:
                    raise DickError("名称匹配多个应用，请使用完整 ID：" + ", ".join(sorted({package.name for package in packages})))
                command = self.install_command(packages[0])
                code = self.execute(command)
                stale = self.looks_like_stale_index()
                attempts.append({"source": source, "command": command, "returncode": code})
                if code != 0 and stale and source not in refreshed:
                    # 本地索引过期：镜像上已经换了新版本，本地记的旧文件名一律 404。
                    # 先刷新一次索引再重试同一个命令，省得用户自己开终端跑 pacman -Sy。
                    refresh = self.refresh_command(source)
                    if refresh:
                        refreshed.add(source)
                        self.report(f"{source} 的本地索引可能已过期（镜像上找不到这个版本），"
                                    "先刷新索引再重试一次。")
                        refresh_code = self.execute(refresh)
                        if refresh_code == 0:
                            code = self.execute(command)
                            stale = self.looks_like_stale_index()
                            attempts.append({"source": source, "command": command,
                                             "returncode": code, "refreshed": True})
                        else:
                            attempts.append({"source": source, "refresh_command": refresh,
                                             "refresh_returncode": refresh_code})
                            self.report(f"{source} 索引刷新失败（退出码 {refresh_code}）。")
                if code == 0:
                    return {"name": target, "source": source, "dry_run": self.dry_run,
                            "attempts": attempts, "success": True}
                if code < 0 or code in {130, 143}:
                    raise KeyboardInterrupt
                if self.auth_failed:  # 密码不对，换来源也一样过不去
                    self.report("sudo 密码不正确，不再尝试其它来源。")
                    break
                if stale:
                    self.report("本地索引里记的版本在镜像上已经不存在了："
                                "在「更新与升级」里做一次完整升级后再装会更稳。")
                if source == "linyaps" and self.looks_like_not_authorized():
                    self.report("玲珑的服务要求管理员认证（polkit），这次调用没通过授权。"
                                "可以在宿主终端里先执行一次："
                                f"sudo ll-cli install -y {target}")
                    self.report("或者按 README「权限」一节写一条 polkit 规则"
                                "（/etc/polkit-1/rules.d/49-dick-linglong.rules），"
                                "允许 wheel 组直接安装，之后网页里就不用再授权。")
                self.report(f"{source} 安装失败（退出码 {code}），尝试下一来源")
            except DickError as error:
                attempts.append({"source": source, "error": str(error)})
                self.report(f"{source}：{error}，尝试下一来源")
        return {"name": target, "attempts": attempts, "success": False}

    def uninstall_command(self, package, deep=False):
        source, name = package.source, package.name
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9+_.:@-]*", name):
            raise DickError(f"包名无效：{name!r}")
        if source in {"pacman", "aur"}:
            flag = "-Rns" if deep else "-R"
            return self.privileged(["pacman", flag, *(["--noconfirm"] if self.yes else []), "--", name])
        if source == "apt":
            command = ["apt-get", "purge" if deep else "remove", *(["-y"] if self.yes else [])]
            if deep:
                command.append("--autoremove")
            return self.privileged([*command, "--", name])
        if source == "dnf":
            command = ["dnf", "remove", *(["-y"] if self.yes else [])]
            if deep:
                command.append("--setopt=clean_requirements_on_remove=True")
            return self.privileged([*command, "--", name])
        if source == "flatpak":
            return ["flatpak", "uninstall", *(["-y"] if self.yes else []),
                    *(["--delete-data"] if deep else []), name]
        if source == "snap":
            return self.privileged(["snap", "remove", name])
        if source == "linyaps":
            # 同安装：卸载也由 PackageManager 服务执行，用 polkit 的 uninstall 规则（auth_admin）。
            return self.privileged(["ll-cli", "uninstall", name])
        raise DickError(f"不支持卸载来源：{source}")

    def native_source(self, sources=None):
        preferred = {"arch": "pacman", "debian": "apt", "fedora": "dnf"}.get(self.settings.family)
        ordered = list(dict.fromkeys([preferred, *self.settings.priority]))
        for source in ordered:
            if source in {"pacman", "apt", "dnf"} and (sources is None or source in sources) and self.settings.available(source):
                return source
        raise DickError("未找到可用的系统包管理器；upgrade 只作用于 pacman/apt/dnf")

    def upgrade_command(self, sources=None):
        source = self.native_source(sources)
        if source == "pacman":
            return self.privileged(["pacman", "-Syu", *(["--noconfirm"] if self.yes else [])])
        if source == "apt":
            return self.privileged(["apt-get", "upgrade", *(["-y"] if self.yes else [])])
        return self.privileged(["dnf", "upgrade", *(["-y"] if self.yes else [])])
