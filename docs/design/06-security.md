## 6. 安全

| 面 | 措施 |
|---|---|
| 传输 | **明文 HTTP，只适用于可信的二层网络**（见本文 §6.2）。服务端不终结 TLS；Agent 能连 `https://`（标准 CA 校验 + `ca_file`），但没有证书 pinning |
| 认证 | 服务端只存 `sha256(token)`；按机吊销；机器身份靠 `machine_uuid`（首选）+ 硬件指纹（兜底） |
| 配对 | 六位数字配对码只存哈希、限时 30 分钟、一次性、需管理员鉴权。知道码就能认领一台机器，而认领一台机器就是决定"谁的成绩算谁的" |
| 授权 | token 只能访问本人场次与资源；下载 asset 必须属于自己且被 `deploy_target` 引用 |
| 路径 | 服务端**独立**做 `resolve()` + 白名单校验，绝不信任 Agent 上报的相对路径 |
| 完整性 | 上传/下载双向 SHA256（防传坏，不防中间人）；升级包额外 RSA 签名（见本文 §6.1）。**自举链没有签名保护**（见本文 §6.2） |
| 本地密钥 | 开发机放 `.key/`（两道 gitignore + 0600/0700），**有则自动加载、无则升级整体关闭**；生产用 `SYNCOJ_KEY_DIR` / `SYNCOJ_RELEASE_KEY` 指到服务端账号专属目录（见 ../design/07-ops-and-install.md §7.1） |
| 隔离 | 服务端专用用户；`source/` 对评测器只读；Agent 以选手账号运行 + `ProtectSystem=strict` 等沙箱。单元里**没写** `NoNewPrivileges=`，但非 root 服务上的 `RestrictAddressFamilies=` / `RestrictNamespaces=` / `PrivateDevices=` 等会让 systemd 隐式打开它（见 [ADR-0008](../decisions/ADR-0008-systemd-unit-hardening.md) 的「现状」） |
| 审计 | 登录、注册、下发、回收、文件消失、批量重写、升级全进 `event_log` |

⚠️ **路径校验必须在服务端独立实现一遍** —— Agent 跑在选手机器上，随时可能被替换。

⚠️ **这张表里的签名与哈希保护的是"包本身"，不是链路。** 链路是明文的，部署前提与三处
已知缺口写在 §6.2。

### 6.1 为什么签名不是 Ed25519（方案修正）

本方案原定 Ed25519。**硬阻塞：Python 3.8 标准库不含任何非对称签名原语**（`hashlib`
只有摘要，没有椭圆曲线），而 Agent 必须零依赖。要用 Ed25519 就得引入
`cryptography`/`pynacl`，直接违背零依赖约束。

改用 **RSA-2048 + PKCS#1 v1.5 (SHA-256)**：

| | Ed25519 | RSA-2048 |
|---|---|---|
| 纯 Python 验证的实现量 | 需要完整的 Curve25519 点运算，数百行大整数运算 | 一次 `pow(sig, 65537, n)` + 字节比较，几十行 |
| 手写风险 | 高 | 低（公钥指数只有 17 位，且不涉及曲线算术） |
| 签名体积 | 64 字节 | 256 字节 |

密钥与签名的具体约定：

- **签名对象是一段规范化文本** `syncoj-release-v1\n<版本>\n<sha256>\n<字节数>\n`，
  不是包本身的字节。安全性等价（PKCS#1 内部本来也是先哈希再签名），验证只需流式算一遍
  摘要：内存占用不随包大小增长，否则 256MB 的包会超出 Agent 的 200MB 预算。**版本号与
  大小在签名里**：曾经只签 sha256，篡改的 tick 响应能把合法的旧包标成"新版本"（降级）。
  服务端、Agent、安装器三份独立实现，`test_signing` 对账；服务端启动时按新格式重签库里
  已有的发布记录。
- **严格校验**：整体重建 `EM` 后恒定时间比较，不做"解析出 hash 再比对"。
  PKCS#1 v1.5 历史上的伪造攻击（Bleichenbacher 2006 等）**全部**源于宽松校验。
- 密钥生成 shell 到 `openssl`：Miller-Rabin 与随机素数搜索是好写但极易写错的
  密码学代码，而这是一次性管理操作，依赖 openssl 完全可接受。
- 正确性证据来自 **openssl 交叉验证**：签名与 openssl 逐字节相同，
  openssl 的签名本代码能验。自签自验恒过，不能作为正确性证据。

### 6.2 信任边界的现状（决策 3：如实写明文）

**部署前提：服务端与考试机在同一个可信的二层网络里**（考场交换机，没有外人设备能接入），
并且**没有人在这个二层上监听或冒充服务端**。注意后半句不是技术保证：
[`SECURITY.md`](../../SECURITY.md) 的威胁模型假设选手对自己的机器有 root，而有 root
就能抓包、做 ARP 欺骗。前提不成立时，下面三条都是现成的攻击面 —— 这是现状，
不是设计目标；改进项在
[`08-milestones-and-open.md`](08-milestones-and-open.md) §10.5（"TLS + pinning"列为推迟）。

**1. 传输是明文 HTTP。** `syncoj-server serve` 只开 HTTP（没有任何 TLS 参数），装机页、
自举命令、文档示例全是 `http://`。过网的明文包括：机器 token 与统一注册密钥（注册时）、
六位配对码、上传的选手代码、下发的题面与 `password.txt`、管理端的登录口令与会话。
能在二层上监听或冒充服务端的人可以读走、篡改其中任何一样。Agent 的 HTTP 客户端支持
`https://` + `ca_file`（`verify_tls`，标准 CA 校验，不是指纹 pinning），要用只能自己
在服务端前面架 TLS 反向代理 —— 仓库里没有现成配置，也没有测试覆盖这条路。

**2. 自举链对中间人没有任何保护。**

* `curl http://…/bootstrap.sh | sudo sh` 以 root 执行一段明文下载的脚本；它再明文下载
  `install.py`（`/api/v1/agent/install/installer`）并以 root 执行。**这两步都不验签**，
  `RELEASE_PUBLIC_KEY` / `--public-key` 只能验后面那个安装包，验不到安装器本身。
* 没有预置发布公钥时，安装包只对照 sha256 —— 而台账与包来自同一个不鉴权的来源，
  挡得住传坏，挡不住中间人（[`../install-agent.md`](../install-agent.md) 里已写明）。
* 勾了「附带统一注册密钥」的包经免鉴权的 `/api/v1/agent/install/bundle` 下发，公开端口
  也可达：拿到包的人就拿到一张能注册进这台服务端的通行证。
* 卸载用的是同一条 `bootstrap.sh`，服务端地址读自机器上的 `agent.ini` —— 而这份文件
  属主是运行账号（选手），改掉地址就能让下一次 `sudo sh` 去别处取"安装器"。

**3. 发布签名私钥常驻在线服务端，等于整个机群的 root 钥匙。** `release-key.pem`
（`.key/` 或 `SYNCOJ_RELEASE_KEY`）由 `serve` 进程常驻加载，用它签：

* **Agent 发布**：管理端「上传升级包」接受任意 tar.gz，服务端当场签名；铺开之后，
  `install.py --from-server` / 自举脚本装的就是它，`upgrade.mode=apply` 的机器也会
  自更新到它。开机注册单元 `syncoj-agent-enroll.service` **每次开机**以 **root** 执行
  `current/` 下的 `run_agent.py` —— 签过名的代码最终以 root 跑在每台考试机上。
  （自更新眼下在真机上会失败：运行账号写不进 `/opt/syncoj`，见 §10.5 阶段 2。那是缺陷，
  不是防护：重装照样装上，自更新修好之后这条路也就通了。）
* **远程卸载令牌**（[ADR-0012](../decisions/ADR-0012-remote-uninstall-authorization.md)）；
* **局域网发现应答**（§7.3）。

所以：拿到服务端主机、`.key/` 目录，或者**任意一个管理员账号**（口令走的又是明文
HTTP），就等于拿到全部考试机。私钥离线保存、签名与在线服务分离属于
[`08-milestones-and-open.md`](08-milestones-and-open.md) §10.5 之外的未排期工作；
§10.5 阶段 2 里"签名覆盖版本号""注册单元不再以 root 执行发布目录里的代码"只缩小
其中一部分。
