# ADR-0008：systemd 单元加固的四处刻意例外

**状态**：已锁定（v1，真机踩坑后补上）。"不要 `NoNewPrivileges=yes`"那一条的目的**实际没有
达到**，见文末「现状（2026-11）」。

## 背景

两个单元：`syncoj-agent.service`（常驻，以**运行账号**跑）与
`syncoj-agent-enroll.service`（oneshot，以 **root** 跑一次）。

常规的 systemd 加固清单套上去之后，单元在真机上出了四种**看起来像 bug、实际都是
"加固清单套错了地方"** 的问题。这四种的原因都不是"少写了一个加固选项"，
而是"某个加固选项与这套东西的语义冲突"。

## 决定

四处**刻意不按常规写**，以及一处容易写错位置的：

| 项 | 刻意怎么做 | 为什么 |
|---|---|---|
| 重启策略 | `Restart=on-failure`，**不用 `always`** | 远程卸载成功时 Agent 以 **exit 0** 正常退出；`always` 会立刻把它重新拉起来 —— 而那时 `/opt/syncoj`、`/etc/syncoj` 都已经删了，拉起来只会刷一堆"找不到文件"，现场看起来像"卸载失败" |
| 组 | **不写 `Group=`** | systemd 按 NSS 解析该账号的主组。写死 `Group=<账号名>` 会在"主组与账号名不同"的机器上撞 `217/GROUP` |
| 提权 | **不要 `NoNewPrivileges=yes`** | 它会让 setuid 的 sudo 无法提权，`sudo -n <自卸载脚本>` 必然失败（`effective uid is not 0`），管理端授权的远程卸载整条链就是死的 |
| 注册单元 | **不写 `Restart=`** | oneshot 的重启策略在各版本 systemd 上行为不一致；"试几次"由 `ExecStart` 里的 shell 循环自己数，并把手工恢复的命令打进日志 |
| 限流 | `StartLimitIntervalSec` / `StartLimitBurst` 写在 **`[Unit]`** | 写在 `[Service]` 会被当成**未知键静默忽略** |

`Restart=on-failure` 那条还连着一条：**"反复起不来"的上限交给 `StartLimit*`** ——
真机上见过重启到 249 次。

它还连着另一条（后补）：**主动退出让 systemd 拉起新版本时，退出码不能是 0**。
`on-failure` 不重启正常退出，而自更新激活 / 启动守卫回滚曾经都以 exit 0 退出 ——
铺开一个版本就让所有机器停在那里。现在用专用退出码 75，并在单元里写
`SuccessExitStatus=75`（不记 failed）与 `RestartForceExitStatus=75`（照样重启）。

## 后果

- 运行时确实会往 journal 里留痕（`StandardError=journal`），但 **stdout 是 `null`**：
  静默是硬要求，Agent 不往终端/日志设备写任何东西。
- `NoNewPrivileges` 不写这一条对"防选手"**没有增量价值**：服务以普通账号运行，
  而**那个账号本人在这台机器上同样能执行任何 setuid 程序**。其余加固一项都没少。
- `StartLimitIntervalSec` 写错位置的表现是"限流完全不生效"，而不是报错 ——
  所以它必须写在 `[Unit]` 里。

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

## 现状（2026-11）

对照 `agent/packaging/install.py:render_unit` 与 `agent/syncoj_agent/main.py`：

| 项 | 与上文一致吗 |
|---|---|
| `Restart=on-failure`、`RestartSec=5`、`StartLimit*` 在 `[Unit]` | 一致 |
| 不写 `Group=` | 一致 |
| 切换版本退出码 75：`main.EXIT_RESTART = 75`，单元里 `SuccessExitStatus=75` + `RestartForceExitStatus=75`，测试对账两边同值 | 一致。但这只解决"退出之后能被拉起"：运行账号写不进 `/opt/syncoj`，真机上自更新会在写 `releases/` 时失败、上报一次 `upgrade_*` 事件后照旧跑旧版本（[`08-milestones-and-open.md`](../design/08-milestones-and-open.md) §10.5 阶段 2） |
| 注册单元不写 `Restart=`，`ExecStart` 里 shell 循环 | 一致（注册单元自己写了 `NoNewPrivileges=yes`，它以 root 跑、不需要 sudo） |
| **不要 `NoNewPrivileges=yes`** | **字面一致、目的落空**。单元里确实没写这一行，但同一单元里的 `RestrictAddressFamilies=`、`RestrictNamespaces=`、`RestrictRealtime=`、`RestrictSUIDSGID=`、`LockPersonality=`、`PrivateDevices=`、`ProtectKernelModules=` 都要装 seccomp 过滤；服务以非 root 的 `User=` 运行（没有 `CAP_SYS_ADMIN`）时，systemd 会为它们**隐式打开 `NoNewPrivileges`**（`systemd.exec(5)` 的 `NoNewPrivileges=` 一节）。于是 setuid 的 sudo 照样提不了权，远程卸载在 systemd 245 上大概率仍然失败 —— 见 [ADR-0012](ADR-0012-remote-uninstall-authorization.md) 的「现状」 |

最后一条是按说明书推断，**没有在 NOI Linux 2.0 实机上验证**（实机冒烟是 §10.5 阶段 2
的待办）。出路不在单元加固上做减法（去掉那一串加固换一条 sudo 不值），而是 §10.5 阶段 2
的 root 管理单元：卸载由 root 单元自己向服务端拉指令执行，Agent 本体随之恢复显式的
`NoNewPrivileges=yes` 与 `Restart=always`，本文的第一、三两条例外届时一起撤掉。
