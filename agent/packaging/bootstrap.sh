#!/bin/sh
# SyncOJ Agent 在线自举入口（装机 / 卸载共用这一条）。
#
#   # 装机：服务端地址必须给
#   curl -fsSL http://<服务端>/api/v1/agent/install/bootstrap.sh \
#     | sudo sh -s -- --server http://<服务端> --bootstrap-key <43 字符密钥> --user <账号>
#
#   # 卸载：地址从机器上已有的 agent.ini 里读，不必再抄一遍
#   curl -fsSL http://<服务端>/api/v1/agent/install/bootstrap.sh \
#     | sudo sh -s -- --uninstall --yes
#
# 这个脚本刻意保持极短：它只负责"把安装器弄下来、跑起来"，真正的逻辑全在
# install.py 里 —— shell 是写错难查、且无法被 CI 覆盖的地方，能少写就少写。
# 包体也交给 install.py 自己取（它才会**对着台账校验 sha256**；给它一个已经
# 下好的文件反而少一层核对）。
#
# 装机入口（`/api/v1/agent/install/*`）**不鉴权**：此刻这台机器上什么都没有，
# 没有任何凭据可用。装完之后靠**配对**建立信任。
#
# 想在这一步就验签（比只对 sha256 强得多）：把 `release-key.pub.json` 放进镜像，
# 用 `RELEASE_PUBLIC_KEY=<路径>` 传进来，install.py 就会验。
#
# 密钥是**秘密**：它会出现在命令行参数里，也就可能出现在 shell 历史和 `ps`
# 输出里。镜像预装场景下请改用 install.py 直接传参，或用环境变量注入。

set -eu

SERVER=""
BOOTSTRAP_KEY=""
SCAN_ROOT=""
DEPLOY_ROOT=""
SHA256=""
UNINSTALL=0
YES=0
DRY_RUN=0
KEEP_STATE=0
KEEP_USER=0
EXTRA=""

# 简易参数解析：认识的参数决定"拼哪条 install.py 命令行"，不认识的一律透传。
while [ $# -gt 0 ]; do
    case "$1" in
        --uninstall)     UNINSTALL=1; shift ;;
        --yes)           YES=1; shift ;;
        --dry-run)       DRY_RUN=1; shift ;;
        --keep-state)    KEEP_STATE=1; shift ;;
        --keep-user)     KEEP_USER=1; shift ;;
        --server)        SERVER="$2"; shift 2 ;;
        --bootstrap-key) BOOTSTRAP_KEY="$2"; shift 2 ;;
        --scan-root)     SCAN_ROOT="$2"; shift 2 ;;
        --deploy-root)   DEPLOY_ROOT="$2"; shift 2 ;;
        --sha256)        SHA256="$2"; shift 2 ;;
        --)              shift; EXTRA="$*"; break ;;
        -h|--help)
            # 打印开头那段注释块。**不要写死行号**（以前是 '2,16p'）：
            # 上面注释一增删就会错位、打印半截。按第一个空行截断才稳。
            sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) EXTRA="$EXTRA $1"; shift ;;
    esac
done

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

# 卸载那条命令（装机页生成的就是它）**不带 --server**，但 install.py 还是得先
# 下下来 —— 离线包刻意不含 packaging/，装完机器上只剩 run_agent.py。要卸载的
# 机器上必然有当初写好的 agent.ini，里面有它连的服务端地址，就拿它来取安装器。
# 这是唯一"不用人再抄一遍地址"的来源。`SYNCOJ_CONFIG_DIR` 只在装机时用了
# 非默认配置目录时才需要（默认 /etc/syncoj）。
CONFIG_DIR="${SYNCOJ_CONFIG_DIR:-/etc/syncoj}"
if [ -z "$SERVER" ] && [ -f "$CONFIG_DIR/agent.ini" ]; then
    SERVER="$(sed -n 's/^[[:space:]]*url[[:space:]]*=[[:space:]]*//p' "$CONFIG_DIR/agent.ini" | head -n 1)"
    if [ -n "$SERVER" ]; then
        echo "==> 从 $CONFIG_DIR/agent.ini 读到服务端地址：$SERVER"
    fi
fi

if [ -z "$SERVER" ]; then
    if [ "$UNINSTALL" -eq 1 ]; then
        echo "错误：卸载也要先把安装器取回来，但没找到服务端地址。请显式指定 --server。" >&2
    else
        echo "错误：必须指定 --server" >&2
    fi
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

if [ "$UNINSTALL" -eq 1 ]; then
    echo "==> 执行卸载"
    # 卸载不注入任何安装相关的参数：不需要服务端地址、不下载包、不碰扫描/下发根。
    # 其余开关原样透传（--yes 是硬要求：管道里 stdin 不是终端，install.py 会拒绝）。
    set -- --uninstall
    if [ "$YES" -eq 1 ]; then
        set -- "$@" --yes
    fi
    if [ "$DRY_RUN" -eq 1 ]; then
        set -- "$@" --dry-run
    fi
    if [ "$KEEP_STATE" -eq 1 ]; then
        set -- "$@" --keep-state
    fi
    if [ "$KEEP_USER" -eq 1 ]; then
        set -- "$@" --keep-user
    fi
else
    echo "==> 执行安装"
    # 包体不再单独下载：交给 install.py 自己取，因为它会**对照台账校验 sha256**，
    # 有两样东西时才知道该信哪一个。给它一个已经下好的文件反而少了一层核对。
    #
    # 扫描根/下发根**刻意不给默认值**：默认只由 install.py 一处持有（运行账号家
    # 目录下的桌面，安装时按那个账号展开）。以前这里写死
    # `${SCAN_ROOT:-/home/student/code}` / `${DEPLOY_ROOT:-/home/student/exam}`，
    # 于是推荐的一条 curl 装机路径**无条件**把这两个值传下去，install.py 里改好的
    # 默认被整个覆盖 —— 现场仍然扫 /home/student/code，而且这条路径会被塞进
    # systemd 单元的沙箱白名单，服务直接 226/NAMESPACE 起不来。两份默认值各自
    # 漂移就是这个坑。要覆盖就显式传 --scan-root / --deploy-root。
    set -- --from-server \
           --server "$SERVER"
    if [ -n "$SCAN_ROOT" ]; then
        set -- "$@" --scan-root "$SCAN_ROOT"
    fi
    if [ -n "$DEPLOY_ROOT" ]; then
        set -- "$@" --deploy-root "$DEPLOY_ROOT"
    fi
    # 只在真的给了密钥时才传：已经有密钥的机器重复自举时不该被一个空值覆盖
    if [ -n "$BOOTSTRAP_KEY" ]; then
        set -- "$@" --bootstrap-key "$BOOTSTRAP_KEY"
    fi
    # 有公钥就会**验签**（比只对 sha256 强得多）。镜像里预置了的话可以靠
    # RELEASE_PUBLIC_KEY 环境变量传进来。
    if [ -n "${RELEASE_PUBLIC_KEY:-}" ]; then
        set -- "$@" --public-key "$RELEASE_PUBLIC_KEY"
    fi
    if [ -n "$SHA256" ]; then
        set -- "$@" --sha256 "$SHA256"
    fi
fi

# shellcheck disable=SC2086
exec "$PYTHON" "$TMPDIR_DL/install.py" "$@" $EXTRA
