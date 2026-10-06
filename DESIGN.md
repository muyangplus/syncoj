# SyncOJ 技术方案

> 本文件是 v1 的权威设计。改动请同步更新本文件与 `docs/protocol.md`。

## 0. 已锁定决策

| 项 | 决策 | 备注 |
|---|---|---|
| 目标环境 | NOI Linux 2.0（Ubuntu 20.04 / glibc 2.31 / systemd 245 / Python 3.8.10） | v1 仅 Linux，平台层预留 Windows |
| Agent 技术栈 | Python 3.8 + **零第三方依赖**，`python3 -E -s` 启动 | 见 §3.1 为何砍依赖 |
| 通信模型 | **HTTP 自适应轮询**（空闲 60s / 有活 2s），无 WebSocket | 同步延迟容忍度 20s |
| 服务端 | FastAPI + uvicorn + SQLAlchemy 2.0 + SQLite(WAL)，单机 | |
| 前端 | Vue3 + Vite + Pinia，TS 类型从 OpenAPI 生成 | M3 起 |
| 评测对接 | 只投递 `source/` + 扫描结果目录回写成绩，**不触发评测** | 开场仍由教师点 LemonLime/Arbiter GUI |
| 认证 | 每机独立 Token（服务端只存 `sha256(token)`） | |
| 机器身份 | `machine_uuid`（首选）+ SMBIOS 指纹（快照还原兜底）；`machine_id` 只供人工辨认 | 见 §3.4 / §4.6 |
| 数据库 | SQLite WAL，内存态优先 + 批量落库 | |
| 场次 | 多场次隔离（`contest_id`）；机器绑的是**人**，场次动态解析 | 见 §4.6 |
| 防篡改 | 完整性校验 + 审计日志，不干预选手 | |
| 自更新 | **RSA-2048 + PKCS#1 v1.5 (SHA-256)** 签名校验，**默认关闭**，教师端显式铺开 | 见 §6.1 为何不是 Ed25519 |
| 后台账号 | 单管理员 | |
| 装机 | 镜像预装 / 离线包 / 在线自举，同一份幂等逻辑 | |
| 网络 | 考场同一内网，双向可达 | Ansible 留作以后赛前批量装机的备选，v1 不用 |
| 目录约定 | `桌面/<准考证号>/<题目名>/<题目名>.cpp`；题面/样例默认下发到**桌面根目录**，**以 zip 文件形态原样落盘，服务端不解压** | 只是**默认值**，三层皆可改，见 §4.5 |
| 代码归题 | 每道题一组 glob 模式，**第一条命中者胜出**；归属是算出来的、不落库 | 见 §4.5 |
| 配对方式 | 镜像统一密钥注册 + **六位数字配对码**绑定到名单条目（人）；**配对永久，取代每选手注册码** | 见 §4.6 |
| 名单 | 全局可复用的 `Roster`，场次**复制**一份进来（不是引用） | 见 §4.7 |
| 管理端 API | 统一列表信封 `{items,total,limit,offset}` + 统一错误体 `{detail,code,details}` | 见 §5.2 / `docs/api-conventions.md` |
| 前端 | 共享数据层（`useList`/`useMutation`）+ 通用页面外壳，页面只描述长相不描述取数 | 见 §5.3 |
| 删除语义 | 结构性数据硬删 + **输入名称确认**；产物性数据（代码/资产）软删留墓碑 | 见 §5.4 |
| 库结构演进 | `PRAGMA user_version` + 有序迁移；重建表需要一套已验证的安全配方 | 见 §4.8 |

## 1. 架构总览

```
┌──────────── 教师 ────────────┐
│  Vue3 SPA  ──HTTPS──> uvicorn│
│  （由服务端 StaticFiles 托管，│
│    不额外引入 Caddy/nginx）   │
└──────────────┬───────────────┘
               │
┌──────────────▼──────────────────────────────────┐
│  SyncOJ Server (uvicorn, 单进程)                │
│   API:  /api/v1/admin/*   /api/v1/agent/*       │
│   内存: AgentRegistry(在线状态，读路径)          │
│   后台: 状态批量落库 / 结果扫描                  │
│   SQLite WAL + blobs/(内容寻址) + source/       │
└──────────────┬──────────────────────────────────┘
               │  HTTP 轮询 (2s ~ 60s 自适应)
┌──────────────▼──────────────────────────────────┐
│  SyncOJ Agent  (systemd, Python 3.8, 零依赖)    │
│   扫描 → 上传 → tick → 下载 → 自更新             │
└─────────────────────────────────────────────────┘

LemonLime / Arbiter (GUI, 教师手动操作)
   读 source/<contest>/<player_no>/...  →  写 judge_result/  →  服务端扫描回写
```

## 2. 核心协议：`tick`

整个系统只有一个关键接口。**服务端持有全部状态，Agent 零状态** —— 这是轮询方案的支点。
完整字段规范见 [`docs/protocol.md`](docs/protocol.md)。

```jsonc
// POST /api/v1/agent/tick   (Authorization: Bearer <token>)
// 请求
{
  "agent_version": "0.1.0",
  "machine_id": "b3f1...",              // /etc/machine-id 派生
  "ts": 1767225600,
  "scan_root": "/home/student/code",
  "scan": [                             // 本周期全量扫描的索引，不含内容
    {"path": "main.cpp", "sha256": "ab12...", "size": 1234, "mtime": 1767225500}
  ],
  "partials": [{"asset_id": 42, "bytes_done": 3145728}],  // 未完成的下载
  "stats": {"disk_free": 10737418240, "last_error": null, "queue": 0}
}

// 响应
{
  "server_time": 1767225600,
  "next_tick_seconds": 20,              // 自适应：有活 2s，空闲 60s
  "need_upload": ["main.cpp"],          // 服务端没有 / 版本落后的文件
  "deploy_jobs": [
    {"asset_id": 42, "url": "/api/v1/agent/assets/42",
     "sha256": "cd34...", "size": 10485760,
     "dest": "exam/testdata.zip", "offset": 3145728}   // 断点续传位置
  ],
  "cancel_assets": [],
  "upgrade": null,                      // 或 {version, url, sha256, sig}
  "config": {"scan_interval": 60, "max_file_size": 2097152}
}
```

**为什么每次 tick 上报全量索引**：50 个文件 × ~100 字节 = **5KB/次**，20s 一次可忽略。收益：

- 服务端**免费**得到完整文件台账（谁有什么、何时变更、什么版本）
- **删除检测免费**：上一轮有、这一轮没了 = 选手删了 → 记审计事件
- 比 inotify 更可靠：Agent 停机期间的事件不会丢，因为比对的是快照而非事件流
- 不需要 `fs.inotify.max_user_watches` 调优（Ubuntu 20.04 默认仅 8192）

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

**代价（诚实记录）**：常驻内存 30~40MB（Go 约 12MB，50 台无所谓）；同步延迟 = 轮询周期（需求已确认 20s 可接受）；依赖目标机有 `python3`（NOI Linux 必有，且 NOI 要考 Python）。

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

NOI Linux 常做整机还原，机器上的凭据会消失 —— 刚需，不是加分项。

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
场次，所以客户端的凭据有三个状态，必须分开处理（见 `docs/protocol.md` §1）：

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

```ini
[Service]
Type=simple
ExecStart=/usr/bin/python3 -E -s /opt/syncoj/agent/main.py
Restart=always
RestartSec=5
User=syncoj
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ReadWritePaths=/var/lib/syncoj /home/student/exam
ReadOnlyPaths=/home/student/code
MemoryMax=200M
CPUQuota=25%
IOSchedulingClass=idle
StandardOutput=null          # 静默：不往终端/日志设备写任何东西
StandardError=journal
```

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

## 4. 服务端

### 4.1 目录布局

```
<data_root>/
├── syncoj.db
├── blobs/<sha[:2]>/<sha>          # 内容寻址，天然去重
├── source/<contest_slug>/<player_no>/<problem>/main.cpp    # 评测器直接读
├── judge_result/<contest_slug>/<player_no>/<problem>/      # 结果扫描源
└── logs/
```

### 4.2 数据模型

| 表 | 作用 |
|---|---|
| `contest` | 场次（draft/running/frozen/closed） |
| `player` | `(contest_id, player_no, name, seat)`。**不随机器出现自动创建** —— 教师应用名单才产生 |
| `roster` / `roster_entry` | 全局可复用的名单（人）。机器永久绑的是 `roster_entry` |
| `bootstrap_key` | 镜像内置的统一注册密钥（只存哈希、可吊销、带过期） |
| `agent` | 每机 Token 哈希、`machine_uuid`、指纹、`roster_entry_id`、可选 `contest_id`、配对码哈希、吊销状态 |
| `agent_status` | 在线状态快照，`last_tick_at`、指标 |
| `source_file` | 回收台账：`(contest, player, path, sha256, size, revision, first_seen, last_seen)` |
| `asset` | 内容寻址的下发文件 |
| `deploy_task` / `deploy_target` | 下发任务与逐选手进度（含 `bytes_done`） |
| `judge_run` | 成绩回写 |
| `event_log` | 审计事件 |
| `agent_release` | Agent 版本与签名 |
| `admin` / `admin_session` | 管理员账号与会话 |

已删除的表：`enroll_code`（每选手注册码）、`machine_claim`（未配对机器另立表）。
后者的语义已经并回 `agent` —— 未配对就是 `agent.roster_entry_id IS NULL`，
不必再多一张要同步的表。

### 4.3 写入策略（SQLite 的命门）

50 台 × 每 20s 一次 tick = **2.5 req/s**。每次 tick 都要 upsert 在线状态 + 比对文件台账，**绝不能每个请求开事务写库**：

- **内存优先**：`AgentRegistry` 用 dict 持有实时状态，Web 读路径走内存（后续经 SSE 推送）
- **批量落库**：后台任务每 5s 把脏数据合并成**一个事务**刷入
- **单写连接**：所有写操作走一个队列 + 独立线程，避免 `database is locked`
- SQLite 参数：`journal_mode=WAL`、`synchronous=NORMAL`、`busy_timeout=5000`、`foreign_keys=ON`

### 4.4 评测结果回写

后台 `ResultScanner` 每 10s 轮询 `judge_result/`，`mtime` 变化才解析。解析器做成可配（`glob` + 格式），先支持通用 JSON/XML/文本模板，拿到 LemonLime/Arbiter 真实输出样例后再补精确解析。Web 端按「选手 × 题目」矩阵展示分数，可导出 CSV。

**保守解析**是硬规则：宁可漏解析（标 `unparsed` 让教师手工补录），也不能错解析 —— 一个悄悄算错的分比一个显眼的空更难发现。手工录入的成绩**永不被自动扫描覆盖**（连"强制重扫"也不行）。

矩阵刻意区分四种状态：`ok` / `unparsed` / `manual` / `missing`。再往下 `missing` 还要分「**已交未评测**」和「**未交**」—— 都显示成「—」的话，教师没法判断该等还是该去催人。前者由 `source_file` 按当前模式实时算出来（`submitted` 字段）。

### 4.5 代码归题与目录约定

目录约定**只是默认值**，三层各自独立可配：

| 层 | 配置 | 默认 | 管什么 |
|---|---|---|---|
| ① Agent | `deploy_root` / `scan.roots` / `scan.prefix` | `{desktop}` / `{desktop}/{player_no}` / `none` | 下发落哪、从哪收、上报路径加不加前缀 |
| ② 服务端 | `SYNCOJ_DEFAULT_FILE_PATTERN` | `{ident}/**` | 题目**没配**模式时怎么认领 |
| ③ 每道题 | `problem.file_patterns` | 跟随 ② | 这道题**特意**怎么认领 |

Agent 侧路径模板支持 `{desktop}` / `{home}`（载入即展开）与 `{player_no}` / `{contest_slug}`（注册后展开）。占位符展开不了时**原样保留**而不是报错 —— 配置校验发生在注册之前，报错会把"还没注册"说成"配置错了"。

归题的几条硬规则：

1. **glob 语义，不是 `fnmatch`**：`*` 不跨 `/`，`**` 跨，`?` 单字符，`[abc]`/`[!abc]` 字符类。`fnmatch` 的 `*` 会吞掉 `/`，教师就没法表达"只在这个目录下"。
2. **第一条命中的题目胜出**，顺序即题目清单顺序。不做"最具体者优先"这类隐式判断 —— 归错了必须能一眼看出是哪条规则接的。
3. `{ident}` / `{title}` 是**活占位符**，匹配那一刻才展开。改标识/标题后模式自动跟着走；`{title}` 在标题留空时退回标识。
4. **归属是算出来的，不落库**：改模式立刻生效，不需要重收文件，也不存在"旧归属"脏数据。
5. 认不出来就是 `None`，不兜底塞给某道题 —— 假归属比没有归属更难发现。
6. 界面上的「路径试算」调服务端接口，**不前端再实现一遍 glob**。两份实现迟早会不一致，而那时教师无法判断是配错了还是显示错了。

### 4.6 注册与配对：一条路

原有的「一选手一注册码」适合临时加人、小规模现场发码；但它**没法规模化** ——
整间机房用同一份镜像时，逐台发码等于把 50 次人工塞进开考前那十分钟。
所以**注册码整条链路被删除**，不是并存，是取代：两种模式并存意味着每种都要
维护、测试，并且迟早有人选错。

留下的只有一条路：机房镜像里放**一份**统一密钥，机器开机时用它换回本机凭据。

```
装机（root 一次性）        机器首次开机              教师
bootstrap.key  ──换凭据──▶  桌面「配对码.txt」 ──读码/输入──▶ 绑到名单里的**人**
```

代价是服务端**不知道这台机器是谁**，所以要有一个把身份补回来的动作 —— 配对，
也就是整条链路上唯一需要人到场确认的环节。

几条定死的规则：

1. **密钥只给 root 读**（`/etc/syncoj/bootstrap.key`，0600）。`agent.ini`
   chown 给了选手账号，密钥放进去等于公开 —— 而它泄露意味着"无限注册"。
2. **配对码是六位数字**，只存哈希，限时 30 分钟，**一次性**，绑定成功即刻作废。
   它只在"绑定的那一刻"用；配对之后认机器一律靠 `machine_uuid`。
   六位数字是刻意的：它要在机器屏幕和教师眼睛之间传递，越长越抄错，
   而"限时 + 一次性 + 管理员鉴权"已经把爆破空间压没了。

   **每一次 tick 都要把这个码发回去**（`claimed=false` 的机器），所以明文必须
   在服务端内存里留到过期或配对为止 —— 库里只有哈希，推不回明文。缓存随进程
   消失，绝不落库、绝不落磁盘；重启后下一次 tick 直接发新码，桌面文件跟着更新。
   之所以不让 Agent 自己保管上次收到的码：那样"桌面上的码还准不准"就变成一个
   客户端状态问题，而 Agent 的状态目录恰恰是最先被快照还原抹掉的东西。见
   `docs/protocol.md` §3。
3. **未配对状态不另立表**，就是 `agent.roster_entry_id IS NULL`。
   多一张表就多一处要同步的状态，而"未配对"本来就是机器的一种状态。
4. **未配对的凭据是有效凭据**，只是没有归属。把它当成"注册失败"会让 Agent
   反复重新注册，服务端每次都换新配对码 —— 教师刚读到的那个立刻失效。
5. **一人一机、一机一人**，服务端双向强制。两台机器绑同一个人，代码会往同一个
   目录里写，而且完全静默（成绩矩阵只看起来"这个人交了两遍"）。覆盖已有绑定
   必须走显式的「改派」，不能让"再输一次配对码"悄悄换掉一个人的身份。
6. **场次是动态解析的**，因为绑的是人。服务端找"这个 `player_no` 在其中、
   且 `is_active` 的场次"：0 个 → 「还没有含你的场次」；多于 1 个 → 「需要指定
   场次」，**绝不自动挑一个**。教师也可以在机器上显式钉一个 `Agent.contest_id`
   覆盖这个解析。

**快照还原怎么自愈。** 机器上的凭据会随整机还原消失，连自己生成的 UUID 也一起
没了，所以注册时还要带**硬件指纹**（SMBIOS UUID，还原后不变）。服务端靠它认回
原来那台机器、保留配对关系 —— 否则每还原一次就要教师重新配对一次，方案直接
不可用。

但自动认回有一条**硬前提：那台机器当前必须离线**。指纹只证明硬件相同，
而克隆镜像的机器报的也是同一个指纹；母机还在心跳时来了一台同指纹的机器，
更可能是克隆 —— 无条件认回等于把那个学生的身份白送出去，且完全静默。
所以在线时不认回，改走人工配对，并留下告警。

「多台机器共用同一个硬件指纹」这个信号本身也暴露给教师（「机器配对」页的克隆
告警）：它几乎总意味着镜像是在某台机器**跑过之后**才克隆的。

**认机器的顺序**是刻意定的：

| 顺序 | 依据 | 为什么 |
|---|---|---|
| 1 | `machine_uuid` | 配对之后的常态。克隆镜像若做了通用化，UUID 各不相同，不会互相冒领 |
| 2 | `machine_fingerprint`（要求原机器离线） | 快照还原把 UUID 抹掉了，只有硬件指纹还在 |
| 3 | 都对不上 → 新机器 + 配对码 | 宁可靠人确认一次，也不猜 |

`machine_id`（`/etc/machine-id`）**不参与身份判定**，只用于列表里显示给人看 ——
克隆镜像没做通用化时它是整批相同的，拿它当身份等于让整批机器共用一个身份。

### 4.7 名单：模板，不是引用

`Roster` 是"这个班/这个考点有哪些人"的事实，跨场次复用；`Player` 是"这场比赛
谁在参赛"，成绩、代码、下发全挂在它身上。

两者的关系刻意做成**一次性复制**。让场次直接引用名单看起来更优雅，但会让
"改一下名单"顺带改掉历史场次的参赛者 —— 那时成绩矩阵的列、代码目录、下发目标、
既有成绩的外键全都会跟着动。教师想要的从来不是这个效果。

应用名单时 ``prune`` 默认关闭；**即使显式打开，已经有代码或成绩的选手也一个都不删**，
并在结果里列出来告诉教师"这些人删不掉、也不该删"。名单调整是常事，
顺手把参赛者的提交一起删掉是不可逆的事故，而且它不报错。

**「默认名单」只是预设，不是"选好就自动补人"。** 这个区别值得单独写下来，因为它
在现场真的会被当成 bug：教师在场次设置里选好默认名单、保存、去选手状态 —— 空的。
（真实反馈："配置/修改了默认名单后也没有修改选手"。）

结论是**不改语义、改界面**：选名单的地方必须把**动作**摆出来（「按这份名单补人」，
只补人不删人），而不是写一句"要落到场次请到「名单库」点『应用』"—— 那是解释，
不是出路，而它把人指向另一个页面之后就没有下一步了。选手状态页的空状态里也给
同一个按钮，因为那里正是教师发现"怎么一个人都没有"的地方。

`test_setting_the_default_roster_does_not_touch_players` 把这个语义钉住：设/改默认
名单之后 `player_count` 必须还是 0，显式应用之后才变 1。否则哪天有人为了"顺手修掉
这个疑惑"把 `PATCH` 改成自动补人，语义变了而没有人会发现。

### 4.8 库结构演进

`Base.metadata.create_all()` 只建缺失的表，看不见"已有表少了一列"。所以有了
`migrations.py`：`PRAGMA user_version` + 一串"从 N 到 N+1"的步骤，幂等、
每步一个事务、**失败不推进版本号**（下次启动重试同一步，绝不跳过）。

**重建表（rebuild）的安全配方。** 删一列、改一次可空性，在 SQLite 里都只能
"建新表 → 搬数据 → 换名"。本方案一开始写的是"绝不重建"（怕关不掉外键，让
`DROP TABLE` 顺着 `ON DELETE CASCADE` 把 `agent_status` 删光）。但那等于永久
放弃改结构，代价太大，所以改成**把配方定死并加看守测试**：

```python
raw = engine.raw_connection()
raw.isolation_level = None          # 自动提交：PRAGMA 才不是"事务里的空操作"
raw.execute("PRAGMA foreign_keys=OFF")
raw.execute("PRAGMA legacy_alter_table=ON")   # 否则 RENAME 会重写引用方
raw.execute("BEGIN"); ... ; raw.execute("COMMIT")
raw.execute("PRAGMA foreign_keys=ON")
```

三条必须同时成立，少一条就出事：

1. **`isolation_level = None`（自动提交）**。`PRAGMA foreign_keys` 在事务里是
   **空操作** —— 用 `engine.begin()` 包着设它，看起来成功、其实没生效，而
   外键还开着时 `DROP TABLE agent` 会级联清掉 `agent_status`。这个坑只有把
   要守的那行改坏才会暴露，所以 `test_legacy_rebuild_keeps_child_table_rows`
   专门断言子表数据在重建后仍然存在。
2. **`legacy_alter_table=ON`**。新版 SQLite 的 `ALTER TABLE ... RENAME` 会去
   改写所有引用旧名的外键/视图/触发器，中间态下会把引用写歪。
3. **不需要的部分别做**。DDL 用文本改写（`CREATE TABLE x (` → `CREATE TABLE
   x__new (`）而不是 `to_metadata()` —— 后者会静默不拷贝某些东西（只发一条
   SQLAlchemy 警告），警告在 CI 日志里没人看。

重建之后仍然要有"没重建的部分不能腐烂"的看守：模型新增了列而既不在老结构里、
也没被声明为迁移要补的，直接变红 —— 最危险的不是迁移写错，是**根本没写**。

迁移测试的重点同样不是"新库能建起来"（那几乎总会过），而是**老库能不能安全
升上来**：数据一行不少、关联表没被级联吃掉、跑第二遍是空操作、全新库与升级库
结构一致。

开发阶段库结构还在动，所以另给一条逃生通道：`syncoj-server db reset --yes`
直接重建空库（可选 `--keep-admin`）。有了它，就没必要写"兼容一切旧结构"的
迁移 —— 这也是本轮敢做破坏性改动的底气。

## 5. 接口约定与前端

### 5.1 协议一致性靠契约测试，不靠共享模型

Agent 零依赖，**不能 import pydantic**。所以一致性不靠共享模型，靠 **CI 契约测试**：

```python
# server/tests/test_agent_contract.py
def test_tick_payload_matches_schema():
    raw = subprocess.run(["python3", "agent/tools/build_fixture.py"],
                         capture_output=True, text=True).stdout
    TickRequest.model_validate(json.loads(raw))     # 不匹配就红
```

`agent/tools/build_fixture.py` 用 Agent 自己的纯标准库构造器生成真实 payload。
服务端 schema 一改、契约一破，CI 立刻拦住。

### 5.2 管理端 API：一个信封、一个错误体

完整约定见 [`docs/api-conventions.md`](docs/api-conventions.md)。两句话概括：

```jsonc
// 所有列表
{ "items": [ … ], "total": 137, "limit": 50, "offset": 0 }

// 所有错误
{ "detail": "这句是给人看的中文", "code": "roster_entry_taken", "details": { } }
```

改这一层的真实原因：这一轮之前，同一件事在 74 个接口里有好几套写法 ——
列表有的裸数组、有的带 `items`；错误的 `detail` 有时是字符串、有时是对象、
422 又是 pydantic 的英文结构，前端只能逐个接口写适配，于是"少写一个入口"和
"这个接口忘了处理"成了常态。约定的价值不在好看，在于**前端能写一次数据层**。

两条具体规则值得单独说：

- **`detail` 永远是字符串**，而且是一句能直接贴到提示条上的中文；
  422 由处理器揉成"每页条数太大了（最大 500）；考号不能为空"，原始错误列表
  留在 `details.errors` 里给表单标红框用。
- **500 必须有兜底处理器**。没有它时 Starlette 返回纯文本 `Internal Server
  Error`，前端 `response.json()` 当场抛 `SyntaxError`，界面上显示的是
  "Unexpected token I" 而不是任何有用的东西。
- **码表要能被静态检查**。`ERROR_CODES` 是唯一清单，`CODE_BY_STATUS` 是"没显式
  给码时按状态码推"的兜底表，四条规矩（发出的码必须登记 / 登记的必须真会发出 /
  兜底码必须登记 / 用得到的状态码必须有兜底）全部由 AST 扫描守着，不是靠人记得。
  这不是洁癖：写这套检查的当下就抓出两个真问题 —— 410 有三个接口在用（"该版本
  已撤回"就在其中）却没有兜底码，于是这类响应带的是毫无含义的 `code: "error"`；
  而 400 的兜底码 `bad_request` 根本没登记，也就是说**最常见的错误状态是唯一
  没有文档的那种**。反面同样抓出 8 个登记了却永远发不出来的码（`contest_frozen`、
  `roster_in_use`、`last_admin` …），它们描述的是设计时想象中的场景，而对应的
  校验要么不存在、要么当初就选了更宽容的做法 —— 死码最坏的地方不是占地方，
  是它**会被当成契约**，前端照着写分支、文档照着写章节。

### 5.3 前端架构

类型链路是单向的，只有一处真相源：

```
server/syncoj_server/schemas.py        ← 类型的唯一来源
        │  python server/tools/dump_openapi.py
        ▼
web/openapi.json
        │  npm run gen:types  (openapi-typescript)
        ▼
web/src/api/schema.d.ts                ← 生成物，不要手改
```

页面代码**只描述"这一页长什么样"，不描述"怎么取数"**：

| 文件 | 职责 |
|---|---|
| `api/client.ts` | `request`/`query`/`download`，把错误体转成带 `code` 的 `ApiError` |
| `api/endpoints.ts` | 所有路径的唯一定义 —— 服务端改路径时只需要改这一处 |
| `api/crud.ts` | 分页、信封、删除语义的公共实现 |
| `api/<资源>.ts` | **一个资源一个模块**，只有「路径 + 参数 + 返回类型」的映射 |
| `api/index.ts` | 入口，几乎全是 re-export；页面只从这里 import |
| `composables/useList.ts` | 分页、筛选、加载/错误、刷新、批量选择、可中断的请求 |
| `composables/useMutation.ts` | 执行写操作、弹提示、刷新列表、管理按钮 pending 态 |
| `components/` | `PageShell` / `DataTable` / `ConfirmByNameDialog` / `FormDialog` |

新增一个资源 = 一个资源模块 + 一个页面（外加在 `index.ts` 里加一行转出），
而不是再抄一遍 loading/error/分页/轮询。这是上一版前端的真实问题：每个 view 都
手写了一遍同样的十几行，于是加一个入口的成本高到没人愿意加。

> 拆开而不是堆在一个 `index.ts` 里，是为了让"这个资源有哪些动作"一眼能看完。
> 十几个资源的七十七个方法挤在一个文件里时，**漏接一个入口**和**看漏一行**是
> 同一件事 —— 而"很多功能没有入口"正是这一轮要解决的用户诉求。

### 5.4 删除语义

判断标准是"删掉之后还有没有人需要它"：

- **结构性数据**（名单/场次/选手/机器/题目）→ 硬删除，而且**确认方式是输入名称**
  （`confirm` 必须逐字等于 `player_no` / `slug` / `name` / `hostname`），
  校验在服务端做，不只是前端弹窗。用"是否确定"的弹窗在连续操作里会被手指
  肌肉记忆点掉，打名称不会。
- **产物性数据**（代码文件/下发资产）→ 软删除留墓碑，`?include_deleted=true`
  可见；导出**默认包含已删除的代码**，因为考场现实是"选手确实交过，只是后来
  被清了"，成绩复核时这份历史比干净的数据重要。

### 5.5 下发资产一律是文件，不解压

题面与样例本身就是 zip，服务端**只负责搬运字节**，不打包也不解压：选手或
评测机自己解压。密码不经过系统 —— 需要密码时，教师把一个 `password.txt`
当普通资产一起下发，它就会像别的文件一样落到桌面上。

这条约束换来的是服务端零压缩/加密依赖（Python 标准库能读加密 zip，写不了），
也换来"落盘字节与教师手里那份逐字节相同"这个可验证的性质。

## 6. 安全

| 面 | 措施 |
|---|---|
| 传输 | HTTPS，内网自签 CA；Agent 固定 CA 指纹（证书 pinning） |
| 认证 | 服务端只存 `sha256(token)`；按机吊销；机器身份靠 `machine_uuid`（首选）+ 硬件指纹（兜底） |
| 配对 | 六位数字配对码只存哈希、限时 30 分钟、一次性、需管理员鉴权。知道码就能认领一台机器，而认领一台机器就是决定"谁的成绩算谁的" |
| 授权 | token 只能访问本人场次与资源；下载 asset 必须属于自己且被 `deploy_target` 引用 |
| 路径 | 服务端**独立**做 `resolve()` + 白名单校验，绝不信任 Agent 上报的相对路径 |
| 完整性 | 上传/下载双向 SHA256；升级包额外 RSA 签名（见 §6.1） |
| 本地密钥 | 开发机放 `.key/`（两道 gitignore + 0600/0700），**有则自动加载、无则升级整体关闭**；生产用 `SYNCOJ_KEY_DIR` / `SYNCOJ_RELEASE_KEY` 指到服务端账号专属目录（见 §7.1） |
| 隔离 | 服务端专用用户；`source/` 对评测器只读；Agent 非 root + `NoNewPrivileges` |
| 审计 | 登录、注册、下发、回收、文件消失、批量重写、升级全进 `event_log` |

⚠️ **路径校验必须在服务端独立实现一遍** —— Agent 跑在选手机器上，随时可能被替换。

### 6.1 为什么签名不是 Ed25519（方案修正）

本方案原定 Ed25519。实现阶段发现一个硬阻塞：**Python 3.8 标准库不含任何非对称
签名原语**（`hashlib` 只有摘要，没有椭圆曲线），而 Agent 必须零依赖。要用
Ed25519 就得引入 `cryptography`/`pynacl`，直接违背零依赖约束。

改用 **RSA-2048 + PKCS#1 v1.5 (SHA-256)**：

| | Ed25519 | RSA-2048 |
|---|---|---|
| 纯 Python 验证的实现量 | 需要完整的 Curve25519 点运算，数百行大整数运算 | 一次 `pow(sig, 65537, n)` + 字节比较，几十行 |
| 手写风险 | 高 | 低（公钥指数只有 17 位，且不涉及曲线算术） |
| 签名体积 | 64 字节 | 256 字节 |

密钥与签名的具体约定：

- **签名对象是 sha256 的十六进制字符串**，不是包本身的字节。安全性等价
  （PKCS#1 内部本来也是先哈希再签名），但验证只需流式算一遍摘要 ——
  内存占用与包大小解耦，否则 Agent 的 200MB 预算会被 256MB 的包撑爆。
- **严格校验**：整体重建 `EM` 后恒定时间比较，不做"解析出 hash 再比对"。
  PKCS#1 v1.5 历史上的伪造攻击（Bleichenbacher 2006 等）**全部**源于宽松校验。
- 密钥生成 shell 到 `openssl`：Miller-Rabin 与随机素数搜索是好写但极易写错的
  密码学代码，而这是一次性管理操作，依赖 openssl 完全可接受。
- 正确性证据来自 **openssl 交叉验证**：我们的签名与 openssl 逐字节相同，
  openssl 的签名我们的代码能验。自签自验永远能过，证明不了任何事。

## 7. 运维与装机

安装器做成**幂等三入口、同一份逻辑**：

```bash
install.sh --mode=offline|online|image
```

- 检测已装版本 → 存在则原地升级，不存在则全量安装
- 幂等：重复执行结果一致；凭据丢失自动重新 enroll（配对关系**不受影响** —— 那
  是服务端按 `machine_uuid` / 硬件指纹认回来的，不依赖机器上的任何文件）
- 离线包 = tar.gz（Agent 目录 + systemd unit + 配置模板），不需要 pip、不需要网络
- 在线自举 = 拉 tar.gz + 校验 sha256 后走同一条路径

装机时容易踩的三个坑（都已修，留在这里免得改回去）：

1. **`bootstrap.key` 不能写进 `agent.ini`** —— 后者 chown 给选手账号，等于公开密钥
2. **写文件必须显式 LF**。Windows 上 `Path.write_text()` 会把 `\n` 翻成 `\r\n`，
   systemd 解析 unit 文件时直接失败，而错误信息完全指不到行尾符
3. **写完要 chown 给运行账号**。装完起不来最常见的原因就是 Agent 读不到自己的
   配置文件，而报错只说"权限不足"，指不到是哪个文件

### 7.1 密钥从哪来：`.key/`

开发时最烦的一件事是"为了试一次升级，先手工 genkey、再手工指向私钥、再手工把
公钥拷进包里"。所以让 `init` 直接把两把密钥都建好：

```
.key/                              ← 默认密钥目录（整个目录不进版本库）
├── release-key.pem                发布签名私钥  0600
├── release-key.pub.json           对应的公钥（装进离线包/镜像）
└── bootstrap.key                  统一注册密钥  0600（哈希同时入 bootstrap_key 表）
```

| 环节 | 行为 |
|---|---|
| `syncoj-server init` | 缺哪把建哪把，**已有的绝不覆盖、绝不轮换**；建完打印它找到/创建了什么 |
| 服务端加载私钥 | `.key/release-key.pem` **存在**就用；`SYNCOJ_RELEASE_KEY` 仍然覆盖一切 |
| 打包 Agent | `.key/release-key.pub.json` 存在就内嵌进包，并让机器上的配置指向它 |
| 统一注册密钥 | 装机时从 `.key/bootstrap.key` 带进镜像；`init` 已把哈希写进库，所以开箱可用 |

**门槛是"文件存在"。** 这三条默认值全部是**有则生效、无则保持原样**：没有私钥就
照旧不提供任何升级，没有公钥就打出和今天一样的包，没有 `.key/` 目录就什么都不发生。
不能改成"无条件默认、缺失就启动失败" —— 那会让一次干净的 `git clone` 变成起不来的
服务端，而"升级整体关闭"本来就是一个安全且可用的状态。

### 7.2 「发布当前版本」：从界面构建并发版

发一个 Agent 版本的原始流程是三步：手工跑 `build_bundle.py` 打包 → 在「Agent 发布」
页上传那个文件 → 点「铺开」。第一步要求教师身边有一台装了仓库的机器，还得记得打包时
带上签名公钥 —— 忘带的表现是"发成功了但所有机器都不升级"。

现在这一步并进界面：`GET /releases/source` 报告本机有没有可构建的源码、会打出哪个
版本、包里会带哪把公钥；`POST /releases/build` 直接构建并签发一个版本。

四个已经定下来的取舍：

| 问题 | 定下来的做法 | 理由 |
|---|---|---|
| "当前版本"指什么 | 服务端**本机仓库**的 `agent/` 源码 | 服务端本来就必须跑在 checkout 里（`.key/` 就是这么找到的），所以不需要任何新的部署前提 |
| 在哪构建 | 起**子进程**跑 `agent/packaging/build_bundle.py` | 见下 |
| 版本号 | 界面预填 `__version__`，但**必须显式提交**，且必须与源码一致 | 见下 |
| 构建完是否铺开 | **只建草稿**，铺开仍是单独的、显式的一步 | 与"上传不等于铺开"同一条界线：构建和推给 50 台机器风险等级完全不同 |

**为什么要起子进程而不是 import。** `build_bundle.py` 只用标准库，import 更省事，
但它失败时抛的是 `SystemExit`（`collect_files`/`launcher_path`/`read_version` 三处），
而 `SystemExit` **不是** `Exception` 的子类 —— `except Exception` 接不住，FastAPI 的
错误处理器也接不住，它会直接穿过请求处理冒上去。起子进程之后它只是一个非零退出码，
还能把 stderr 一起带回来。顺带还赚到两条：构建崩了不会带走服务端进程；以及"界面上
点一下"和"手工跑那条命令"是同一个入口，而后者已经被可复现打包保证过（`mtime=0`、
gzip 头不带文件名），所以两者产物逐字节相同。

**为什么版本号必须显式提交、且必须与源码一致。** 自动取 `__version__` 的失败方式是
"改了代码忘了改版本号，于是静默覆盖掉上一版" —— 而已经升级过的机器会因为"版本不高于
当前"拒绝升级，界面上一切正常。要求显式提交是为了让发版这件事必须经过一次确认；要求
与源码一致，是因为不一致时**包里那份代码和发布记录上那个号对不上**，而排查"这台机器
到底升没升"正是靠这两个数对齐。撞上不一致时给的是 409 加两个版本号，直接告诉你改哪边。

**两道拒绝，都是为了不在现场留下"看起来成功了"的假象：**

1. 没有签名私钥 → 503 `release_not_signed`。打出的包 Agent 一律拒收。
2. 有私钥、却没有要内嵌的发布公钥 → 503 `release_trust_anchor_missing`。
   这时**服务端能签、机器验不了**：构建成功、铺开成功、然后所有机器一动不动。
   这是整个功能里最难查的一种故障，所以宁可在最前面拦住。

检查的顺序也是有意排的：先答"这台服务器到底能不能发布"（配置问题），再答"你这次
提交的对不对"（输入问题）。反过来的话，一个只是填错版本号的人会先撞上版本不一致，
改对了再撞上"没有公钥" —— 两轮才走到真正该修的那一步。

**审计**：一次构建留一条 `event_log`（`category=release_built`），里面记版本号、
sha256 前 12 位、以及操作人。`EventLog` 没有管理员外键（它记的是全场次的事件流），
所以操作人写在 `meta_json.by` 里 —— 只写版本号是不够的，那就答不出"是谁"。
这条路径等价于"让服务端发布可执行代码"，匿名不可达，只有管理员能触发。

**守住它的是** `server/tests/test_release_build.py`。其中最要紧的一条不检查任何中间
结果，而是把整条链跑一遍：构建 → 签发 → 铺开 → 从**面向 Agent 的那个接口**下载 →
交给 Agent 自己的 `verify_bundle` 验签验摘要 → `safe_extract_tar` 解开。全程用目标机
上会跑的那份代码，没有一步是"我们自己再实现一遍"。信任锚取的是**包里内嵌的那份**而
不是密钥目录里再读一次 —— 于是「`.key/` 那对文件是否配套」「公钥有没有真的内嵌进去」
「签名对象是不是 sha256 的十六进制串」这三件事任何一件错，它都会红。


**判据是"这个哈希在库里登记了没有"，不是"文件在不在"。** 密钥明文在 `.key/`、
哈希在库里，而 `db reset` 只删库、不碰 `.key/` —— 所以"文件还在、哈希没了"这一态
必然会出现，那时密钥在界面上看着好好的，注册却被判「密钥无效」。`init` 因此会
**补登记**，但**已被吊销的绝不复活**（吊销是有意动作，不能被一次 init 撤销）。

> **两个踩过的坑，都加了回归测试。**
>
> 1. `Settings.release_signing_key` 曾经被**声明两遍**（一次读 `SYNCOJ_RELEASE_KEY`，
>    一次写死 `= None`）。后一次静默覆盖前一次，于是那条环境变量从来没生效过 ——
>    而代码怎么看都像是配好了。**513 项测试全绿也没发现它**，因为没有一条测试
>    覆盖这个环境变量。教训不是"要写测试"，而是：**一个配置项必须有测试证明它
>    真的被读到**，否则"看起来配好了"和"真的配好了"没有区别。
> 2. 测试必须用 `SYNCOJ_KEY_DIR` 把密钥目录钉到临时目录。否则 `Settings()` 会解析到
>    开发者**真实的 `.key/`**，整套测试就跑在"已配好私钥"的状态下 —— 那会把
>    "没有私钥就不提供升级"这条最重要的安全默认值**测反**。这条夹具是 autouse 的。

> ⚠️ **私钥放进仓库相对路径是一个明确的取舍**：任何拿到这份 checkout 的人都能签出
> 考试机会接受的升级包。所以 `.key/` 由**两道** `gitignore` 挡着（目录自带的
> `.gitignore` 与根 `.gitignore`），而不是只靠其中一个 —— 私钥的安全不该挂在
> 一个可以被删掉的文件上。生产部署请把密钥放到只有服务端账号读得到的地方，
> 并用 `SYNCOJ_RELEASE_KEY` / `SYNCOJ_KEY_DIR` 指过去。

## 8. 里程碑

| | 内容 | 验收 |
|---|---|---|
| M1 | 服务端骨架 + enroll + tick + 在线列表 | 50 台机器能在 Web 上看到在线状态 |
| M2 | 扫描 + 上传 + `source/` 落盘 + 文件树 | 选手改代码 → 20s 内出现在 Web |
| M3 | asset + deploy + Range 续传 + 进度条 | 下发 500MB 测试点，中途断网能续 |
| M4 | 结果扫描 + 成绩矩阵 + 审计日志 + 资源限制 | 教师手动跑 LemonLime → 成绩自动汇总 |
| M5 | 签名自更新 + 离线包 + 幂等 installer | 一条命令升级 50 台 |
| M6 | 永久配对（统一密钥 + 六位码，取代注册码）+ 名单库 | 一份镜像装 50 台，教师读码配对即可开考 |
| M7 | API 形状统一（信封/错误体）+ 前端共享数据层 + 补齐全部增删改查入口 | 新增一个资源的成本是"一条声明 + 一个页面" |
| M8 | 「发布当前版本」：从服务端本机源码构建并签发 Agent 包 | 界面上点一下，机器真能升级上去（`test_release_build` 全链路） |

## 9. 待补信息

1. 真实 NOI Linux 机器上的探测输出（`python3 -V`、`fs.inotify.max_user_watches`、
   权限模型、`cat /sys/class/dmi/id/product_uuid` 是否可读 —— 最后这条决定
   硬件指纹认回这条路在目标机上到底可不可用）
2. LemonLime / Arbiter 的结果输出路径与格式样例（阻塞 M4 精确解析）
3. 选手代码目录的规范位置（决定 `agent.ini` 默认值与 systemd `ReadOnlyPaths`）
4. 浏览器里的实际界面验收 —— 所有前端代码都只经过 `vue-tsc` 与 `vite build`，
   没有人真正点过

## 10. 待办

（暂无。原「10.1 发布当前版本」已实现，见 §7.2。）

