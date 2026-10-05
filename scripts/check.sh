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

PYTHON="${PYTHON:-python3}"

step() { printf '\n\033[1;36m==> %s\033[0m\n' "$1"; }

step "Python 3.8 兼容门禁（Agent 必须能跑在 NOI Linux 的 Python 3.8 上）"
"$PYTHON" agent/tools/check_py38.py agent/
# 服务端也一并检查。它自己有第三方依赖，用 --allow 显式列出，
# 而不是把 fastapi/sqlalchemy 混进标准库白名单里。
"$PYTHON" agent/tools/check_py38.py \
  --allow fastapi,sqlalchemy,pydantic,starlette,uvicorn \
  server/syncoj_server/

step "Agent 测试"
(cd agent && "$PYTHON" -m pytest -p no:cacheprovider -p no:tmpdir)

step "服务端测试（含协议契约测试与端到端集成测试）"
(cd server && PYTHONPATH="$REPO_ROOT/server" "$PYTHON" -m pytest -p no:cacheprovider -p no:tmpdir)

step "OpenAPI 与前端类型是否同步"
# 服务端 schema 改了但 web/openapi.json 没重新生成时，这里会拦住。
# 否则前端会在运行期才发现字段对不上，而那时已经很难定位。
"$PYTHON" server/tools/dump_openapi.py --check

if command -v npm >/dev/null 2>&1 && [ -d web/node_modules ]; then
  step "前端类型检查与构建"
  (cd web && npm run typecheck && npm run build)
else
  step "跳过前端检查（未安装 npm 或 web/node_modules 不存在）"
fi

step "全部检查通过"
