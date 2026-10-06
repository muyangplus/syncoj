# SyncOJ 文档地图

这份文档按**受众**分流：先找到你自己的那一行，再去读那一份。

**读者要什么** → 看哪一份：

| 我是谁 | 我想干什么 | 去哪 |
|---|---|---|
| 教师 | 开一场考试：建场次、导名单、配对机器、发文件、收成绩 | [`quickstart.md`](quickstart.md) |
| 装机的人 | 把 Agent 装进一间机房的镜像 / 空机器上，并排障 | [`install-agent.md`](install-agent.md) |
| 运维 | 部署服务端、发版、升级、卸载、查日志 | [`operate.md`](operate.md) |
| 开发者 | 查协议字段、API 形状、配置项、成绩格式 | [`reference/`](reference/) |
| 想改设计的人 | 看架构、看"为什么这么定"、看被否掉的方案 | [`design/`](design/) 与 [`decisions/`](decisions/) |

## 全量文件清单

本页自己也在清单里。`docs/**` 下每个文件都必须在清单中，清单里每个文件也都必须
存在 —— 两边都由 `server/tests/test_docs.py` 检查。

### 上手与运维

| 文件 | 一句话 |
|---|---|
| [`README.md`](README.md) | 本页：按「我是谁」分流的入口与全量文件清单 |
| [`quickstart.md`](quickstart.md) | 教师上手：从 `syncoj-server init` 到机器配对成功，含两个端口的区别 |
| [`install-agent.md`](install-agent.md) | 装机器：四种安装入口、注册密钥、注册单元、运行身份、目录约定、卸载与排障 |
| [`operate.md`](operate.md) | 服务端运维：部署、`.key/`、发版与升级、公开端口与局域网发现 |

### 参考（对着代码核过的字段与约定）

| 文件 | 一句话 |
|---|---|
| [`reference/protocol.md`](reference/protocol.md) | Agent ⇄ Server 协议：`enroll` / `tick` / `files` / `assets` / `events` 的字段与三态 |
| [`reference/api-conventions.md`](reference/api-conventions.md) | 管理端 API 的形状约定：列表信封、错误体、命名、删除语义 |
| [`reference/judge-result.md`](reference/judge-result.md) | 评测器对接：成绩怎么进 SyncOJ，以及三条保守解析规则 |
| [`reference/config-keys.md`](reference/config-keys.md) | `agent.ini` 逐键说明，与 `CONFIG_POLICY_KEYS` 逐键对账 |

### 设计（原 `DESIGN.md` 按主题拆开，**编号一律没动**）

| 文件 | 一句话 |
|---|---|
| [`design/00-decisions.md`](design/00-decisions.md) | 已锁定决策表（原 §0），每条指向对应的 ADR |
| [`design/01-architecture.md`](design/01-architecture.md) | 架构总览（原 §1） |
| [`design/02-tick-protocol.md`](design/02-tick-protocol.md) | 核心协议 `tick` 的设计取舍（原 §2） |
| [`design/03-agent.md`](design/03-agent.md) | 客户端 Agent：零依赖、主循环、扫描、注册、systemd unit、py38 清单（原 §3） |
| [`design/04-server.md`](design/04-server.md) | 服务端：目录、数据模型、写入策略、成绩回写、代码归题、注册配对、名单、迁移（原 §4） |
| [`design/05-api-and-frontend.md`](design/05-api-and-frontend.md) | 接口约定与前端：契约测试、信封、前端架构、文案口径、删除语义、公开端口（原 §5） |
| [`design/06-security.md`](design/06-security.md) | 安全面清单与签名算法选型（原 §6） |
| [`design/07-ops-and-install.md`](design/07-ops-and-install.md) | 运维与装机：`.key/`、发布、安装策略三档、服务端发现、远程卸载（原 §7） |
| [`design/08-milestones-and-open.md`](design/08-milestones-and-open.md) | 里程碑、待补信息、待办与被明确否决的想法（原 §8 / §9 / §10） |

### 决策记录（ADR）

每篇一个主题：**状态 / 背景 / 决定 / 后果 / 被否掉的替代方案与理由**。
`design/` 记"现在是什么"，ADR 记"为什么是它、当初否掉了什么"。

| 文件 | 一句话 |
|---|---|
| [`decisions/README.md`](decisions/README.md) | ADR 清单与写法 |
| [`decisions/ADR-0001-tech-stack-and-dependencies.md`](decisions/ADR-0001-tech-stack-and-dependencies.md) | 技术栈与依赖：为什么是 Python 3.8 + 零依赖、INI 而不是 TOML |
| [`decisions/ADR-0002-communication-and-identity.md`](decisions/ADR-0002-communication-and-identity.md) | 通信与身份：为什么轮询、机器怎么被认出来、配对为什么绑"人" |
| [`decisions/ADR-0003-delete-semantics.md`](decisions/ADR-0003-delete-semantics.md) | 删除语义：结构性数据硬删、产物性数据留墓碑 |
| [`decisions/ADR-0004-ui-copy-and-notice.md`](decisions/ADR-0004-ui-copy-and-notice.md) | 界面文案口径与考场公告为什么用 `NOTICE.md` |
| [`decisions/ADR-0005-public-port.md`](decisions/ADR-0005-public-port.md) | 公开端口：默认 80、白名单还是黑名单、为什么回 404 |
| [`decisions/ADR-0006-delete-nothing-of-the-verifier.md`](decisions/ADR-0006-delete-nothing-of-the-verifier.md) | 验收不许拿自己人替代：信任锚取包里那一份、交叉验证 |
| [`decisions/ADR-0007-unit-path-whitelist.md`](decisions/ADR-0007-unit-path-whitelist.md) | 单元里的 `ReadWritePaths` 每条都要带 `-` |
| [`decisions/ADR-0008-systemd-unit-hardening.md`](decisions/ADR-0008-systemd-unit-hardening.md) | 单元加固的四处刻意例外（`Restart=`/`Group=`/`NoNewPrivileges=`） |
| [`decisions/ADR-0009-registration-is-a-root-unit.md`](decisions/ADR-0009-registration-is-a-root-unit.md) | 注册由 root 的一次性单元做，服务本体永不自己注册 |
| [`decisions/ADR-0010-run-user-owns-path-templates.md`](decisions/ADR-0010-run-user-owns-path-templates.md) | `{home}`/`{desktop}` 恒按**运行账号**展开 |
| [`decisions/ADR-0011-zipcrypto-not-aes.md`](decisions/ADR-0011-zipcrypto-not-aes.md) | zip 密码用 ZipCrypto 而不是 AES，口径只到"挡得住随手翻看" |
| [`decisions/ADR-0012-remote-uninstall-authorization.md`](decisions/ADR-0012-remote-uninstall-authorization.md) | 远程卸载的授权模型，以及为什么放开那四条沙箱路径 |
| [`decisions/ADR-0013-install-policy-defaults.md`](decisions/ADR-0013-install-policy-defaults.md) | 安装策略三条的默认值 |
| [`decisions/ADR-0014-config-must-prove-it-is-read.md`](decisions/ADR-0014-config-must-prove-it-is-read.md) | 一个配置项必须有测试证明它真的被读到 |

## 文档之间的关系

```
README.md                 是什么 + 三条命令 + 这一页的入口
DESIGN.md                 § 编号 -> 新文件的映射表（老引用靠它落点）
docs/README.md            这一页
├── quickstart.md         教师视角的"怎么用"
├── install-agent.md      装机视角的"怎么装、怎么修"
├── operate.md            运维视角的"怎么部署与发版"
├── reference/            字段与约定的权威描述（改代码要同步改这里）
├── design/               系统"是什么样、为什么这样"
└── decisions/            "当初否掉了什么" —— ADR
```

字段与约定的权威描述在 `reference/` 下：`reference/protocol.md`、
`reference/api-conventions.md`、`reference/config-keys.md`，改代码要同步改这三份。
契约测试（`server/tests/test_agent_contract.py`）会自动发现不一致。

## 要给这个仓库改代码的人

`仓库结构` 与 `开发约定` 两节放在这里：它们不是操作说明、也不是设计依据，
而是"在这个 checkout 里干活要知道的规矩"。

### 仓库结构

```
SyncOJ/
├── DESIGN.md                 完整技术方案
├── docs/
│   ├── protocol.md           Agent ⇄ Server 协议规范
│   ├── api-conventions.md    管理端 API 形状约定（信封 / 错误体 / 删除语义）
│   └── judge-result.md       评测器对接：成绩怎么进 SyncOJ
├── server/                   服务端（FastAPI + SQLite WAL）
│   ├── syncoj_server/
│   └── tests/
├── agent/                    选手端 Agent（Python 3.8 零依赖）
│   ├── syncoj_agent/
│   ├── packaging/            幂等安装器 / 打包器 / 在线自举
│   ├── tools/                py38 兼容门禁、协议 fixture 生成
│   └── tests/
├── scripts/                  一键跑全部检查（check.sh / check.ps1，步骤一一对应）
└── web/                      管理界面（Vue3 + TS + Vite + Element Plus）
    ├── src/api/              client + resource 声明 + 从 OpenAPI 生成的类型
    ├── src/composables/      useList / useMutation —— 所有页面共用的数据层
    ├── src/components/       通用页面外壳、表格、确认对话框
    ├── src/views/            9 个页面
    └── openapi.json          由 server/tools/dump_openapi.py 导出（纳入版本管理）
```

> 上面这棵树里的 `DESIGN.md`、`docs/protocol.md`、`docs/api-conventions.md`、
> `docs/judge-result.md` 是**重建之前**的样子（树是逐字留档的，所以它没有跟着变）。
> 现在的落点：`DESIGN.md` 只剩一张 § 映射表；那三份文档移到了 `docs/reference/`；
> `docs/` 下另有 `quickstart.md` / `install-agent.md` / `operate.md` / `design/` /
> `decisions/`，全在本文开头那份清单里。

### 开发约定

- **Agent 侧代码必须是 Python 3.8 兼容**（目标机是 NOI Linux 2.0）。
  提交前跑 `python agent/tools/check_py38.py agent/`，CI 也会跑。
- **协议改动必须同步三处**：`server/syncoj_server/schemas.py`、
  `agent/syncoj_agent/` 的报文体构造、`docs/reference/protocol.md`。
  契约测试 `server/tests/test_agent_contract.py` 会自动发现不一致。
- **服务端的路径安全校验必须独立实现**，绝不信任 Agent 上报的路径。
  `agent/syncoj_agent/safepath.py` 是**另一份独立实现**（纵深防御），
  两者的一致性由契约测试中的 `test_path_validation_parity` 守住。
- **前端类型绝不手写**。改完服务端模型后依次跑：

  ```bash
  python server/tools/dump_openapi.py && (cd web && npm run gen:types)
  ```

  `scripts/check.sh` 会校验 `web/openapi.json` 是否已同步，不同步直接失败。

## 跑全部检查

```bash
./scripts/check.sh          # Linux / CI；Windows 的 Git bash 里也能直接跑
```

```powershell
.\scripts\check.ps1            # Windows PowerShell 5.1 与 pwsh 7 都能跑
```

两个脚本**步骤一一对应**，只换执行方式：改了其中一个就要改另一个，否则两个平台
给出的"通过"含义不同。

- `check.sh` 自动挑可用的 Python：先 `python3`，不行就退回仓库里的 `.venv`。
  Windows 上 `python3` 常常指向应用商店的占位程序（`command -v` 找得到、执行直接
  失败），所以判据是"**真的能跑起来**且 ≥3.8"，不是"命令存在"。实测在 Git bash 里
  可以直接跑通全程。
- `check.ps1` 是 Windows 原生入口，少跨一层 msys 路径翻译，出错信息更直白。

> **`.ps1` 必须带 UTF-8 BOM。** Windows PowerShell 5.1 在**没有 BOM** 时按当前
> ANSI 代码页（简体中文 Windows 上是 GBK）解码脚本文件，于是仓库里这些中文注释
> 会让它**整个脚本解析失败**，报的还是"字符串缺少终止符"这种指向完全无关位置的
> 错。PS 7 认 BOM，所以带上两边都对。编辑类工具保存时**不会保留 BOM** ——
> 改一行中文注释就可能把它弄丢，所以这件事由 `check.sh` 里一步专门守着。

检查内容：`PowerShell` 脚本编码 → py38 兼容门禁 → Agent 测试 → 服务端测试
→ `openapi.json` 是否已同步 → `schema.d.ts` 是否与 `openapi.json` 一致
（**重新生成一遍再逐字节比对**）→ 前端入口对账 + `typecheck` + `build`。

> 生成物那条检查刻意**不用 `git diff`**：git diff 比的是工作树和 HEAD，而开发中
> 工作树本来就有未提交的改动，于是它在最常见的场景下恒红。判据是"重新生成的结果
> 和仓库里那份是否一致"。

## 状态

| 里程碑 | 状态 |
|---|---|
| M1 骨架：enroll / tick / 在线状态 / 管理后台 | ✅ 完成 |
| M2 回收：扫描 / 上传 / 内容寻址存储 / `source/` 落盘 / 删除审计 | ✅ 完成 |
| M3 下发：资产管理 / 任务编排（全员·按人·按分组）/ Range 断点续传 / 进度聚合 | ✅ 完成 |
| M4 成绩：结果扫描回写 / 成绩矩阵 / 手工补录 / 原始记录 | ✅ 完成 |
| M5 运维：RSA 签名自更新 / 安全解包 / 自动回滚 / 幂等安装器 / 可复现打包 | ✅ 完成 |
| 管理界面：9 个页面 + 初始化闭环（建场次 / 导名单 / 维护题目 / 机器配对） | ✅ 完成 |
| 代码归题：题目 glob 模式 / 文件台账归类 / 路径试算 / 三层可配置 | ✅ 完成 |
| 交付视图：区分「未交」「已待评测」「已出成绩」，列表页给交题进度 | ✅ 完成 |
| 名单库：全局可复用名单，场次引用默认名单并一键应用 | ✅ 完成 |
| 永久配对：镜像一份统一密钥 + 六位配对码绑定名单条目 + 指纹自愈 + 克隆告警 | ✅ 完成 |
| API 形状统一：列表信封 / 统一错误体 / 输入名称确认的删除 | ✅ 完成 |
| 局域网发现：UDP 探测 + 签名应答，机器侧零配置找服务端 | ✅ 完成 |
| 安装入口：`/install` 一键装机 + 从服务端直接安装（台账/安装器/自举脚本） | ✅ 完成 |
| 公开端口：默认 80 只放装机页与选手页；管理端与 `/docs` 在那个端口上不存在 | ✅ 完成 |
| 选手页公告：正文取自下发的 `NOTICE.md`，**不另设公告字段** | ✅ 完成 |
| 考试时间窗：窗口外拒收；到点**自动**把场次置为「已结束」（不做违规判定） | ✅ 完成 |

### 快速开始（开发）

这一节讲开发机上怎么把三块跑起来；面向教师的开考流程见 [`quickstart.md`](quickstart.md)，
仓库根 `README.md` 的「三条命令」是最短形式。

```bash
# 服务端
python -m venv server/.venv
server/.venv/Scripts/activate      # Linux: source server/.venv/bin/activate
pip install -e "server[dev]"
syncoj-server init                 # 建库 + 生成管理员口令
syncoj-server serve --host 0.0.0.0

# 管理界面（开发模式，热更新）
cd web && npm install
npm run dev                        # http://localhost:5173，API 自动代理到 :8000

# 管理界面（构建后由服务端托管 —— 单进程部署，不需要额外的 Caddy/nginx）
npm run build                      # 产出 web/dist
# 服务端启动时自动探测 web/dist 并挂载；探测不到就只提供 API，/docs 仍可用

# Agent（零依赖）
python agent/run_agent.py --check                          # 用内置默认配置自检
python agent/run_agent.py --config agent/config.example.ini --check
python agent/run_agent.py --once
```

> `--check` **不带 `--config`** 时用的是内置默认值（`ca_file` 留空、服务端
> `https://127.0.0.1:8000`），所以开箱就能跑。带上 `config.example.ini` 会去
> 找 `/etc/syncoj/ca.pem` —— 那是个**部署模板**，填好真实路径再用。

> **Agent 必须通过 `run_agent.py` 启动，不能是 `syncoj_agent/main.py`。**
> 后者使用包内相对导入，当脚本直接执行会报
> `ImportError: attempted relative import with no known parent package`。
> `-E` 会连 `PYTHONPATH` 一起忽略，所以只能靠启动器显式设置 `sys.path`。
