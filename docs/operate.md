# 服务端运维

## 部署

```bash
python -m venv .venv && .venv/bin/pip install -e "server[dev]"
.venv/bin/syncoj-server init                    # 建库 + 管理员 + .key/ 下的密钥
.venv/bin/syncoj-server serve --host 0.0.0.0    # 管理端 :8000，公开端口 :80
```

| `serve` 参数 | 默认 | 说明 |
|---|---|---|
| `--host` | `127.0.0.1` | 监听地址 |
| `--port` | `8000` | 管理端端口 |
| `--public-url` | 空 | 对考试机的地址，同 `SYNCOJ_PUBLIC_URL` |
| `--log-level` | `info` | `critical` / `error` / `warning` / `info` / `debug` / `trace` |
| `--quiet-access` | 关 | 关闭访问日志 |
| `--rebuild-web` | 关 | 前端构建过期时先重新构建再启动（需要 npm） |

全局参数：`--data-root`（默认 `runtime/server`）、`--verbose`。`syncoj-server info` 打印配置与统计。

### 前端构建是否过期

`serve` 启动时比对 `web/dist/build-info.json`（构建时写入的版本号与前端源码指纹）与当前代码：

| 情况 | 启动日志 |
|---|---|
| 一致 | 无提示 |
| 版本号不同、`web/` 源码改过、或产物里没有 `build-info.json` | `[!] 前端构建已过期（原因），管理界面仍是旧版本。` 并给出命令 |

重新构建（需要 Node.js）：

```bash
.venv/bin/syncoj-server build-web            # 没有 node_modules 或 package-lock.json 更新过时先 npm ci，再 npm run build
.venv/bin/syncoj-server build-web --install  # 强制先 npm ci
```

或启动时加 `--rebuild-web`：过期则先构建再启动，构建失败不启动。发布包自带构建好的前端，版本一致时无提示。

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `SYNCOJ_DATA_ROOT` | `runtime/server` | 数据目录 |
| `SYNCOJ_KEY_DIR` | 源码仓库内为 `.key/` | 密钥目录 |
| `SYNCOJ_RELEASE_KEY` | `<密钥目录>/release-key.pem`（存在才用） | 发布签名私钥，优先于密钥目录 |
| `SYNCOJ_PUBLIC_URL` | 空 | 对考试机的地址 |
| `SYNCOJ_PUBLIC_PORT` | `80` | 公开端口；`0` 关闭 → 见 [`quickstart.md`](quickstart.md)「两个端口」 |
| `SYNCOJ_WEB_DIST` | 自动探测 `web/dist` | 前端产物目录；不存在则只提供 API |
| `SYNCOJ_DISCOVERY` | `true` | 应答局域网发现探测 |
| `SYNCOJ_DISCOVERY_PORT` | `45871` | 发现 UDP 端口 |
| `SYNCOJ_DISCOVERY_MIN_PROBE` | `640` | 探测报文最小字节数 |
| `SYNCOJ_ANNOUNCE` | `true` | 服务端主动通告 |
| `SYNCOJ_ANNOUNCE_PORT` | `45872` | 通告 UDP 端口 |
| `SYNCOJ_DEFAULT_FILE_PATTERN` | 内置 | 题目未配代码路径时的默认 glob |
| `SYNCOJ_JUDGE_LOCAL_RUN_AS` | `nobody` | 本机评测机用 rlimit 后端且服务端以 root 运行时，选手程序的运行账号 |

⚠️ 改 `SYNCOJ_DISCOVERY_PORT` / `SYNCOJ_ANNOUNCE_PORT` 需整间机房 Agent 同步修改。

Agent 装机 → 见 [`install-agent.md`](install-agent.md)。

## 密钥从哪来：`.key/`

```
.key/                         ← 不进版本库（目录内 .gitignore + 根 .gitignore）
├── release-key.pem           发布签名私钥  0600
├── release-key.pub.json      对应公钥（打包 Agent 时内嵌）
└── bootstrap.key             统一注册密钥  0600（哈希同时写进库）
```

| 环节 | 行为 |
|---|---|
| `syncoj-server init` | 缺哪把建哪把；已有的不覆盖、不轮换；输出建了 / 找到了什么 |
| 服务端取私钥 | `SYNCOJ_RELEASE_KEY` > `.key/release-key.pem`；都没有则不提供升级 |
| `build_bundle.py` | 默认打入 `.key/release-key.pub.json`（存在才用）；`--public-key` 路径错误时报错退出 |
| `install.py` | 统一密钥：`--bootstrap-key` > `--bootstrap-key-file` > `.key/bootstrap.key`（存在才用）> 无；包内公钥落到机器配置目录，`agent.ini` 指向该副本 |
| `syncoj-server db reset` | 只删库，不动 `.key/`；之后 `init` 补登记密钥哈希，已吊销的不恢复 |

生产部署：用 `SYNCOJ_KEY_DIR` / `SYNCOJ_RELEASE_KEY` 指向仅服务端账号可读的目录。

⚠️ 持有 `.key/release-key.pem` 即可签出考试机接受的升级包 → 见 [`design/07-ops-and-install.md`](design/07-ops-and-install.md)。

手工生成发布密钥对：

```bash
syncoj-server genkey --out /etc/syncoj/release-key.pem   # 公钥默认同目录 .pub.json；--bits 默认 2048；--force 覆盖
```

## 重置管理员密码

在服务端机器上运行（`--data-root` 与 `serve` 用的一致；服务端不必停）：

```bash
.venv/bin/syncoj-server admin list                                   # 列出账号
.venv/bin/syncoj-server admin reset-password                         # 重置 admin：终端里输两次新密码
.venv/bin/syncoj-server admin reset-password --user <账号> --generate  # 随机生成新密码并打印一次
```

| 参数 | 含义 |
|---|---|
| `--user` | 账号，默认 `admin` |
| `--password` | 直接给新密码（至少 8 个字符；会留在 shell 历史里） |
| `--generate` | 随机生成；不在终端里运行（管道、脚本）时不给参数也是随机生成 |

结果：只改密码，已登录的会话不受影响；被停用的账号重新启用；审计日志记一条 `admin_password_reset`。

## 机器怎么知道服务端在哪

| 方式 | 触发 | 校验 |
|---|---|---|
| 包内嵌地址 | `build_bundle.py --server-url` 写入包内 `server.json`；界面「发布当前版本」自动写入 | — |
| 服务端主动通告 | 服务端 UDP `45872` 广播：启动后 10 分钟内每 30 秒一次，之后每 5 分钟一次；「机器配对」→「召集机器」或顶栏「快捷操作」→「召集机器」立即发一次 | Agent 到通告地址做挑战应答（`/api/v1/agent/whoami`），验签后切换 |
| UDP 发现（兜底） | Agent 连不上时每 3 轮（约 90 秒）向 UDP `45871` 广播一次，直到连上 | 应答用发布私钥签名，Agent 用包内公钥验签；无公钥不发现；记着服务端实例 ID 时只认同一台 |

→ 见 [`design/07-ops-and-install.md §7.3`](design/07-ops-and-install.md)、[`design/09-server-announce.md`](design/09-server-announce.md)、[ADR-0015](decisions/ADR-0015-server-announce.md)。

```bash
# 打包时内嵌地址
python3 agent/packaging/build_bundle.py --server-url http://10.0.0.5:8000

# 装机
sudo python3 install.py --bundle ./x.tar.gz --sha256 <校验和>              # 用包内地址
sudo python3 install.py --bundle ./x.tar.gz --server http://1.2.3.4:8000  # 覆盖
sudo python3 install.py --bundle ./x.tar.gz --no-discover                 # 禁止发现
```

### 对外地址

| 优先级 | 来源 |
|---|---|
| 1 | `SYNCOJ_PUBLIC_URL` / `serve --public-url` |
| 2 | 请求来源反推的本机地址 |
| 3 | 管理界面访问过的非回环 `Host` |
| — | 都没有：不内嵌、不应答发现，不退回 `127.0.0.1` |

查看当前值：

- 启动日志：`对外地址: http://…（显式配置）` 或 `对外地址: 还没有`；首次从局域网打开管理界面时补打 `对外地址: http://10.0.0.5:8000（来源：…）`。
- `GET /healthz` 的 `public_url`（无则 `null`）。

来源不是显式配置时，用 `SYNCOJ_PUBLIC_URL` 固定。

### 通告收不到

| 现象 | 处理 |
|---|---|
| 机器开 ufw / 交换机隔离广播 | 通告无效，依赖 Agent 主动发现；或装机时 `--discover-address <服务端>` 单播 |
| 不需要通告 | `SYNCOJ_ANNOUNCE=false` |

发现应答不大于探测：探测短于 640 字节不应答，地址过长时不应答。

## 发一个 Agent 版本

服务端在源码仓库中运行时：

1. 「Agent 发布」→「发布当前版本」卡片 → 核对版本号 → 构建。预期：版本列表新增一行，状态为未铺开。
2. 该行「铺开」。未铺开的版本机器不可见，装机入口也不发。预期：下一轮心跳后「机器配对」的「Agent 版本」列陆续变为新版本（`upgrade.mode=apply`）。

命令行等价：

```bash
python3 agent/packaging/build_bundle.py --out dist/syncoj-agent-0.1.0.tar.gz
```

| 拒绝情况 | 现象 | 处理 |
|---|---|---|
| 提交的版本号 ≠ `agent/syncoj_agent/__init__.py` 的 `__version__` | 409，报出两个版本号 | 改版本号 |
| 缺私钥或公钥 | 拒绝构建 | 补 `.key/` 或设 `SYNCOJ_RELEASE_KEY` |
| 服务端无 `agent/` 源码（pip 安装的实例） | 「发布当前版本」不可用 | 用「上传新版本」上传离线包 |

附带密钥与「高级选项」→ 见 [`install-agent.md`](install-agent.md)「发版时附带统一注册密钥（可选）」。

## 发布 SyncOJ（GitHub Release）

1. 四处版本号改成同一个值：`server/pyproject.toml`、两个 `__init__.py`、`web/package.json`。
2. 检查：

   ```bash
   python scripts/release.py versions
   ./scripts/check.sh
   ```

3. 触发发布，二选一：
   - 推标签：

     ```bash
     git tag v<版本号> && git push origin v<版本号>
     ```

   - GitHub → Actions →「release」→ Run workflow，`version` 填 `<版本号>`（不带 `v`）。在 `main` 当前提交上检查、构建，最后打标签并发布；标签已存在则失败。

`.github/workflows/release.yml` 依次：`release.py versions --tag` 对账 → 复用 `check.yml` 全量检查（含 systemd 245 冒烟）→ `release.py build` → `gh release create`。版本号带 `a` / `b` / `rc` / `dev` 发为预发布。

| 产物 | 内容 |
|---|---|
| `syncoj-<版本>.tar.gz` | 源码 + 预构建 `web/dist`，解开后 `pip install -e server`；第三方许可证：根目录 `THIRD-PARTY-LICENSES.md`、`web/dist/assets/third-party-licenses.txt`（打包进前端产物的全部第三方包） |
| `syncoj-agent-<版本>.tar.gz` | Agent 离线包，不带公钥、不带服务端地址 |
| `install.py`、`bootstrap.sh` | 独立安装器 |
| `SHA256SUMS` | 校验和 |

本地构建：`python scripts/release.py build --out dist [--tag v<版本号>]`。Release 里的 Agent 包需上传到本服务端签名铺开后机器才会升级 → 见 [`design/07-ops-and-install.md`](design/07-ops-and-install.md)「发布（CD）」。

## systemd 245 冒烟

```bash
python scripts/smoke_systemd245.py          # 需要 docker 与已安装的服务端（pip install -e server）
python scripts/smoke_systemd245.py --keep   # 失败后保留容器与临时目录
```

覆盖：umask 0 安装、日志与配对码、注册单元不跟随符号链接、升级切换、启动守卫回滚、主动通告切换、授权卸载、评测机一键安装与远程卸载（`ExecStopPost=+` 经 `systemd-run` 卸干净）。CI 同样运行。

## 自带评测

1. 「场次管理」→ 该行「题目」→ 登记题目（已登记则跳过）。
2. 「评测」→「题目」卡片 → 该题「设置」：评测方式选「自带评测」，填时限（ms）/ 内存（MB）/ 输入输出 / 编译参数，上传数据包。预期：数据包上传时即校验，结构不对直接报错。
3. 「评测」→「评测机」卡片 →「添加评测机」→ 输入名称 →「签发令牌」。预期：对话框给出令牌（只显示一次）与 Linux / Windows 两条安装命令，各带「复制」。
4. 在评测机上运行对应命令：

   ```bash
   # Linux：有 isolate 用 isolate；没有会询问是否用 rlimit（加 --yes 不询问）
   curl -fsSL http://10.0.0.5:8000/judge/install.sh | sudo bash -s -- <令牌>
   ```

   ```powershell
   # Windows：「以管理员身份运行」的 PowerShell；需先装 Python 3.8+ 与 MinGW-w64 g++（加进系统 PATH）
   $p = Join-Path $env:TEMP syncoj-judge-install.ps1; iwr -UseBasicParsing http://10.0.0.5:8000/judge/install.ps1 -OutFile $p; & $p -Token '<令牌>'
   ```

   预期：脚本最后打印「评测机已启动」；「评测机」卡片中该评测机为在线，显示后端、主机、版本。已装好 `syncoj-judge` 时也可用对话框里的「手动运行」命令。

   只有服务端一台 Linux 机器时：卡片头「本机评测机」→「启动」。预期：表格多出「本机评测机」一行并在线；「停止」后它评完手上的任务退出。非 Linux 服务端上该按钮禁用，标「仅 Linux」。
5. 「评测」→「任务」卡片 → 选「全部自带评测题目」或某题 →「评测 / 重测」（或顶栏「快捷操作」→「评测全部题目」）。场次未结束时服务端回 409 `contest_in_progress`，界面弹「仍要评测？」，确认后带 `force` 排队。预期：进度条显示排队 / 评测中 / 完成 / 失败计数；失败行列出考号、题目、失败原因与「重测本题」。
6. 「成绩」：矩阵出现分数；点格子看测试点明细。

自动评测与版本（§12.9）：

1. 「评测」→「任务」卡片 → 打开「收到新版本自动评测」，按需改「合并 N 秒内的连续改动」（默认 60）。预期：选手保存某题代码后，进度显示「等待改动停止」，到点后评测；改回评过的代码时直接复用结果，评测机不动。
2. 只评一个人 / 一道题：「任务」卡片选题目与选手 →「评测 / 重测」；要真跑不复用，勾「不复用已有结果」。
3. 看分数变化：「成绩」→ 选手列「版本与分数」图标（全部题目小图），或格子明细 →「版本」；「评测」→「评测记录」行「版本」。点图上的点或表格行选中版本。
4. 重测过去的版本：版本表该行「重测」。预期：结果出现在 timeline 与「评测记录」（触发 = 历史版本），成绩矩阵不变。
5. 改用某个版本的成绩：该行「采用」→ 确认。预期：矩阵该格换成那一版的分数，版本表标「计入成绩」。手工录入的格子先在「成绩」清除再采用。

| `syncoj-judge` 参数 | 默认 | 说明 |
|---|---|---|
| `--server` | 必填 | 服务端管理端口地址 |
| `--token-file` | 必填 | 评测机令牌文件 |
| `--backend` | `isolate` | `isolate`（需先装 isolate）/ `rlimit`（不隔离网络与文件系统，仅开发自测） |
| `--run-as` | — | `rlimit` 以 root 运行时必需：选手程序用此账号跑 |
| `--box-id` | `0` | isolate box 编号，同机多个 worker 各不相同 |
| `--isolate` | `isolate` | isolate 可执行文件路径 |
| `--cgroups` | 关 | isolate 用 cgroup 统计内存 |
| `--cache-dir` | `~/.cache/syncoj-judge` | 数据包缓存目录 |
| `--box-dir` | 系统临时目录 | `rlimit` / `winjob` 的工作目录所在 |
| `--slots` | `1` | 同时评几个任务（大于 1 只能用 isolate）；管理端改了并发数时评测机会以新值重启自己 |
| `--poll` | `5` 秒 | 轮询间隔（评测中也按它报状态） |
| `--log-file` | stderr | 日志文件 |
| `--install-manifest` | pyz 旁的 `install.json` | 安装方式；远程卸载时据此清理（安装脚本自动传） |
| `--once` | 关 | 评一个任务后退出 |
| `--verbose` / `-v` | 关 | 调试日志 |

管理评测机（「评测机」卡片该行）：

1. 「领任务」开关：关 = 暂停，不再领新任务，手上的评完照常交；打开重新领。
2. 改并发数：「并发」列的数字框改成 N（1–64）。预期：0.6 秒后提交，下一次轮询（默认 5 秒内）送达，评测机评完手上的任务后以 `--slots N` 重启，列里「当前」消失。只有 isolate 后端可改；rlimit / winjob 显示「仅 isolate」；旧版本评测机只显示数字（到那台机器上改 `--slots` 后重启，或先升级）。
3. 「更多」→「停止」/「重启」：下一次轮询（默认 5 秒内）送达，评完手上的任务后退出 / 以同样参数重启。离线或旧版本评测机上不可用，到那台机器上 `systemctl stop|restart syncoj-judge`（Windows：计划任务「SyncOJ Judge」）。
4. 「更多」→「改名」。
5. 「更多」→「卸载」→ 输入名称确认。预期：状态列出现「卸载中」；评测机评完手上的任务后从表格里消失（审计日志「评测机卸载」）。之后那台机器上：Linux 几秒内 `systemctl status syncoj-judge` 报单元不存在，`/opt/syncoj-judge`、`/etc/syncoj-judge`、`/var/cache/syncoj-judge` 与账号 `syncoj-judge` / `syncoj-judge-run` 都删了；Windows 2 分钟内两个计划任务「SyncOJ Judge」「SyncOJ Judge Cleanup」与 `%ProgramData%\SyncOJ\judge` 都删了。离线或旧版本评测机上不可用：到那台机器上手动卸载（Linux `sudo /opt/syncoj-judge/uninstall.sh`；Windows 以管理员运行 `%ProgramData%\SyncOJ\judge\uninstall.ps1`），再回来「吊销」。本机评测机没在跑时直接删掉这一行与 `<数据目录>/judge-local`。
6. 「更多」→「吊销」→ 输入名称确认。预期：令牌立即失效，它正在评的任务立即回到队列。评测机程序留在那台机器上（之后连不上服务端，日志一直报 401）。

暂停全部评测（例如考试中途要把 CPU 让给别的事）：「评测机」卡片头「评测服务」开关 → 关 → 确认。预期：所有评测机都不再领新任务，手上的评完照常交回；旁边「排队 N」保留不减（自动评测照常入队）。打开后按原顺序接着评。

筛出有风险的评测记录（§12.11）：

1. 「成绩」→「评测记录」卡片，或「评测」→「评测记录」卡片 → 第一个筛选框选「有风险」（或「有疑似违规」）。预期：只剩带红色（或黄色）左边框与「风险 N」/「疑似 N」标签的行；成绩矩阵里对应格子有红 / 黄细边框，分数颜色不变。
2. 该行「明细」。预期：红框是风险（逃出进程组、篡改 checker、工作目录里的符号链接、预检拦截），黄框是疑似违规，风险在上。
3. 展开「源码」，或点带行号的条目。预期：代码按行显示，标注的行黄 / 红底色，点条目滚到那一行。代码超过 1 MB 不显示。
4. 风险只是标注，不改分；要处理就「手工录分」或在版本里改用别的结果。分级与条件 → [`reference/judge-result.md`](reference/judge-result.md) 第 8 节。

⚠️ 考试进行中评测的是未完成的代码。数据包格式、结果格式、接口 → 见 [`reference/judge-result.md`](reference/judge-result.md) 第 6 节、[`design/10-builtin-judge.md`](design/10-builtin-judge.md)。

## 导入导出题目配置

导入（LemonLime 比赛目录、HOJ / Hydro / QDUOJ 导出包、FPS XML、SyncOJ 原生包）：

1. 「评测」→「题目」卡片 →「导入题目配置」→ 拖入 `.zip` / `.xml`；格式保持「自动识别」或手选。LemonLime 须把整个比赛目录（`.cdf` + `data/`）打成一个 zip。
2. 预览表逐题核对：「题目」列可改标识，「动作」选新建 / 覆盖 / 跳过（有错误的题只能跳过）；「提示」列点开看每条错误与警告。
3. 「设为自带评测」默认开（覆盖已有题目时把评测方式改为自带评测）→「导入 N 道」。预期：提示「新建 a · 覆盖 b」，题目列表刷新；标识冲突时对应行标红。

导出：「评测」→「题目」卡片 →「导出」→ 选格式（SyncOJ / HOJ / LemonLime / Hydro）→ 勾选题目，核对每题下方的警告 →「下载 N 道」。单题：该题「设置」→ 数据包一栏「导出」；「下载」取原样数据包。

⚠️ 暂存包 2 小时后失效，过期后重新上传。各格式映射、警告含义、上限 → 见 [`reference/problem-formats.md`](reference/problem-formats.md)。

## 命令行等价流程

```bash
# 场次与选手
syncoj-server contest create --name "2025 校内模拟赛"     # 可选 --slug / --status draft|running|frozen|closed（默认 running）/ --note
syncoj-server contest import-players --contest 2025 --file roster.csv
syncoj-server contest list

# 统一注册密钥（明文只显示一次）
syncoj-server bootstrap-key issue --label "2025 机房镜像" --expires-days 30 \
    --out /srv/syncoj/bootstrap.key      # 同时写成 root 只读文件
syncoj-server bootstrap-key list
syncoj-server bootstrap-key pending      # 未配对的机器（含配对码剩余时间）
syncoj-server bootstrap-key revoke --id 1

# 删库重建（⚠️ 删除全部数据；不带 --yes 只打印计划）
syncoj-server db reset --yes [--keep-admin]
```

名单 CSV：列顺序 `选手编号,姓名,座位,分组`，仅编号必填；逗号或制表符分隔；自动跳过表头与 `#` 注释行。

## 心跳停了怎么排查

### 第一步：服务端

1. 「选手状态」或「机器配对」：看「最后心跳」列。离线阈值默认 **90 秒**，在「机器配对」→「运行参数」修改。
2. 「审计日志」筛 `agent_`：`agent_revoke`（凭据作废）、`agent_unbind`（解除配对）、`agent_uninstall`（请求卸载）；`contest_auto_closed`（场次到点自动结束）、`contest_archive`（场次已归档：机器不再进入）。
   机器回报「这台机器在场次所用的名单里没有绑定」：场次默认名单与机器绑定的名单不同 →「名单库」对场次默认名单「按姓名匹配机器」（[`quickstart.md`](quickstart.md)「一台机器绑多份名单」）。
3. 服务端日志搜机器名或 `machine_id`：

| 响应 | 含义 | 心跳 |
|---|---|---|
| `401` | 凭据失效（作废 / 换库），Agent 清本地凭据回到等待注册 | 停止 |
| `403 pairing_required` / `contest_player_missing` | 未绑人，或场次里没有此人 | 继续 |
| `409 contest_ended` | 场次时间窗结束 | 正常安静，不再上传 |
| `404` | `agent.ini` 地址指错 | 停止 |

### 第二步：机器

```bash
systemctl status syncoj-agent --no-pager -l | head -20
systemctl status syncoj-agent-enroll.service --no-pager -l | head -20
journalctl -u syncoj-agent -n 50 --no-pager
journalctl -u syncoj-agent-enroll.service -n 50 --no-pager
sudo tail -n 100 /var/lib/syncoj/agent.log
sudo ls -l /etc/syncoj/ /var/lib/syncoj/
df -h /; systemctl show syncoj-agent -p NRestarts -p ExecMainStatus -p User
```

| 现象 | 原因 | 处理 |
|---|---|---|
| `226/NAMESPACE`，服务反复起不来 | `ReadWritePaths=` 含不存在的目录 | 升级到每条路径带 `-` 的版本 |
| `统一密钥文件 … 读不出来` + exit 1 | 旧版 Agent 自己读 root 只读密钥 | 升级；新版由 `syncoj-agent-enroll.service` 注册 |
| `Start request repeated too quickly` | systemd 重启额度用完 | 修根因后 `systemctl reset-failed syncoj-agent && systemctl start syncoj-agent` |
| 注册单元 `HTTP 403 bootstrap_key_revoked` | 服务端不认机器上的密钥 | 重新签发 → 覆盖 `/etc/syncoj/bootstrap.key`（0600、属主 root）→ 重跑注册单元 |
| 注册单元 `Permission denied: '/etc/syncoj/agent.ini'` | 配置属主不是运行账号 | 重跑安装器，再清限流状态 |
| 服务在跑、日志安静、服务端无 tick | 无凭据，等待注册 | `systemctl status syncoj-agent-enroll` |
| `activating (auto-restart)` | 起不来或起来就崩 | `journalctl -u syncoj-agent -n 50` |
| 机器时间偏差大 | 配对码 / 令牌时效判断错误 | 校时（开 NTP） |
| 服务端换了 IP | 机器仍连旧地址 | 「机器配对」→「召集机器」（或顶栏「快捷操作」→「召集机器」）；不召集也会每约 90 秒自己找一次 |
| `agent.log` 出现 `实例 ID 与本机记住的不同` / `不认得这台机器` | 局域网里应答的不是原来那台服务端（数据目录重建过、或隔壁考场），或原来那台已删除这台机器 | 确认要连新服务端：改 `agent.ini` 的地址（连上后实例 ID 自动更新）或重装；实例 ID 在 `<data_root>/server-id`，迁移服务端时连数据目录一起搬 |

前台跑一轮：

```bash
sudo -u <运行账号> /usr/bin/python3 -E -s /opt/syncoj/current/run_agent.py \
    --config /etc/syncoj/agent.ini --verbose --once
```

只校验配置（打印出错的键）：

```bash
sudo -u <运行账号> /usr/bin/python3 -E -s /opt/syncoj/current/run_agent.py \
    --config /etc/syncoj/agent.ini --check
```

### 第三步：非故障的安静

| 情况 | 表现 |
|---|---|
| 场次时间窗结束 | 409 `contest_ended`，Agent 停止上传 |
| 未超离线阈值 | 90 秒内无心跳仍显示在线 |
| 正在升级 | `agent.log` 出现 `upgrade_*`：下载 → 验签 → 切目录 → 重启；新版起不来时启动守卫自动回滚 |
| 大文件下发中 | tick 间隔收紧到 2 秒，列表显示在线 |
