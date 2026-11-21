# ADR-0009：注册由 root 的一次性单元做，服务本体永不自己注册

**状态**：已锁定（v1，真机故障后补上）

## 背景

真机故障：机器永远不出现。统一注册密钥 `/etc/syncoj/bootstrap.key` 是 **0600、属 root**（它能注册整间机房），而 `syncoj-agent.service` 以运行账号（选手）跑，读不到它。让服务"顺手注册"的结果是：

```
读密钥 → PermissionError → 抛异常 → 退出 1
  → systemd 按 RestartSec=5 反复重启
  → 5 次后撞上 StartLimit，罢手
  → 而注册单元从来没被跑过
```

现场只见"反复重启"或"什么都没有"，真正原因不在任何日志里。

## 决定

两个单元，分工写死：

| 单元 | 身份 | 干什么 |
|---|---|---|
| `syncoj-agent-enroll.service` | **root**（oneshot；装机时 enable 并立刻跑一次） | 读 `/etc/syncoj/bootstrap.key`，换回本机凭据，写进状态目录并交给运行账号 |
| `syncoj-agent.service` | **运行账号**（= 执行安装的账号） | 只读凭据干活；**没有凭据就等**，绝不自己读密钥 |

- 服务拿到凭据前只**安静地等**（每 `REGISTRATION_POLL_SECONDS` 看一次凭据），并在日志写明下一步该看哪个单元。
- **"服务不自己注册"不能松**：它也是"选手账号拿不到注册密钥"的前提。

## 后果

- 排障先看注册单元：`systemctl status syncoj-agent-enroll.service`、`journalctl -u syncoj-agent-enroll.service -n 30`。注册失败原文（密钥被吊销、还没配对、`agent.ini` 属主不对、被限速、扫描目录父目录不存在…）都在那里，排障表见 [`../install-agent.md`](../install-agent.md)。
- 出现 `Start request repeated too quickly` 说明 systemd 已罢手：修掉真正错误后 `systemctl reset-failed syncoj-agent`，按顺序起注册单元、再起服务。
- 代价：多一个单元；注册是一次性的，服务重启不会再注册，重新注册要显式跑那个单元（开机时它也会再跑）。

## 被否掉的替代方案

* **服务本体自己注册**（用 root 跑服务，或服务里 `sudo`）：正是那次事故的成因，且会毁掉"选手拿不到注册密钥"的保证。
* **放宽密钥权限让选手可读**：等于把"能注册整间机房"的通行证给学生。
* **密钥写进 `agent.ini`**：它 chown 给选手账号，等于公开；还可能被某次 `config_policy` 合并冲掉。
* **没凭据就失败退出**：刷屏后撞 `StartLimit` 彻底罢手，仍没人知道该跑注册单元；"安静地等 + 日志写下一步"让现场一条命令就能修好。
* **注册单元用 `Restart=`**：oneshot 重启语义跨版本不一致（[ADR-0008](ADR-0008-systemd-unit-hardening.md)），有限重试写在 `ExecStart` 的 shell 循环里更可控。
* **服务每次启动都重新注册**：凭据会随整机还原消失，重注册确实要能发生，但应是明确的动作（跑那个单元），不是每次都去动凭据。
