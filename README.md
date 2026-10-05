# DICK — Detective Index Collection Kit

一个零运行时第三方依赖的 Python 原型：命令行之外还带一个本地 Web GUI。搜索直接读取系统镜像配置、下载远端索引，再用 SQLite 统一查询；本机列表和卸载直接读各原生管理器的已安装数据库。安装、卸载和升级按照发行版配置的来源优先级调用原生管理器。

## 安装与运行

需要 **Python 3.11+**。实际安装、卸载、升级用于 Linux；其他平台可以使用帮助、解析索引和 `--root` 测试源配置。

```bash
python -m pip install .
dick --help
dick source list
dick source scan
dick search firefox
dick list --source pacman
dick install firefox --dry-run
dick install firefox
dick upgrade
dick web            # 然后打开 http://127.0.0.1:3907
```

不安装也可以在项目目录运行 `python -m dick`。项目仅依赖 Python 标准库，包括 `urllib.request`、`sqlite3`、`tomllib`、`tarfile` 和 XML 解析器。

## 命令

命令面刻意与 pacman 的动作名保持一致，但全部使用长命令，不再支持 `-S`、`-Ss`、`-Sy` 这类 Arch 短语法。

| 命令 | 作用 |
| --- | --- |
| `dick source list` | 只读取配置文件，列出来源、启用状态与已识别仓库（不执行任何原生命令） |
| `dick source scan` | 让原生命令重新探测仓库（`flatpak remotes`、`ll-cli --json repo show` 等） |
| `dick source enable pacman apt` | 启用来源，写回配置文件 |
| `dick source disable snap flatpak` | 禁用来源，写回配置文件 |
| `dick install <包> [--source X]` | 按优先级安装，逐来源降级 |
| `dick remove <包> [--deep]` | 扫描本机已安装列表，列出同名/相似名候选后卸载 |
| `dick list [关键词] [--source X]` | 列出本机已安装的包 |
| `dick search <关键词> [--source X] [--exact]` | 搜索远端统一索引 |
| `dick update [--source X]` | 刷新 DICK 索引与按需查询缓存 |
| `dick upgrade [--source X]` | 调用系统管理器整机升级 |
| `dick web [--host H] [--port P] [--open] [--tls\|--no-tls] [--token T\|--no-token]` | 启动本地 Web GUI（应用商店界面），默认 `http://127.0.0.1:3907` |

全局选项：`--source`（可重复）、`--json`、`--dry-run`、`-y` / `--yes` / `--noconfirm`、`--exact`（搜索只做精确匹配，同时匹配 ID 别名）、`--deep`（仅 `remove`，连带清理依赖与配置）、`--limit`（每个来源显示上限，默认 50）、`--jobs`、`--config`、`--cache-dir`、`--root`、`--host` / `--port` / `--open` / `--tls` / `--token` / `--no-token`（仅 `web`）、`--version`、`-h`。

正常结果写入 stdout，警告、进度和错误写入 stderr，因此 `--json` 可以安全地管道给 `jq`。退出码 `0` 表示成功，`1` 表示出错、未找到或安装全部失败，`130` 表示 Ctrl+C。

## 来源支持

| 来源 | 搜索数据 | 本机列表 / 卸载 | 原生安装 | 当前范围 |
| --- | --- | --- | --- | --- |
| Pacman | `pacman.conf` 和递归 `Include` 中的 `Server` → `.db` | `pacman -Qn` / `pacman -R [-Rns]` | `pacman -S repo/name` | 首要链路，多镜像失败自动切换，zstd/gzip/xz/bzip2/未压缩 tar |
| AUR | 官方 RPC v5 的 search/info | `pacman -Qm` / `pacman -R [-Rns]` | `paru` 或 `yay` | Arch 上按需查询，默认缓存 15 分钟；不自行下载或构建 PKGBUILD |
| Flatpak | `flatpak remotes`、`flatpak remote-ls` 的 TSV | `flatpak list --app` / `flatpak uninstall` | `flatpak install remote ID` | 已配置远端的应用，默认 Flatpak 安装作用域 |
| APT | `.list` / Deb822 `.sources` → 各组件与架构的 `Packages` | `dpkg-query -W` / `apt-get remove\|purge` | `apt-get install` | 基础实现，xz/gzip/未压缩索引、平铺仓库、多文件合并 |
| DNF | `.repo` → `repomd.xml` → primary XML | `rpm -qa` / `dnf remove` | `dnf install` | 基础实现，baseurl/mirrorlist/metalink、元数据 checksum、架构筛选 |
| Snap | `snap find` 的列表输出 | `snap list` / `snap remove` | `snap install` | 已安装 Snap 时按需查询，TTL 缓存 |
| Linyaps（如意玲珑） | `ll-cli --json search` 的 JSON | `ll-cli --json list --type=app` / `ll-cli uninstall` | `ll-cli install ID` | 已安装 `ll-cli` 时按需查询，TTL 缓存；仓库来自 `ll-cli --json repo show`，读取失败时退化为单一 `linglong` 仓库 |

Pacman/APT/DNF 搜索不执行 `pacman -Ss`、`apt search` 或 `dnf search`。Flatpak 的 OSTree/AppStream 数据需要额外协议和解析依赖，Snap 商店和 Linyaps 仓库也没有本原型采用的稳定公开全量索引格式（Linyaps 依赖 `ll-cli` 自己输出 JSON），所以按需求允许的例外使用原生命令。Flatpak/Snap/Linyaps 版本过旧、无远端或输出列不兼容时会报告错误。

AUR 在 Arch 上没有独立数据库，`pacman -Qm` 列出的是同步数据库之外的外来包（AUR 或手工构建），因此与 `pacman -Qn` 分开统计，同一个包不会被列出两次。Flatpak/Snap/Linyaps 只参与搜索、列表、安装和卸载；`upgrade` 只作用于 pacman/apt/dnf 这类系统管理器。

APT 的 `.sources` 支持 `Types`、`Enabled`、`URIs`、`Suites`、`Components`、`Architectures`；高级架构增减字段、APT pinning、所有发行版特有配置不在原型范围内。DNF 展开 `$basearch`、`$arch`、`$releasever`；其他变量会报错。DNF **尚未复现模块流过滤**，发现模块元数据时会提示：搜索结果可能包含未启用流中的包，实际可安装性由原生 dnf 判断。Python 3.14 使用标准库 zstd；Python 3.11–3.13 使用系统 `zstd` 命令。不声称完全兼容全部镜像格式。

## 来源的启用与禁用

`source enable` / `source disable` 把结果写入配置文件的 `[sources] enabled`，使用外科式编辑，保留其余内容和注释；配置文件或 `[sources]` 段不存在时会创建。禁用后 `search`、`install`、`list`、`update` 都会跳过该来源；显式传入 `--source` 指向已禁用来源会直接报错，并提示运行 `dick source enable`。

`source list` 只读配置，无 `flatpak`、`ll-cli` 或对应原生工具也能运行；`source scan` 才会真正执行原生命令重新探测远端仓库，两者的输出差异就是探测结果。

## 搜索与缓存

```bash
dick search browser
dick search firefox --source pacman --source aur
dick search firefox --exact
dick search calculator --source linyaps
dick search firefox --json --limit 100
dick update --source pacman
dick source scan --json
```

统一包记录包含 `name`、`source`、`description`、`version`，以及 `repository`、`architecture`。不同来源与仓库保留独立结果，避免丢失版本或安装位置。

SQLite 默认在 `$XDG_CACHE_HOME/dick/index.sqlite3`，未设置时为 `~/.cache/dick/index.sqlite3`。第一次搜索缺少本地快照时自动拉取所选来源的索引；后续直接查本地快照，通过 `dick update` 手动更新。刷新逐仓库事务提交，下载或解析失败会保留该仓库上一次成功快照，其他仓库可以继续刷新。已移除或已禁用的源不参与搜索。

AUR/Snap/Linyaps 没有全量本地索引，查询结果按关键词缓存，`dick update` 会清空它们的查询缓存。查询失败会打印警告，跨源搜索仍展示成功来源；JSON 的 `errors` 字段保留失败信息。完整刷新有任意失败时返回非零状态。

## 本机列表与卸载

```bash
dick list
dick list --source pacman
dick list firefox --limit 50
dick remove firefox --dry-run
dick remove firefox
dick remove firefox --deep
dick remove firefox --source flatpak --yes
```

`list` 直接执行各来源的已安装列表命令（见来源支持表），按来源分组、按名称排序，`--limit` 是**每个来源**的显示上限，超出时在 stderr 提示总数。带关键词时按「完整名称、反向 DNS 末段、名称中包含」的顺序匹配，因此 `list firefox` 也会列出 `firefox-esr`。

`remove` 不推测应用来源，而是扫描所有启用来源的已安装列表，按「完全同名 → ID 末段 → 前缀 → 子串」排序后展示候选：

- 只有一个候选时，交互终端会要求 `[y/N]` 确认；`--yes` 跳过确认。
- 多个候选时，交互终端打印编号列表，可以用 `1` 或 `1,3` 选择，直接回车取消。
- 非交互（管道、`--json`）遇到多个候选会报错并列出全部候选，要求用 `--source` 或完整名称指定，不会自行猜测。
- 非交互卸载必须显式给出 `--yes`，否则报错，避免脚本意外删除软件；`--dry-run` 只预览命令，因此不需要 `--yes`。

`--deep` 翻译为 Pacman `-Rns`、APT `purge --autoremove`、DNF 的依赖清理配置、Flatpak `--delete-data`；各工具语义不完全相同。Flatpak、Snap、Linyaps 的卸载直接交给各自工具，玲珑不需要 `sudo`。

## 安装与降级

Arch 默认 `pacman → aur → flatpak → linyaps → snap`；Debian 默认 `apt → flatpak → linyaps → snap`；Fedora 默认 `dnf → flatpak → linyaps → snap`。**snap 是固定垫底的兜底来源**：不管 `priority.order` 怎么写，它都会被挪到最后一名；配置里漏掉它也会自动补上（体积大、首次启动慢、桌面集成最差，只在其它来源都没有时才用）。逐来源查询准确名称，跳过不存在或不可执行的来源；原生命令失败后尝试下一来源。Ctrl+C 或信号退出会停止降级。多包安装逐包处理，结果逐项报告。

```bash
dick install firefox
dick install firefox --source pacman
dick install org.mozilla.firefox --source flatpak
dick install org.deepin.calculator --source linyaps
dick install firefox --dry-run --json
```

Flatpak 优先匹配完整应用 ID，也支持唯一的 ID 末段别名：`firefox` 可以定位 `org.mozilla.firefox`。多个不同 ID 匹配时要求使用完整 ID。Linyaps 同样接受完整应用 ID、反向 DNS 末段或显示名（`calculator`、`deepin-calculator`、`org.deepin.calculator` 都可定位），多个 ID 匹配时要求完整 ID；安装命令只传应用 ID，具体仓库由 `ll-cli` 自身的仓库优先级解析。其他来源要求同名；此原型不维护“同一软件在不同生态中的所有别名”数据库，也不自动选择 Snap classic 模式等额外权限。

`--dry-run` 显示要执行的原生命令；仍可能联网查询、更新 DICK 缓存。它不调用安装、卸载、升级命令，也无法预测这些命令真正执行后会不会失败。AUR 安装需要事先安装 `paru` 或 `yay`，并以普通用户运行。系统管理器需要 root 或 sudo；Flatpak/AUR/Linyaps 交由各自工具处理权限，玲珑默认安装到用户目录，DICK 不会为它加 `sudo`。

**DICK 的索引与原生管理器本地数据库是两套数据。** `dick update` 只更新 DICK，原生安装仍使用管理器自身的数据库、签名、依赖和优先级规则。Arch 请保持系统正常滚动更新（`dick upgrade` 会调用 `sudo pacman -Syu`）；APT 如需同步原生数据库使用 `sudo apt-get update`。DICK 不以单独的原生 `pacman -Sy` 自动引入局部升级风险。

刷新会并行下载和解析多个仓库索引，默认并发数为 4，可用 `--jobs 2` 或配置文件中的 `[network] workers = 2` 调整。索引写入 SQLite 时使用事务和线程锁；原生安装、卸载和升级保持串行，因为 pacman、apt、dnf 各自会锁定系统数据库。交互终端会在 stderr 显示一个进度条：Arch 使用 pacman 风格的 `:: Synchronizing package databases...`，Debian 使用 apt 风格的 `Get:`，Fedora 使用 dnf 风格的仓库刷新行。`--json` 不输出进度字符，保证 stdout 始终是有效 JSON。

`upgrade` 只作用于系统管理器，翻译为 Pacman `-Syu`、APT `upgrade` 或 DNF `upgrade`；没有可用的系统管理器时会明确报错。其他动作不支持的来源会跳过而不是报错。

索引是搜索线索，原型不独立验证 APT Release 签名或 Pacman 数据库签名。安装认证由原生管理器负责。失败的原生安装可能已经下载或改变部分状态，自动降级不会回滚前一管理器的操作；建议先检查 `--dry-run`，默认保留交互确认。

## Web GUI（应用商店界面）

`dick web` 启动一个只用标准库 `http.server` 写的本地界面，默认监听 `http://127.0.0.1:3907`（`--host` / `--port` 可改，`--open` 自动打开浏览器）。要给手机或别的电脑用，就加 `--tls`：会用 `openssl` 在配置目录旁生成自签证书，地址变成 `https://<局域网地址>:3907`，首次访问需要在浏览器里确认一次证书警告。视觉参考 Claude 官网：象牙白 `#F0EEE6` 纸底、暖黑 `#1B1A18` 深色主题、衬线标题配无衬线正文，主题选择保存在浏览器 `localStorage`。界面与后端都在 `dick/webui/`、`dick/web.py` 和 `dick/catalog.py` 里，没有 CDN，也没有前端框架。

- 首页：像应用商店一样先给内容——「今日精选」大卡按日期轮换，下面是 13 个分类的横向货架（编辑精选 / 开发工具 / 网络与下载 / 图形与设计 / 影音播放 / 办公与文档 / 通讯社交 / 游戏与娱乐 / 系统工具 / 安全与备份 / 学习与科学 / 容器与虚拟机 / 字体与美化），卡片可以直接「获取（先演练）」，也能点进详情页。选品是 `dick/catalog.py` 里的静态目录（每个应用带中文一句话简介和多个包名候选，按发行版回落），版本、仓库、描述与安装状态由 `GET /api/featured` 在本机索引里解析，没有命中就退化成「去搜索」。
- 分类浏览：侧栏第二个入口列出全部分类、图标与收录数量，点进去是完整网格；详情页底部还有「同分类推荐」。
- 来源：侧栏列出全部来源、启用开关与已识别仓库；「重新扫描」按钮等同于 `dick source scan`，会真正调用 `flatpak remotes`、`ll-cli repo show` 等原生命令。
- 搜索：顶部搜索框随时可用，支持来源过滤、精确匹配、分来源排序，结果卡片显示来源、版本、仓库与描述，标出哪些已安装；筛选条只在搜索结果页出现。
- 已安装：读取各管理器的本机数据库（`pacman -Qn` / `-Qm`、`dpkg-query`、`rpm -qa`、`flatpak list`、`snap list`、`ll-cli list`）。
- 变更：安装、卸载、升级、刷新索引都先在网页里演练并展示命令与日志；接口层不带 `dry_run` 或 `confirm` 的变更请求会被直接拒绝，所以**每次真实系统变更都必须先预览再确认**。卸载会先列出本机的同名/相似名候选让人选择。原生命令的 stdout/stderr（含下载进度、`pacman`/`apt` 的输出）会实时流进任务日志，失败时能直接看到原生工具的原话。
- 权限（网页里的提权）：网页任务没有终端，pacman / apt / dnf 的安装、卸载、升级需要提权。点「确认执行」时会弹出密码框，填入 sudo 密码即可——密码只通过 stdin 交给 `sudo -S`，不落盘、不进日志、任务结束就丢弃；密码错误会在日志里说清并允许重试，连续错 5 次会冷却 60 秒。也可以完全不用密码框：在宿主终端里配一次免密（`echo "$USER ALL=(root) NOPASSWD: /usr/sbin/pacman, /usr/bin/pacman" | sudo tee /etc/sudoers.d/dick >/dev/null && sudo chmod 440 /etc/sudoers.d/dick`；`/usr/sbin` 常是 `/usr/bin` 的软链，所以两个路径都写上），日志里也会打印这条可照抄的命令。AUR（`paru` / `yay`）、Flatpak、Linyaps 由各自工具处理权限，不需要提权。CLI 在真终端里跑（含 `--json`）不受这条限制，`sudo` 照常自己弹密码提示。
- 图标：按需从 Flathub（`dl.flathub.org` 直链与 `flathub.org/api/v2/search`）和 Snapcraft（`api.snapcraft.io`）抓取，缓存在 `cache_dir/icons`（命中 30 天、未命中 6 小时），抓不到时生成暖色字母头像。
- AI 翻译：填好 OpenAI 兼容或 Anthropic 的 `base_url`、`model`、API key 后，可把结果里的包描述翻译成目标语言（默认「中文」），译文按「模型 + 目标语言 + 原文」缓存在 `cache_dir/translations`，同一段描述只翻译一次。
- 安全：默认只绑定回环地址，但仍然接一层访问令牌（首次启动自动生成 12 位十六进制，写在配置目录的 `web-token` 里并打印在启动横幅上；`--token` 指定、`--no-token` 关闭）。打开 `0.0.0.0` 给局域网时务必同时开 `--tls`：界面里有 sudo 密码框，明文 HTTP 会把密码暴露给同网段的人。前端把令牌存在 `localStorage` 并随请求走 `X-Dick-Token` 头（图片等 `GET` 用 `?token=`），静态页面本身不带数据、不需要令牌即可加载；令牌不对时接口回 `401` 并弹出入令牌的界面。前端自己「加密」再发明文没有意义（页面、密钥与密码走同一根网线，谁都能改页面），所以要传输安全就只有 TLS 这一条路。

网页用的接口（脚本也可直接调用）：`GET /api/status`、`GET /api/sources?scan=1`、`POST /api/sources/enable|disable`、`GET /api/featured?sources=`、`GET /api/search?q=&sources=&exact=&limit=`、`GET /api/installed`、`GET /api/candidates?target=`、`GET /api/package?source=&name=`、`POST /api/action`、`GET /api/job/<id>?since=`、`GET|POST /api/ai`、`POST /api/ai/test`、`POST /api/translate`、`GET /api/icon?source=&name=`。任务日志用 `?since=<offset>` 增量拉取。开了令牌后，接口都需要 `X-Dick-Token` 头（或 `?token=`）：包括 `GET /`、`/assets/app.js`、`/assets/app.css` 在内的静态骨架不用令牌，其余接口没有令牌一律 `401`，返回体里带 `"code": "token"`。`POST /api/action` 接受可选的 `password` 字段（就是上面 sudo 密码框的值，接口只回传 `"password": true`，绝不回显明文）。

## 配置

保存配置至 `$XDG_CONFIG_HOME/dick/config.toml`（默认 `~/.config/dick/config.toml`），或传入 `--config`。完整示例见 `config.example.toml`。

```toml
[syntax]
prefer = "auto"

[sources]
enabled = ["pacman", "aur", "flatpak", "linyaps"]

# snap 无论怎么写都会被挪到最后一名（漏写会自动补上），它是兜底来源。
[priority.arch]
order = ["pacman", "aur", "flatpak", "linyaps", "snap"]

[priority.debian]
order = ["apt", "flatpak", "linyaps", "snap"]

[cache]
ttl = 900

[network]
timeout = 20
max_bytes = 67108864

[web]
host = "127.0.0.1"
port = 3907
tls = false          # 局域网使用时打开：自动生成自签证书（cert / key 可指向自己的证书）
# cert = "/etc/dick/server.crt"
# key = "/etc/dick/server.key"
# token = ""         # 留空则自动生成并写入配置目录的 web-token

[ai]
enabled = false
base_url = "https://api.deepseek.com/v1"
model = "deepseek-chat"
api_key = ""
target = "中文"
timeout = 60
```

省略 `[sources] enabled` 时全部来源启用。`enabled` 必须是合法来源列表，出现未知来源或错误类型会直接报错，避免拼写错误被静默忽略。

`[web] host` / `port` 决定 `dick web` 的监听地址（默认 `127.0.0.1:3907`），`tls` 打开自签 HTTPS，`cert` / `key` 指向自定义证书，`token` 固定访问令牌（留空则首次启动生成并写入配置目录的 `web-token`）；命令行上的 `--host` / `--port` / `--tls` / `--token` / `--no-token` 优先。`[ai]` 段只在网页里用到：`base_url` 兼容 OpenAI（`/chat/completions`）与 Anthropic（`/messages`，按 URL 里是否含 `anthropic` 判定），`target` 是目标语言，`prompt` 可覆盖内置的翻译提示词；`DICK_AI_API_KEY`、`DICK_AI_BASE_URL`、`DICK_AI_MODEL`、`DICK_AI_TARGET`、`DICK_AI_ENABLED` 环境变量优先于配置文件。网页设置页保存时只改写 `[ai]` 段，注释和其他段原样保留。

默认每次下载上限 64 MiB、解压上限 256 MiB；大仓库需要调整下载上限，超出解压上限会明确失败。下载使用系统的 HTTPS 证书校验，支持标准代理环境变量；支持 `file://` 方便离线测试。

## 结构与验证

`discovery.py` 读取源配置，`network.py` 下载与解压，`parsers.py` 解析各类索引，`cache.py` 管理 SQLite，`index.py` 合并本地和 RPC 查询，`local.py` 读取已安装列表，`syntax.py` 归一化 CLI，`install.py` 翻译和执行原生命令，`security.py` 生成自签证书与访问令牌，`cli.py` 组织流程；`web.py` 是 Web GUI 的 HTTP 与业务层，`catalog.py` 放首页的精选目录，`webui/` 放静态界面，`ai.py` 负责可选的翻译接口。

```bash
python -m unittest discover -s tests
python -m compileall -q dick
```

测试使用临时文件、本地镜像和模拟原生命令，无需外网或 root，不会安装软件。可以用 `--root /path/to/fixture --cache-dir /tmp/dick-test` 读取测试系统的源配置；`--root` 不同于 `/` 时只允许搜索、刷新或变更命令的 `--dry-run`。

## 许可证

MIT，见 [LICENSE](LICENSE)。
