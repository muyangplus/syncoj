#!/bin/bash
# SyncOJ 评测机卸载（Linux + systemd，§12.10）。install.sh 把它写到 /opt/syncoj-judge/uninstall.sh
# （root 所有，0755，别人改不了）。
#
#   sudo /opt/syncoj-judge/uninstall.sh       手动卸载；幂等，装了一半、卸了一半都能再跑
#
# 内部用法：
#   --if-requested   单元的 ExecStopPost=+（以 root 执行）：评测机告别服务端后在缓存目录写了标记
#                    才动手。本单元此刻正在停止，不能在这里停它自己，所以用 systemd-run 起一个
#                    临时单元、2 秒后以 --now 跑这个脚本
#   --now            真正的卸载步骤（手动运行时不带参数也走这里）
#
# 删除：systemd 单元 syncoj-judge（停止、禁用、删单元文件）、/opt/syncoj-judge、/etc/syncoj-judge、
# /var/cache/syncoj-judge、账号 syncoj-judge / syncoj-judge-run。不动 isolate、Python、g++。
# 标记文件只看在不在，不读内容。

set -uo pipefail

# 只给测试用：把下面所有路径挪到一个临时根目录下（root 的 sudo / systemd 环境里不会有它）
ROOT="${SYNCOJ_JUDGE_ROOT:-}"
SERVICE=syncoj-judge
INSTALL_DIR="$ROOT/opt/syncoj-judge"
CONF_DIR="$ROOT/etc/syncoj-judge"
CACHE_DIR="$ROOT/var/cache/syncoj-judge"
UNIT_DIR="$ROOT/etc/systemd/system"
MARKER="$CACHE_DIR/.uninstall-requested"
ACCOUNTS="syncoj-judge syncoj-judge-run"
SELF="$INSTALL_DIR/uninstall.sh"

die() { echo "错误：$*" >&2; exit 1; }
say() { echo "==> $*"; }
warn() { echo "!!  $*" >&2; }

if_requested() {
    if [ ! -e "$MARKER" ] && [ ! -L "$MARKER" ]; then
        exit 0
    fi
    say "评测机已告别服务端：2 秒后由临时单元 $SERVICE-uninstall 卸载"
    local extra=()
    [ -z "$ROOT" ] || extra=(--setenv="SYNCOJ_JUDGE_ROOT=$ROOT")
    if systemd-run --unit="$SERVICE-uninstall" --description="SyncOJ judge uninstall" \
        --on-active=2 --timer-property=RemainAfterElapse=no ${extra[@]+"${extra[@]}"} "$SELF" --now; then
        exit 0
    fi
    warn "systemd-run 失败：请手动运行 sudo $SELF"
    exit 0
}

uninstall_now() {
    [ "$(id -u)" -eq 0 ] || die "需要 root：sudo $SELF"
    cd / || true
    # 先删标记：下面停服务时 ExecStopPost 还会跑一次 --if-requested，别让它再排一次卸载
    rm -f -- "$MARKER"
    if command -v systemctl >/dev/null 2>&1; then
        systemctl disable --now "$SERVICE.service" >/dev/null 2>&1 || true
    fi
    rm -f -- "$UNIT_DIR/$SERVICE.service"
    rm -rf -- "$UNIT_DIR/$SERVICE.service.d"
    if command -v systemctl >/dev/null 2>&1; then
        systemctl daemon-reload || warn "systemctl daemon-reload 失败"
        systemctl reset-failed "$SERVICE.service" >/dev/null 2>&1 || true
    fi
    say "已删除服务 $SERVICE"
    rm -rf -- "$INSTALL_DIR" "$CONF_DIR" "$CACHE_DIR"
    say "已删除 $INSTALL_DIR、$CONF_DIR、$CACHE_DIR"
    for account in $ACCOUNTS; do
        if id "$account" >/dev/null 2>&1; then
            if userdel "$account" >/dev/null 2>&1; then
                say "已删除账号 $account"
            else
                warn "没能删除账号 $account（还有它的进程？）：稍后 sudo userdel $account"
            fi
        fi
    done
    say "评测机已卸载"
}

main() {
    case "${1:-}" in
        --if-requested) if_requested ;;
        --now|"") uninstall_now ;;
        -h|--help) sed -n '2,16p' "$0" 2>/dev/null || true ;;
        *) die "不认识的参数：$1（手动卸载不带参数：sudo $SELF）" ;;
    esac
}

# 整个脚本先读完再执行：卸载途中会删掉这个文件所在的目录
main "$@"
exit $?
