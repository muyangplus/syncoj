## 6. 安全

| 面 | 措施 |
|---|---|
| 传输 | **明文 HTTP，只适用于可信二层网络**（见本文 §6.2）。服务端不终结 TLS；Agent 能连 `https://`（标准 CA 校验 + `ca_file`），无证书 pinning |
| 认证 | 服务端只存 `sha256(token)`；按机吊销；机器身份靠 `machine_uuid`（首选）+ 硬件指纹（兜底） |
| 配对 | 六位数字配对码只存哈希、限时 30 分钟、一次性、需管理员鉴权；认领机器 = 决定"谁的成绩算谁的" |
| 授权 | token 只能访问本人场次与资源；下载的 asset 必须属于自己且被 `deploy_target` 引用 |
| 路径 | 服务端**独立**做 `resolve()` + 白名单校验，绝不信任 Agent 上报的相对路径 |
| 完整性 | 上传/下载双向 SHA256（防传坏，不防中间人）；升级包另有 RSA 签名（本文 §6.1）。**自举链没有签名保护**（本文 §6.2） |
| 本地密钥 | 开发机放 `.key/`（两道 gitignore + 0600/0700），**有则自动加载、无则升级整体关闭**；生产用 `SYNCOJ_KEY_DIR` / `SYNCOJ_RELEASE_KEY` 指到服务端账号专属目录（../design/07-ops-and-install.md §7.1） |
| 隔离 | 服务端专用用户；`source/` 对评测器只读；Agent 以选手账号 + `ProtectSystem=strict` 等沙箱运行，`NoNewPrivileges=yes`；需要 root 的升级切换与卸载由 root 管理单元验签后执行（ADR-0012） |
| 审计 | 登录、注册、下发、回收、文件消失、批量重写、升级全进 `event_log` |

⚠️ **路径校验必须在服务端独立实现**：Agent 跑在选手机器上，随时可能被替换。

⚠️ **签名与哈希保护的是"包本身"，不是链路**。链路明文，部署前提与已知缺口见 §6.2。

### 6.1 为什么签名不是 Ed25519（方案修正）

原定 Ed25519，**硬阻塞**：Python 3.8 标准库没有任何非对称签名原语（`hashlib` 只有摘要），用 Ed25519 就得引入 `cryptography`/`pynacl`，违背零依赖。改用 **RSA-2048 + PKCS#1 v1.5 (SHA-256)**：

| | Ed25519 | RSA-2048 |
|---|---|---|
| 纯 Python 验证的实现量 | 完整 Curve25519 点运算，数百行大整数运算 | 一次 `pow(sig, 65537, n)` + 字节比较，几十行 |
| 手写风险 | 高 | 低（公钥指数只有 17 位，不涉及曲线算术） |
| 签名体积 | 64 字节 | 256 字节 |

- **签名对象是规范化文本** `syncoj-release-v1\n<版本>\n<sha256>\n<字节数>\n`，不是包字节。安全性等价（PKCS#1 本就先哈希再签），验证只需流式算摘要，内存不随包大小增长（否则 256MB 的包超出 Agent 200MB 预算）。**版本号与大小在签名里**，否则篡改的 tick 响应能把合法旧包标成"新版本"（降级）。服务端、Agent、安装器三份独立实现，`test_signing` 对账；服务端启动时按此格式重签库里已有的发布记录。
- **严格校验**：整体重建 `EM` 后恒定时间比较，不做"解析出 hash 再比对"；PKCS#1 v1.5 历史上的伪造攻击（Bleichenbacher 2006 等）全部源于宽松校验。
- **密钥生成调用 `openssl`**：Miller-Rabin 与随机素数搜索易写错，而生成是一次性管理操作，依赖 openssl 可接受。
- **正确性证据来自 openssl 交叉验证**：签名与 openssl 逐字节相同，openssl 的签名本代码能验；自签自验恒过，不算证据。

### 6.2 信任边界的现状（决策 3：如实写明文）

**部署前提：服务端与考试机在同一个可信二层网络里**（考场交换机，外人设备接不进来），且**没有人在二层上监听或冒充服务端**。
后半句不是技术保证：[`SECURITY.md`](../../SECURITY.md) 的威胁模型假设选手对自己的机器有 root，有 root 就能抓包、做 ARP 欺骗。
前提不成立时下面三条都是现成攻击面；这是现状不是设计目标，改进项见 [`08-milestones-and-open.md`](08-milestones-and-open.md) §10.5（"TLS + pinning"列为推迟）。

**1. 传输是明文 HTTP。** `syncoj-server serve` 只开 HTTP（无 TLS 参数），装机页、自举命令、文档示例全是 `http://`。
过网明文包括：机器 token 与统一注册密钥（注册时）、六位配对码、上传的选手代码、下发的题面与 `password.txt`、管理端登录口令与会话。
Agent 支持 `https://` + `ca_file`（`verify_tls`，标准 CA 校验，非 pinning），要用只能自己在服务端前架 TLS 反向代理；仓库没有现成配置，也没有测试覆盖。

**2. 自举链对中间人没有保护。**

* `curl http://…/bootstrap.sh | sudo sh` 以 root 执行明文下载的脚本，脚本再明文下载 `install.py`（`/api/v1/agent/install/installer`）并以 root 执行，**两步都不验签**。脚本嵌着安装器的 sha256，但与脚本同源，只挡传坏；`RELEASE_PUBLIC_KEY` / `--public-key` 只能验后面的安装包，验不到安装器本身。
* 没有预置发布公钥时，安装包只对照 sha256，而台账与包同源且不鉴权（[`../install-agent.md`](../install-agent.md) 已写明）。
* 勾了「附带统一注册密钥」的包**不经**免鉴权装机入口下发（409 `install_bundle_has_key`），只能管理端登录后下载做成离线包；离线包拷出去就收不回来。
* 卸载用同一条 `bootstrap.sh`；脚本里的服务端地址由服务端发脚本时嵌入，不读机器上属运行账号的 `agent.ini`（否则改掉地址就能让下次 `sudo sh` 去别处取"安装器"）。

**3. 发布签名私钥常驻在线服务端，等于整个机群的 root 钥匙。** `release-key.pem`（`.key/` 或 `SYNCOJ_RELEASE_KEY`）由 `serve` 常驻加载，用它签：

* **Agent 发布**：「上传升级包」接受任意 tar.gz 并当场签名；铺开后 `install.py --from-server` / 自举脚本装的就是它，`upgrade.mode=apply` 的机器经 root 管理单元自更新到它。注册单元 `syncoj-agent-enroll.service` **每次开机**以 **root** 执行 `current/` 下的 `run_agent.py` —— 签过名的代码最终以 root 跑在每台考试机上。
* **远程卸载令牌**（[ADR-0012](../decisions/ADR-0012-remote-uninstall-authorization.md)）；
* **局域网发现应答**（§7.3）与**通告挑战应答**（`whoami`）。

所以拿到服务端主机、`.key/` 目录或**任意一个管理员账号**（口令走明文 HTTP）就等于拿到全部考试机。私钥离线保存、签名与在线服务分离属于 [`08-milestones-and-open.md`](08-milestones-and-open.md) §10.5 之外的未排期工作；§10.5 里"签名覆盖版本号"（已完成）、"注册单元不再以 root 执行发布目录里的代码"（还没做）只缩小其中一部分。
