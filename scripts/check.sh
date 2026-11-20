#!/usr/bin/env bash
#
# 一键跑全部检查。CI 与本地提交前都用它。
#
#   ./scripts/check.sh                 # 全部
#   ./scripts/check.sh --skip-web      # 没装 web/node_modules 时
#   ./scripts/check.sh --list          # 列出步骤；--only <步骤> 只跑某几步
#
# 步骤本身只写在 scripts/check.py 里（两个 shell 入口曾各写一遍、靠约定同步，
# 实际已经漂开）。这个壳只做一件事：挑一个**真的能跑**的 Python 3.8+，把参数原样转过去。
#
# 需要：已安装 server[dev] 依赖的 Python 3.8+。Agent 侧不需要任何依赖。
# 前端两步在 npm 与 web/node_modules 都存在时才跑。

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# 候选解释器必须**真的能跑**再算数：Windows 上 `python3` 常常指向应用商店的占位
# 程序（`command -v` 找得到、执行直接失败）。判据是"能起来且 >= 3.8"。
usable_python() {
  [ -n "$1" ] || return 1
  command -v "$1" >/dev/null 2>&1 || [ -x "$1" ] || return 1
  "$1" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3, 8) else 1)' \
    >/dev/null 2>&1
}

PYTHON="${PYTHON:-}"
if [ -n "$PYTHON" ]; then
  if ! usable_python "$PYTHON"; then
    echo "PYTHON=$PYTHON 不可用（不存在，或不是 Python 3.8+）" >&2
    exit 1
  fi
else
  # 先找仓库里的 .venv（装着 server[dev]），再退回系统解释器 —— 与 check.ps1 同序
  for candidate in "$REPO_ROOT/.venv/bin/python" "$REPO_ROOT/.venv/Scripts/python.exe" \
                   python3 python; do
    if usable_python "$candidate"; then
      PYTHON="$candidate"
      break
    fi
  done
fi

if [ -z "$PYTHON" ]; then
  echo "找不到可用的 Python 3.8+；请装一个，或用 PYTHON=... 指定" >&2
  exit 1
fi

exec "$PYTHON" "$REPO_ROOT/scripts/check.py" "$@"
