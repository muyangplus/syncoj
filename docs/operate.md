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

全局参数：`--data-root`（默认 `runtime/server`）、`--verbose`。`syncoj-server info` 打印配置与统计。

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
| 服务端主动通告 | 服务端 UDP `45872` 广播：启动后 10 分钟内每 30 秒一次，之后每 5 分钟一次；管理端「机器配对」→「召集机器」立即发一次 | Agent 到通告地址做挑战应答（`/api/v1/agent/whoami`），验签后切换 |
| UDP 发现（兜底） | Agent 向 UDP `45871` 广播探测 | 应答用发布私钥签名，Agent 用包内公钥验签；无公钥不发现 |

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

1. 「Agent 发布」→「发布当前版本」（从本机 `agent/` 构建并签名）。
2. 「铺开」。未铺开的版本机器不可见，装机入口也不发。

命令行等价：

```bash
python3 agent/packaging/build_bundle.py --out dist/syncoj-agent-0.1.0.tar.gz
```

| 拒绝情况 | 现象 | 处理 |
|---|---|---|
| 提交的版本号 ≠ `agent/syncoj_agent/__init__.py` 的 `__version__` | 409，报出两个版本号 | 改版本号 |
| 缺私钥或公钥 | 拒绝构建 | 补 `.key/` 或设 `SYNCOJ_RELEASE_KEY` |
| 服务端无 `agent/` 源码（pip 安装的实例） | 该栏不可用 | 走「上传安装包」 |

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
| `syncoj-<版本>.tar.gz` | 源码 + 预构建 `web/dist`，解开后 `pip install -e server` |
| `syncoj-agent-<版本>.tar.gz` | Agent 离线包，不带公钥、不带服务端地址 |
| `install.py`、`bootstrap.sh` | 独立安装器 |
| `SHA256SUMS` | 校验和 |

本地构建：`python scripts/release.py build --out dist [--tag v<版本号>]`。Release 里的 Agent 包需上传到本服务端签名铺开后机器才会升级 → 见 [`design/07-ops-and-install.md`](design/07-ops-and-install.md)「发布（CD）」。

## systemd 245 冒烟

```bash
python scripts/smoke_systemd245.py          # 需要 docker 与已安装的服务端（pip install -e server）
python scripts/smoke_systemd245.py --keep   # 失败后保留容器与临时目录
```

覆盖：umask 0 安装、日志与配对码、注册单元不跟随符号链接、升级切换、启动守卫回滚、主动通告切换、授权卸载。CI 同样运行。

## 自带评测

1. 「题目」→「评测设置」：评测方式选「自带评测」，填时限 / 内存 / IO 方式 / 编译参数，上传数据包。
2. 「成绩」→「自带评测」→「添加评测机」，复制令牌（只显示一次）写入文件。
3. 评测机上以专用账号运行：

   ```bash
   pip install -e server
   syncoj-judge --server http://10.0.0.5:8000 --token-file token --backend isolate
   ```

4. 「成绩」→「自带评测」→ 排任务 / 重测，查看进度与失败原因。

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
| `--poll` | `5` 秒 | 无任务时轮询间隔 |
| `--once` | 关 | 评一个任务后退出 |
| `--verbose` / `-v` | 关 | 调试日志 |

⚠️ 考试进行中评测的是未完成的代码。数据包格式、结果格式、接口 → 见 [`reference/judge-result.md`](reference/judge-result.md) 第 6 节、[`design/10-builtin-judge.md`](design/10-builtin-judge.md)。

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

1. 机器列表 / 总览：看「上次心跳」「距上次心跳」。离线阈值默认 **90 秒**，在「运行参数」修改。
2. 事件页筛 `agent_`：`agent_revoke`（凭据作废）、`agent_unbind`（解除配对）、`agent_uninstall`（请求卸载）；`contest_auto_closed`（场次到点自动结束）。
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
| 服务端换了 IP | 机器仍连旧地址 | 「机器配对」→「召集机器」 |

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
