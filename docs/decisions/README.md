# ADR：决策记录

**这份目录回答的是"为什么"。** [`../design/`](../design/) 讲系统现在是什么样；
这里讲当初**否掉了什么**、代价是什么。查"某个设计为什么长这样"从这里入手，
改设计之前也应该先读一遍相关的那篇。

每篇一个主题，五节固定：**状态 / 背景 / 决定 / 后果 / 被否掉的替代方案与理由**。
最后一节是重点：没有它，一篇 ADR 就只是一段设计说明的复制品。

## 按主题合页的"锁定决策"

来源是 [`../design/00-decisions.md`](../design/00-decisions.md) 里那张 25 行的决策表。
**没有拆成 25 个文件** —— 那样每篇都短到没有内容，读的人还得自己把同一主题的几条拼回去。

| ADR | 主题 |
|---|---|
| [ADR-0001](ADR-0001-tech-stack-and-dependencies.md) | 技术栈与依赖：Python 3.8 零依赖、轮询、INI、SQLite |
| [ADR-0002](ADR-0002-communication-and-identity.md) | 通信与身份：token、`machine_uuid`、六位配对码、一人一机 |
| [ADR-0003](ADR-0003-delete-semantics.md) | 删除语义：结构性数据硬删、产物性数据留墓碑 |
| [ADR-0004](ADR-0004-ui-copy-and-notice.md) | 界面文案口径与考场公告为什么用 `NOTICE.md` |
| [ADR-0005](ADR-0005-public-port.md) | 公开端口：默认 80、黑名单、回 404 |

## 真机故障单独成篇

这些是机器上真的出过故障之后才定下来的。它们和上面的"锁定决策"分开，
因为它们的证据不是推理，是一份具体的故障记录。

| ADR | 主题 |
|---|---|
| [ADR-0006](ADR-0006-delete-nothing-of-the-verifier.md) | 验收不许拿自己人替代：信任锚取包里那一份、openssl 交叉验证 |
| [ADR-0007](ADR-0007-unit-path-whitelist.md) | 单元里的 `ReadWritePaths` 每条都要带 `-`（226/NAMESPACE） |
| [ADR-0008](ADR-0008-systemd-unit-hardening.md) | 单元加固的四处刻意例外 |
| [ADR-0009](ADR-0009-registration-is-a-root-unit.md) | 注册由 root 的一次性单元做，服务本体永不自己注册 |
| [ADR-0010](ADR-0010-run-user-owns-path-templates.md) | `{home}`/`{desktop}` 恒按**运行账号**展开 |
| [ADR-0011](ADR-0011-zipcrypto-not-aes.md) | zip 密码用 ZipCrypto 而不是 AES |
| [ADR-0012](ADR-0012-remote-uninstall-authorization.md) | 远程卸载的授权模型 |
| [ADR-0013](ADR-0013-install-policy-defaults.md) | 安装策略三条的默认值 |
| [ADR-0014](ADR-0014-config-must-prove-it-is-read.md) | 一个配置项必须有测试证明它真的被读到 |

## 新增一篇 ADR 的门槛

**"定下来一个决定"就写一篇，但只在"以后有人可能会想改回去"的时候写。**
判据是那条**被否掉的替代方案**：如果你说不出当初否掉了什么、为什么否，
那这件事不值得一篇 ADR —— 它要么没有值得记录的替代方案，要么还没想清楚。
一句话能说清的决定不必单独立篇：这种条目留在
[`../design/00-decisions.md`](../design/00-decisions.md) 的决策表里。

编号连续、不回收：`ADR-0015-…`。文件名是 `ADR-<四位序号>-<短横线主题>.md`。
