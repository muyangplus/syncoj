#!/usr/bin/env bash
#
# 一键跑全部检查。CI 与本地提交前都用它。
#
#   ./scripts/check.sh
#
# 需要：Python 3.8+（服务端测试用）、已安装 server[dev] 依赖。
# Agent 侧不需要任何依赖 —— 它本身是零依赖的。
# 前端检查在 npm 与 web/node_modules 都存在时才跑，没有就跳过。

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# 强制子进程的 stdin/stdout 用 UTF-8。
#
# 不能靠环境：Windows 上子进程默认按控制台代码页（GBK）写中文，而
# `test_agent_contract.py` 用 utf-8 解码 Agent 生成的 JSON 样本，会当场抛
# UnicodeDecodeError；Linux CI 如果跑在 LC_ALL=C 下也一样会退化成 ascii。
# 这是"进程间约定"问题，不是"这台机器的问题"，所以两边都钉死。
export PYTHONIOENCODING=utf-8
export PYTHONUTF8=1

# 候选解释器必须**真的能跑**再算数。
#
# Windows 上 `python3` 常常存在但不可用 —— 它指向应用商店的占位程序
# （`.../WindowsApps/python3`），`command -v` 找得到，执行却直接失败。只判断
# "命令存在"会让脚本在第一步就莫名其妙地挂掉，而那台机器上其实有一个完好的
# .venv。所以在决定用谁之前，先让它报一下自己的版本。
usable_python() {
  [ -n "$1" ] || return 1
  command -v "$1" >/dev/null 2>&1 || [ -x "$1" ] || return 1
  "$1" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3, 8) else 1)' \
    >/dev/null 2>&1
}

PYTHON="${PYTHON:-}"
if [ -n "$PYTHON" ]; then
  # 显式给了就听调用方的，但仍然要能跑 —— 否则后面的报错会指向奇怪的地方
  if ! usable_python "$PYTHON"; then
    echo "PYTHON=$PYTHON 不可用（不存在，或不是 Python 3.8+）" >&2
    exit 1
  fi
else
  for candidate in python3 "$REPO_ROOT/.venv/Scripts/python.exe" \
                   "$REPO_ROOT/.venv/bin/python" python; do
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
echo "使用 Python: $PYTHON"

step() { printf '\n\033[1;36m==> %s\033[0m\n' "$1"; }

step "Python 3.8 兼容门禁（Agent 必须能跑在 NOI Linux 的 Python 3.8 上）"
"$PYTHON" agent/tools/check_py38.py agent/
# 服务端也一并检查。它自己有第三方依赖，用 --allow 显式列出，
# 而不是把 fastapi/sqlalchemy 混进标准库白名单里。
"$PYTHON" agent/tools/check_py38.py \
  --allow fastapi,sqlalchemy,pydantic,starlette,uvicorn \
  server/syncoj_server/

# 临时目录收进仓库里已忽略的 .pytest-tmp/，而不是散在系统的 %TEMP% 里。
#
# 曾经这里写的是 `-p no:tmpdir`，那把 `tmp_path` **整个夹具**一起禁掉了 ——
# 任何用到它的测试都会在 setup 阶段报 "fixture 'tmp_path' not found"，
# 而报错看起来像"测试写错了"，不像"脚本禁掉了标准夹具"。收临时目录用
# --basetemp 就够了，不需要把整个插件关掉。
#
# basetemp 与两个 conftest 自己的 workdir 根**必须是不同的目录**。它们曾经是
# 同一个（`.pytest-tmp/{agent,server}` 既是 basetemp、又是 workdir 的家），于是
# pytest 在第一次用到 `tmp_path` 时会**清空整个 basetemp** —— 而那时里面已经有
# 本次会话里别的测试留下的临时目录，其中 `state/agent.log` 还被日志处理器占着。
# 表现是 Windows 上一串 "PermissionError: 另一个程序正在使用此文件"，指向
# 一个跟被测代码毫无关系的文件。所以 basetemp 单独放 `.pytest-tmp/basetemp/`。
PYTEST_TMP="$REPO_ROOT/.pytest-tmp"
PYTEST_BASE="$PYTEST_TMP/basetemp"
mkdir -p "$PYTEST_BASE"

step "PowerShell 脚本的编码"
# 仓库里的 .ps1 都写着中文注释，而 Windows PowerShell 5.1 在**没有 BOM** 时按
# 当前 ANSI 代码页（简体中文 Windows 上是 GBK）解码脚本文件：于是一个 UTF-8 无 BOM
# 的脚本在它上面**整个解析失败**，报的还是"字符串缺少终止符"这种指向别处的错。
# PS 7（pwsh）默认识别 BOM，所以带上 BOM 两边都对。
#
# 为什么值得一条守卫：编辑类工具保存时**不会保留 BOM**，改一行中文注释就把它弄丢了，
# 而症状（换一台只有 PS 5.1 的机器才发现脚本跑不起来）看起来跟这次改动毫无关系。
for f in "$REPO_ROOT"/scripts/*.ps1; do
  [ -e "$f" ] || continue
  magic="$(head -c 3 "$f" | od -An -tx1 | tr -d ' \n')"
  if [ "$magic" != "efbbbf" ]; then
    echo "$f 缺少 UTF-8 BOM（文件开头是 $magic）。" >&2
    echo "Windows PowerShell 5.1 会把它的中文按 ANSI 解码，整个脚本解析失败。" >&2
    echo "修法：在文件最前面补上 EF BB BF 这三个字节。" >&2
    exit 1
  fi
done

step "Agent 测试"
# 按文件并行跑（`server/tools/parallel_tests.py`，零依赖）：服务端那套串行要 40 分钟，
# 而它慢在"每个用例都造一次真 app + 真库 + 真 HTTP"，不在测试逻辑本身 —— 按文件分片
# 就能把核用起来。worker 数用 `SYNCOJ_TEST_JOBS` 覆盖（默认 min(核数, 8)）。
"$PYTHON" server/tools/parallel_tests.py --root agent --jobs "${SYNCOJ_TEST_JOBS:-8}"

step "服务端测试（含协议契约测试与端到端集成测试）"
"$PYTHON" server/tools/parallel_tests.py --root server --jobs "${SYNCOJ_TEST_JOBS:-8}"

step "OpenAPI 与前端类型是否同步"
# 服务端 schema 改了但 web/openapi.json 没重新生成时，这里会拦住。
# 否则前端会在运行期才发现字段对不上，而那时已经很难定位。
"$PYTHON" server/tools/dump_openapi.py --check

if command -v npm >/dev/null 2>&1 && [ -d web/node_modules ]; then
  step "前端类型是否已从 openapi.json 重新生成"
  # 判据是"重新生成一遍，结果和仓库里的那份是不是一致" —— **不要用 git diff**。
  #
  # git diff 比的是工作树和 HEAD，而开发中工作树本来就有未提交的改动，于是这条
  # 检查在"你正在干活"这个最常见的场景下恒红。假警报比没有检查更糟：它会训练
  # 人无视这一步，真的不同步时也就没人看了。
  mkdir -p "$PYTEST_TMP"
  schema_before="$PYTEST_TMP/schema.d.ts.before"
  cp "$REPO_ROOT/web/src/api/schema.d.ts" "$schema_before"
  (cd web && npm run gen:types)
  if ! diff -q "$schema_before" "$REPO_ROOT/web/src/api/schema.d.ts" >/dev/null; then
    echo "web/src/api/schema.d.ts 与 web/openapi.json 不同步：重新生成的结果和仓库里的那份不一样。" >&2
    echo "请把刚重新生成的 web/src/api/schema.d.ts 一起提交。" >&2
    exit 1
  fi

  step "前端入口对账、类型检查与构建"
  # npm run build 已经串了 check:routes → typecheck → vite build，
  # 所以这一条同时守住"声明的接口都有界面入口"。
  (cd web && npm run typecheck && npm run build)
else
  step "跳过前端检查（未安装 npm 或 web/node_modules 不存在）"
fi

step "全部检查通过"
