# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

SyncOJ 是考场用的「在线同步评测」系统：选手机上的静默 Agent 回收代码、接收下发文件；
服务端落盘到 `source/<场次>/<选手编号>/` 供 LemonLime / Arbiter 读取，并扫描评测结果回写成绩。
仓库里的文档、注释、提交信息都是中文 —— 新写的也用中文。

## 常用命令

```bash
# 服务端（Python 3.8+）
python -m venv .venv && .venv/bin/pip install -e "server[dev]"
.venv/bin/syncoj-server init                    # 建库 + 管理员 + .key/ 下的密钥
.venv/bin/syncoj-server serve --host 0.0.0.0     # 管理端 :8000

# 前端
cd web && npm install && npm run dev             # :5173，API 代理到 :8000
cd web && npm run build                          # check:routes → typecheck → vite build，产出 web/dist（服务端自动托管）

# Agent（零依赖，必须经 run_agent.py 启动，不能直接跑 syncoj_agent/main.py）
python agent/run_agent.py --check
python agent/run_agent.py --once
```

测试与检查：

```bash
./scripts/check.sh                                               # 全部检查；提交前必须全绿（--list / --only <步骤> / --skip-web）
python server/tools/parallel_tests.py --root server --jobs 8     # 服务端测试，按文件并行
python server/tools/parallel_tests.py --root agent  --jobs 8     # Agent 测试
python server/tools/parallel_tests.py --root server --pattern "tests/test_docs.py"   # --pattern 是相对 root 的 glob
cd server && python -m pytest tests/test_api_flow.py -k 名字      # 单个文件 / 单个用例
python agent/tools/check_py38.py agent/                          # py38 兼容门禁
python server/tools/dump_openapi.py && (cd web && npm run gen:types)   # 改了服务端 schema 之后
python scripts/smoke_systemd245.py                               # systemd 245 容器冒烟（要 docker；CI 也跑）
python scripts/release.py versions                               # 四处版本号一致；发版 = 改版本号后推 v<版本号> 标签
```

服务端测试串行很慢（每个用例都造真 app + 真库），优先用 `parallel_tests.py` 或只跑相关文件。
检查步骤只写在 `scripts/check.py`；`check.sh` / `check.ps1` 只是挑 Python 的壳，`.ps1` 必须保留 UTF-8 BOM 与 CRLF。

## 架构

三块：`server/`（FastAPI + SQLAlchemy 2.0 + SQLite WAL，单进程 uvicorn）、`agent/`（Python 3.8 标准库、systemd 常驻）、
`web/`（Vue3 + TS + Vite + Element Plus + Pinia）。

- **核心只有一个接口 `POST /api/v1/agent/tick`**：Agent 每次上报全量扫描索引（路径/sha256/size/mtime）与下载进度，
  服务端回 `need_upload`、`deploy_jobs`、`upgrade`、下一次 tick 间隔等。服务端持有全部状态，Agent 近乎无状态；
  通信是 HTTP 自适应轮询（有活 2s / 空闲 30s），刻意没有 WebSocket。字段规范在 `docs/reference/protocol.md`。
- **服务端分层**：`api/`（`admin.py` 管理端、`agent.py` 机器端、`player.py` 免登录选手页、`meta.py`）→ `services/`（业务逻辑）
  → `models.py` / `db.py`。`main.py:create_app()` 是工厂（不在模块级建 app，避免 import 时建目录）。
  在线状态放内存 `registry.py`，`tasks/flush.py` 批量落库，`tasks/judge.py` 扫描结果目录回写成绩。
  自带评测（§12）：`judging/` 是只用标准库的评测机（`syncoj-judge`，沙箱 isolate / rlimit），经 `api/judge.py`
  领任务，结果写进 `judge_result/` 走同一套扫描；服务端主动通告（§11）在 `services/announce.py`。
  上传内容按内容寻址存 `blobs/`，再落到 `source/`。库结构演进用 `PRAGMA user_version` + `migrations.py` 有序迁移。
- **两个端口**：管理端端口提供全部 API 与 `/docs`；公开端口（默认 80）只暴露装机页与选手页，管理端在那里不存在。
- **管理端 API 形状统一**：列表信封 `{items,total,limit,offset}`、错误体 `{detail,code,details}`；
  结构性数据硬删 + 输入名称确认；产物性数据（代码/资产）教师删除也是硬删，只有扫描对账的"已消失"留墓碑（ADR-0003 修订）。见 `docs/reference/api-conventions.md`。
- **前端**：`src/api/` 声明资源，`schema.d.ts` 由 `web/openapi.json` 生成（`openapi.json` 由 `server/tools/dump_openapi.py` 导出并入库）；
  页面通过 `composables/useList` / `useMutation` 取数，只描述长相。`scripts/check-routes.mjs` 校验声明的接口都有界面入口。
- **Agent**：扫描 → 上传 → tick → 下载（Range 续传）→ RSA 签名自更新（自实现 `rsa.py`，PKCS#1 v1.5 + SHA-256）。
  `agent/packaging/` 是幂等安装器 / 打包器 / 在线自举脚本，镜像预装、离线包、在线安装共用同一份逻辑。

## 硬规则（有测试或检查守着）

- **Agent 代码必须兼容 Python 3.8 且零第三方依赖**（目标机 NOI Linux 2.0）。服务端也要过 py38 门禁。
- **协议改动同步三处**：`server/syncoj_server/schemas.py`、`agent/syncoj_agent/` 的报文构造、`docs/reference/protocol.md`
  —— `server/tests/test_agent_contract.py` 会抓不一致。
- **路径校验两份独立实现**：服务端 `paths.py` 不信任 Agent 上报路径；`agent/syncoj_agent/safepath.py` 是另一份，
  一致性由 `test_path_validation_parity` 守住。不要让一方复用另一方。
- **前端类型绝不手写**：改服务端模型后重新生成 `openapi.json` 与 `schema.d.ts` 并一起提交。
- **文档守卫**（`server/tests/test_docs.py`）：`docs/**` 下每个文件都要列进 `docs/README.md` 清单；
  任何 md/代码里的 `§X.Y` 引用必须能落到某份设计文档的标题上；根 `README.md` ≤ 40 行、`DESIGN.md` ≤ 60 行。
- **界面文案**：页面上不写声明式/解释式句子，只留动作、破坏性后果与实时数据（ADR-0004）。
- 提交：不提交红状态；一次提交一件事；提交信息用中文写清「改了什么、为什么」。
- 分支：默认在 `dev` 上提交与推送；`main` 只接受从 `dev` 发起的 PR，不直接推 `main`。

## 文档在哪

`DESIGN.md` 只是一张「§ 编号 → `docs/design/*.md`」映射表，代码注释里的 `DESIGN.md §X.Y` 引用按它找。
`docs/design/` 记现状，`docs/decisions/`（ADR）记当初否掉了什么 —— 改已锁定决策（`docs/design/00-decisions.md`）之前先读对应 ADR。
配置项见 `docs/reference/config-keys.md`（也与代码对账），成绩格式见 `docs/reference/judge-result.md`。
