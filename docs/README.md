# SyncOJ 文档地图

| 读者 | 任务 | 文档 |
|---|---|---|
| 教师 | 建场次、导名单、配对机器、发文件、收成绩 | [`quickstart.md`](quickstart.md) |
| 装机人员 | 装 Agent、排障、卸载 | [`install-agent.md`](install-agent.md) |
| 运维 | 部署服务端、发版、自带评测、排障 | [`operate.md`](operate.md) |
| 开发者 | 协议字段、API 形状、配置项、成绩格式 | [`reference/`](reference/) |
| 改设计 | 架构现状、已否决方案 | [`design/`](design/)、[`decisions/`](decisions/) |

## 全量文件清单

`docs/**` 下每个文件都须列在此处，且此处每个文件都须存在（`server/tests/test_docs.py` 检查）。

### 上手与运维

| 文件 | 内容 |
|---|---|
| [`README.md`](README.md) | 文档地图与全量清单 |
| [`quickstart.md`](quickstart.md) | 教师开考流程、配对、两个端口、目录约定 |
| [`install-agent.md`](install-agent.md) | 安装入口与参数、注册单元、运行身份、目录与下发、卸载、排障 |
| [`operate.md`](operate.md) | 服务端部署与环境变量、`.key/`、服务端发现与主动通告、Agent 发版、GitHub Release、systemd 245 冒烟、自带评测、导入导出题目配置、心跳排障 |

### 参考

| 文件 | 内容 |
|---|---|
| [`reference/protocol.md`](reference/protocol.md) | Agent ⇄ Server 协议：`enroll` / `tick` / `files` / `assets` / `events` 字段与三态 |
| [`reference/api-conventions.md`](reference/api-conventions.md) | 管理端 API 约定：列表信封、错误体、命名、删除语义 |
| [`reference/judge-result.md`](reference/judge-result.md) | 成绩目录与格式、三条解析规则、自带评测数据包与评测机接口 |
| [`reference/config-keys.md`](reference/config-keys.md) | `agent.ini` 逐键说明（与 `CONFIG_POLICY_KEYS` 对账） |
| [`reference/player-api.md`](reference/player-api.md) | 考生页接口：降级预览、准考证号 + 密码登录与令牌、本人上下文、网页下载 |
| [`reference/problem-formats.md`](reference/problem-formats.md) | 题目配置导入导出：识别顺序、各格式映射、原生格式、提示代码、上限 |

### 设计（原 `DESIGN.md` 拆分，编号不变）

| 文件 | 内容 |
|---|---|
| [`design/00-decisions.md`](design/00-decisions.md) | 已锁定决策表（原 §0） |
| [`design/01-architecture.md`](design/01-architecture.md) | 架构总览（原 §1） |
| [`design/02-tick-protocol.md`](design/02-tick-protocol.md) | `tick` 协议取舍（原 §2） |
| [`design/03-agent.md`](design/03-agent.md) | Agent：主循环、扫描、注册、systemd unit、py38 清单（原 §3） |
| [`design/04-server.md`](design/04-server.md) | 服务端：数据模型、写入、成绩回写、代码归题、注册配对（多名单绑定、同名匹配）、名单、迁移、场次归档（原 §4） |
| [`design/05-api-and-frontend.md`](design/05-api-and-frontend.md) | 接口与前端：契约测试、信封、文案口径、删除语义、公开端口（原 §5） |
| [`design/06-security.md`](design/06-security.md) | 安全面、签名算法、信任边界（原 §6） |
| [`design/07-ops-and-install.md`](design/07-ops-and-install.md) | 运维与装机：`.key/`、发布、安装策略、服务端发现、远程卸载（原 §7） |
| [`design/08-milestones-and-open.md`](design/08-milestones-and-open.md) | 里程碑、待补信息、待办、否决的想法（原 §8 / §9 / §10） |
| [`design/09-server-announce.md`](design/09-server-announce.md) | 服务端主动通告与挑战应答（§11） |
| [`design/10-builtin-judge.md`](design/10-builtin-judge.md) | 自带沙箱评测、题目配置导入导出（§12） |

### 决策记录（ADR）

格式：状态 / 背景 / 决定 / 后果 / 被否决的替代方案。

| 文件 | 内容 |
|---|---|
| [`decisions/README.md`](decisions/README.md) | ADR 清单与写法 |
| [`decisions/ADR-0001-tech-stack-and-dependencies.md`](decisions/ADR-0001-tech-stack-and-dependencies.md) | 技术栈：Python 3.8 零依赖、INI |
| [`decisions/ADR-0002-communication-and-identity.md`](decisions/ADR-0002-communication-and-identity.md) | 通信与身份：轮询、机器识别、配对绑人 |
| [`decisions/ADR-0003-delete-semantics.md`](decisions/ADR-0003-delete-semantics.md) | 删除语义：硬删 + 名称确认；代码 / 资产硬删 |
| [`decisions/ADR-0004-ui-copy-and-notice.md`](decisions/ADR-0004-ui-copy-and-notice.md) | 界面文案口径、`NOTICE.md` 公告 |
| [`decisions/ADR-0005-public-port.md`](decisions/ADR-0005-public-port.md) | 公开端口：默认 80、白名单、404 |
| [`decisions/ADR-0006-delete-nothing-of-the-verifier.md`](decisions/ADR-0006-delete-nothing-of-the-verifier.md) | 验收信任锚取包内副本、交叉验证 |
| [`decisions/ADR-0007-unit-path-whitelist.md`](decisions/ADR-0007-unit-path-whitelist.md) | `ReadWritePaths` 每条带 `-` |
| [`decisions/ADR-0008-systemd-unit-hardening.md`](decisions/ADR-0008-systemd-unit-hardening.md) | 单元加固例外：`Restart=` / `Group=` |
| [`decisions/ADR-0009-registration-is-a-root-unit.md`](decisions/ADR-0009-registration-is-a-root-unit.md) | 注册由 root 一次性单元执行 |
| [`decisions/ADR-0010-run-user-owns-path-templates.md`](decisions/ADR-0010-run-user-owns-path-templates.md) | `{home}` / `{desktop}` 按运行账号展开 |
| [`decisions/ADR-0011-zipcrypto-not-aes.md`](decisions/ADR-0011-zipcrypto-not-aes.md) | zip 密码用 ZipCrypto |
| [`decisions/ADR-0012-remote-uninstall-authorization.md`](decisions/ADR-0012-remote-uninstall-authorization.md) | 远程卸载授权模型 |
| [`decisions/ADR-0013-install-policy-defaults.md`](decisions/ADR-0013-install-policy-defaults.md) | 安装策略三条的默认值 |
| [`decisions/ADR-0014-config-must-prove-it-is-read.md`](decisions/ADR-0014-config-must-prove-it-is-read.md) | 配置项须有测试证明被读取 |
| [`decisions/ADR-0015-server-announce.md`](decisions/ADR-0015-server-announce.md) | 主动通告只作线索，切换前挑战应答 |
| [`decisions/ADR-0016-builtin-judge.md`](decisions/ADR-0016-builtin-judge.md) | 自带评测：写 `judge_result/`、独立评测机、isolate 沙箱 |
| [`decisions/ADR-0017-problem-format-import.md`](decisions/ADR-0017-problem-format-import.md) | 题目配置导入导出：折算成现有数据包、只加 IO 文件名两列 |
| [`decisions/ADR-0018-multi-roster-binding.md`](decisions/ADR-0018-multi-roster-binding.md) | 一机绑多份名单：按场次名单取人、同名建议 |

## 文档之间的关系

```
README.md                 入口 + 三条命令
DESIGN.md                 § 编号 -> docs/design/*.md 映射表
docs/README.md            本页
├── quickstart.md         教师
├── install-agent.md      装机
├── operate.md            运维
├── reference/            字段与约定（改代码同步改）
├── design/               现状设计
└── decisions/            ADR
```

## 仓库结构

```
SyncOJ/
├── DESIGN.md                 § 编号映射表
├── docs/                     见上
├── server/                   服务端（FastAPI + SQLite WAL）
│   ├── syncoj_server/        含 judging/（syncoj-judge 评测机）
│   ├── tools/                dump_openapi.py、parallel_tests.py
│   └── tests/
├── agent/                    Agent（Python 3.8 零依赖）
│   ├── syncoj_agent/
│   ├── packaging/            install.py / build_bundle.py / bootstrap.sh
│   ├── tools/                py38 门禁、协议 fixture 生成
│   └── tests/
├── scripts/                  check.py（检查步骤）、check.sh / check.ps1、release.py、smoke_systemd245.py
└── web/                      管理界面（Vue3 + TS + Vite + Element Plus）
    ├── src/api/              client、资源声明、生成的类型
    ├── src/composables/      useList / useMutation
    ├── src/components/       页面外壳、表格、确认对话框
    ├── src/views/            页面
    └── openapi.json          由 server/tools/dump_openapi.py 导出（入库）
```

## 开发约定

| 规则 | 守卫 |
|---|---|
| Agent 代码兼容 Python 3.8：`python agent/tools/check_py38.py agent/` | `check.py`、CI |
| 协议改动同步 `server/syncoj_server/schemas.py`、`agent/syncoj_agent/` 报文构造、`docs/reference/protocol.md` | `server/tests/test_agent_contract.py` |
| 路径校验服务端与 `agent/syncoj_agent/safepath.py` 各自独立实现 | `test_path_validation_parity` |
| 前端类型不手写；改服务端模型后运行下方命令 | `check.py` 校验 `web/openapi.json` 同步 |

```bash
python server/tools/dump_openapi.py && (cd web && npm run gen:types)
```

## 跑全部检查

```bash
./scripts/check.sh          # Linux / CI / Windows Git bash
```

```powershell
.\scripts\check.ps1         # Windows PowerShell 5.1 / pwsh 7
```

| 参数 / 变量 | 作用 |
|---|---|
| （默认） | 跑完全部步骤后汇总 |
| `--fail-fast` | 第一处失败即停 |
| `--list` | 列出步骤名 |
| `--only <步骤>` | 只跑指定步骤 |
| `--skip-web` | 跳过前端两步 |
| `PYTHON` | 指定解释器；默认先找仓库 `.venv`，再找系统中可运行的 Python ≥ 3.8 |

步骤：`py38` → `ps1-bom` → `versions` → `agent-tests` → `server-tests` → `openapi` → `web-types`（重新生成后逐字节比对）→ `web-build`（入口对账 + `typecheck` + `build`）。步骤只写在 `scripts/check.py`。

⚠️ `.ps1` 须保留 UTF-8 BOM 与 CRLF，丢失 BOM 会使 Windows PowerShell 5.1 解析失败（`ps1-bom` 步骤检查）。

## 状态

| 里程碑 | 状态 |
|---|---|
| M1 骨架：enroll / tick / 在线状态 / 管理后台 | ✅ |
| M2 回收：扫描 / 上传 / 内容寻址存储 / `source/` 落盘 / 删除审计 | ✅ |
| M3 下发：资产管理 / 任务编排（全员·按人·按分组）/ Range 断点续传 / 进度聚合 | ✅ |
| M4 成绩：结果扫描回写 / 成绩矩阵 / 手工补录 / 原始记录 | ✅ |
| M5 运维：RSA 签名自更新 / 安全解包 / 自动回滚 / 幂等安装器 / 可复现打包 | ✅ |
| 管理界面 + 初始化闭环（建场次 / 导名单 / 维护题目 / 机器配对） | ✅ |
| 代码归题：题目 glob / 文件台账归类 / 路径试算 / 三层可配置 | ✅ |
| 交付视图：「未交」「已待评测」「已出成绩」、交题进度 | ✅ |
| 名单库：全局名单、场次默认名单、一键应用 | ✅ |
| 永久配对：统一密钥 + 六位配对码 + 指纹自愈 + 克隆告警 | ✅ |
| 一机多名单绑定 + 按姓名匹配机器 | ✅ |
| 场次归档 | ✅ |
| API 形状统一：列表信封 / 统一错误体 / 名称确认删除 | ✅ |
| 局域网发现：UDP 探测 + 签名应答 | ✅ |
| 服务端主动通告：UDP 通告 + 挑战应答 +「召集机器」 | ✅ |
| 自带评测：`syncoj-judge` 评测机 + isolate / rlimit 沙箱 | ✅ |
| 安装入口：`/install` 装机页 + 从服务端直接安装 | ✅ |
| 公开端口：默认 80，仅装机页与选手页 | ✅ |
| 选手页公告：取自下发的 `NOTICE.md` | ✅ |
| 考试时间窗：窗口外拒收，到点自动置「已结束」 | ✅ |
| 发布：推 `v<版本号>` 标签或在 Actions 手动运行，生成 GitHub Release | ✅ |

## 快速开始（开发）

```bash
# 服务端
python -m venv server/.venv
server/.venv/Scripts/activate      # Linux: source server/.venv/bin/activate
pip install -e "server[dev]"
syncoj-server init                 # 建库 + 生成管理员口令
syncoj-server serve --host 0.0.0.0

# 管理界面（开发模式，热更新）
cd web && npm install
npm run dev                        # http://localhost:5173，API 代理到 :8000

# 管理界面（构建后由服务端托管）
npm run build                      # 产出 web/dist；服务端启动时自动挂载，缺失时只提供 API 与 /docs

# Agent
python agent/run_agent.py --check                          # 内置默认配置自检
python agent/run_agent.py --config agent/config.example.ini --check
python agent/run_agent.py --once
```

| 情况 | 结果 |
|---|---|
| `--check` 不带 `--config` | 用内置默认值（`ca_file` 空、服务端 `https://127.0.0.1:8000`） |
| 带 `agent/config.example.ini` | 读取 `/etc/syncoj/ca.pem`；先填真实路径 |
| 直接运行 `syncoj_agent/main.py` | `ImportError: attempted relative import with no known parent package`；须经 `run_agent.py` 启动 |
