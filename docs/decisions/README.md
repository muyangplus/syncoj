# ADR：决策记录

[`../design/`](../design/) 讲系统现在是什么样；这里讲当初**否掉了什么**、代价是什么。改设计之前先读相关的那篇。

每篇五节固定：**状态 / 背景 / 决定 / 后果 / 被否掉的替代方案**。最后一节是重点，没有它 ADR 只是设计说明的复制品。
决定被修订时直接改写正文成当前决定，状态行注明修订日期，旧方案移进"被否掉的替代方案"。

## 按主题合页的"锁定决策"

来源是 [`../design/00-decisions.md`](../design/00-decisions.md) 的决策表；按主题合页而不是一行一篇，否则每篇短到没有内容、同一主题要读者自己拼。

| ADR | 主题 |
|---|---|
| [ADR-0001](ADR-0001-tech-stack-and-dependencies.md) | 技术栈与依赖：Python 3.8 零依赖、轮询、INI、SQLite |
| [ADR-0002](ADR-0002-communication-and-identity.md) | 通信与身份：token、`machine_uuid`、六位配对码、一人一机 |
| [ADR-0003](ADR-0003-delete-semantics.md) | 删除语义：结构性数据硬删 + 输名称确认；代码 / 资产也硬删（2026-11 修订） |
| [ADR-0004](ADR-0004-ui-copy-and-notice.md) | 界面文案口径与考场公告为什么用 `NOTICE.md` |
| [ADR-0005](ADR-0005-public-port.md) | 公开端口：默认 80、黑名单、回 404 |

## 真机故障单独成篇

这些是机器上真出过故障后才定下的，证据是具体的故障记录而非推理，所以与上面分开。

| ADR | 主题 |
|---|---|
| [ADR-0006](ADR-0006-delete-nothing-of-the-verifier.md) | 验收不许拿自己人替代：信任锚取包里那一份、openssl 交叉验证 |
| [ADR-0007](ADR-0007-unit-path-whitelist.md) | 单元里的 `ReadWritePaths` 每条都要带 `-`（226/NAMESPACE） |
| [ADR-0008](ADR-0008-systemd-unit-hardening.md) | 单元加固的刻意例外 |
| [ADR-0009](ADR-0009-registration-is-a-root-unit.md) | 注册由 root 的一次性单元做，服务本体永不自己注册 |
| [ADR-0010](ADR-0010-run-user-owns-path-templates.md) | `{home}`/`{desktop}` 恒按**运行账号**展开 |
| [ADR-0011](ADR-0011-zipcrypto-not-aes.md) | zip 密码用 ZipCrypto 而不是 AES |
| [ADR-0012](ADR-0012-remote-uninstall-authorization.md) | 远程卸载的授权模型与 root 管理单元 |
| [ADR-0013](ADR-0013-install-policy-defaults.md) | 安装策略三条的默认值 |
| [ADR-0014](ADR-0014-config-must-prove-it-is-read.md) | 一个配置项必须有测试证明它真的被读到 |
| [ADR-0015](ADR-0015-server-announce.md) | 服务端主动通告只当线索，切换前挑战应答；机器自己探测保留 |
| [ADR-0016](ADR-0016-builtin-judge.md) | 自带评测写 `judge_result/`、独立评测机、沙箱借用 isolate |

## 新增一篇 ADR 的门槛

只在"以后有人可能想改回去"时写。判据是**被否掉的替代方案**：说不出否掉了什么、为什么否，就不值得一篇 ADR；一句话能说清的决定留在 [`../design/00-decisions.md`](../design/00-decisions.md) 的决策表里。

编号连续、不回收；文件名 `ADR-<四位序号>-<短横线主题>.md`。
