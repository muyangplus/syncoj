# SyncOJ 技术方案

正文已经按受众拆到 [`docs/design/`](docs/design/) 下。**这份文件只剩一张映射表**，
存在的唯一理由：代码与注释里有几十处 `DESIGN.md §5.4`、`§7.4` 这类引用
（`git grep -n "§"` 可看现状），逐个改成新路径收益是零、改错风险很大。
**章节编号一律没有动** —— 所以 `DESIGN.md §X.Y` 与 `docs/design/<文件> §X.Y`
指的是同一节，只是正文换了地方。

| 说法 | 现在指哪儿 |
|---|---|
| `DESIGN.md §X` | 下表中 X 那一行指到的那份设计文档，编号不变 |
| 本文件 | **只有映射表**；正文不在里面 |
| 改动要先读什么 | 先读那一节对应的 ADR（[`docs/decisions/`](docs/decisions/)） |

## 顶层章节

| § | 新位置 |
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

## 小节（代码与注释里真的有引用的那些）

| § | 新位置 |
|---|---|
| §3.1 为什么砍掉所有第三方依赖 | [`docs/design/03-agent.md`](docs/design/03-agent.md) |
| §3.2 主循环 | [`docs/design/03-agent.md`](docs/design/03-agent.md) |
| §3.3 扫描与上传 | [`docs/design/03-agent.md`](docs/design/03-agent.md) |
| §3.4 注册、"永久配对"与"快照还原自愈" | [`docs/design/03-agent.md`](docs/design/03-agent.md) |
| §3.5 systemd unit | [`docs/design/03-agent.md`](docs/design/03-agent.md) |
| §3.6 Python 3.8 兼容清单 | [`docs/design/03-agent.md`](docs/design/03-agent.md) |
| §4.1 目录布局 / §4.2 数据模型 / §4.3 写入策略 / §4.4 评测结果回写 | [`docs/design/04-server.md`](docs/design/04-server.md) |
| §4.5 代码归题与目录约定 / §4.6 注册与配对 / §4.7 名单 / §4.8 库结构演进 | [`docs/design/04-server.md`](docs/design/04-server.md) |
| §5.1 契约测试 / §5.2 信封与错误体 / §5.3 前端架构 / §5.4 删除语义 | [`docs/design/05-api-and-frontend.md`](docs/design/05-api-and-frontend.md) |
| §5.5 下发资产不解压 / §5.6 两个免登录页面 / §5.7 公开端口 | [`docs/design/05-api-and-frontend.md`](docs/design/05-api-and-frontend.md) |
| §6.1 为什么签名不是 Ed25519 | [`docs/design/06-security.md`](docs/design/06-security.md) |
| §7.1 密钥从哪来 / §7.2 发布当前版本 / §7.2.1 安装策略 | [`docs/design/07-ops-and-install.md`](docs/design/07-ops-and-install.md) |
| §7.3 机器怎么知道服务端在哪 / §7.4 远程卸载 | [`docs/design/07-ops-and-install.md`](docs/design/07-ops-and-install.md) |

**"为什么这么定"在 ADR 里**（[`docs/decisions/`](docs/decisions/)）：`design/` 讲
现在是什么样，ADR 讲当初否掉了什么、代价是什么。§0 那张表里每一行都指向对应的 ADR。

上手、装机、运维三份操作说明不在 `design/` 下 —— 它们是给人照着做的，不是设计依据：
[`docs/quickstart.md`](docs/quickstart.md)、[`docs/install-agent.md`](docs/install-agent.md)、
[`docs/operate.md`](docs/operate.md)。文档总入口是 [`docs/README.md`](docs/README.md)。
