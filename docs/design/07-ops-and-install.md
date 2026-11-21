## 7. 运维与装机

安装器**幂等、多入口、同一份逻辑**：只有一个 `agent/packaging/install.py`（没有 `install.sh`、没有 `--mode`），入口由"安装包从哪来"决定：

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

1. **`bootstrap.key` 不写进 `agent.ini`**：后者 chown 给选手账号，等于公开密钥。
2. **写文件显式用 LF**：Windows 上 `Path.write_text()` 会写成 `\r\n`，systemd 解析 unit 直接失败且报错指不到行尾符。
3. **写完 chown 给运行账号**：否则 Agent 读不到自己的配置，报错只说"权限不足"。

### 7.1 密钥从哪来：`.key/`

`syncoj-server init` 直接建好两把密钥，免去手工 genkey、指向私钥、拷公钥进包：

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

- **门槛是"文件存在"**：有则生效、无则保持原样（没私钥就不提供升级，没公钥就打不带公钥的包）。不能改成"缺失就启动失败"，否则一次干净的 `git clone` 就起不来，而"升级整体关闭"是安全可用的状态。
- **注册密钥看"哈希在库里登记了没有"，不看文件在不在**：`db reset` 只删库、不碰 `.key/`，会出现"文件在、哈希没了"，注册被判「密钥无效」。所以 `init` 会**补登记**，但**已吊销的绝不复活**（吊销是有意动作）。
- **配置项必须有测试证明被读到**（ADR-0014）：`Settings.release_signing_key` 曾被声明两遍、后者静默覆盖前者，`SYNCOJ_RELEASE_KEY` 从未生效而全部测试仍绿。
- 测试用 autouse 夹具以 `SYNCOJ_KEY_DIR` 把密钥目录钉到临时目录，否则 `Settings()` 会读到开发者真实的 `.key/`，把"没有私钥就不提供升级"测反。

> ⚠️ **私钥放在仓库相对路径是明确的取舍**：拿到这份 checkout 的人就能签出考试机会接受的升级包。所以 `.key/` 由**两道** `gitignore`（目录自带 + 根目录）挡着，不挂在一个可被删掉的文件上。生产部署把密钥放到只有服务端账号可读的地方，用 `SYNCOJ_RELEASE_KEY` / `SYNCOJ_KEY_DIR` 指过去。

### 7.2 「发布当前版本」：从界面构建并发版

`GET /releases/source` 报告本机有没有可构建的源码、会打出哪个版本、包里带哪把公钥；`POST /releases/build` 直接构建并签发一个版本。
这样不需要教师另找一台装了仓库的机器手跑 `build_bundle.py`，也不会忘带签名公钥（表现是"发成功了但所有机器都不升级"）。

| 问题 | 做法 | 理由 |
|---|---|---|
| "当前版本"指什么 | 服务端**本机仓库**的 `agent/` 源码 | 服务端本就跑在 checkout 里（`.key/` 也是这么找到的），无新部署前提 |
| 在哪构建 | **子进程**跑 `agent/packaging/build_bundle.py` | 见下 |
| 版本号 | 界面预填 `__version__`，但**必须显式提交且与源码一致** | 见下 |
| 构建后是否铺开 | **只建草稿**，铺开是单独的显式一步 | 构建与推给 50 台机器风险等级完全不同 |

- **用子进程不用 import**：`build_bundle.py` 失败时抛 `SystemExit`（`collect_files`/`launcher_path`/`read_version`），它不是 `Exception` 子类，FastAPI 错误处理器接不住；子进程里它只是非零退出码，还能带回 stderr。构建崩溃也带不走服务端；界面与手跑命令是同一入口，可复现打包（`mtime=0`、gzip 头不带文件名）保证两者逐字节相同。
- **版本号显式提交且须与源码一致**：自动取 `__version__` 时"改了代码忘改版本号"会静默覆盖上一版，已升级的机器因"版本不高于当前"拒绝升级而界面一切正常；不一致时包里的代码与发布记录的号对不上，无法排查"这台到底升没升"。不一致回 409 并给出两个版本号。
- **两道拒绝**，避免"看起来成功"：
  1. 没有签名私钥 → 503 `release_not_signed`（打出的包 Agent 一律拒收）。
  2. 有私钥、没有要内嵌的发布公钥 → 503 `release_trust_anchor_missing`（服务端能签、机器验不了，构建铺开都成功而机器一动不动，最难查）。
  检查顺序先配置（能不能发布）后输入（这次提交对不对），否则填错版本号的人要撞两轮才到真正该修的那步。
- **审计**：每次构建一条 `event_log`（`category=release_built`），记版本号、sha256 前 12 位、操作人（`EventLog` 无管理员外键，操作人写 `meta_json.by`）。这等价于让服务端发布可执行代码，只有管理员能触发。
- **测试**：`server/tests/test_release_build.py` 跑全链：构建 → 签发 → 铺开 → 从**面向 Agent 的接口**下载 → Agent 自己的 `verify_bundle` 验签验摘要 → `safe_extract_tar` 解开，信任锚取**包内嵌的那份**。所以「`.key/` 那对文件是否配套」「公钥是否真的内嵌」「签名对象是否为 `release_signing_message` 文本」任一出错都会红。

#### 7.2.1 安装策略：随包带下去的三条

发版时决定**机器上已有的东西要不要跟着改**。这些在构建卡片**高级选项**里，随包（及装机台账、升级清单）带下去，字段名冻结：

| 字段 | 取值 | 默认 | 管什么 |
|---|---|---|---|
| `bootstrap_key_policy` | `keep` / `replace` | **随包内有没有密钥**：带 → `replace`，不带 → 忽略 | 机器上已有的**统一注册密钥**是否被包内那把覆盖 |
| `config_policy` | `{"<section>.<key>": "keep"｜"default"｜"force"}` | 全部 `keep` | 机器上 `agent.ini` 的**逐键**更新策略 |
| `upgrade_mode` | `apply` / `stage` / `off` | `apply` | 拿到新版本后：自动切换重启 / 只下载验签 / 不动 |

* **`bootstrap_key_policy` 带密钥时默认 `replace`**：否则机器上一把已吊销的旧钥会让包内新钥用不上、永远注册不上。替换不可逆，所以先备份旧钥（`bootstrap.key.replaced-<时间戳>`，0600 root），回执写明备份路径与旧钥指纹；显式 `--bootstrap-key` 永远最优先。
* **`config_policy` 默认全 `keep`**：扫描根、下发根、服务端地址都是现场数据，默认不动才安全，要放开就逐键放开。
  * `keep`：一个字节都不动（缺的键也不补）；
  * `default`：**只更新没人动过的键**——机器上缺这个键，或值等于安装器上次写下的值（快照 `agent.ini.syncoj-default`，0600、不含秘密）才写新值；快照缺失（老机器）按"人可能改过"保留；
  * `force`：无条件写新值。
  状态与凭据文件（`credential.json` / `machine_uuid` / `machine_id` / 缓存）**永不在作用域内**，不留策略位。
* **`upgrade_mode` 默认 `apply`**（产品决定：默认自动更新、切换、重启）。例外：**镜像/包里没有发布公钥时降级为 `off` 并明确提示**，否则 `validate()` 报错、服务起不来、整批机器失联。显式写 `apply` 却没有公钥是**硬错误**。
* **优先级**：显式 CLI 参数 > 服务端台账 > 包内 `install_policy.json` > 模板默认。台账优先，因为在线装机时台账是服务端此刻的真相，而包可能是几天前打的。
* **生效时机**：装机，以及经 root 管理单元的升级（`--admin-handoff` 走安装器同一个 `run()`，读暂存包内的 `install_policy.json`；交接请求不带台账）。Agent 进程本身不改 `agent.ini`、不碰注册密钥（root 只读）。

### 7.3 机器怎么知道服务端在哪

服务端地址是装机时唯一要人输的一项，坏法有两种：填错（"注册不上"），或服务端换 IP 后已装好的机器一起失联。地址入口主次分明：

| 层 | 何时起作用 | 凭什么可信 |
|---|---|---|
| **包内嵌地址**（主路径） | 包是这台服务端自己打的 | `build_bundle.py --server-url` 写进包内 `server.json`，装机时不用输 |
| **UDP 广播发现**（兜底） | 包是别处打的，或服务端换了地址 | 应答用**发布签名私钥**签「随机数 + 地址」，机器用包内公钥验 |

**服务端的对外地址**算不准（多网卡时枚举会挑错），按可信度取三条来源：

1. `SYNCOJ_PUBLIC_URL` / `serve --public-url`：操作员说的最权威。
2. **从请求来源反推的本机地址**（内核给出走哪个网卡）：客户端伪造不了，多网卡时给出"从那一侧真可达"的地址。
3. 教师浏览器用过的非回环 `Host`：真人证据但可伪造，只记**成功**请求的，排在推导之后。

三条都没有就**什么都不做**（不内嵌、不应答）。退回 `127.0.0.1` 最坏：每台机器都连自己，现场只见"注册不上"。

- **发现必须验签**：机器找到服务端后第一件事是发送**统一注册密钥**，不验签等于把它交给局域网里任何应答者。用注册密钥做 HMAC 挡不住（全机房共享）；只有服务端私钥能挡。没有公钥就**不发现**（同"没有密钥就不升级"）。
- **UDP 端口防反射**：**应答不得大于探测**（探测填充到 640 字节，更短的探测不理，地址太长宁可不应答），并按来源 IP 限速。
- **运行时重新发现**：连续 `REDISCOVER_AFTER_FAILURES`（3）轮连不上（约 5+10+20 秒）才问一次，因为服务端重启、网线抖动时地址并没变，而广播打扰整个局域网。新地址**只在进程内生效**，不写回 `agent.ini`（那是教师的文件）。
- **反方向：服务端主动通告**（§11）：服务端周期广播地址，机器挑战应答验签后切换，换 IP 后一两秒内转过来；"机器自己问"保留为兜底。
- **多个答案时不猜**：两个考场同层或有假服务端时可能有多个验得过的地址；猜错表现为"代码交了但成绩是空的"，所以如实报出、让人显式指定。

### 7.4 远程卸载：管理端点一下，机器自己清干净

Agent 以选手账号运行（../design/00-decisions.md §0），而卸载要删的 `/opt/syncoj`、`/etc/syncoj`（含统一注册密钥）、systemd 单元都属 root，所以必须由 root 执行，且授权要经得起不可信链路（局域网与机器上那个账号本人）。

| 要求 | 做法 |
|---|---|
| 选手**伪造不出来** | 服务端用发布签名私钥签令牌，机器用**已有的升级信任锚**（`/etc/syncoj/release-key.pub.json`）验签；不新增第二套信任链（多一把密钥就多一处要铺、要轮换、会出错） |
| **搬不到别的机器** | payload 绑本机 `machine_uuid`（以 root 拥有、选手改不动的那份为准），机器逐字比对 |
| **不会一直有效** | 15 分钟有效期；`kind` / `v` 防止被当成别的用途的令牌 |

- 令牌 = `b64url(payload) + "." + b64url(签名)`，签名覆盖**第一段 ASCII 字符串**而非原始 JSON，因为两端的键序与空白不保证相同。
- **守住"选手不能自己卸掉监控"的是"令牌只在教师点过之后才存在"**：选手没有私钥，任何重放都只能发生在教师已点过之后。待卸载登记表因此**只放内存**：落库等于把一枚 root 删除凭据留在 `syncoj.db`（备份、误传都会带走）；代价是服务端重启后请求消失，重点一次即可。
- **服务端两道当场拒绝**（否则界面显示成功、机器什么都没发生）：服务端没有发布私钥；机器最近一次心跳没报本机有公钥（这是心跳上报 `release_public_key` 的理由）。两者 `detail` 指向同一替代办法：到机器前跑装机页的 `--uninstall` 命令（../design/05-api-and-frontend.md §5.6），所以不分两个错误码。
- 卸载与配对状态**无关**：可能是配错人的机器，也可能是未认领的测试机。
- **机器侧执行**：Agent 把令牌写进 `<状态目录>/handoff/request.json`；root 管理单元（`syncoj-agent-admin.path` → `syncoj-agent-admin.service` → `install.py --admin-handoff`）以 no-follow 方式读取（`handoff` 可能被选手换成符号链接）、自己验签，然后走 `--uninstall` 同一条路：先 stop 主单元再删除，**绝不动**选手桌面、家目录、运行账号、`/tmp`。Agent 不需要任何提权，主单元保持 `NoNewPrivileges=yes`（../design/03-agent.md §3.5）。细节与被否方案（sudo + 无参 sudoers）见 [ADR-0012](../decisions/ADR-0012-remote-uninstall-authorization.md)。
- systemd 245 容器冒烟（`scripts/smoke_systemd245.py`，CI 每次跑）验证未配对机器经 root 管理单元卸干净。
- **审计不写令牌**：它能在那台机器换一次 root 删除，写进到处被导出的日志等于泄露一次性钥匙（同"审计不写 zip 密码"，../design/05-api-and-frontend.md §5.5）。

### 7.5 发布（CD）：打标签即发版

`.github/workflows/release.yml`：推 `v<版本号>` 标签触发；或在 Actions 页手动运行（填版本号），在 `main` 当前提交上跑完下面四步后由工作流打标签（标签已存在则拒绝）。

1. **版本号对账**（`scripts/release.py versions --tag`）：`server/pyproject.toml`、两个 `__init__.py`、`web/package.json` 四处一致，标签恰为 `v<版本号>`；带 `a/b/rc/dev` 的发成预发布。日常 `check.py` 也跑"四处一致"。
2. **全量检查**：以 `workflow_call` 复用 `check.yml`（py38 / 最新 Python + 前端 / root / systemd 245 冒烟），任一路不过不发。
3. **构建**（`scripts/release.py build`）：`syncoj-<版本>.tar.gz`（`git archive` + 预构建 `web/dist`，解开后 `pip install -e server`）、`syncoj-agent-<版本>.tar.gz`（可复现的 Agent 离线包）、独立的 `install.py` 与 `bootstrap.sh`、`SHA256SUMS`。产物 mtime 取提交时间，同一提交逐字节相同。
4. **发布**：`gh release create`，附部署命令与自动生成的变更列表。

**CI 不持有发布私钥，Release 里的 Agent 包不带公钥与服务端地址**：信任锚是每台服务端自己的私钥（§7.1），一把 CI 里所有学校共用的私钥泄露一次就能给所有机房推包。所以 Release 是**服务端**的分发渠道；机房 Agent 照旧从自己的服务端升级（「发布当前版本」，或上传这个离线包由服务端签名铺开）。
