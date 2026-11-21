# ADR-0007：单元里的 `ReadWritePaths` 每条都要带 `-`

**状态**：已锁定（v1，真机故障后补上）。"每条带 `-`"仍然成立；但文中把 `%h` 当成
运行账号家目录是**错的**，见文末「现状（2026-11）」。

## 背景

`syncoj-agent.service` 里 `ProtectSystem=strict` 配了一串 `ReadWritePaths`，其中有一条
写的是 `/home/student/code` —— 而**那台机器上根本没有那个目录**。systemd 在给单元设
沙箱时遇到不存在的路径会直接失败：

```
226/NAMESPACE
```

表现是**服务根本起不来**，而报错里出现的那个路径本身很正常，
第一眼不会想到问题出在"它不存在"。

同一类事情的第二个来源：`deploy_root` / `scan.roots` 是**可选配置**，
真机上可能不存在，而默认值都在 `%h` 之下（已经被覆盖了）。
（**更正**：在系统单元里 `%h` 不是运行账号的家目录，这句前提不成立，见文末。）

## 决定

- **`ReadWritePaths=` 里每一条都带 `-`**：路径不存在时 systemd 跳过它，而不是让单元
  在设沙箱时报 `226/NAMESPACE` 起不来。
- 单元里**刻意不列 `deploy_root` / `scan.roots` 的具体值**。它们默认都在 `%h` 之下，
  多列一条就多一个 226 的机会。
- 只列真正必需的四加三条：
  * 前三条是服务自己要写的地方：家目录（`%h`）、安装根（`/opt/syncoj`）、
    状态目录（`/var/lib/syncoj`）；
    （**更正**：`-%h` 在系统单元里展开成 `-/root`，并没有放开运行账号的家目录。）
  * 后四条只服务于"以 root 执行远程卸载"
    （`/etc/syncoj`、`/etc/systemd/system`、`/etc/sudoers.d`、`/usr/local/lib/syncoj`，
    见 [ADR-0012](ADR-0012-remote-uninstall-authorization.md)）。

## 后果

- 这四条放开**不构成提权**：那些目录是 root:0755/0644，运行账号在 DAC 上写不进去，
  能用上它们的只有"令牌验过签的那个 root 脚本"。
- `agent/tests/test_installer_unit_paths.py` 守着这条 —— 它检查的是"每条路径都带 `-`"
  这个性质，而不是某个具体路径存在与否（后者在开发机上永远为真，那种检查是恒绿的）。
- 代价是单元里看不出"这台机器实际会写哪些目录"；换来的是**换一台机器也起得来**。

## 被否掉的替代方案与理由

* **不加 `-`，让路径必须存在**。那等于要求"配置里出现过的目录都得先在机器上建好"，
  而 `deploy_root` / `scan.roots` 是教师随时会改的现场数据 —— 改一次配置就要记得建
  一次目录，忘一次机器就起不来，而报错（`226/NAMESPACE`）指的方向完全对不上。
* **把 `deploy_root` / `scan.roots` 展开后写进单元**。安装时按当时的配置算一份、
  写进 unit，更"精确"。但它把**运行时配置**和**安装产物**绑在一起：
  教师改完 `agent.ini` 里的一行，还得重装一次 Agent 才能生效，而界面/文档里写的却是
  "改完重启服务即可"。
* **不用沙箱**（省掉 `ProtectSystem=strict` 与 `ReadWritePaths`）。那确实不会有
  226，但会丢掉整套加固 —— 而这些加固是"以选手身份运行的常驻进程"唯一的边界。
* **用 `%h` 覆盖一切，不列具体目录**。`%h` 只管家目录；`/opt/syncoj` 与
  `/var/lib/syncoj` 不在它下面，服务连自己的状态目录都写不了。

## 现状（2026-11）：`%h` 在系统单元里是 `/root`

`syncoj-agent.service` 装在 `/etc/systemd/system/`，由**系统**实例（PID 1）管理。
systemd 的说明书（`systemd.unit(5)`，"Specifiers"）对 `%h` 的定义是：**运行服务管理器
的那个用户**的家目录，系统实例里就是 `/root`，并且明确**不受 `[Service]` 里 `User=`
影响**。所以 `install.py:_writable_paths` 生成的 `ReadWritePaths=-%h …` 在每台机器上
都等于 `-/root`：

* 对运行账号来说这是一条**无效项** —— `/root` 本来就是 root 的 0700 目录，放开挂载层面的
  写权限它也写不进去；
* 本意要放开的**运行账号家目录并不在白名单里**。而 `ProtectSystem=strict` 把除
  `/dev`、`/proc`、`/sys` 之外的整个文件系统挂成只读（不设 `ProtectHome` 不改变这一点），
  按说明书推断，Agent 往桌面写配对码文件、往 `{desktop}` 下落下发文件都会 `EROFS`。

上面"默认值都在 `%h` 之下（已经被覆盖）"的推理因此不成立。这一条**没有经过实机验证**
（systemd 245 实机冒烟是 [`08-milestones-and-open.md`](../design/08-milestones-and-open.md)
§10.5 阶段 2 的待办），也**不是文档能修的**：单元要改成安装时按运行账号解析出的绝对家
目录（仍然带 `-`），`agent/tests/test_installer.py`、`test_installer_unit_paths.py` 里
断言 `-%h` 的用例要一起改。"每条都带 `-`"这个决定本身不受影响。

**已改（2026-11）**：`install.py:_writable_paths` 现在写安装时按运行账号解析出的绝对家目录
（`pwd.getpwnam(<账号>).pw_dir`，查不到按 `/home/<账号>`），例如 `-/home/noi`；单元里不再
出现 `%h`，测试一并改了。仍待 systemd 245 实机冒烟确认。
