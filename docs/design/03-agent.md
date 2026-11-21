## 3. 客户端 Agent

### 3.1 为什么砍掉所有第三方依赖

| 原方案依赖 | 替代 | 收益 |
|---|---|---|
| `watchdog`（inotify） | 周期 `os.scandir` 全量扫描 | 免依赖；tick 本就上报全量索引，inotify 冗余 |
| `websockets` | HTTP 轮询 | 免依赖；没有重连/半开检测/连接状态机 |
| `requests` / `httpx` | `http.client` + `urllib.request` | 标准库 |
| `pydantic` | 纯 dict + CI 契约测试 | 标准库 |
| `tomli`/`tomllib` | `configparser`（INI） | `tomllib` 要 3.11+ |
| 本地队列 SQLite | 服务端持有状态，Agent 无状态 | 免依赖；崩溃/重装天然自愈 |

结果：**零第三方依赖、零编译、零 pip、无状态**；部署 = 拷一个目录 + 一个 systemd unit。
用 `python3 -E -s` 启动（`-E` 忽略 `PYTHON*` 环境变量，`-s` 忽略 user site-packages），选手的 `pip install` 污染不到 Agent。

**代价**：常驻内存 30~40MB（Go 约 12MB，50 台无所谓）；同步延迟 = 轮询周期（20s 已确认可接受）；依赖目标机有 `python3`（NOI Linux 必有）。

### 3.2 主循环（同步串行，不用 asyncio）

```python
while True:
    files  = scan(root)                       # os.scandir + sha256(mtime/size 缓存)
    tick   = post("/agent/tick", files, deploy_progress/done/failed, stats)
    for f in tick["need_upload"]:
        upload(f)                             # ThreadPoolExecutor 并发 2
    for j in tick["deploy_jobs"]:
        download(j)                           # Range 续传 + 校验 + 原子落盘
    if tick["upgrade"]: handle_upgrade(tick["upgrade"])
    sleep(tick["next_tick_seconds"])
```

不用 asyncio：3.8 没有 `TaskGroup`；串行 IO + 小线程池已够，同步代码更好排错。

### 3.3 扫描与上传

- `os.scandir` 递归，**只处理白名单后缀**（`c/cpp/cc/pas/java/py/...`）
- 排除 `.git`、`build`、`__pycache__`、隐藏文件、`*.swp`、`*~`、`*.tmp`
- **sha256 缓存**：`(path, mtime, size)` 不变则复用上次哈希
- 单文件上限默认 2MB，超限跳过并记审计事件
- 上传用 `http.client`，1MB 分块流式读，不整文件入内存
- **失败只记录、下轮重试**，不弹窗、不写 stdout（静默是硬要求）

### 3.4 注册、"永久配对"与"快照还原自愈"

NOI Linux 常整机还原，机器上的凭据随还原消失。

```
装机（root 一次性）          机器首次开机                教师
bootstrap.key ──换凭据──▶   桌面「配对码.txt」 ──读码/输入──▶ 绑到名单里的**人**

还原后:  state_dir 里的 token 没了 → 用镜像里的 bootstrap.key 重新 enroll
        服务端按 machine_uuid 认回；uuid 也被抹掉就按 SMBIOS 指纹认回
        → 换发新 token，**配对关系原样保留**，教师不用再配
```

配对**绑人（`roster_entry`），不绑某场比赛的选手**，且**永久**：换场次只要新场次应用了那份名单，不用重配。
它取代了每选手注册码，因为一份镜像装遍机房时逐台发码不可行，两种模式并存又会有人选错。

**"已配对"不等于"能干活"**：场次里有没有这个人取决于名单是否已应用进场次，凭据因此有三个状态
（见 [`../reference/protocol.md`](../reference/protocol.md) §1）：

| `claimed` | `bound` | Agent 做什么 |
|---|---|---|
| `false` | `false` | 把六位配对码写到桌面，**不扫描**，等 tick |
| `true` | `false` | 写「等待场次.txt」（内容 = 服务端给的原因），**不扫描** |
| `true` | `true` | 正常循环：扫代码、传文件、收下发 |

- 第二行不是"注册失败"：否则 Agent 会反复重注册，而服务端每次注册都换新配对码，教师刚读到的码立刻失效。
- **未配对收到 `403` 绝不清 token 重注册**（凭据有效但没归属）；只有 `401` 才是凭据坏了。
- **密钥放置**：`bootstrap.key` 只给 root 读（`/etc/syncoj/bootstrap.key`，0600）；不能放 `agent.ini`，因为它 chown 给选手账号，泄露即"无限注册"。Agent 本体只读凭据，碰不到密钥。

### 3.5 systemd unit

两个单元分工不能混（后果见 ../design/04-server.md §4.6）：注册是 **root 的一次性单元**，服务本体以**运行账号**跑、只读凭据。

```ini
# syncoj-agent.service —— 常驻，以运行账号跑
[Unit]
StartLimitIntervalSec=300
StartLimitBurst=5              # 必须在 [Unit]：写在 [Service] 会被当未知键忽略

[Service]
Type=simple
ExecStartPre=-+/usr/bin/python3 -E -s /usr/lib/syncoj/install.py --boot-guard …   # root 跑启动守卫
ExecStart=/usr/bin/python3 -E -s /opt/syncoj/current/run_agent.py --config /etc/syncoj/agent.ini
StandardOutput=null            # 静默：不往终端/日志设备写任何东西
StandardError=journal
Restart=always                 # 卸载由 root 管理单元先 stop 再删，意外的 0 退出也要拉起来
RestartSec=5
SuccessExitStatus=75           # 切换版本（自更新激活 / 守卫回滚）的专用退出码：不算 failed
RestartForceExitStatus=75      # ……但仍然要重启，拉起新版本
User=<运行账号>                 # 默认 = 跑安装的那个账号；刻意不写 Group=（交给 NSS）
PrivateTmp=yes
PrivateDevices=yes
ProtectSystem=strict
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
RestrictNamespaces=yes
RestrictRealtime=yes
RestrictAddressFamilies=AF_INET AF_INET6
MemoryMax=200M
CPUQuota=25%
IOSchedulingClass=idle
NoNewPrivileges=yes
ReadWritePaths=-/home/noi -/opt/syncoj -/var/lib/syncoj

[Install]
WantedBy=multi-user.target
```

```ini
# syncoj-agent-enroll.service —— oneshot，以 root 跑一次（注册），带有限重试
[Service]
Type=oneshot
RemainAfterExit=no
User=root
ExecStart=/bin/sh -c '...(最多 3 次，每次失败打一句可操作的收尾话)...'
TimeoutStartSec=…              # 几次尝试加网络超时可能超过默认 90s，别让 systemd 中途杀掉
ProtectSystem=strict
ReadWritePaths=-/var/lib/syncoj
```

与常规写法不同之处：

* **`Restart=always`**：卸载由 root 管理单元先 stop 再删，Agent 不会以 0 退出后被拉回已删的文件，所以意外的 0 退出也该拉起；"反复起不来"的上限交给 `StartLimit*`。
* **启动守卫 `ExecStartPre=-+`**：每次启动前以 root（`+`）计数，新版本连续 2 次（`MAX_BOOT_ATTEMPTS`）没报平稳就把 `current` 切回上一版并记进 `rolled-back.json`；守卫失败不挡启动（`-`）。必须由 root 做，因为 Agent 写不进属 root 的 `/opt/syncoj`。
* **切换版本用退出码 75 而非 0**：自更新激活或守卫回滚后 Agent 退出、由 systemd 拉起新代码；用非 0 是因为旧单元的 `on-failure` 不重启 exit 0（否则铺开一个版本就让机房停摆）。新单元用 `SuccessExitStatus=75` 不显示 failed、`RestartForceExitStatus=75` 保证重启。代码常量 `main.EXIT_RESTART` 与 `install.py:AGENT_EXIT_RESTART`，测试对账。
* **不写 `Group=`**：交给 NSS 解析主组，否则主组与账号名不同的机器会撞 `217/GROUP`。
* **`NoNewPrivileges=yes`**：需要 root 的两件事（切换已验签的升级、按授权卸载）由 Agent 写进 `<状态目录>/handoff/request.json`，**root 管理单元**（`syncoj-agent-admin.path` 触发 `syncoj-agent-admin.service`，执行 `/usr/lib/syncoj/install.py --admin-handoff`，升级时不替换）no-follow 读取、自行验签，走装机 / 卸载同一条路。见 [ADR-0012](../decisions/ADR-0012-remote-uninstall-authorization.md) 与 [ADR-0008](../decisions/ADR-0008-systemd-unit-hardening.md)。
* **`ReadWritePaths=` 每条带 `-`**：路径不存在时跳过，否则设沙箱报 `226/NAMESPACE` 起不来。只有三条：运行账号家目录（安装时解析的绝对路径；不能写 `%h`，系统单元里它是 `/root`，见 [ADR-0007](../decisions/ADR-0007-unit-path-whitelist.md)）、安装根（自更新写 `releases/`）、状态目录（日志、凭据、哈希缓存）。
* **不列 `deploy_root` / `scan.roots` 的具体值**：它们可选、真机上可能不存在，默认值都在家目录下，多列一条就多一个 226 的机会。
* **注册单元不用 `Restart=`**：oneshot 的重启策略在各版本 systemd 上行为不一致；重试由 ExecStart 的 shell 循环自己数，并把手工恢复命令打进日志。

### 3.6 Python 3.8 兼容清单（代码评审硬规则）

开发机 Python 3.14、目标机 3.8，靠 `agent/tools/check_py38.py` 自动门禁。

| 不能用 | 替代 |
|---|---|
| `int \| None` (3.10+) | `Optional[int]` + `from __future__ import annotations` |
| `match` (3.10+) | `if/elif` |
| `dict1 \| dict2` (3.9+) | `{**dict1, **dict2}` |
| `str.removeprefix/suffix` (3.9+) | 切片 |
| `list[int]` 运行时求值 (3.9+) | `List[int]` 或加 `from __future__ import annotations` |
| `asyncio.TaskGroup` (3.11+) | `concurrent.futures.ThreadPoolExecutor` |
| `tomllib` (3.11+) | `configparser` |
| `zoneinfo` (3.9+) | `datetime.timezone.utc` |
| `functools.cache` (3.9+) | `functools.lru_cache(maxsize=None)` |
| `graphlib` (3.9+) | 自写拓扑排序 |

### 3.7 诊断回传（机器健康时就把现场留下）

机器离线后管理端什么也拿不到，所以 Agent 在**健康时**就定期回传现场。

| 触发 | `reason` | 节奏 |
|---|---|---|
| 健康时定期回传 | `periodic` | 每 600 秒一份 |
| 连续失败 / 升级失败后补发 | `error` | 连续失败 2 轮补一份；两次间隔 ≥ 60 秒 |
| tick 响应 `diagnostics_request=true` | `manual` | 管理端点「要一份」后的下一次心跳 |

硬约束（有测试）：

* **不带密钥与 token**：`config_summary` 按**白名单**构造（已知安全的路径、开关、节奏；`bootstrap.key` 只留路径），`credential.json` 内容不进包。
* **上传失败不影响心跳**：独立请求（15 秒超时），失败记 DEBUG，连续多次才升 WARNING。
* **服务端限速 60 秒一份**：收到 `429` 当"这次没传成"，不重试、不报错。

压缩后超过 256 KB 先丢日志尾部再压（骨架比日志重要）；`log_tail` 取最后 200 行与最后 64 KB 中更小者。
报文形状冻结在 `agent/syncoj_agent/diagnostics.py` 的 docstring；服务端见 `../reference/protocol.md` §6.1。

**待配对 / 没有场次的机器同样回传**（定期与被点名的两种）：卡在配对阶段的现场最有用，而这条心跳路径在 `cycle()` 里提前返回、不走正常代码段，必须单独处理。
