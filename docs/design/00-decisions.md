# 0. 已锁定决策

这一节只回答"定下来的是什么"：**"为什么这么定、当初否掉了什么"在 ADR 里**
（[`../decisions/`](../decisions/)）—— 最后一列指过去。要改哪一行之前先读它指向的
那篇 ADR，里面记着被否掉的替代方案，免得同一个想法被重新提一遍。

**这张表的正文是原来 `DESIGN.md` 那一节逐行搬过来的**（只做了两件事：把跨文件的
`见 §X` 改成可点的链接，以及在备注列尾巴上补一条 ADR 指路）。所以代码注释里的
老式编号引用与新位置指的是同一件事。

| 项 | 决策 | 备注 |
|---|---|---|
| 目标环境 | NOI Linux 2.0（Ubuntu 20.04 / glibc 2.31 / systemd 245 / Python 3.8.10） | v1 仅 Linux，平台层预留 Windows（[`ADR-0001`](../decisions/ADR-0001-tech-stack-and-dependencies.md)） |
| Agent 技术栈 | Python 3.8 + **零第三方依赖**，`python3 -E -s` 启动 | 见 [03-agent.md §3.1](03-agent.md) 为何砍依赖（[`ADR-0001`](../decisions/ADR-0001-tech-stack-and-dependencies.md)） |
| 通信模型 | **HTTP 自适应轮询**（空闲 60s / 有活 2s），无 WebSocket | 同步延迟容忍度 20s（[`ADR-0002`](../decisions/ADR-0002-communication-and-identity.md)） |
| 服务端 | FastAPI + uvicorn + SQLAlchemy 2.0 + SQLite(WAL)，单机 | （[`ADR-0001`](../decisions/ADR-0001-tech-stack-and-dependencies.md)） |
| 前端 | Vue3 + Vite + Pinia，TS 类型从 OpenAPI 生成 | M3 起（[`ADR-0001`](../decisions/ADR-0001-tech-stack-and-dependencies.md)） |
| 评测对接 | 只投递 `source/` + 扫描结果目录回写成绩，**不触发评测** | 开场仍由教师点 LemonLime/Arbiter GUI（[`ADR-0001`](../decisions/ADR-0001-tech-stack-and-dependencies.md)） |
| 认证 | 每机独立 Token（服务端只存 `sha256(token)`） | （[`ADR-0002`](../decisions/ADR-0002-communication-and-identity.md)） |
| 机器身份 | `machine_uuid`（首选）+ SMBIOS 指纹（快照还原兜底）；`machine_id` 只供人工辨认 | 见 [03-agent.md §3.4](03-agent.md) / [04-server.md §4.6](04-server.md)（[`ADR-0002`](../decisions/ADR-0002-communication-and-identity.md)） |
| 数据库 | SQLite WAL，内存态优先 + 批量落库 | （[`ADR-0001`](../decisions/ADR-0001-tech-stack-and-dependencies.md)） |
| 场次 | 多场次隔离（`contest_id`）；机器绑的是**人**，场次动态解析 | 见 [04-server.md §4.6](04-server.md)（[`ADR-0002`](../decisions/ADR-0002-communication-and-identity.md)） |
| 防篡改 | 完整性校验 + 审计日志，不干预选手 | （[`ADR-0002`](../decisions/ADR-0002-communication-and-identity.md)） |
| 自更新 | **RSA-2048 + PKCS#1 v1.5 (SHA-256)** 签名校验；`upgrade.mode` **默认 `apply`**（下载→验签→切换→自动重启），可在发布时的「高级选项」里改成 `stage`/`off`；**服务端仍要显式铺开**（机器只拿铺开的版本） | 见 [06-security.md §6.1](06-security.md) 为何不是 Ed25519、[07-ops-and-install.md §7.2](07-ops-and-install.md) 安装策略（[`ADR-0013`](../decisions/ADR-0013-install-policy-defaults.md)） |
| 后台账号 | 单管理员 | （[`ADR-0002`](../decisions/ADR-0002-communication-and-identity.md)） |
| 装机 | 镜像预装 / 离线包 / 在线自举，同一份幂等逻辑 | （[`ADR-0013`](../decisions/ADR-0013-install-policy-defaults.md)） |
| 网络 | 考场同一内网，双向可达 | Ansible 留作以后赛前批量装机的备选，v1 不用（[`ADR-0002`](../decisions/ADR-0002-communication-and-identity.md)） |
| 目录约定 | `桌面/<准考证号>/<题目名>/<题目名>.cpp`；题面/样例默认下发到**桌面根目录**，**以 zip 文件形态原样落盘，服务端不解压** | 只是**默认值**，三层皆可改，见 [04-server.md §4.5](04-server.md)（[`ADR-0011`](../decisions/ADR-0011-zipcrypto-not-aes.md)） |
| 代码归题 | 每道题一组 glob 模式，**第一条命中者胜出**；归属是算出来的、不落库 | 见 [04-server.md §4.5](04-server.md) |
| 配对方式 | 镜像统一密钥注册 + **六位数字配对码**绑定到名单条目（人）；**配对永久，取代每选手注册码** | 见 [04-server.md §4.6](04-server.md)（[`ADR-0009`](../decisions/ADR-0009-registration-is-a-root-unit.md)） |
| 名单 | 全局可复用的 `Roster`，场次**复制**一份进来（不是引用） | 见 [04-server.md §4.7](04-server.md)（[`ADR-0003`](../decisions/ADR-0003-delete-semantics.md)） |
| 管理端 API | 统一列表信封 `{items,total,limit,offset}` + 统一错误体 `{detail,code,details}` | 见 [05-api-and-frontend.md §5.2](05-api-and-frontend.md) / `../reference/api-conventions.md` |
| 前端 | 共享数据层（`useList`/`useMutation`）+ 通用页面外壳，页面只描述长相不描述取数 | 见 [05-api-and-frontend.md §5.3](05-api-and-frontend.md)（[`ADR-0001`](../decisions/ADR-0001-tech-stack-and-dependencies.md)） |
| 删除语义 | 结构性数据硬删 + **输入名称确认**；产物性数据（代码/资产）软删留墓碑 | 见 [05-api-and-frontend.md §5.4](05-api-and-frontend.md)（[`ADR-0003`](../decisions/ADR-0003-delete-semantics.md)） |
| 库结构演进 | `PRAGMA user_version` + 有序迁移；重建表需要一套已验证的安全配方 | 见 [04-server.md §4.8](04-server.md)（[`ADR-0003`](../decisions/ADR-0003-delete-semantics.md)） |
| 选手代码目录 | **默认 `~/Desktop/<准考证号>`**（`{desktop}` 按运行账号的家目录展开），下发根默认 `~/Desktop` | 用户拍板；见 [04-server.md §4.5](04-server.md)（[`ADR-0010`](../decisions/ADR-0010-run-user-owns-path-templates.md)） |
| Agent 运行账号 | **默认 = 跑安装的那个账号**（显式 `--user` 才创建专用账号）；状态目录属主跟着它 | 代码与下发文件都在那个账号的桌面下，跨用户授权在现场容易装成"服务起来了但什么都不传"（[`ADR-0010`](../decisions/ADR-0010-run-user-owns-path-templates.md)） |
| 界面文案 | 页面上**禁止**声明式/解释式语句，只留动作、破坏性后果与实时数据 | 见 [05-api-and-frontend.md §5.3](05-api-and-frontend.md)（[`ADR-0004`](../decisions/ADR-0004-ui-copy-and-notice.md)） |

> 表里只留**结论**，论证在它指向的那一节与那篇 ADR 里 —— 免得同一个道理写两遍，
> 然后随着时间慢慢不一致。
