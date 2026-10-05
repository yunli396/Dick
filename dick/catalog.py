"""精选应用目录：给 Web GUI 首页提供「编辑精选」这类应用商店内容。

设计取舍：
- 目录是纯静态数据（显示名、中文简介、分类、包名候选），真实版本、仓库、描述与
  安装状态由 `WebApp.featured` 在本机索引里解析。这样首页离线也能渲染，索引缺失
  或系统里没有该包时退化成「点击去搜索」，而不是空白。
- 每个应用给出多个包名候选，按发行版差异回落（Arch 的 `code`、Debian 的
  `visual-studio-code`、Flatpak 的应用 ID 等），因此同一份目录在 apt/dnf 上也有命中。
- `sources` 用于限定来源偏好（例如 WPS 只从玲珑或 Flatpak 取），`on_demand` 里的来源
  在没有本地索引时才会真正发起一次按需查询（AUR / 玲珑 / snap），避免首页打太多请求。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Category:
    id: str
    title: str
    subtitle: str
    icon: str


@dataclass(frozen=True)
class App:
    key: str
    name: str
    tagline: str
    category: str
    match: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    on_demand: tuple[str, ...] = ()
    featured: bool = False

    def names(self):
        """包名候选，按优先级排列；没有显式候选时用 key 本身。"""
        return self.match or (self.key,)


CATEGORIES = (
    Category("featured", "编辑精选", "每个月都会被翻出来用的那些", "★"),
    Category("dev", "开发工具", "编辑器、容器与工程效率", "⌨"),
    Category("internet", "网络与下载", "浏览器、下载与远程连接", "◈"),
    Category("graphics", "图形与设计", "修图、矢量与三维创作", "✎"),
    Category("media", "影音播放", "播放器、音频与视频剪辑", "▶"),
    Category("office", "办公与文档", "文档、笔记与阅读器", "▤"),
    Category("chat", "通讯社交", "聊天、语音与协作", "✉"),
    Category("games", "游戏与娱乐", "游戏平台与增强工具", "♞"),
    Category("system", "系统工具", "监控、磁盘与终端", "⚙"),
    Category("security", "安全与备份", "密码、加密与备份", "⛨"),
    Category("study", "学习与科学", "学习、地理与计算", "∑"),
    Category("virtual", "容器与虚拟机", "容器、虚拟机与安卓", "▣"),
    Category("fonts", "字体与美化", "图标、字体与输入法", "❖"),
)

APPS = (
    # ---------------------------------------------------------------- 开发工具
    App("code", "Visual Studio Code", "微软出品的跨语言编辑器，插件生态庞大", "dev",
        ("code", "visual-studio-code", "vscode"), featured=True),
    App("neovim", "Neovim", "现代 Vim：异步插件、LSP 与 Lua 配置", "dev", ("neovim",), featured=True),
    App("git", "Git", "分布式版本控制的事实标准", "dev", ("git",)),
    App("docker", "Docker", "容器运行时与镜像管理", "dev", ("docker", "docker.io", "docker-ce")),
    App("podman", "Podman", "无需常驻守护进程的容器引擎", "dev", ("podman",)),
    App("lazygit", "Lazygit", "终端里的 Git 图形界面", "dev", ("lazygit",)),
    App("dbeaver", "DBeaver", "几乎支持所有数据库的通用客户端", "dev", ("dbeaver", "dbeaver-ce")),
    App("meld", "Meld", "可视化的文件与目录差异对比", "dev", ("meld",)),
    App("qtcreator", "Qt Creator", "Qt 官方 IDE：C++ 与 QML 开发", "dev",
        ("qtcreator", "qtcreator-qt6", "qt6-tools")),
    App("micro", "Micro", "像 nano 一样易用、又有现代快捷键的终端编辑器", "dev", ("micro",)),

    # ---------------------------------------------------------------- 网络与下载
    App("firefox", "Firefox", "Mozilla 的开源浏览器，隐私与扩展兼顾", "internet",
        ("firefox", "firefox-esr"), featured=True),
    App("chromium", "Chromium", "Chrome 的开源上游，账号同步需自备", "internet", ("chromium",)),
    App("brave", "Brave", "默认拦截广告与跟踪器的浏览器", "internet",
        ("brave-bin", "brave-browser", "brave")),
    App("qbittorrent", "qBittorrent", "功能齐全、无广告的 BT 客户端", "internet",
        ("qbittorrent", "qbittorrent-enhanced"), featured=True),
    App("transmission", "Transmission", "轻量简洁的 BT 下载器", "internet",
        ("transmission-gtk", "transmission", "transmission-cli")),
    App("filezilla", "FileZilla", "跨平台的 FTP / FTPS / SFTP 客户端", "internet", ("filezilla",)),
    App("remmina", "Remmina", "支持 RDP / VNC / SSH 的远程桌面", "internet", ("remmina",)),
    App("syncthing", "Syncthing", "点对点文件同步，不经过中心服务器", "internet", ("syncthing",)),

    # ---------------------------------------------------------------- 图形与设计
    App("gimp", "GIMP", "开源图像编辑与照片修饰", "graphics", ("gimp",), featured=True),
    App("inkscape", "Inkscape", "矢量图形与 SVG 编辑", "graphics", ("inkscape",), featured=True),
    App("blender", "Blender", "三维建模、渲染与动画全家桶", "graphics", ("blender",), featured=True),
    App("krita", "Krita", "面向数字绘画的开源工具", "graphics", ("krita",), featured=True),
    App("darktable", "darktable", "非破坏性的摄影后期与 RAW 工作流", "graphics", ("darktable",)),
    App("rawtherapee", "RawTherapee", "高精度 RAW 显影与色彩处理", "graphics", ("rawtherapee",)),
    App("obs-studio", "OBS Studio", "录屏与直播推流的行业标准", "graphics", ("obs-studio",), featured=True),
    App("kdenlive", "Kdenlive", "多轨非线性视频剪辑", "graphics", ("kdenlive",)),
    App("shotcut", "Shotcut", "跨平台视频剪辑，格式支持广", "graphics", ("shotcut",)),
    App("flameshot", "Flameshot", "截图加标注一步到位", "graphics", ("flameshot",)),
    App("imagemagick", "ImageMagick", "命令行图像处理万能工具", "graphics", ("imagemagick",)),

    # ---------------------------------------------------------------- 影音播放
    App("vlc", "VLC", "什么都能播的万能播放器", "media", ("vlc",), featured=True),
    App("mpv", "mpv", "极简高效的命令行播放器", "media", ("mpv",), featured=True),
    App("audacity", "Audacity", "多轨音频录制与剪辑", "media", ("audacity", "tenacity")),
    App("strawberry", "Strawberry", "面向本地音乐库的播放器", "media", ("strawberry",)),
    App("rhythmbox", "Rhythmbox", "GNOME 的音乐播放器", "media", ("rhythmbox",)),
    App("elisa", "Elisa", "KDE 的简洁音乐播放器", "media", ("elisa",)),
    App("kodi", "Kodi", "家庭影院的媒体中心", "media", ("kodi",)),
    App("handbrake", "HandBrake", "视频转码与压缩", "media", ("handbrake", "handbrake-cli")),
    App("ffmpeg", "FFmpeg", "音视频处理的底层工具箱", "media", ("ffmpeg",)),
    App("amberol", "Amberol", "专注播放、不折腾的音乐播放器", "media", ("amberol",)),

    # ---------------------------------------------------------------- 办公与文档
    App("libreoffice", "LibreOffice", "完整的开源办公套件", "office",
        ("libreoffice-fresh", "libreoffice", "libreoffice-still"), featured=True),
    App("wps", "WPS Office", "中文排版友好的办公套件（玲珑 / Flatpak）", "office",
        ("cn.wps.wps-office", "wps-office", "wps-office-cn", "com.wps.Office"),
        ("linyaps", "flatpak"), ("linyaps",), featured=True),
    App("onlyoffice", "ONLYOFFICE", "兼容 Office 格式的协作办公套件", "office",
        ("onlyoffice-bin", "onlyoffice-desktopeditors", "onlyoffice")),
    App("thunderbird", "Thunderbird", "邮件、日历与通讯录", "office", ("thunderbird",)),
    App("okular", "Okular", "支持批注的通用文档阅读器", "office", ("okular",)),
    App("zathura", "zathura", "键盘驱动的轻量 PDF 阅读器", "office",
        ("zathura", "zathura-pdf-poppler", "zathura-pdf-mupdf")),
    App("calibre", "Calibre", "电子书管理与格式转换", "office", ("calibre",)),
    App("obsidian", "Obsidian", "基于 Markdown 的双链笔记", "office", ("obsidian",)),
    App("joplin", "Joplin", "端到端加密的开源笔记", "office", ("joplin-desktop", "joplin")),
    App("foliate", "Foliate", "优雅的电子书阅读器", "office", ("foliate",)),
    App("evince", "Evince", "GNOME 的文档查看器", "office", ("evince", "papers")),

    # ---------------------------------------------------------------- 通讯社交
    App("telegram", "Telegram", "云端同步的即时通讯", "chat", ("telegram-desktop",), featured=True),
    App("discord", "Discord", "社区、语音与屏幕共享", "chat", ("discord",)),
    App("signal", "Signal", "端到端加密的私密通讯", "chat", ("signal-desktop", "signal")),
    App("element", "Element", "基于 Matrix 的去中心化聊天", "chat", ("element-desktop", "element")),

    # ---------------------------------------------------------------- 游戏与娱乐
    App("steam", "Steam", "最大的 PC 游戏平台", "games", ("steam",), featured=True),
    App("lutris", "Lutris", "统一管理 Linux 上的游戏", "games", ("lutris",)),
    App("heroic", "Heroic", "Epic 与 GOG 游戏启动器", "games",
        ("heroic-games-launcher-bin", "heroic-games-launcher")),
    App("prismlauncher", "Prism Launcher", "支持多实例的 Minecraft 启动器", "games", ("prismlauncher",)),
    App("retroarch", "RetroArch", "多平台模拟器前端", "games", ("retroarch",)),
    App("mangohud", "MangoHud", "游戏内性能悬浮层", "games", ("mangohud", "mangohud-common")),
    App("gamemode", "GameMode", "按需切换调度策略提升游戏性能", "games", ("gamemode",)),

    # ---------------------------------------------------------------- 系统工具
    App("htop", "htop", "经典的交互式进程查看器", "system", ("htop",), featured=True),
    App("btop", "btop++", "漂亮的资源监控面板", "system", ("btop", "btop++")),
    App("fastfetch", "Fastfetch", "快速好看的系统信息展示", "system", ("fastfetch", "neofetch")),
    App("timeshift", "Timeshift", "系统快照与一键回滚", "system", ("timeshift",)),
    App("gparted", "GParted", "图形化分区管理", "system", ("gparted",)),
    App("gnome-disks", "GNOME Disks", "磁盘、分区与镜像写入", "system",
        ("gnome-disk-utility", "gnome-disks", "gnome-disk-utility-bin")),
    App("filelight", "Filelight", "可视化磁盘占用", "system", ("filelight",)),
    App("baobab", "Baobab", "目录占用分析", "system", ("baobab",)),
    App("gdu", "gdu", "终端里的磁盘占用分析", "system", ("gdu",)),
    App("yazi", "Yazi", "用 Rust 写的终端文件管理器", "system", ("yazi",)),
    App("ranger", "ranger", "类 Vim 的终端文件管理器", "system", ("ranger",)),
    App("tmux", "tmux", "终端复用器，断开也不丢会话", "system", ("tmux",)),
    App("kitty", "kitty", "GPU 加速的终端", "system", ("kitty",)),
    App("alacritty", "Alacritty", "极速的 GPU 终端", "system", ("alacritty",)),
    App("starship", "Starship", "全 Shell 通用的提示符", "system", ("starship",)),
    App("zsh", "Zsh", "功能强大的交互式 Shell", "system", ("zsh",)),

    # ---------------------------------------------------------------- 安全与备份
    App("keepassxc", "KeePassXC", "本地离线的密码库", "security", ("keepassxc",), featured=True),
    App("bitwarden", "Bitwarden", "跨平台密码管理客户端", "security",
        ("bitwarden", "bitwarden-desktop", "bitwarden-bin")),
    App("veracrypt", "VeraCrypt", "磁盘与文件加密容器", "security", ("veracrypt",)),
    App("gnupg", "GnuPG", "OpenPGP 加密、签名与密钥管理", "security", ("gnupg",)),
    App("rclone", "rclone", "云存储同步与挂载", "security", ("rclone",)),
    App("restic", "restic", "去重加密的增量备份", "security", ("restic",)),
    App("bleachbit", "BleachBit", "清理系统垃圾与隐私痕迹", "security", ("bleachbit",)),

    # ---------------------------------------------------------------- 学习与科学
    App("anki", "Anki", "基于间隔重复的记忆卡片", "study", ("anki",), featured=True),
    App("zotero", "Zotero", "文献管理与引用", "study", ("zotero",)),
    App("stellarium", "Stellarium", "桌面虚拟天文馆", "study", ("stellarium",)),
    App("geogebra", "GeoGebra", "数学与几何的动态演示", "study", ("geogebra", "geogebra-6")),
    App("qgis", "QGIS", "开源地理信息系统", "study", ("qgis", "qgis-ltr")),
    App("octave", "GNU Octave", "与 MATLAB 兼容的数值计算", "study", ("octave", "octave-cli")),
    App("kalzium", "Kalzium", "元素周期表与化学数据", "study", ("kalzium",)),

    # ---------------------------------------------------------------- 容器与虚拟机
    App("distrobox", "Distrobox", "在终端里跑别的发行版", "virtual", ("distrobox",)),
    App("virtualbox", "VirtualBox", "经典的桌面虚拟机", "virtual",
        ("virtualbox", "virtualbox-bin", "virtualbox-host-modules-arch")),
    App("virt-manager", "virt-manager", "libvirt 的图形化管理界面", "virtual", ("virt-manager",)),
    App("qemu", "QEMU", "全虚拟化与跨架构仿真", "virtual",
        ("qemu-desktop", "qemu-full", "qemu-system-x86", "qemu-kvm")),
    App("waydroid", "Waydroid", "在 Wayland 里运行 Android 应用", "virtual", ("waydroid",)),

    # ---------------------------------------------------------------- 字体与美化
    App("papirus", "Papirus", "覆盖度极高的图标主题", "fonts", ("papirus-icon-theme",)),
    App("noto-sans", "Noto Sans", "无衬线字体全家桶", "fonts", ("noto-fonts",)),
    App("noto-cjk", "Noto Sans CJK", "中日韩字体，与思源系出同源", "fonts",
        ("noto-fonts-cjk", "fonts-noto-cjk")),
    App("jetbrains-mono", "JetBrains Mono Nerd", "带图标字形的编程字体", "fonts",
        ("ttf-jetbrains-mono-nerd", "ttf-jetbrains-mono", "fonts-jetbrains-mono")),
    App("kvantum", "Kvantum", "Qt 主题引擎", "fonts", ("kvantum", "qt6ct-kde")),
    App("fcitx5", "Fcitx5", "新一代输入法框架", "fonts", ("fcitx5",)),
    App("fcitx5-rime", "Fcitx5 Rime", "把 Rime 引擎接进 Fcitx5", "fonts", ("fcitx5-rime",)),
)

APPS_BY_KEY = {app.key: app for app in APPS}
CATEGORIES_BY_ID = {category.id: category for category in CATEGORIES}


def in_category(category_id):
    """某个分类下的应用；featured 是虚拟分类，返回所有精选应用。"""
    if category_id == "featured":
        return [app for app in APPS if app.featured]
    return [app for app in APPS if app.category == category_id]


def category_title(category_id):
    category = CATEGORIES_BY_ID.get(category_id)
    return category.title if category else category_id


def featured_apps():
    return [app for app in APPS if app.featured]
