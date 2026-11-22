# 0. 已锁定决策

每行指向对应 ADR（[`../decisions/`](../decisions/)）；改哪一行之前先读它的 ADR。

| 项 | 决策 | 备注 |
|---|---|---|
| 目标环境 | NOI Linux 2.0（Ubuntu 20.04 / glibc 2.31 / systemd 245 / Python 3.8.10） | v1 仅 Linux，平台层预留 Windows（[`ADR-0001`](../decisions/ADR-0001-tech-stack-and-dependencies.md)） |
| Agent 技术栈 | Python 3.8 + **零第三方依赖**，`python3 -E -s` 启动 | 理由见 [03-agent.md §3.1](03-agent.md)（[`ADR-0001`](../decisions/ADR-0001-tech-stack-and-dependencies.md)） |
| 通信模型 | **HTTP 自适应轮询**（空闲 30s / 有活 2s），无 WebSocket；三个节奏值在「机器配对」→「运行参数」修改 | 同步延迟容忍 20s（[`ADR-0002`](../decisions/ADR-0002-communication-and-identity.md)） |
| 服务端 | FastAPI + uvicorn + SQLAlchemy 2.0 + SQLite(WAL)，单机 | [`ADR-0001`](../decisions/ADR-0001-tech-stack-and-dependencies.md) |
| 前端 | Vue3 + Vite + Pinia，TS 类型从 OpenAPI 生成 | M3 起（[`ADR-0001`](../decisions/ADR-0001-tech-stack-and-dependencies.md)） |
| 评测对接 | 外部评测器：只投递 `source/` + 扫描结果目录回写成绩，**不触发评测**；按题可改用自带评测，结果同样写进 `judge_result/` | 外部由教师在 LemonLime/Arbiter GUI 开评（[`ADR-0001`](../decisions/ADR-0001-tech-stack-and-dependencies.md)）；自带评测见 [10-builtin-judge.md](10-builtin-judge.md)（[`ADR-0016`](../decisions/ADR-0016-builtin-judge.md)） |
| 题目配置导入导出 | 外来格式折算成现有数据包，模型只加 `input_file` / `output_file`；折算不了的语义逐题警告 | [10-builtin-judge.md §12.8](10-builtin-judge.md)（[`ADR-0017`](../decisions/ADR-0017-problem-format-import.md)） |
| 认证 | 每机独立 Token，服务端只存 `sha256(token)` | [`ADR-0002`](../decisions/ADR-0002-communication-and-identity.md) |
| 机器身份 | `machine_uuid`（首选）+ SMBIOS 指纹（快照还原兜底）；`machine_id` 只供人工辨认 | [03-agent.md §3.4](03-agent.md) / [04-server.md §4.6](04-server.md)（[`ADR-0002`](../decisions/ADR-0002-communication-and-identity.md)） |
| 数据库 | SQLite WAL，内存态优先 + 批量落库 | [`ADR-0001`](../decisions/ADR-0001-tech-stack-and-dependencies.md) |
| 场次 | 多场次隔离（`contest_id`）；机器绑**人**（可同时绑多份名单），场次按所用名单动态解析；可归档 | [04-server.md §4.6](04-server.md) / [§4.9](04-server.md)（[`ADR-0002`](../decisions/ADR-0002-communication-and-identity.md)、[`ADR-0018`](../decisions/ADR-0018-multi-roster-binding.md)） |
| 防篡改 | 完整性校验 + 审计日志，不干预选手 | [`ADR-0002`](../decisions/ADR-0002-communication-and-identity.md) |
| 自更新 | **RSA-2048 + PKCS#1 v1.5 (SHA-256)** 验签；`upgrade.mode` **默认 `apply`**（下载→验签→切换→重启），发布「高级选项」可改 `stage`/`off`；服务端仍需**显式铺开** | 非 Ed25519 见 [06-security.md §6.1](06-security.md)，安装策略见 [07-ops-and-install.md §7.2](07-ops-and-install.md)（[`ADR-0013`](../decisions/ADR-0013-install-policy-defaults.md)） |
| 后台账号 | 单管理员 | [`ADR-0002`](../decisions/ADR-0002-communication-and-identity.md) |
| 装机 | 镜像预装 / 离线包 / 在线自举，同一份幂等逻辑 | [`ADR-0013`](../decisions/ADR-0013-install-policy-defaults.md) |
| 网络 | 考场同一内网，双向可达 | Ansible 留作赛前批量装机备选，v1 不用（[`ADR-0002`](../decisions/ADR-0002-communication-and-identity.md)） |
| 目录约定 | `桌面/<准考证号>/<题目名>/<题目名>.cpp`；题面/样例默认下发到**桌面根**，**zip 原样落盘、不解压** | 只是默认值，三层可改，见 [04-server.md §4.5](04-server.md)（[`ADR-0011`](../decisions/ADR-0011-zipcrypto-not-aes.md)） |
| 代码归题 | 每题一组 glob，**第一条命中者胜**；归属现算、不落库 | [04-server.md §4.5](04-server.md) |
| 配对方式 | 镜像统一密钥注册 + **六位数字配对码**绑定名单条目（人）；配对永久 | [04-server.md §4.6](04-server.md)（[`ADR-0009`](../decisions/ADR-0009-registration-is-a-root-unit.md)） |
| 名单 | 全局可复用 `Roster`，场次**复制**一份（不是引用） | [04-server.md §4.7](04-server.md)（[`ADR-0003`](../decisions/ADR-0003-delete-semantics.md)） |
| 管理端 API | 列表信封 `{items,total,limit,offset}` + 错误体 `{detail,code,details}` | [05-api-and-frontend.md §5.2](05-api-and-frontend.md) / `../reference/api-conventions.md` |
| 前端 | 共享数据层（`useList`/`useMutation`）+ 通用页面外壳，页面只描述长相 | [05-api-and-frontend.md §5.3](05-api-and-frontend.md)（[`ADR-0001`](../decisions/ADR-0001-tech-stack-and-dependencies.md)） |
| 删除语义 | 结构性数据硬删 + **输入名称确认**；产物性数据（代码/资产）也硬删、blob 按引用回收；只有扫描对账的"已消失"留墓碑 | [05-api-and-frontend.md §5.4](05-api-and-frontend.md)（[`ADR-0003`](../decisions/ADR-0003-delete-semantics.md)） |
| 库结构演进 | `PRAGMA user_version` + 有序迁移；重建表走已验证的安全配方 | [04-server.md §4.8](04-server.md)（[`ADR-0003`](../decisions/ADR-0003-delete-semantics.md)） |
| 选手代码目录 | **默认 `~/Desktop/<准考证号>`**（`{desktop}` 按运行账号家目录展开），下发根默认 `~/Desktop` | [04-server.md §4.5](04-server.md)（[`ADR-0010`](../decisions/ADR-0010-run-user-owns-path-templates.md)） |
| Agent 运行账号 | **默认 = 执行安装的账号**（显式 `--user` 才建专用账号）；状态目录属主跟随 | 否则跨用户授权易装成"服务在跑但什么都不传"（[`ADR-0010`](../decisions/ADR-0010-run-user-owns-path-templates.md)） |
| 界面文案 | 页面**禁止**声明式/解释式语句，只留动作、破坏性后果与实时数据 | [05-api-and-frontend.md §5.3](05-api-and-frontend.md)（[`ADR-0004`](../decisions/ADR-0004-ui-copy-and-notice.md)） |
