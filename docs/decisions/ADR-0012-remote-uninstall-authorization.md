# ADR-0012：远程卸载的授权模型

**状态**：已锁定（v1）；2026-11 重写机器侧执行链：由 sudo 自卸载脚本改为 root 管理单元，已并入下文。授权模型（签名令牌）不变。

## 背景

Agent 以选手账号运行（[ADR-0010](ADR-0010-run-user-owns-path-templates.md)），而卸载要删的 `/opt/syncoj`、`/etc/syncoj`（含统一注册密钥）、systemd 单元**都属 root**。"管理端按钮 → 机器清干净"缺的是 root 权限，给谁、怎么给决定了安全性。
机器与按钮之间隔着**不可信链路**（局域网，以及机器上那个账号本人），"服务端说一句卸载吧"谁都能伪造，不能当授权。

## 决定

**授权**：服务端签发一次性令牌。

| 要求 | 做法 |
|---|---|
| 选手**伪造不出来** | 服务端用**发布签名私钥**签令牌，机器用**已有的升级信任锚**验签 |
| **搬不到别的机器** | payload 绑本机 `machine_uuid`（以 root 拥有、选手改不动的那份为准），机器逐字比对 |
| **不会一直有效** | **15 分钟**（`TOKEN_TTL_SECONDS = 900`）；`kind` / `v` 防止被当成别的用途的令牌 |

- 令牌 = `b64url(payload) + "." + b64url(签名)`，签名覆盖**第一段 ASCII 字符串**。
- 真正守住"选手不能自己卸掉监控"的是**令牌只在教师点过之后才存在**。
- 实现：服务端 `server/syncoj_server/services/uninstall.py`。

**执行**：root 管理单元。Agent（选手账号、`NoNewPrivileges=yes`）收到令牌后把 `{"kind": "uninstall", "token": …}` 写进 `<状态目录>/handoff/request.json`（目录 0700、属运行账号）；`syncoj-agent-admin.path`（`PathExists=`）触发 root oneshot `syncoj-agent-admin.service`，执行 `/usr/lib/syncoj/install.py --admin-handoff`：

1. 经 `O_NOFOLLOW` / `dir_fd` 打开交接目录与请求（运行账号可把 `handoff` 换成符号链接），限长、只认普通文件，读到先删请求（读不了的请求也挪走，免得 `PathExists=` 反复触发）；
2. 用安装器里那份独立验签实现验令牌：签名、`kind`/`v`、本机 `machine_uuid`、有效期；
3. 验过走与 `install.py --uninstall` **完全相同**的卸载路径：先 stop 主单元再删（管理单元自己的 service 文件只删不 stop，它正在执行这次卸载），**绝不动**选手桌面、家目录、运行账号、`/tmp`；结果写 `handoff/result.json`（临时名 `O_EXCL|O_NOFOLLOW` + rename），Agent 下一轮读走上报。
4. Agent 转交后退避 300 秒（`UNINSTALL_RETRY_SECONDS`，短于令牌有效期）再试。

同一个管理单元也做**升级切换**：Agent 把验过签的包留在 `<状态目录>/upgrade/`，请求带版本、sha256、大小、签名；root 按声明大小 no-follow 拷进自己的临时目录、**自己**再验 sha256 与签名（签名覆盖版本号与大小），拒绝降级与已回滚过的版本，然后走装机同一条路（`--bundle`）并重启服务。

## 后果

- **待卸载登记表只在内存**：落库等于把一枚 root 删除凭据留在 `syncoj.db`（备份、误传都会带走）；代价是服务端重启后请求消失，教师重点一次。
- **审计不写令牌**（同"审计不写 zip 密码"，[ADR-0011](ADR-0011-zipcrypto-not-aes.md)）。
- **服务端两道当场拒绝**，否则界面成功、机器什么都没发生：服务端没有发布私钥；机器最近一次心跳没报本机有公钥（心跳上报 `release_public_key` 的理由）。两者 `detail` 都指向到机器前跑装机页的 `--uninstall` 命令。
- 不需要 sudoers、`/usr/local/lib/syncoj` 自卸载脚本，Agent 单元不为卸载放开任何系统目录（[ADR-0007](ADR-0007-unit-path-whitelist.md)），并恢复 `NoNewPrivileges=yes`（[ADR-0008](ADR-0008-systemd-unit-hardening.md)）。安装器重跑时会清掉老安装留下的 sudoers 与脚本。
- 令牌会在 0700、属运行账号的目录里落盘到 root 单元取走（秒级）；它绑本机、15 分钟过期、只能在教师点过后存在，所以可以接受。
- systemd 245 容器冒烟（`scripts/smoke_systemd245.py`，CI 每次跑）验证：未配对机器收到令牌 → 交接 → root 验签 → 卸干净，运行账号与家目录不动。

## 被否掉的替代方案

* **sudo 自卸载**（v1）：`/etc/sudoers.d/syncoj-uninstall` 允许运行账号无参执行 `/usr/local/lib/syncoj/self_uninstall.sh`，令牌走 stdin。落空：同单元的 `RestrictAddressFamilies=` / `RestrictNamespaces=` / `PrivateDevices=` 等在非 root `User=` 下让 systemd **隐式打开 `NoNewPrivileges`**，setuid 的 sudo 提不了权；即便能提权，还要为它放开配置 / 单元 / sudoers / 辅助目录四条 `ReadWritePaths`。
* **新增第二套信任链**（卸载专用密钥）：多一把要铺、要轮换、会出错的密钥，而升级信任锚已经在机器上。
* **`nsenter` 进 PID 1 的 mount namespace**：往沙箱开逃逸通道，还依赖 util-linux。
* **令牌长有效期 / 不过期**：它换的是一次 root 删除；15 分钟够教师点完，也短到"事后捡到"无价值。
* **签名覆盖原始 JSON**：两端键序与空白不保证相同，会变成谁也不知道的隐含契约。
* **两种拒绝分两个错误码**：前端多一个无分支价值的分支，两处下一步是同一件事。
* **卸载时连选手桌面一起清**：那里有学生代码，卸载 Agent 不该碰。
