## 7. 运维与装机

决定：安装器**幂等、多入口、同一份逻辑**。只有一个 `agent/packaging/install.py`（无 `install.sh`、无 `--mode`），入口由安装包来源决定：

```bash
python3 install.py --from-server --server http://<服务端>   # 从服务端拿已铺开的版本
python3 install.py --bundle <tar.gz> --sha256 <校验和>       # 离线包 / 镜像预装
python3 install.py --download-url <URL> --sha256 <校验和>    # 任意 URL
python3 install.py --from-dir <已解开的 Agent 目录>
curl … /api/v1/agent/install/bootstrap.sh | sudo sh -s -- …  # 在线自举：取回 install.py 再 --from-server
python3 install.py --uninstall [--yes] [--dry-run]           # 卸载
```

完整参数与步骤见 [`../install-agent.md`](../install-agent.md)。

- 已装则原地升级，未装则全量安装；重复执行结果一致。
- 凭据丢失自动重新 enroll，配对关系不受影响（服务端按 `machine_uuid` / 硬件指纹认回，不依赖机器上的文件）。
- 离线包 = tar.gz（`syncoj_agent/` + `run_agent.py` 启动器，**不含安装器**；systemd 单元与 `agent.ini` 由安装器现场生成），不需要 pip 与网络。
- 在线自举 = `bootstrap.sh` 取回 `install.py`，再拉 tar.gz、对照台账校验 sha256，走同一条路径。

装机硬规则（有回归测试）：

1. **`bootstrap.key` 不写进 `agent.ini`**：后者 chown 给选手账号。
2. **写文件显式用 LF**：Windows 上 `Path.write_text()` 写出 `\r\n`，systemd 解析 unit 失败且报错不指向行尾符。
3. **写完 chown 给运行账号**：否则 Agent 读不到自己的配置，只报"权限不足"。

### 7.1 密钥从哪来：`.key/`

`syncoj-server init` 建好两把密钥（无需手工 genkey、指定私钥、拷公钥进包）：

```
.key/                              ← 默认密钥目录（整个目录不进版本库）
├── release-key.pem                发布签名私钥  0600
├── release-key.pub.json           对应的公钥（装进离线包/镜像）
└── bootstrap.key                  统一注册密钥  0600（哈希同时入 bootstrap_key 表）
```

| 环节 | 行为 |
|---|---|
| `syncoj-server init` | 缺哪把建哪把，**已有的绝不覆盖、绝不轮换**；打印找到/创建了什么 |
| 服务端加载私钥 | `.key/release-key.pem` **存在**就用；`SYNCOJ_RELEASE_KEY` 覆盖一切 |
| 打包 Agent | `.key/release-key.pub.json` 存在就内嵌进包，并让机器配置指向它 |
| 统一注册密钥 | 装机时从 `.key/bootstrap.key` 带进镜像；`init` 已把哈希写进库，开箱可用 |

- **门槛是"文件存在"**：有则生效，无则保持原样（无私钥不提供升级，无公钥打不带公钥的包）。否决"缺失即启动失败"：干净的 `git clone` 会起不来；"升级整体关闭"是安全可用状态。
- **注册密钥看"哈希是否已在库里登记"，不看文件**：`db reset` 只删库、不动 `.key/`，会出现"文件在、哈希没了"（注册报「密钥无效」）；`init` **补登记**，**已吊销的不恢复**。
- **配置项须有测试证明被读到**（ADR-0014）：`Settings.release_signing_key` 曾被声明两遍、后者静默覆盖前者，`SYNCOJ_RELEASE_KEY` 从未生效而全部测试仍绿。
- 测试用 autouse 夹具以 `SYNCOJ_KEY_DIR` 把密钥目录钉到临时目录，避免 `Settings()` 读到开发者真实的 `.key/`。

> ⚠️ 取舍：私钥放在仓库相对路径，持有该 checkout 即可签出考试机接受的升级包。`.key/` 由**两道** `gitignore`（目录自带 + 根目录）挡住。生产部署把密钥放到仅服务端账号可读的目录，用 `SYNCOJ_RELEASE_KEY` / `SYNCOJ_KEY_DIR` 指定。

### 7.2 「发布当前版本」：从界面构建并发版

- `GET /releases/source`：本机有无可构建的源码、会打出哪个版本、包里带哪把公钥。
- `POST /releases/build`：构建并签发一个版本（无需另找机器手跑 `build_bundle.py`；不会漏带签名公钥）。

| 问题 | 决定 | 依据 |
|---|---|---|
| "当前版本"指什么 | 服务端**本机仓库**的 `agent/` 源码 | 服务端运行在 checkout 中（`.key/` 同样由此定位），无新部署前提 |
| 在哪构建 | **子进程**跑 `agent/packaging/build_bundle.py` | 见下 |
| 版本号 | 界面预填 `__version__`，**须显式提交且与源码一致** | 见下 |
| 构建后是否铺开 | **只建草稿**，铺开是单独的显式一步 | 构建与推给 50 台机器风险等级不同 |

- **子进程构建**。否决 import：`build_bundle.py` 失败时抛 `SystemExit`（`collect_files`/`launcher_path`/`read_version`），非 `Exception` 子类，FastAPI 错误处理器接不住；子进程中只是非零退出码并带回 stderr，构建崩溃不影响服务端。界面与手跑命令同一入口，可复现打包（`mtime=0`、gzip 头不带文件名）保证两者逐字节相同。
- **版本号显式提交且须与源码一致**，不一致回 409 并给出两个版本号。否决自动取 `__version__`："改了代码忘改版本号"会静默覆盖上一版，已升级的机器因"版本不高于当前"拒绝升级而界面显示正常。
- **两道拒绝**：
  1. 无签名私钥 → 503 `release_not_signed`（打出的包 Agent 一律拒收）。
  2. 有私钥、无要内嵌的发布公钥 → 503 `release_trust_anchor_missing`（否则构建、铺开都成功而机器不升级）。

  检查顺序：先配置（能否发布）后输入（本次提交是否正确）。
- **审计**：每次构建一条 `event_log`（`category=release_built`），记版本号、sha256 前 12 位、操作人（`EventLog` 无管理员外键，操作人写 `meta_json.by`）。仅管理员可触发。
- **测试**：`server/tests/test_release_build.py` 跑全链：构建 → 签发 → 铺开 → 从**面向 Agent 的接口**下载 → Agent 自己的 `verify_bundle` 验签验摘要 → `safe_extract_tar` 解开，信任锚取**包内嵌的那份**。「`.key/` 那对文件是否配套」「公钥是否真的内嵌」「签名对象是否为 `release_signing_message` 文本」任一出错即红。

#### 7.2.1 安装策略：随包带下去的三条

发版时决定**机器上已有的内容是否随之改动**。位于「Agent 发布」→「发布当前版本」卡片的「高级选项」，随包（及装机台账、升级清单）带下去，字段名冻结：

| 字段 | 取值 | 默认 | 管什么 |
|---|---|---|---|
| `bootstrap_key_policy` | `keep` / `replace` | **随包内有没有密钥**：带 → `replace`，不带 → 忽略 | 机器上已有的**统一注册密钥**是否被包内那把覆盖 |
| `config_policy` | `{"<section>.<key>": "keep"｜"default"｜"force"}` | 全部 `keep` | 机器上 `agent.ini` 的**逐键**更新策略 |
| `upgrade_mode` | `apply` / `stage` / `off` | `apply` | 拿到新版本后：自动切换重启 / 只下载验签 / 不动 |

* **`bootstrap_key_policy` 带密钥时默认 `replace`**：否则机器上已吊销的旧钥会让包内新钥用不上、永远注册不上。替换前备份旧钥（`bootstrap.key.replaced-<时间戳>`，0600 root），回执写明备份路径与旧钥指纹；显式 `--bootstrap-key` 永远最优先。
* **`config_policy` 默认全 `keep`**：扫描根、下发根、服务端地址是现场数据，默认不动；按键逐个放开。
  * `keep`：一个字节都不动（缺的键也不补）；
  * `default`：**只更新没人动过的键**——机器上缺这个键，或值等于安装器上次写下的值（快照 `agent.ini.syncoj-default`，0600、不含秘密）才写新值；快照缺失（老机器）按"人可能改过"保留；
  * `force`：无条件写新值。
  状态与凭据文件（`credential.json` / `machine_uuid` / `machine_id` / 缓存）**永不在作用域内**，不留策略位。
* **`upgrade_mode` 默认 `apply`**（产品决定：默认自动更新、切换、重启）。例外：**镜像/包里无发布公钥时降级为 `off` 并提示**（否则 `validate()` 报错、服务起不来、整批机器失联）。显式写 `apply` 而无公钥是**硬错误**。
* **优先级**：显式 CLI 参数 > 服务端台账 > 包内 `install_policy.json` > 模板默认。台账高于包内：台账反映服务端当前状态，包可能是早先打的。
* **生效时机**：装机，以及经 root 管理单元的升级（`--admin-handoff` 走安装器同一个 `run()`，读暂存包内的 `install_policy.json`；交接请求不带台账）。Agent 进程本身不改 `agent.ini`、不碰注册密钥（root 只读）。

### 7.3 机器怎么知道服务端在哪

服务端地址是装机时唯一需人工输入的一项；故障形态：填错（注册不上），或服务端换 IP 后已装机器失联。

| 层 | 何时起作用 | 可信依据 |
|---|---|---|
| **包内嵌地址**（主路径） | 包由本服务端打出 | `build_bundle.py --server-url` 写进包内 `server.json`，装机时不用输 |
| **UDP 广播发现**（兜底） | 包在别处打出，或服务端换了地址 | 应答用**发布签名私钥**签「随机数 + 地址」，机器用包内公钥验 |

**服务端的对外地址**（否决枚举网卡：多网卡时会挑错），按可信度取三条来源：

1. `SYNCOJ_PUBLIC_URL` / `serve --public-url`：操作员显式配置。
2. **从请求来源反推的本机地址**（内核选出的出口网卡）：客户端无法伪造；多网卡时给出该侧真实可达的地址。
3. 教师浏览器用过的非回环 `Host`：可伪造，只记**成功**请求，排在推导之后。

三条都没有时**不做任何事**（不内嵌、不应答）。否决退回 `127.0.0.1`：每台机器连自己，现场只见"注册不上"。

- **发现必须验签**：机器找到服务端后首先发送**统一注册密钥**。否决用注册密钥做 HMAC：全机房共享，挡不住。无公钥即**不发现**（同"无密钥不升级"）。
- **UDP 防反射**：**应答不大于探测**（探测填充到 640 字节，更短的不理；地址过长时不应答），并按来源 IP 限速。
- **运行时重新发现**：连续 `REDISCOVER_AFTER_FAILURES`（3）轮连不上（约 5+10+20 秒）才问一次（服务端重启、网线抖动时地址未变；广播打扰整个局域网）。新地址**只在进程内生效**，不写回 `agent.ini`（教师的文件）。
- **只认原来那台（双向 UUID）**：服务端有持久的实例 ID（`<data_root>/server-id`），随 tick 下发，Agent 记进 `<state_dir>/server_uuid`。运行期探测带本机 `machine_uuid` 与记住的实例 ID，服务端回 v2 应答，签名覆盖「实例 ID + 机器 UUID + 是否认得这台机器」。记着 ID 时只认 ID 相同且认得本机的应答。否决只靠验签：同一份 `.key/` 拷到隔壁考场时两台都签得出。否决把实例 ID 放 `.key/` 或库里：它不是密钥，应随数据目录走；放库里要占一个迁移。实例 ID 变了（数据目录重建）的服务端不会被自动跟随，要改 `agent.ini` 或重装。
- **反方向：服务端主动通告**（§11）：服务端周期广播地址，机器挑战应答验签后切换，换 IP 后一两秒内转过来；机器主动探测保留为兜底。
- **多个答案时不猜**：两个考场同层或有假服务端时可能有多个验得过的地址；如实报出，由人显式指定（猜错表现为"代码交了但成绩为空"）。

### 7.4 远程卸载：管理端点一下，机器自己清干净

前提：Agent 以选手账号运行（../design/00-decisions.md §0），卸载要删的 `/opt/syncoj`、`/etc/syncoj`（含统一注册密钥）、systemd 单元都属 root；授权须经得起不可信链路（局域网与机器上的该账号本人）。

| 要求 | 做法 |
|---|---|
| 选手**伪造不出来** | 服务端用发布签名私钥签令牌，机器用**已有的升级信任锚**（`/etc/syncoj/release-key.pub.json`）验签。否决第二套信任链：多一把密钥多一处铺设、轮换与出错点 |
| **搬不到别的机器** | payload 绑本机 `machine_uuid`（以 root 拥有、选手改不动的那份为准），机器逐字比对 |
| **不会一直有效** | 15 分钟有效期；`kind` / `v` 防止被当成别的用途的令牌 |

- 令牌 = `b64url(payload) + "." + b64url(签名)`，签名覆盖**第一段 ASCII 字符串**而非原始 JSON（两端键序与空白不保证相同）。
- **令牌只在教师点过之后才存在**：选手无私钥，任何重放只能发生在教师点过之后。待卸载登记表**只放内存**。否决落库：等于把一枚 root 删除凭据留在 `syncoj.db`（备份、误传都会带走）；代价是服务端重启后请求消失，重点一次即可。
- **服务端两道当场拒绝**：服务端无发布私钥；机器最近一次心跳未报本机有公钥（心跳上报 `release_public_key` 即为此）。两者 `detail` 指向同一替代办法：到机器前运行装机页的 `--uninstall` 命令（../design/05-api-and-frontend.md §5.6），共用一个错误码。
- 卸载与配对状态**无关**（可能是配错人的机器，也可能是未认领的测试机）。
- **机器侧执行**：Agent 把令牌写进 `<状态目录>/handoff/request.json`；root 管理单元（`syncoj-agent-admin.path` → `syncoj-agent-admin.service` → `install.py --admin-handoff`）以 no-follow 方式读取（`handoff` 可能被选手换成符号链接）、自行验签，走 `--uninstall` 同一条路：先 stop 主单元再删除，**不动**选手桌面、家目录、运行账号、`/tmp`。Agent 无需提权，主单元保持 `NoNewPrivileges=yes`（../design/03-agent.md §3.5）。被否方案（sudo + 无参 sudoers）见 [ADR-0012](../decisions/ADR-0012-remote-uninstall-authorization.md)。
- systemd 245 容器冒烟（`scripts/smoke_systemd245.py`，CI 每次跑）验证未配对机器经 root 管理单元卸干净。
- **审计不写令牌**：令牌可在该机器换一次 root 删除，写进日志即泄露（同"审计不写 zip 密码"，../design/05-api-and-frontend.md §5.5）。

### 7.5 发布（CD）：打标签即发版

`.github/workflows/release.yml`：推 `v<版本号>` 标签触发；或在 Actions 页手动运行（填版本号），在 `main` 当前提交上跑完下面四步后由工作流打标签（标签已存在则拒绝）。

1. **版本号对账**（`scripts/release.py versions --tag`）：`server/pyproject.toml`、两个 `__init__.py`、`web/package.json` 四处一致，标签恰为 `v<版本号>`；带 `a/b/rc/dev` 的发成预发布。日常 `check.py` 也跑"四处一致"。
2. **全量检查**：以 `workflow_call` 复用 `check.yml`（py38 / 最新 Python + 前端 / root / systemd 245 冒烟），任一路不过不发。
3. **构建**（`scripts/release.py build`）：`syncoj-<版本>.tar.gz`（`git archive` + 预构建 `web/dist`，解开后 `pip install -e server`）、`syncoj-agent-<版本>.tar.gz`（可复现的 Agent 离线包）、独立的 `install.py` 与 `bootstrap.sh`、`SHA256SUMS`。产物 mtime 取提交时间，同一提交逐字节相同。
4. **发布**：`gh release create`，附部署命令与自动生成的变更列表。

- 决定：**CI 不持有发布私钥，Release 里的 Agent 包不带公钥与服务端地址**；信任锚是每台服务端自己的私钥（§7.1）。否决 CI 统一签名：一把所有学校共用的私钥泄露一次即可给所有机房推包。
- Release 是**服务端**的分发渠道；机房 Agent 从自己的服务端升级（「发布当前版本」，或「上传新版本」上传该离线包，由服务端签名铺开）。
