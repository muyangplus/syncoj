# ADR-0007：单元里的 `ReadWritePaths` 每条都要带 `-`

**状态**：已锁定（v1，真机故障后补上）；2026-11 修订：家目录由 `%h` 改为安装时解析的绝对路径，远程卸载用的四条系统目录撤掉。

## 背景

`syncoj-agent.service` 用 `ProtectSystem=strict` 配一串 `ReadWritePaths`，其中一条 `/home/student/code` 在那台机器上不存在。systemd 设沙箱时遇到不存在的路径直接失败（`226/NAMESPACE`），服务起不来，而报错里的路径看着很正常，不会想到是"它不存在"。
`deploy_root` / `scan.roots` 是可选配置，同样可能不存在。

另外，`ProtectSystem=strict` 把除 `/dev`、`/proc`、`/sys` 外的整个文件系统挂成只读（不设 `ProtectHome` 也一样），Agent 要写的目录必须显式放开。

## 决定

- **`ReadWritePaths=` 每条都带 `-`**：路径不存在时跳过，而不是 226 起不来。
- **只列三条**：
  * 运行账号家目录 —— **安装时解析的绝对路径**（`pwd.getpwnam(<账号>).pw_dir`，查不到按 `/home/<账号>`），如 `-/home/noi`；桌面配对码、下发落点都在其下。
  * 安装根（`/opt/syncoj`）—— 自更新写 `releases/`。
  * 状态目录（`/var/lib/syncoj`）—— 日志、凭据、哈希缓存。
- **不列 `deploy_root` / `scan.roots` 的具体值**：默认值（`{desktop}` / `{desktop}/{player_no}`）都在家目录下，多列一条就多一次 226 的机会。
- 实现在 `install.py:_writable_paths`；去重并丢掉 `/`（只可能来自 `--config-dir /` 之类误配）。

## 后果

- `agent/tests/test_installer_unit_paths.py` 守"每条都带 `-`"这一**性质**，而不是某个路径存在（那在开发机上恒真）。
- 单元里看不出机器实际会写哪些子目录，换来**换一台机器也起得来**。
- systemd 245 容器冒烟（`scripts/smoke_systemd245.py`，CI 每次跑）验证家目录可写、配对码由服务写出。

## 被否掉的替代方案

* **不加 `-`，要求路径必须存在**：`deploy_root` / `scan.roots` 是教师随时会改的现场数据，忘建一次目录机器就起不来，且 226 指向完全不对。
* **把 `deploy_root` / `scan.roots` 展开写进单元**：把运行时配置与安装产物绑死，改一行 `agent.ini` 还得重装才生效。
* **不用沙箱**：没有 226，但丢掉以选手身份运行的常驻进程唯一的边界。
* **用 `%h` 表示家目录**（v1 写法）：系统单元里 `%h` 是服务管理器（root）的家目录 `/root`，**不受 `User=` 影响**（`systemd.unit(5)` Specifiers），等于没放开选手桌面，写配对码与下发文件会 `EROFS`。
* **只用家目录覆盖一切**：`/opt/syncoj` 与 `/var/lib/syncoj` 不在家目录下，服务连自己的状态目录都写不了。
* **为远程卸载再放开 `/etc/syncoj`、`/etc/systemd/system`、`/etc/sudoers.d`、`/usr/local/lib/syncoj`**（v1 做法）：那是让 Agent 经 sudo 自卸载用的；卸载改由 root 管理单元执行后（[ADR-0012](ADR-0012-remote-uninstall-authorization.md)）不再需要，多放一条就多一分风险。
