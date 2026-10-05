#!/bin/sh
# SyncOJ Agent 在线自举入口。
#
#   curl -fsSL https://<服务端>/dist/bootstrap.sh | sudo sh -s -- \
#       --server https://<服务端> --bootstrap-key <43 字符的密钥> --user student
#
# 这个脚本刻意保持极短：它只负责"把安装器和安装包弄下来，然后交给 Python 安装器"。
# 真正的逻辑全在 install.py 里 —— shell 是写错难查、且无法被 CI 覆盖的地方，
# 能少写就少写。
#
# 注意：本脚本与安装包都应通过 HTTPS 获取，并用 --sha256 校验安装包完整性，
# 否则一个被劫持的下载链接就能给整场考试装上后门。
#
# 密钥是**秘密**：它会出现在这里的命令行参数里，也就可能出现在 shell 历史和
# `ps` 输出里。镜像预装场景下请改用 install.py 直接传参，或用环境变量注入。

set -eu

SERVER=""
BOOTSTRAP_KEY=""
SCAN_ROOT=""
DEPLOY_ROOT=""
SHA256=""
EXTRA=""

# 简易参数解析：不认识的一律透传给 install.py
while [ $# -gt 0 ]; do
    case "$1" in
        --server)        SERVER="$2"; shift 2 ;;
        --bootstrap-key) BOOTSTRAP_KEY="$2"; shift 2 ;;
        --scan-root)     SCAN_ROOT="$2"; shift 2 ;;
        --deploy-root)   DEPLOY_ROOT="$2"; shift 2 ;;
        --sha256)        SHA256="$2"; shift 2 ;;
        --)              shift; EXTRA="$*"; break ;;
        -h|--help)
            sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) EXTRA="$EXTRA $1"; shift ;;
    esac
done

if [ -z "$SERVER" ]; then
    echo "错误：必须指定 --server" >&2
    exit 2
fi

PYTHON=""
for candidate in python3 /usr/bin/python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        PYTHON="$candidate"
        break
    fi
done
if [ -z "$PYTHON" ]; then
    echo "错误：找不到 python3。Agent 与安装器都只需要 Python 3 标准库。" >&2
    exit 2
fi

TMPDIR_DL="${TMPDIR:-/tmp}/syncoj-bootstrap.$$"
mkdir -p "$TMPDIR_DL"
# 无论成功失败都清掉临时目录 —— 残留的安装包会占掉几十 MB，而考试机磁盘不宽裕
trap 'rm -rf "$TMPDIR_DL"' EXIT INT TERM

BASE="${SERVER%/}"

echo "==> 下载安装器"
curl -fsSL "$BASE/dist/install.py" -o "$TMPDIR_DL/install.py"

echo "==> 下载 Agent 安装包"
curl -fsSL "$BASE/dist/syncoj-agent-bundle.tar.gz" -o "$TMPDIR_DL/bundle.tar.gz"

echo "==> 执行安装"
set -- --bundle "$TMPDIR_DL/bundle.tar.gz" \
       --server "$SERVER" \
       --scan-root "${SCAN_ROOT:-/home/student/code}" \
       --deploy-root "${DEPLOY_ROOT:-/home/student/exam}"
# 只在真的给了密钥时才传：已经有密钥的机器重复自举时不该被一个空值覆盖
if [ -n "$BOOTSTRAP_KEY" ]; then
    set -- "$@" --bootstrap-key "$BOOTSTRAP_KEY"
fi
[ -n "$SHA256" ] && set -- "$@" --sha256 "$SHA256"
# shellcheck disable=SC2086
exec "$PYTHON" "$TMPDIR_DL/install.py" "$@" $EXTRA
