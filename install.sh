#!/usr/bin/env bash
# DICK 一键安装：把 DICK 装进一个独立 venv，并在 <前缀>/bin 放一个 dick 命令。
#
#   curl -fsSL https://raw.githubusercontent.com/yunli396/Dick/main/install.sh | bash
#
# 选项（用 `bash -s -- <选项>` 或 `bash install.sh <选项>` 传）：
#   --ref <分支/标签>   默认 main
#   --dir <前缀>        默认 ~/.local（命令 → <前缀>/bin/dick，程序 → <前缀>/share/dick）
#   --from <目录>       用本地源码目录安装，而不是从 GitHub 拉取
#   --uninstall         卸载（删掉程序目录与命令）
#   -h | --help         显示帮助
#
# 环境变量：DICK_REPO、DICK_REF、DICK_PREFIX 分别对应仓库地址、分支、前缀。
set -euo pipefail

REPO_URL="${DICK_REPO:-https://github.com/yunli396/Dick.git}"
REF="${DICK_REF:-main}"
PREFIX="${DICK_PREFIX:-$HOME/.local}"
FROM=""
UNINSTALL=0

usage() {
    sed -n '2,16p' "$0" 2>/dev/null | sed 's/^# \{0,1\}//' || true
}

while [ $# -gt 0 ]; do
    case "$1" in
        --ref) REF="${2:?--ref 需要一个分支或标签}"; shift 2 ;;
        --dir|--prefix) PREFIX="${2:?--dir 需要一个路径}"; shift 2 ;;
        --from) FROM="${2:?--from 需要一个目录}"; shift 2 ;;
        --uninstall) UNINSTALL=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "未知参数：$1（用 --help 看用法）" >&2; exit 2 ;;
    esac
done

PREFIX="${PREFIX/#\~/$HOME}"
BIN="$PREFIX/bin"
SHARE="$PREFIX/share/dick"
VENV="$SHARE/venv"
SRC="$SHARE/src"

say() { printf '%s\n' "$*"; }
die() { printf '错误：%s\n' "$*" >&2; exit 1; }

if [ "$UNINSTALL" = "1" ]; then
    rm -rf "$SHARE" "$BIN/dick"
    say "已卸载：删除了 $SHARE 和 $BIN/dick"
    exit 0
fi

# 1) Python：必须 3.11+（项目只依赖标准库，venv 用来隔离，不污染系统 Python）
command -v python3 >/dev/null 2>&1 || die "没找到 python3，请先安装 Python 3.11 或更新版本"
PY_VERSION="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
    || die "需要 Python 3.11+，当前是 $PY_VERSION"
say "Python $PY_VERSION ✓"

# 2) 虚拟环境：放在 $SHARE/venv，这样不用 sudo，也不会碰系统 Python
if [ ! -x "$VENV/bin/python" ]; then
    mkdir -p "$SHARE"
    python3 -m venv "$VENV" || die "创建虚拟环境失败。Debian/Ubuntu 上可能需要：sudo apt install python3-venv"
fi
say "虚拟环境 $VENV ✓"

# 3) 取源码：优先 git（以后能直接再跑一次升级），没有 git 就下 tarball
TEMP_SRC=""
cleanup() { [ -n "$TEMP_SRC" ] && rm -rf "$TEMP_SRC"; }
trap cleanup EXIT
if [ -n "$FROM" ]; then
    [ -f "$FROM/pyproject.toml" ] || die "$FROM 里没有 pyproject.toml，不是 DICK 源码目录"
    SOURCE_DIR="$(cd "$FROM" && pwd)"
    say "用本地源码 $SOURCE_DIR"
elif command -v git >/dev/null 2>&1; then
    if [ -d "$SRC/.git" ]; then
        say "更新已有源码 $SRC（$REF）"
        git -C "$SRC" fetch --depth 1 origin "$REF"
        git -C "$SRC" checkout -q -f FETCH_HEAD
    else
        say "克隆 $REPO_URL（$REF）"
        rm -rf "$SRC"
        git clone --depth 1 --branch "$REF" "$REPO_URL" "$SRC"
    fi
    SOURCE_DIR="$SRC"
else
    TARBALL="https://codeload.github.com/yunli396/Dick/tar.gz/refs/heads/$REF"
    say "没装 git，改从 $TARBALL 下载源码"
    command -v curl >/dev/null 2>&1 || die "既没有 git 也没有 curl，请先装其中一个"
    TEMP_SRC="$(mktemp -d)"
    curl -fsSL "$TARBALL" | tar -xz -C "$TEMP_SRC" --strip-components=1
    SOURCE_DIR="$TEMP_SRC"
fi

# 4) 安装进 venv，再把命令软链到 $BIN
say "安装 DICK 到虚拟环境…"
"$VENV/bin/pip" install --quiet --upgrade pip >/dev/null 2>&1 || true
"$VENV/bin/pip" install --quiet --upgrade "$SOURCE_DIR" \
    || die "安装失败。若卡在下载 setuptools，请确认能访问 PyPI（或配置 pip 镜像）"
mkdir -p "$BIN"
ln -sfn "$VENV/bin/dick" "$BIN/dick"

"$BIN/dick" --help >/dev/null 2>&1 || die "装完了但 $BIN/dick 跑不起来，请把上面的输出发出来"
say ""
say "DICK 装好了 ✓  $("$VENV/bin/python" -c 'import dick; print(dick.__version__)')"
say ""
say "  dick --help          看全部命令"
say "  dick source list     看有哪些来源"
say "  dick source scan     下载索引（第一次用之前跑一次）"
say "  dick search firefox  搜索"
say "  dick install firefox 安装（会先演练，确认后执行）"
say "  dick web             网页应用商店：http://127.0.0.1:3907"
say "    给别的设备用：dick web --host 0.0.0.0 --tls（手机首次访问要确认一次自签证书）"
say ""

if ! case ":$PATH:" in *":$BIN:"*) false ;; *) true ;; esac; then
    say "注意：$BIN 不在 PATH 里，把它加进去才能直接敲 dick："
    say "  echo 'export PATH=\"$BIN:\$PATH\"' >> ~/.bashrc && source ~/.bashrc"
    say ""
fi
say "卸载：curl -fsSL https://raw.githubusercontent.com/yunli396/Dick/main/install.sh | bash -s -- --uninstall"
