## 3. 客户端 Agent

### 3.1 为什么砍掉所有第三方依赖

| 原方案依赖 | 替代 | 收益 |
|---|---|---|
| `watchdog`（inotify） | 周期 `os.scandir` 全量扫描 | 免依赖；且 tick 本就上报全量索引，inotify 是冗余的 |
| `websockets` | HTTP 轮询 | 免依赖；去掉重连/半开检测/连接生命周期状态机 |
| `requests` / `httpx` | `http.client` + `urllib.request` | 标准库 |
| `pydantic` | 纯 dict + CI 契约测试 | 标准库 |
| `tomli`/`tomllib` | `configparser`（INI） | `tomllib` 是 3.11+，3.8 没有 |
| 本地队列 SQLite | 服务端持有状态，Agent 无状态 | 免依赖；崩溃/重装天然自愈 |

砍完之后的画像：**零第三方依赖、零编译、零 pip、无状态**。部署 = 拷一个目录 + 一个 systemd unit。

选手折腾 Python 环境的担忧用 `python3 -E -s` 解决（`-E` 忽略所有 `PYTHON*` 环境变量，`-s` 忽略 user site-packages）—— 选手怎么 `pip install` 都污染不到 Agent。

**代价**：常驻内存 30~40MB（Go 约 12MB，50 台无所谓）；同步延迟 = 轮询周期（需求已确认 20s 可接受）；依赖目标机有 `python3`（NOI Linux 必有，且 NOI 要考 Python）。

### 3.2 主循环（同步串行，不用 asyncio）

```python
while True:
    files  = scan(root)                       # os.scandir + sha256(mtime/size 缓存)
    tick   = post("/agent/tick", files, partials, stats)
    for f in tick["need_upload"]:
        upload(f)                             # ThreadPoolExecutor 并发 2
    for j in tick["deploy_jobs"]:
        download(j)                           # Range 续传 + 校验 + 原子落盘
    if tick["upgrade"]: handle_upgrade(tick["upgrade"])
    sleep(tick["next_tick_seconds"])
```

不用 asyncio：3.8 没有 `TaskGroup`，且纯 IO 串行 + 小并发线程池已足够，同步代码的异常处理与调试成本低得多。

### 3.3 扫描与上传

- `os.scandir` 递归，**只处理白名单后缀**（`c/cpp/cc/pas/java/py/...`）
- 排除 `.git`、`build`、`__pycache__`、隐藏文件、`*.swp`、`*~`、`*.tmp`
- **sha256 缓存**：`(path, mtime, size)` 不变则复用上次哈希
- 单文件上限默认 2MB，超限跳过并记审计事件
- 上传用 `http.client`，流式 1MB 分块读，不整文件入内存
- **失败只记录并下轮重试**，不弹窗、不写 stdout —— 静默是硬要求

### 3.4 注册、"永久配对"与"快照还原自愈"

NOI Linux 常做整机还原，机器上的凭据会随还原消失。

```
装机（root 一次性）          机器首次开机                教师
bootstrap.key ──换凭据──▶   桌面「配对码.txt」 ──读码/输入──▶ 绑到名单里的**人**

还原后:  state_dir 里的 token 没了 → 用镜像里的 bootstrap.key 重新 enroll
        服务端按 machine_uuid 认回；uuid 也没了（还原抹掉）就按 SMBIOS 指纹认回
        → 换发新 token，**配对关系原样保留**，不需要教师再配一次
```

配对**绑的是人（`roster_entry`），不是某场比赛的选手**，而且是**永久**的。
同一个学生换一场比赛不用重新配对，只要新场次应用了那份名单 —— 这是"注册码"
被整条取代的原因：逐台发码在"一份镜像装遍整间机房"的现实里根本不可行，
而两种模式并存意味着每种都要维护、测试，并且迟早有人选错。

**"已配对"不等于"能干活"。** 场次有没有这个人，取决于那份名单有没有被应用进
场次，所以客户端的凭据有三个状态，必须分开处理（见
[`../reference/protocol.md`](../reference/protocol.md) §1）：

| `claimed` | `bound` | Agent 做什么 |
|---|---|---|
| `false` | `false` | 把六位配对码写到桌面，**不扫描**，安静等 tick |
| `true` | `false` | 写「等待场次.txt」（内容 = 服务端给的原因），**不扫描** |
| `true` | `true` | 正常循环：扫代码、传文件、收下发 |

第二条特别容易漏：把它当成"注册失败"会让 Agent 反复重新注册，而服务端每次
注册都换一个新配对码 —— 教师刚在屏幕上读到的那个立刻失效。
同理，**未配对时收到 `403` 绝不能清 token 重新注册**，那是"凭据有效但没归属"，
只有 `401` 才是凭据坏了。

**装机时密钥怎么放。** `bootstrap.key` 只给 root 读（`/etc/syncoj/bootstrap.key`，
0600）。`agent.ini` 是 chown 给选手账号的，密钥放进去等于公开 —— 而它泄露意味着
"无限注册"。Agent 本体只读凭据，碰不到密钥。

### 3.5 systemd unit

两个单元，分工不能混（混了的后果见 ../design/04-server.md §4.6）：注册是 **root 的一次性单元**，
服务本体以**运行账号**跑、只读凭据。

```ini
# syncoj-agent.service —— 常驻，以运行账号跑
[Unit]
StartLimitIntervalSec=300
StartLimitBurst=5              # 必须在 [Unit]：写在 [Service] 会被当未知键忽略

[Service]
Type=simple
ExecStart=/usr/bin/python3 -E -s /opt/syncoj/current/run_agent.py --config /etc/syncoj/agent.ini
StandardOutput=null            # 静默：不往终端/日志设备写任何东西
StandardError=journal
Restart=on-failure             # 自卸载成功时 Agent 以 exit 0 退出，always 会把它拉回来
RestartSec=5
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
ReadWritePaths=-%h -/opt/syncoj -/var/lib/syncoj \
               -/etc/syncoj -/etc/systemd/system -/etc/sudoers.d -/usr/local/lib/syncoj

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

几条与常规写法不同的地方，各有原因：

* **`Restart=on-failure` 而不是 `always`**：远程卸载成功时 Agent 以 exit 0 正常退出，
  `always` 会立刻把它重新拉起来 —— 而那时 `/opt/syncoj`、`/etc/syncoj` 都已经删了，
  拉起来只会刷一堆"找不到文件"，现场看起来像"卸载失败"。崩溃仍然自愈；
  "反复起不来"的上限交给 `StartLimit*`（真机上见到过重启到 249 次）。
* **刻意不写 `Group=`**：systemd 按 NSS 解析该账号的主组。写死 `Group=<账号名>`
  会在"主组与账号名不同"的机器上撞 `217/GROUP`。
* **刻意不要 `NoNewPrivileges=yes`**：它会让 setuid 的 sudo 无法提权，
  `sudo -n <自卸载脚本>` 必然失败（`effective uid is not 0`），管理端授权的远程卸载
  整条链就是死的。服务以普通账号运行，而**那个账号本人在这台机器上同样能执行任何
  setuid 程序**，所以这一条对"防选手"没有增量价值。其余加固一条都没少。
* **`ReadWritePaths=` 里每一条都带 `-`**：路径不存在时 systemd 跳过它，而不是让单元
  在设沙箱时报 `226/NAMESPACE` 起不来。实例：单元里写了一条 `/home/student/code`，
  而那台机器没有这个目录。前三条是服务自己要写的地方（家目录、安装根、状态目录）；
  后四条只服务于"以 root 执行远程卸载"（../design/07-ops-and-install.md §7.4）：
  它们是 root:0755/0644，运行账号在 DAC 上写不进去，所以**放开 mount 层面的写权限
  不构成提权**，唯一用得上的是验过令牌的那个 root 脚本。
* **刻意不列 `deploy_root` / `scan.roots` 的具体值**：它们是可选配置、真机上可能
  不存在，而默认值都在 `%h` 之下（已经被覆盖）。多列一条就多一个 226 的机会。
* **注册单元刻意不用 `Restart=`**：oneshot 的重启策略在各版本 systemd 上行为不一致；
  "试几次"由 ExecStart 里的 shell 循环自己数，并把手工恢复的命令打进日志。

### 3.6 Python 3.8 兼容清单（代码评审硬规则）

开发机是 Python 3.14，目标机 3.8 —— 靠 `agent/tools/check_py38.py` 自动门禁，不靠人眼。

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

机器一旦离线，管理端再也拿不到任何东西。所以 Agent 在**健康的时候**就定期把现场
回传给服务端 —— 出事时至少还有"最后一份"。

| 触发 | `reason` | 节奏 |
|---|---|---|
| 健康时的定期回传 | `periodic` | 每 600 秒一份 |
| 连续失败 / 升级失败后的补发 | `error` | 连续失败 2 轮补一份；两次之间至少隔 60 秒，一次故障不会刷十份 |
| 服务端在 tick 里下 `diagnostics_request=true` | `manual` | 管理端点「要一份」之后的下一次心跳 |

三条硬约束（都有测试盯着）：

* **绝不带密钥与 token**：`config_summary` 按**白名单**构造（只列已知安全的路径、
  开关、节奏；`bootstrap.key` 只留路径），`credential.json` 的内容一个字节都不进包。
* **上传失败绝不影响心跳**：它走独立的一次请求（15 秒超时），失败只记 DEBUG，
  连续多次才升到 WARNING。
* **服务端限速 60 秒一份**：Agent 收到 `429` 就当"这次没传成" —— 不重试、不报错，
  下一轮自然再来。

包体压缩后超过 256 KB 时先丢掉日志尾部再压一次（诊断包的骨架比日志重要）；
`log_tail` 只取最后 200 行与最后 64 KB 里更小的那个。报文形状冻结在
`agent/syncoj_agent/diagnostics.py` 的 docstring 里；服务端那半见
`../reference/protocol.md` §6.1。

**待配对 / 没有场次的机器同样回传**（周期性的那份，以及被点名的那份）：它卡在配对
阶段的现场恰恰最有用，而那条心跳路径在 `cycle()` 里**提前返回**，与干得了活的机器
走的不是同一段代码 —— 少了这一条，一台配不上的机器在管理端永远看不到任何现场。

