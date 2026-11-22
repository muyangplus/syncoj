# 参与 SyncOJ 开发

## 先跑检查

提交前运行全部检查。步骤只写在 `scripts/check.py`；`check.sh`（Linux / Git bash）与 `check.ps1`（Windows）只负责选 Python 并转发参数：

```bash
./scripts/check.sh
```

```powershell
.\scripts\check.ps1
```

步骤：py38 兼容门禁（Agent、服务端与 `scripts/` 各一遍）→ PowerShell 脚本编码 → Agent 测试 → 服务端测试 → `web/openapi.json` 与 schema 同步 → `schema.d.ts` 与 `openapi.json` 同步 → 前端入口对账 + typecheck + build。
默认某步失败后继续跑完并汇总；`--fail-fast` 在第一处失败即停。

只跑一部分：

```bash
./scripts/check.sh --list                     # 列出步骤名
./scripts/check.sh --only server-tests        # 只跑某几步（可重复）
./scripts/check.sh --skip-web                 # 没装 web/node_modules 时
python server/tools/parallel_tests.py --root server --pattern "tests/test_docs.py"   # 挑文件（glob）
```

## 硬规则

| 规则 | 检查 |
|---|---|
| Agent 代码兼容 Python 3.8、零第三方依赖（目标机 NOI Linux 2.0） | `python agent/tools/check_py38.py agent/` |
| 协议改动同步三处：`server/syncoj_server/schemas.py`、`agent/syncoj_agent/` 报文构造、`docs/reference/protocol.md` | `server/tests/test_agent_contract.py` |
| 前端类型不手写；改服务端模型后运行 `python server/tools/dump_openapi.py && (cd web && npm run gen:types)` | `check.py` 的 `openapi` / `web-types` 步骤 |
| 服务端路径校验独立实现，不信任 Agent 上报路径；`agent/syncoj_agent/safepath.py` 是另一份实现 | `test_path_validation_parity` |
| 增删或升级依赖（`web/package.json` 的 `dependencies`、`server/pyproject.toml` 的 `dependencies`、`judging/vendor/`）后运行 `python scripts/collect_licenses.py`，生成的 `THIRD-PARTY-LICENSES.md` 与 `web/src/legal/credits.generated.ts` 一起提交 | `check.py` 的 `licenses` 步骤 |
| `docs/**` 每个文件列进 `docs/README.md`；`§` 引用落到设计文档标题；`README.md` ≤ 40 行、`DESIGN.md` ≤ 60 行 | `server/tests/test_docs.py` |

规则清单另见 [`docs/README.md`](docs/README.md)「开发约定」。

## 提交

- 检查未全绿不提交（部分失败也不提交）。
- 一次提交一件事（接口改动 + 文案改动 = 两次提交）。
- 提交信息用中文，写清「改了什么、为什么」；写明破坏性改动与主动留下的代价。
- 日常开发在 `dev` 分支（或从 `dev` 拉出的功能分支）；`main` 只经 PR 从 `dev` 合入，合并前 CI 全绿。

## 许可与漏洞

- 贡献按 [`LICENSE`](LICENSE)（Apache-2.0）授权（第 5 条覆盖未另行声明的贡献），不需要签 CLA。
- 安全问题走 [`SECURITY.md`](SECURITY.md) 的私密报告，不开公开 issue。
