# ADR：决策记录

[`../design/`](../design/) 记现状；ADR 记否决的方案与代价。改设计前先读相关 ADR。

## 写法

- 五节固定：**状态 / 背景 / 决定 / 后果 / 被否掉的替代方案**。
- 否决条目写成"**方案**：否决原因一句"。
- 决定被修订时：正文改写为当前决定，状态行注明修订日期，旧方案移进"被否掉的替代方案"。
- 门槛：只在"以后可能有人想改回去"时写；说不出否决了什么的决定，留在 [`../design/00-decisions.md`](../design/00-decisions.md) 决策表。
- 编号连续、不回收；文件名 `ADR-<四位序号>-<短横线主题>.md`。

## 锁定决策（按主题合页）

来源：[`../design/00-decisions.md`](../design/00-decisions.md) 决策表。

| ADR | 主题 |
|---|---|
| [ADR-0001](ADR-0001-tech-stack-and-dependencies.md) | 技术栈与依赖：Python 3.8 零依赖、轮询、INI、SQLite |
| [ADR-0002](ADR-0002-communication-and-identity.md) | 通信与身份：token、`machine_uuid`、六位配对码、一人一机 |
| [ADR-0003](ADR-0003-delete-semantics.md) | 删除语义：结构性数据硬删 + 输名称确认；代码 / 资产也硬删（2026-11 修订） |
| [ADR-0004](ADR-0004-ui-copy-and-notice.md) | 界面文案口径；考场公告用 `NOTICE.md` |
| [ADR-0005](ADR-0005-public-port.md) | 公开端口：默认 80、黑名单、回 404 |

## 真机故障后定下的决策

| ADR | 主题 |
|---|---|
| [ADR-0006](ADR-0006-delete-nothing-of-the-verifier.md) | 验收不用自验：信任锚取包内副本、openssl 交叉验证 |
| [ADR-0007](ADR-0007-unit-path-whitelist.md) | 单元 `ReadWritePaths` 每条带 `-`（226/NAMESPACE） |
| [ADR-0008](ADR-0008-systemd-unit-hardening.md) | 单元加固的刻意例外 |
| [ADR-0009](ADR-0009-registration-is-a-root-unit.md) | 注册由 root 一次性单元执行，服务本体不注册 |
| [ADR-0010](ADR-0010-run-user-owns-path-templates.md) | `{home}`/`{desktop}` 恒按**运行账号**展开 |
| [ADR-0011](ADR-0011-zipcrypto-not-aes.md) | zip 密码用 ZipCrypto，不用 AES |
| [ADR-0012](ADR-0012-remote-uninstall-authorization.md) | 远程卸载的授权模型与 root 管理单元 |
| [ADR-0013](ADR-0013-install-policy-defaults.md) | 安装策略三条的默认值 |
| [ADR-0014](ADR-0014-config-must-prove-it-is-read.md) | 配置项须有测试证明被读到 |
| [ADR-0015](ADR-0015-server-announce.md) | 服务端主动通告只作线索，切换前挑战应答；保留机器探测 |
| [ADR-0016](ADR-0016-builtin-judge.md) | 自带评测写 `judge_result/`、独立评测机、借用 isolate |
