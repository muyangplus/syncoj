#!/bin/bash
# SyncOJ 评测机一键安装（Linux + systemd，§12.10）。服务端 GET /judge/install.sh 发出。
#
#   curl -fsSL http://<服务端管理端口>/judge/install.sh | sudo bash -s -- <评测机令牌>
#
# 可选参数（放在令牌后面）：
#   --server URL      服务端管理端口地址（默认：发这个脚本的那台服务端）
#   --backend NAME    isolate / rlimit（默认：装了 isolate 用 isolate，否则 rlimit）
#   --run-as USER     rlimit 后端下选手程序的运行账号（默认 syncoj-judge-run）
#   --slots N         同时评几个任务（默认 1）
#   --yes             没有 isolate 时不询问，直接用 rlimit
#
# 幂等：重复运行 = 换令牌 / 换参数 / 升级评测机程序，然后重启服务。
#
# 装到：
#   /opt/syncoj-judge/syncoj-judge.pyz     评测机程序（服务端按需打包的 zipapp，只用标准库）
#   /opt/syncoj-judge/uninstall.sh         卸载脚本（root 所有，0755）：sudo /opt/syncoj-judge/uninstall.sh
#   /opt/syncoj-judge/install.json         安装方式（评测机远程卸载时据此清理自己）
#   /etc/syncoj-judge/token                令牌（0600）
#   /var/cache/syncoj-judge                数据包缓存
#   /etc/systemd/system/syncoj-judge.service

set -euo pipefail

# 服务端发脚本时换成自己的地址与评测机程序的 sha256；从仓库里直接跑时占位符原样留着，视为没有
EMBEDDED_SERVER="__SYNCOJ_SERVER__"
PYZ_SHA256="__SYNCOJ_PYZ_SHA256__"
case "$EMBEDDED_SERVER" in __SYNCOJ_*__) EMBEDDED_SERVER="" ;; esac
case "$PYZ_SHA256" in __SYNCOJ_*__) PYZ_SHA256="" ;; esac

SERVICE=syncoj-judge
ACCOUNT=syncoj-judge
INSTALL_DIR=/opt/syncoj-judge
CONF_DIR=/etc/syncoj-judge
UNIT=/etc/systemd/system/$SERVICE.service
CACHE_DIR=/var/cache/$SERVICE

TOKEN=""
SERVER="$EMBEDDED_SERVER"
BACKEND=""
RUN_AS=""
SLOTS=1
YES=0

die() { echo "错误：$*" >&2; exit 1; }
say() { echo "==> $*"; }
warn() { echo "!!  $*" >&2; }

while [ $# -gt 0 ]; do
    case "$1" in
        --server) SERVER="${2:-}"; shift 2 ;;
        --backend) BACKEND="${2:-}"; shift 2 ;;
        --run-as) RUN_AS="${2:-}"; shift 2 ;;
        --slots) SLOTS="${2:-}"; shift 2 ;;
        --yes|-y) YES=1; shift ;;
        --token) TOKEN="${2:-}"; shift 2 ;;
        -h|--help) sed -n '2,20p' "$0" 2>/dev/null || true; exit 0 ;;
        # 令牌是 base64url，可能以 - 开头：不认识的参数先当令牌收下，下面按形状与长度把关
        *) [ -z "$TOKEN" ] || die "多了一个参数：$1"; TOKEN="$1"; shift ;;
    esac
done

[ -n "$TOKEN" ] || die "缺少评测机令牌：curl ... | sudo bash -s -- <令牌>"
case "$TOKEN" in *[!A-Za-z0-9_-]*) die "令牌只含字母、数字、- 和 _（抄错了？）" ;; esac
[ "${#TOKEN}" -ge 32 ] || die "令牌太短（抄漏了？或者参数写错了：$TOKEN）"
[ -n "$SERVER" ] || die "缺少服务端地址：加 --server http://<服务端>:8000"
SERVER="${SERVER%/}"
case "$SLOTS" in ''|*[!0-9]*) die "--slots 要是正整数" ;; esac
[ "$SLOTS" -ge 1 ] || die "--slots 要是正整数"
[ "$(id -u)" -eq 0 ] || die "需要 root：在命令前加 sudo"
command -v systemctl >/dev/null 2>&1 || die "没有 systemd（systemctl）；请手动运行 syncoj-judge"

PYTHON="$(command -v python3 || true)"
[ -n "$PYTHON" ] || die "没有 python3（需要 3.8 或更新）"
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3, 8) else 1)' \
    || die "python3 版本太旧（$("$PYTHON" -V 2>&1)），需要 3.8 或更新"

if ! command -v g++ >/dev/null 2>&1; then
    warn "没有 g++：评测会全部记为系统错误。先装编译器（apt install g++）"
fi

# ---- 沙箱后端 ----
if [ -z "$BACKEND" ]; then
    if command -v isolate >/dev/null 2>&1; then
        BACKEND=isolate
    else
        warn "没有找到 isolate（https://github.com/ioi/isolate）"
        warn "rlimit 后端不隔离网络与文件系统，结果标记为「不隔离」"
        if [ "$YES" -ne 1 ]; then
            if [ -r /dev/tty ]; then
                printf '用 rlimit 后端继续安装？[y/N] ' >/dev/tty
                read -r answer </dev/tty || answer=""
                case "$answer" in y|Y|yes|YES) ;; *) die "已取消；装好 isolate 后重跑，或加 --yes 用 rlimit" ;; esac
            else
                die "没有 isolate；加 --yes 用 rlimit 后端，或先装 isolate"
            fi
        fi
        BACKEND=rlimit
    fi
fi
case "$BACKEND" in
    isolate) command -v isolate >/dev/null 2>&1 || die "--backend isolate 但没有找到 isolate" ;;
    rlimit) ;;
    *) die "Linux 上的后端只有 isolate / rlimit（收到 $BACKEND）" ;;
esac
if [ "$BACKEND" != isolate ] && [ "$SLOTS" -gt 1 ]; then
    die "--slots 大于 1 只能用 isolate（rlimit 的并发任务共用一个运行账号，能互相读写）"
fi

# ---- 账号 ----
NOLOGIN="$(command -v nologin || echo /usr/sbin/nologin)"
ensure_account() {
    if ! id "$1" >/dev/null 2>&1; then
        useradd --system --no-create-home --home-dir /nonexistent --shell "$NOLOGIN" "$1"
        say "已建账号 $1"
    fi
}
ensure_account "$ACCOUNT"
if [ "$BACKEND" = rlimit ]; then
    # rlimit 后端要切换账号运行选手程序，服务本身以 root 跑，选手程序换成一个专用的低权限账号
    if [ -z "$RUN_AS" ]; then
        RUN_AS=syncoj-judge-run
        ensure_account "$RUN_AS"
    fi
    id "$RUN_AS" >/dev/null 2>&1 || die "--run-as 账号不存在：$RUN_AS"
    SERVICE_USER=root
else
    SERVICE_USER="$ACCOUNT"
fi

# ---- 程序 ----
mkdir -p "$INSTALL_DIR" "$CONF_DIR"
# umask 0 下 mkdir 出来是 0777：这两个目录里放着以 root 执行的卸载脚本与令牌，收紧
chown root:root "$INSTALL_DIR" "$CONF_DIR"
chmod 0755 "$INSTALL_DIR" "$CONF_DIR"
TMP="$(mktemp "$INSTALL_DIR/.syncoj-judge.pyz.XXXXXX")"
trap 'rm -f "$TMP"' EXIT
say "下载评测机程序：$SERVER/judge/worker.pyz"
if command -v curl >/dev/null 2>&1; then
    curl -fsSL --noproxy '*' -o "$TMP" "$SERVER/judge/worker.pyz" || die "下载失败：$SERVER/judge/worker.pyz"
elif command -v wget >/dev/null 2>&1; then
    wget -q --no-proxy -O "$TMP" "$SERVER/judge/worker.pyz" || die "下载失败：$SERVER/judge/worker.pyz"
else
    die "没有 curl 也没有 wget"
fi
if [ -n "$PYZ_SHA256" ] && command -v sha256sum >/dev/null 2>&1; then
    actual="$(sha256sum "$TMP" | cut -d' ' -f1)"
    if [ "$actual" != "$PYZ_SHA256" ]; then
        rm -f "$TMP"
        die "评测机程序的 sha256 与脚本里记的不同（$actual）：下载被篡改，或服务端刚升级过 —— 重新复制安装命令再跑"
    fi
fi
"$PYTHON" "$TMP" --help >/dev/null || die "下载的评测机程序跑不起来"
chmod 0755 "$TMP"
mv -f "$TMP" "$INSTALL_DIR/syncoj-judge.pyz"
trap - EXIT

# ---- 卸载脚本与安装清单 ----
# 卸载脚本由服务端发脚本时嵌进下面的 heredoc；从仓库里直接跑时占位符原样留着，取旁边那份
write_uninstaller() {
    cat > "$1" <<'SYNCOJ_UNINSTALL_EOF'
__SYNCOJ_UNINSTALL_SH__
SYNCOJ_UNINSTALL_EOF
}
UNINSTALL_TMP="$(mktemp "$INSTALL_DIR/.uninstall.sh.XXXXXX")"
trap 'rm -f "$UNINSTALL_TMP"' EXIT
write_uninstaller "$UNINSTALL_TMP"
if grep -qx '__SYNCOJ_UNINSTALL''_SH__' "$UNINSTALL_TMP"; then
    LOCAL_UNINSTALLER="$(dirname "$0")/uninstall.sh"
    [ -f "$LOCAL_UNINSTALLER" ] || die "缺少卸载脚本（从服务端下载 install.sh 再跑）"
    cat "$LOCAL_UNINSTALLER" > "$UNINSTALL_TMP"
fi
bash -n "$UNINSTALL_TMP" || die "卸载脚本语法不对"
chown root:root "$UNINSTALL_TMP"
chmod 0755 "$UNINSTALL_TMP"
mv -f "$UNINSTALL_TMP" "$INSTALL_DIR/uninstall.sh"
trap - EXIT
MARKER="$CACHE_DIR/.uninstall-requested"
cat > "$INSTALL_DIR/install.json" <<EOF
{"kind": "systemd", "service": "$SERVICE", "marker": "$MARKER",
 "paths": ["$INSTALL_DIR", "$CONF_DIR", "$CACHE_DIR", "$UNIT"],
 "accounts": ["$ACCOUNT", "syncoj-judge-run"]}
EOF
chown root:root "$INSTALL_DIR/install.json"
chmod 0644 "$INSTALL_DIR/install.json"
# 重装：上一次远程卸载没做完留下的标记会让新服务一起来就退出，清掉
rm -f "$MARKER"

# ---- 令牌 ----
umask 077
printf '%s\n' "$TOKEN" > "$CONF_DIR/token"
chown "$SERVICE_USER" "$CONF_DIR/token"
chmod 0600 "$CONF_DIR/token"

# ---- 服务 ----
ARGS="--server $SERVER --token-file $CONF_DIR/token --backend $BACKEND --slots $SLOTS --cache-dir $CACHE_DIR"
ARGS="$ARGS --install-manifest $INSTALL_DIR/install.json"
[ "$BACKEND" = rlimit ] && ARGS="$ARGS --run-as $RUN_AS"
cat > "$UNIT" <<EOF
[Unit]
Description=SyncOJ judge worker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
ExecStart=$PYTHON $INSTALL_DIR/syncoj-judge.pyz $ARGS
CacheDirectory=$SERVICE
# 远程卸载（§12.10）：评测机告别后写标记、以 0 退出；这里以 root（+）看到标记就脱离本单元卸载。
# 没有标记时什么都不做（平常的停止 / 重启照常）
ExecStopPost=+$INSTALL_DIR/uninstall.sh --if-requested
# 停止（systemctl stop / 管理端「停止」）= 评完手上的任务再退出
KillMode=mixed
TimeoutStopSec=300
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null 2>&1
systemctl restart "$SERVICE"

sleep 2
if systemctl is-active --quiet "$SERVICE"; then
    say "评测机已启动：后端 $BACKEND，并发 $SLOTS，服务端 $SERVER"
    say "日志：journalctl -u $SERVICE -f"
else
    systemctl --no-pager status "$SERVICE" || true
    die "评测机没有起来，见上面的日志"
fi
