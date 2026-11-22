# SyncOJ 技术方案

本文件只有「§ 编号 → 设计文档」映射表；正文在 [`docs/design/`](docs/design/)。

| 写法 | 指向 |
|---|---|
| `DESIGN.md §X.Y` | 下表 §X.Y 所在的设计文档，编号与 `docs/design/<文件> §X.Y` 相同 |
| 改某一节之前 | 先读对应 ADR（[`docs/decisions/`](docs/decisions/)） |

## 顶层章节

| § | 文件 |
|---|---|
| §0 已锁定决策 | [`docs/design/00-decisions.md`](docs/design/00-decisions.md) |
| §1 架构总览 | [`docs/design/01-architecture.md`](docs/design/01-architecture.md) |
| §2 核心协议 `tick` | [`docs/design/02-tick-protocol.md`](docs/design/02-tick-protocol.md) |
| §3 客户端 Agent | [`docs/design/03-agent.md`](docs/design/03-agent.md) |
| §4 服务端 | [`docs/design/04-server.md`](docs/design/04-server.md) |
| §5 接口约定与前端 | [`docs/design/05-api-and-frontend.md`](docs/design/05-api-and-frontend.md) |
| §6 安全 | [`docs/design/06-security.md`](docs/design/06-security.md) |
| §7 运维与装机 | [`docs/design/07-ops-and-install.md`](docs/design/07-ops-and-install.md) |
| §8 里程碑 / §9 待补信息 / §10 待办 | [`docs/design/08-milestones-and-open.md`](docs/design/08-milestones-and-open.md) |
| §11 服务端主动通告 | [`docs/design/09-server-announce.md`](docs/design/09-server-announce.md) |
| §12 自带沙箱评测 | [`docs/design/10-builtin-judge.md`](docs/design/10-builtin-judge.md) |

## 小节（代码与注释中有引用的）

| § | 文件 |
|---|---|
| §3.1 零第三方依赖 | [`docs/design/03-agent.md`](docs/design/03-agent.md) |
| §3.2 主循环 | [`docs/design/03-agent.md`](docs/design/03-agent.md) |
| §3.3 扫描与上传 | [`docs/design/03-agent.md`](docs/design/03-agent.md) |
| §3.4 注册、"永久配对"与"快照还原自愈" | [`docs/design/03-agent.md`](docs/design/03-agent.md) |
| §3.5 systemd unit | [`docs/design/03-agent.md`](docs/design/03-agent.md) |
| §3.6 Python 3.8 兼容清单 | [`docs/design/03-agent.md`](docs/design/03-agent.md) |
| §4.1 目录布局 / §4.2 数据模型 / §4.3 写入策略 / §4.4 评测结果回写 | [`docs/design/04-server.md`](docs/design/04-server.md) |
| §4.5 代码归题与目录约定 / §4.6 注册与配对 / §4.7 名单 / §4.8 库结构演进 | [`docs/design/04-server.md`](docs/design/04-server.md) |
| §5.1 契约测试 / §5.2 信封与错误体 / §5.3 前端架构 / §5.4 删除语义 | [`docs/design/05-api-and-frontend.md`](docs/design/05-api-and-frontend.md) |
| §5.5 下发资产不解压 / §5.6 两个免登录页面 / §5.7 公开端口 | [`docs/design/05-api-and-frontend.md`](docs/design/05-api-and-frontend.md) |
| §6.1 签名算法 / §6.2 信任边界的现状 | [`docs/design/06-security.md`](docs/design/06-security.md) |
| §7.1 密钥从哪来 / §7.2 发布当前版本 / §7.2.1 安装策略 | [`docs/design/07-ops-and-install.md`](docs/design/07-ops-and-install.md) |
| §7.3 机器怎么知道服务端在哪 / §7.4 远程卸载 / §7.5 发布（CD） | [`docs/design/07-ops-and-install.md`](docs/design/07-ops-and-install.md) |
| §10.5 重构遗留 | [`docs/design/08-milestones-and-open.md`](docs/design/08-milestones-and-open.md) |
| §12.5 评测语义 / §12.8 题目配置导入导出 | [`docs/design/10-builtin-judge.md`](docs/design/10-builtin-judge.md) |

`design/` 记现状，ADR 记否决的方案与代价；§0 每行指向对应 ADR。
操作步骤：[`docs/quickstart.md`](docs/quickstart.md)、[`docs/install-agent.md`](docs/install-agent.md)、[`docs/operate.md`](docs/operate.md)。文档总入口：[`docs/README.md`](docs/README.md)。
