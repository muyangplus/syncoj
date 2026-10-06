#!/bin/sh
# SyncOJ Agent 在线自举入口。
#
#   curl -fsSL http://<服务端>/api/v1/agent/install/bootstrap.sh | sudo sh -s -- \
#       --server http://<服务端> --bootstrap-key <43 字符的密钥> --user student
#
# 这个脚本刻意保持极短：它只负责"把安装器弄下来、跑起来"，包体交给 install.py
# 自己取（它才会**对着台账校验 sha256**；给它一个已经下好的文件反而少一层核对）。
# 真正的逻辑全在 install.py 里 —— shell 是写错难查、且无法被 CI 覆盖的地方，
# 能少写就少写。
#
# 装机入口（`/api/v1/agent/install/*`）**不鉴权**：此刻这台机器上什么都没有，
# 没有任何凭据可用。装完之后靠**配对**建立信任 —— 机器注册上来是待认领状态，
# 教师必须在管理界面把它绑到名单里的某个人。
#
# 想在这一步就验签（比只对 sha256 强得多）：把 `release-key.pub.json` 放进镜像，
# 用 `RELEASE_PUBLIC_KEY=<路径>` 传进来，install.py 就会验。
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
# 这三个 /api/v1/agent/install* 端点是**装机入口**，故意不鉴权 —— 此刻这台机器
# 上什么都没有，没有任何凭据可用。装完之后靠"配对"建立信任：机器注册上来是
# 待认领状态，教师必须在管理界面把它绑到名单里的某个人。
#
# 从前这里 curl 的是 `/dist/install.py` —— 那是照着一台静态文件服务器写的，
# 而 SyncOJ 服务端从来没有 `/dist/` 这条路。也就是说这条自举链一直是断的。
curl -fsSL "$BASE/api/v1/agent/install/installer" -o "$TMPDIR_DL/install.py"

echo "==> 执行安装"
# 包体不再单独下载：交给 install.py 自己取，因为它会**对照台账校验 sha256**，
# 有两样东西时才知道该信哪一个。给它一个已经下好的文件反而少了一层核对。
set -- --from-server \
       --server "$SERVER" \
       --scan-root "${SCAN_ROOT:-/home/student/code}" \
       --deploy-root "${DEPLOY_ROOT:-/home/student/exam}"
# 只在真的给了密钥时才传：已经有密钥的机器重复自举时不该被一个空值覆盖
if [ -n "$BOOTSTRAP_KEY" ]; then
    set -- "$@" --bootstrap-key "$BOOTSTRAP_KEY"
fi
# 有公钥就会**验签**（比只对 sha256 强得多）。镜像里预置了的话可以靠
# RELEASE_PUBLIC_KEY 环境变量传进来。
if [ -n "${RELEASE_PUBLIC_KEY:-}" ]; then
    set -- "$@" --public-key "$RELEASE_PUBLIC_KEY"
fi
[ -n "$SHA256" ] && set -- "$@" --sha256 "$SHA256"
# shellcheck disable=SC2086
exec "$PYTHON" "$TMPDIR_DL/install.py" "$@" $EXTRA
