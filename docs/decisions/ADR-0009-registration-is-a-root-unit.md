# ADR-0009：注册由 root 的一次性单元做，服务本体永不自己注册

**状态**：已锁定（v1，真机故障后补上）

## 背景

真机故障：机器不出现。统一注册密钥 `/etc/syncoj/bootstrap.key` 为 **0600、属 root**（可注册整间机房），`syncoj-agent.service` 以运行账号（选手）运行，读不到它。服务本体注册时：

```
读密钥 → PermissionError → 抛异常 → 退出 1
  → systemd 按 RestartSec=5 反复重启
  → 5 次后撞上 StartLimit，罢手
  → 而注册单元从来没被跑过
```

现场只见"反复重启"或无任何表现，日志里没有真正原因。

## 决定

两个单元，分工固定：

| 单元 | 身份 | 职责 |
|---|---|---|
| `syncoj-agent-enroll.service` | **root**（oneshot；装机时 enable 并立刻跑一次） | 读 `/etc/syncoj/bootstrap.key`，换回本机凭据，写进状态目录并交给运行账号 |
| `syncoj-agent.service` | **运行账号**（= 执行安装的账号） | 只读凭据；**无凭据时等待**，不读密钥 |

- 服务拿到凭据前每 `REGISTRATION_POLL_SECONDS` 检查一次凭据，日志写明该查哪个单元。
- "服务不自己注册"同时保证选手账号拿不到注册密钥。

## 后果

- 排障先看注册单元：`systemctl status syncoj-agent-enroll.service`、`journalctl -u syncoj-agent-enroll.service -n 30`。注册失败原文（密钥被吊销、还没配对、`agent.ini` 属主不对、被限速、扫描目录父目录不存在…）都在那里，排障表见 [`../install-agent.md`](../install-agent.md)。
- 出现 `Start request repeated too quickly` 表示 systemd 已停止重启：修复根因后 `systemctl reset-failed syncoj-agent`，先起注册单元、再起服务。
- 代价：多一个单元；服务重启不会再注册，重新注册须显式运行注册单元（开机时它也会运行）。

## 被否掉的替代方案

* **服务本体自己注册**（用 root 跑服务，或服务里 `sudo`）：即该故障的成因，且破坏"选手拿不到注册密钥"。
* **放宽密钥权限让选手可读**：学生可注册任意机器。
* **密钥写进 `agent.ini`**：该文件 chown 给选手账号；还可能被 `config_policy` 合并覆盖。
* **无凭据即失败退出**：撞 `StartLimit` 后停止，日志不提示注册单元。
* **注册单元用 `Restart=`**：oneshot 重启语义跨版本不一致（[ADR-0008](ADR-0008-systemd-unit-hardening.md)）；有限重试写在 `ExecStart` 的 shell 循环里。
* **服务每次启动都重新注册**：重注册应是显式动作（运行注册单元），不应每次启动都改凭据。
