# 参与 SyncOJ 开发

## 先跑检查

提交之前跑一遍仓库自带的检查。两个入口**步骤一一对应**（Linux / Git bash 与 Windows）：

```bash
./scripts/check.sh
```

```powershell
.\scripts\check.ps1
```

依次跑：py38 兼容门禁（Agent 与服务端各一遍）→ PowerShell 脚本编码 → Agent 测试 →
服务端测试 → `web/openapi.json` 是否与 schema 同步 → `schema.d.ts` 是否与
`openapi.json` 同步 → 前端入口对账 + typecheck + build。

只跑一部分：

```bash
python server/tools/parallel_tests.py --root server --jobs 8      # 按文件并行
python server/tools/parallel_tests.py --root server --pattern 关键字   # 挑文件
```

## 几条会撞上的硬规则

- **Agent 侧必须 Python 3.8 兼容**（目标机是 NOI Linux 2.0）。单独跑
  `python agent/tools/check_py38.py agent/`。
- **协议改动同步三处**：`server/syncoj_server/schemas.py`、`agent/syncoj_agent/`
  里的报文体、`docs/reference/protocol.md`。不一致会被
  `server/tests/test_agent_contract.py` 抓到。
- **前端类型绝不手写**：改完服务端模型后跑
  `python server/tools/dump_openapi.py && (cd web && npm run gen:types)`。
- **服务端的路径校验必须独立实现**，不信任 Agent 上报的路径 ——
  `agent/syncoj_agent/safepath.py` 是另一份实现，两者的一致性由契约测试里的
  `test_path_validation_parity` 守住。
- **文档也有守卫**（`server/tests/test_docs.py`）：`docs/` 下每个文件都要列进
  `docs/README.md` 的清单；`§` 引用要能落到一份设计文档的标题上；
  `README.md` 不超过 40 行、`DESIGN.md` 不超过 60 行。

这些规则的**理由**写在 [`docs/README.md`](docs/README.md) 的「要给这个仓库改代码的人」
一节。那份是正文，这里只做入口 —— 两处都写一遍会变成两个真相。

## 提交

- **不许提交红状态**：检查没全绿就不提交，一部分红也不行。
- 一次提交一件事，不要混装（接口改动 + 文案改动 = 两次提交）。
- 提交信息用中文，写清「改了什么、为什么」；破坏性改动与主动留下的代价一并写在里面。
- 分支名随意，合回 `main` 之前跑一遍完整检查。

## 许可与漏洞

- 提交进来的代码按 [`LICENSE`](LICENSE)（Apache-2.0）授权：Apache-2.0 第 5 条
  默认覆盖没有显式声明的贡献，所以**不需要单独签 CLA**。
- 安全问题走 [`SECURITY.md`](SECURITY.md) 里的私密报告，不要开公开 issue。
