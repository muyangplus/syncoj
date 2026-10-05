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
| 认证 | 选手编号 + 每机独立 Token（服务端只存 `sha256(token)`） | |
| 注册码 | 绑定 `player_no + machine_id`、**长期有效**（非一次性） | 见 §3.4 快照还原自愈 |
| 数据库 | SQLite WAL，内存态优先 + 批量落库 | |
| 场次 | 多场次隔离（`contest_id`） | |
| 防篡改 | 完整性校验 + 审计日志，不干预选手 | |
| 自更新 | Ed25519 签名校验，**默认关闭**，教师端显式开启 | |
| 后台账号 | 单管理员 | |
| 装机 | 镜像预装 / 离线包 / 在线自举，同一份幂等逻辑 | |
| 网络 | 考场同一内网，双向可达 | Ansible 留作以后赛前批量装机的备选，v1 不用 |

## 1. 架构总览

```
┌──────────── 教师 ────────────┐
│  Vue3 SPA  ──HTTPS──> Caddy  │
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

### 3.4 注册与"快照还原自愈"

NOI Linux 常做整机还原，机器上的凭据会消失 —— 刚需，不是加分项：

```
首次启动: POST /agent/enroll {enroll_code, machine_id, hostname}
       ← {token, player_no, contest}
       写 /var/lib/syncoj/credential.json (0600)

还原后:   credential.json 不存在 → 用镜像内置的 enroll_code 重新 enroll
       服务端按 machine_id 认出是老机器 → 签发新 token（旧 token 作废）
```

**所以 `enroll_code` 不能是一次性的**，而是绑定 `player_no + machine_id`、长期有效的"机器凭据种子"。教师装机时按座位写入 `/etc/syncoj/agent.ini`。

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
| `player` | `(contest_id, player_no, name, seat)` |
| `enroll_code` | 注册码，绑定 `player_no + machine_id`，长期有效 |
| `agent` | 每机 Token 哈希、`machine_id`、版本、吊销状态 |
| `agent_status` | 在线状态快照，`last_tick_at`、指标 |
| `source_file` | 回收台账：`(contest, player, path, sha256, size, revision, first_seen, last_seen)` |
| `asset` | 内容寻址的下发文件 |
| `deploy_task` / `deploy_target` | 下发任务与逐选手进度（含 `bytes_done`） |
| `judge_run` | 成绩回写 |
| `event_log` | 审计事件 |
| `agent_release` | Agent 版本与签名 |
| `admin` | 管理员账号 |

### 4.3 写入策略（SQLite 的命门）

50 台 × 每 20s 一次 tick = **2.5 req/s**。每次 tick 都要 upsert 在线状态 + 比对文件台账，**绝不能每个请求开事务写库**：

- **内存优先**：`AgentRegistry` 用 dict 持有实时状态，Web 读路径走内存（后续经 SSE 推送）
- **批量落库**：后台任务每 5s 把脏数据合并成**一个事务**刷入
- **单写连接**：所有写操作走一个队列 + 独立线程，避免 `database is locked`
- SQLite 参数：`journal_mode=WAL`、`synchronous=NORMAL`、`busy_timeout=5000`、`foreign_keys=ON`

### 4.4 评测结果回写

后台 `ResultScanner` 每 10s 轮询 `judge_result/`，`mtime` 变化才解析。解析器做成可配（`glob` + 格式），先支持通用 JSON/XML/文本模板，拿到 LemonLime/Arbiter 真实输出样例后再补精确解析。Web 端按「选手 × 题目」矩阵展示分数，可导出 CSV。

## 5. 协议一致性

Agent 零依赖，**不能 import pydantic**。所以一致性不靠共享模型，靠 **CI 契约测试**：

```python
# server/tests/test_agent_contract.py
def test_tick_payload_matches_schema():
    raw = subprocess.run(["python3", "agent/tools/build_fixture.py"],
                         capture_output=True, text=True).stdout
    TickRequest.model_validate(json.loads(raw))     # 不匹配就红
```

`agent/tools/build_fixture.py` 用 Agent 自己的纯标准库构造器生成真实 payload。服务端 schema 一改、契约一破，CI 立刻拦住。

同一份 OpenAPI 生成前端 TS 类型：`npx openapi-typescript http://localhost:8000/openapi.json -o web/src/api/types.d.ts`。

## 6. 安全

| 面 | 措施 |
|---|---|
| 传输 | HTTPS，内网自签 CA；Agent 固定 CA 指纹（证书 pinning） |
| 认证 | 服务端只存 `sha256(token)`；按机吊销；`enroll_code` 绑定 `player_no + machine_id` |
| 授权 | token 只能访问本人场次与资源；下载 asset 必须属于自己且被 `deploy_target` 引用 |
| 路径 | 服务端**独立**做 `resolve()` + 白名单校验，绝不信任 Agent 上报的相对路径 |
| 完整性 | 上传/下载双向 SHA256；升级包额外 Ed25519 签名 |
| 隔离 | 服务端专用用户；`source/` 对评测器只读；Agent 非 root + `NoNewPrivileges` |
| 审计 | 登录、注册、下发、回收、文件消失、批量重写、升级全进 `event_log` |

⚠️ **路径校验必须在服务端独立实现一遍** —— Agent 跑在选手机器上，随时可能被替换。

## 7. 运维与装机

安装器做成**幂等三入口、同一份逻辑**：

```bash
install.sh --mode=offline|online|image
```

- 检测已装版本 → 存在则原地升级，不存在则全量安装
- 幂等：重复执行结果一致；凭据丢失自动重新 enroll
- 离线包 = tar.gz（Agent 目录 + systemd unit + 配置模板），不需要 pip、不需要网络
- 在线自举 = 拉 tar.gz + 校验 sha256 后走同一条路径

## 8. 里程碑

| | 内容 | 验收 |
|---|---|---|
| M1 | 服务端骨架 + enroll + tick + 在线列表 | 50 台机器能在 Web 上看到在线状态 |
| M2 | 扫描 + 上传 + `source/` 落盘 + 文件树 | 选手改代码 → 20s 内出现在 Web |
| M3 | asset + deploy + Range 续传 + 进度条 | 下发 500MB 测试点，中途断网能续 |
| M4 | 结果扫描 + 成绩矩阵 + 审计日志 + 资源限制 | 教师手动跑 LemonLime → 成绩自动汇总 |
| M5 | 签名自更新 + 离线包 + 幂等 installer | 一条命令升级 50 台 |

## 9. 待补信息

1. 真实 NOI Linux 机器上的探测输出（`python3 -V`、`fs.inotify.max_user_watches`、权限模型）
2. LemonLime / Arbiter 的结果输出路径与格式样例（阻塞 M4 精确解析）
3. 选手代码目录的规范位置（决定 `agent.ini` 默认值与 systemd `ReadOnlyPaths`）
