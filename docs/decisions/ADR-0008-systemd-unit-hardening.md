# ADR-0008：systemd 单元加固的四处刻意例外

**状态**：已锁定（v1，真机踩坑后补上）

## 背景

两个单元：`syncoj-agent.service`（常驻，以**运行账号**跑）与
`syncoj-agent-enroll.service`（oneshot，以 **root** 跑一次）。

常规的 systemd 加固清单套上去之后，单元在真机上出了四种**看起来像 bug、其实都是
"加固清单套错了地方"** 的问题。每一条都不是"少写了一个加固选项"，
而是"某个加固选项与这套东西的语义冲突"。

## 决定

四处**刻意不按常规写**，以及一处容易写错位置的：

| 项 | 刻意怎么做 | 为什么 |
|---|---|---|
| 重启策略 | `Restart=on-failure`，**不用 `always`** | 远程卸载成功时 Agent 以 **exit 0** 正常退出；`always` 会立刻把它重新拉起来 —— 而那时 `/opt/syncoj`、`/etc/syncoj` 都已经删了，拉起来只会刷一堆"找不到文件"，现场看起来像"卸载失败" |
| 组 | **不写 `Group=`** | systemd 按 NSS 解析该账号的主组。写死 `Group=<账号名>` 会在"主组与账号名不同"的机器上撞 `217/GROUP` |
| 提权 | **不要 `NoNewPrivileges=yes`** | 它会让 setuid 的 sudo 无法提权，`sudo -n <自卸载脚本>` 必然失败（`effective uid is not 0`），管理端授权的远程卸载整条链就是死的 |
| 注册单元 | **不写 `Restart=`** | oneshot 的重启策略在各版本 systemd 上行为不一致；"试几次"由 `ExecStart` 里的 shell 循环自己数，顺便把手工恢复的命令打进日志 |
| 限流 | `StartLimitIntervalSec` / `StartLimitBurst` 写在 **`[Unit]`** | 写在 `[Service]` 会被当成**未知键静默忽略** |

`Restart=on-failure` 那条还连着一条：**"反复起不来"的上限交给 `StartLimit*`** ——
真机上见到过重启到 249 次。

## 后果

- 运行时确实会往 journal 里留痕（`StandardError=journal`），但 **stdout 是 `null`**：
  静默是硬要求，Agent 不往终端/日志设备写任何东西。
- `NoNewPrivileges` 不写这一条对"防选手"**没有增量价值**：服务以普通账号运行，
  而**那个账号本人在这台机器上同样能执行任何 setuid 程序**。其余加固一条都没少。
- `StartLimitIntervalSec` 写错位置的表现是"限流完全不生效"，而不是报错 ——
  所以它必须写在 `[Unit]` 里，且这一点值得写下来。

## 被否掉的替代方案与理由

* **`Restart=always`**。见上：它会把"卸载成功"变成"卸载失败"，而那正是最容易被
  当成系统故障的一类假象。崩溃仍然自愈（`on-failure`），没有丢任何东西。
* **写死 `Group=<账号名>`**。"账号名 = 主组名"在大多数机器上成立，而这条加固在
  不成立的机器上会让单元起不来，报的还是 `217/GROUP` 这种指向 NSS 的错。
  交给 NSS 解析没有任何代价。
* **保留 `NoNewPrivileges=yes`（更"安全"的写法）**。它会静默地把远程卸载整条链弄死，
  而失败发生在**考场里点下按钮之后**。用一层没有增量价值的加固换一条会死的功能，
  不划算。
* **给注册单元也写 `Restart=on-failure`**。oneshot 的重启语义跨 systemd 版本不一致，
  而"注册失败重试三次"这件事需要的是**有限重试 + 把手工恢复命令打进日志**，
  那更适合写在 `ExecStart` 的 shell 循环里。
* **`PrivateTmp` / 其他加固一并再加码**。它们已经在单元里；再加码要注意别碰到
  "Agent 要往桌面上写配对码"这件事（例如 `ProtectHome=read-only` 会让家目录整个
  变只读，Agent 一个文件都写不出去）。
