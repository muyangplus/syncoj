# ADR-0012：远程卸载的授权模型

**状态**：已锁定（v1）；**机器侧执行链 2026-11 重写**为 root 管理单元，见文末「重写（2026-11）」。
授权模型（签名令牌）不变。中间那段 sudo 执行链与「现状（2026-11）」是被替换掉的历史。

## 背景

**起点是权限**：Agent 以选手账号运行（见 [ADR-0010](ADR-0010-run-user-owns-path-templates.md)），
而卸载要删的 `/opt/syncoj`、`/etc/syncoj`（里面有统一注册密钥）、systemd 单元
**全是 root 的**。所以"管理端按钮 → Agent 自己删干净"这条链缺一件东西：root 权限。
给谁、怎么给，决定了这套东西安不安全。

机器与按钮之间隔着一条**不可信链路**：局域网，以及机器上那个账号本人。
所以"服务端说一句卸载吧"不能是授权 —— 那句话在这条链路上谁都能伪造。

## 决定

授权用**服务端签发的一次性令牌**，满足三条：

| 要求 | 做法 |
|---|---|
| 选手**伪造不出来** | 服务端用**发布签名私钥**签一枚令牌，机器用**已有的升级信任锚**验签 |
| **搬不到别的机器上** | payload 里绑本机 `machine_uuid`（以 root 拥有、选手改不动的那份为准），机器本地逐字比对 |
| **不会一直有效** | **15 分钟**有效期；`kind` / `v` 让它不会被当成别的用途的令牌 |

令牌是 `b64url(payload) + "." + b64url(签名)`，签名覆盖**第一段那串 ASCII 字符**
而不是原始 JSON（键序与空白在两端不保证相同）。

**真正守住"选手不能自己卸掉监控"的不是签名，而是"令牌只在教师点过之后才存在"。**

机器侧的执行链，每一步都有理由：

1. `/etc/sudoers.d/syncoj-uninstall` **只允许一条命令、且没有参数**
   （`<运行账号> ALL=(root) NOPASSWD: <固定路径>`）。写之前先 `visudo -c -f` 校验 ——
   半个 sudoers 文件会让 sudo 整体不可用，比装不上严重得多。
2. 脚本放在 `/usr/local/lib/syncoj/` 这种**固定路径**，**不能放在 `current` 符号链接
   下** —— sudo 的路径匹配不解析符号链接，放链接下等于白名单永远匹配不上。
3. 令牌**只走 stdin**：不进 argv（`ps` 里看得见）、不写文件、不进日志。
   验签实现**独立、零依赖、不 import `syncoj_agent`** —— 脚本执行时那个包可能正在被删。
4. 删除顺序：`systemctl disable`（**不用 `--now`**：脚本自己就跑在那个单元的 cgroup
   里；现状是 best-effort —— 沙箱挡掉了 AF_UNIX，多半连不上 D-Bus，靠下一步直接删）→ 删单元文件与 `*.wants/` 软链 → 删 prefix/config/state → 删 sudoers 与 helper
   → 逐项打印、**幂等**。**绝不动**选手桌面、家目录、运行账号、`/tmp`。
5. 成功后 Agent **exit 0**，所以主单元必须是 `Restart=on-failure`
   （见 [ADR-0008](ADR-0008-systemd-unit-hardening.md)）。

## 后果

- **待卸载登记表只在内存里。** 落库等于把一枚能在某台机器上换一次 root 删除的凭据
  留在 `syncoj.db` 里，而备份、误传、随手拷走的库文件都会带上它。代价是服务端重启后
  这次请求消失，教师重点一次即可。
- **审计里不写令牌**（与"审计不写 zip 密码"同一条纪律，见
  [ADR-0011](ADR-0011-zipcrypto-not-aes.md)）。
- 服务端有**两道拒绝**，都必须当场拒绝而不是"下发一个机器注定拒收的授权"
  （否则界面上显示成功、机器上什么都没发生）：服务端没有配置发布私钥；
  机器最近一次心跳没有报告本机有公钥。两者的 `detail` 都指向同一个替代办法 ——
  站到机器前面跑装机页那条 `--uninstall` 命令，因为两处的**下一步是同一件事**。
  这条也是"心跳要上报 `release_public_key`"的全部理由。
- **为此必须放开四处沙箱**：`ProtectSystem=strict` 的只读挂载会被 sudo 出来的子进程
  继承，即使成了 root 也会 `EROFS`。所以 `ReadWritePaths` 增加 config 目录、单元目录、
  `/etc/sudoers.d`、helper 目录，且**每条都带 `-`**
  （见 [ADR-0007](ADR-0007-unit-path-whitelist.md)）。这四处放开**不构成提权**：
  那些目录是 root:0755/0644，运行账号在 DAC 上写不进去，能用上它们的只有
  "令牌验过签的那个 root 脚本"。

## 被否掉的替代方案与理由

* **新增第二套信任链**（为卸载单独发一对密钥）。多一把密钥就多一个要铺、要轮换、
  要出错的东西；而升级信任锚**已经在机器上**了（装机时随包带走）。
* **`nsenter` 进 PID 1 的 mount namespace** 再删。那是往沙箱里开一条逃逸通道，
  还依赖 util-linux 的在场。
* **`PathExists=` 触发的独立 root 单元**。它要求令牌**落一次盘** ——
  而"令牌不落盘"正是上面那条"只在内存里"的一部分。
* **令牌长有效期 / 不过期**。它换的是一次 root 删除；15 分钟足够教师点完那一下，
  也短到"事后捡到"没有价值。
* **签名覆盖原始 JSON**。键序与空白在两端不保证相同，签原文等于把"两边编码一模一样"
  变成一条谁也不知道的隐含契约。签第一段那串 ASCII 字符，两端的编码差异就不涉及了。
* **两个错误码分开报**（"服务端没私钥" vs "机器没公钥"）。前端会多一个没有分支价值
  的分支，而两处该做的下一步**是同一件事**：走到机器前面跑那条命令。
* **删除时连选手桌面一起清**。那里面有学生的代码；卸载 Agent 不该碰它。
  脚本结尾逐条说明删了什么、没删什么。

## 现状（2026-11）

令牌的签发、绑定与验签都按上文实现（`server/syncoj_server/services/uninstall.py`，
`TOKEN_TTL_SECONDS = 900`）。**断在机器侧第 1 步之前**：

* Agent 执行的是 `sudo -n /usr/local/lib/syncoj/self_uninstall.sh`（`main.py`
  `_handle_uninstall_token`）。上文假设"单元里不写 `NoNewPrivileges=yes`，sudo 就能提权"，
  但同一单元里的 `RestrictAddressFamilies=` / `RestrictNamespaces=` / `PrivateDevices=` 等
  在非 root 的 `User=` 下会让 systemd **隐式打开 `NoNewPrivileges`**（见
  [ADR-0008](ADR-0008-systemd-unit-hardening.md) 的「现状」）。setuid 位因此失效，sudo
  报 `effective uid is not 0` 一类错误，脚本根本跑不起来。
* 机器侧的表现：记一条 `uninstall_failed`、`last_error` 写明原因，**一小时后**
  （`UNINSTALL_RETRY_SECONDS = 3600`）再试 —— 令牌那时早已过期（15 分钟）。
  教师在界面上看到的是"已记下这台机器的卸载请求……届时自己清干净"，机器什么都没删。
* 这是按 systemd 说明书推断的结论，**没有在 NOI Linux 2.0 实机上验证过**。在验证或
  重写之前，可靠的卸载办法只有站到机器前面跑装机页那条
  `bootstrap.sh … --uninstall --yes`（[`../install-agent.md`](../install-agent.md)「卸载 Agent」）。

重写方向在 [`08-milestones-and-open.md`](../design/08-milestones-and-open.md) §10.5 阶段 2：
root 管理单元（`syncoj-agent-admin`）用 timer 自己向服务端拉卸载指令，不再经过 sudo；
届时删掉 sudoers 白名单、`/usr/local/lib/syncoj` 与为它放开的四条 `ReadWritePaths`。

## 重写（2026-11）：root 管理单元

机器侧不再经过 sudo。Agent（选手账号、`NoNewPrivileges=yes`）收到令牌后，把
`{"kind": "uninstall", "token": …}` 写进 `<状态目录>/handoff/request.json`（目录 0700、属
运行账号），`syncoj-agent-admin.path`（`PathExists=`）触发 root oneshot
`syncoj-agent-admin.service`，执行 `/usr/lib/syncoj/install.py --admin-handoff`：

1. 经 `O_NOFOLLOW` / `dir_fd` 打开交接目录与请求（运行账号能把 `handoff` 换成指向别处的
   符号链接，root 不顺着它读写），限长、只认普通文件，读到就先删请求；
2. 用与服务端签发对应的同一段验签代码（安装器里那份独立实现）验令牌：签名、`kind`/`v`、
   绑本机 `machine_uuid`、15 分钟有效期；
3. 验过就走与 `install.py --uninstall` **完全相同**的卸载路径（管理单元自己的 service
   文件只删不 stop —— 它正在执行这次卸载）；结果写进 `handoff/result.json`（临时名
   `O_EXCL|O_NOFOLLOW` + rename），Agent 下一轮读走上报。

**与上文的取舍变化**：上文否掉"`PathExists=` 触发的 root 单元"，理由是令牌要**落一次盘**。
现在接受它：令牌只在 0700、属运行账号的目录里待到 root 单元取走（秒级），而它本来就绑
本机、15 分钟过期、只能在教师点过之后存在 —— 真正守住"选手不能自己卸掉监控"的一直是后
一条（见上文），不是"不落盘"。换来的是：没有 sudoers 白名单、没有 `/usr/local/lib/syncoj`
自卸载脚本、Agent 单元不再为它放开配置 / 单元 / sudoers / 辅助目录四条 `ReadWritePaths`，
并恢复 `NoNewPrivileges=yes`。Agent 转交之后退避 300 秒（短于令牌有效期；曾经是 3600 秒）。

同一个管理单元也负责**升级切换**：Agent 把验过签的包留在 `<状态目录>/upgrade/`，交接请求
带版本、sha256、大小、签名；root 那边按声明大小 no-follow 拷进自己的临时目录、**自己**
再验 sha256 与签名（签名覆盖版本号与大小），然后走装机同一条路（`--bundle`）并重启服务。

**仍未在 systemd 245 实机上验证**（§10.5 阶段 2 的实机冒烟）。
