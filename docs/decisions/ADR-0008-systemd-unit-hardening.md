# ADR-0008：systemd 单元加固的四处刻意例外

**状态**：已锁定（v1，真机踩坑后补上）；2026-11 修订：root 管理单元落地后，"`Restart=on-failure`"与"不要 `NoNewPrivileges=yes`"两条例外撤销，已并入下文。

## 背景

两个单元：`syncoj-agent.service`（常驻，以**运行账号**跑）与 `syncoj-agent-enroll.service`（oneshot，以 **root** 跑一次）。
常规 systemd 加固清单套上去后，真机上出过几种看似 bug、实为"某个加固选项与这套语义冲突"的问题。

## 决定

| 项 | 怎么做 | 为什么 |
|---|---|---|
| 组 | **不写 `Group=`** | 交给 NSS 解析主组；写死 `Group=<账号名>` 在主组与账号名不同的机器上撞 `217/GROUP` |
| 注册单元 | **不写 `Restart=`** | oneshot 的重启语义在各版本 systemd 上不一致；有限重试由 `ExecStart` 的 shell 循环自己数，并把手工恢复命令打进日志 |
| 限流 | `StartLimitIntervalSec` / `StartLimitBurst` 写在 **`[Unit]`** | 写在 `[Service]` 会被当未知键**静默忽略**，限流完全不生效 |
| 切换版本退出码 | 专用退出码 **75**；`SuccessExitStatus=75` + `RestartForceExitStatus=75` | 自更新激活 / 启动守卫回滚后要让 systemd 拉起新代码；exit 0 在 `on-failure` 单元里不重启，铺开一个版本就让机器停摆。`main.EXIT_RESTART` 与 `install.py:AGENT_EXIT_RESTART` 测试对账 |
| 重启策略 | `Restart=always`，`RestartSec=5` | 卸载时 root 管理单元先 stop 再删，Agent 不会以 0 退出后被拉回已删的文件；意外的 0 退出也该拉起。上限交给 `StartLimit*` |
| 提权 | **`NoNewPrivileges=yes`** | Agent 不再需要 sudo：升级切换与卸载由 root 管理单元执行（[ADR-0012](ADR-0012-remote-uninstall-authorization.md)） |
| 启动守卫 | `ExecStartPre=-+…install.py --boot-guard` | 回滚要写属 root 的 `/opt/syncoj`，必须 root（`+`）；守卫失败不挡启动（`-`） |

## 后果

- stderr 进 journal，**stdout 为 `null`**：静默是硬要求。
- 不写 `Group=` 没有任何代价。
- 不设 `ProtectHome`：Agent 要读写自己的家目录（桌面配对码、下发文件），`ProtectHome=read-only` 会让它一个文件都写不出。
- systemd 245 容器冒烟（`scripts/smoke_systemd245.py`，CI 每次跑）验证 `NoNewPrivileges=yes` 下服务正常运行、升级经 root 管理单元切换、一启动就崩的版本被守卫切回。

## 被否掉的替代方案

* **`Restart=on-failure`**（v1）：当年远程卸载成功时 Agent 以 exit 0 退出，`always` 会把它拉回已删的文件、看起来像"卸载失败"；卸载改由 root 单元先 stop 再删后理由消失，而 `on-failure` 会漏掉意外的 0 退出。
* **不写 `NoNewPrivileges=yes`**（v1，为让 Agent `sudo -n <自卸载脚本>`）：目的本就落空 —— 同单元的 `RestrictAddressFamilies=`、`RestrictNamespaces=`、`RestrictSUIDSGID=`、`PrivateDevices=` 等需要 seccomp 过滤，非 root 的 `User=` 下 systemd 会**隐式打开** `NoNewPrivileges`（`systemd.exec(5)`），setuid 的 sudo 照样提不了权；去掉那一串加固换一条 sudo 不值。
* **写死 `Group=<账号名>`**：多数机器上成立，不成立时单元起不来，且 `217/GROUP` 指向 NSS 很难查。
* **注册单元写 `Restart=on-failure`**：跨版本语义不一致，且做不到"有限重试 + 打出手工恢复命令"。
* **退出码用 0 让 systemd 重启**：`on-failure` 不重启正常退出，旧单元下铺开一个版本就让整个机房停摆。
* **再加 `ProtectHome=read-only` 等加码**：家目录整个只读，Agent 写不出配对码。
