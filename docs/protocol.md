# SyncOJ Agent ⇄ Server 协议

> 本文件是协议的权威描述。改协议必须同时更新：
> - `server/syncoj_server/schemas.py`（pydantic 模型，OpenAPI 的来源）
> - `agent/syncoj_agent/client.py` 与 `main.py`（报文体构造）
> - `server/tests/test_agent_contract.py`（契约测试会自动发现不一致）

## 0. 总则

| 项 | 约定 |
|---|---|
| 传输 | HTTP/1.1，生产环境走 HTTPS |
| 编码 | 请求/响应体统一 UTF-8 JSON；文件内容走 multipart 或原始字节流 |
| 认证 | `Authorization: Bearer <token>`（`/enroll` 除外） |
| 时间 | 全部 Unix 秒（UTC） |
| 路径 | **只使用 POSIX 相对路径**，不接受反斜杠、绝对路径、`..` |
| 命名 | JSON 字段用 `snake_case` |

**错误响应**：统一 `{"detail": ...}`，`detail` 可能是字符串或对象。

| 状态码 | 含义 | Agent 应如何处理 |
|---|---|---|
| 400 | 请求不合法（路径、哈希格式） | 记日志，不重试；等下一轮 |
| 401 / 403 | 凭据无效或无权访问 | 清空本地凭据，用注册码重新注册 |
| 404 | 注册码/资源不存在 | 记日志，不重试 |
| 409 | **内容已不是当前版本**（迟到旧版本） | **静默丢弃**，下一轮 tick 会拿到正确版本 |
| 410 | 服务端资源内容缺失 | 上报审计事件 |
| 413 | 文件超过策略上限 | 跳过该文件，不重试 |
| 5xx | 服务端故障 | 指数退避重试 |

---

## 1. `POST /api/v1/agent/enroll`

换长期凭据。**无需认证**（凭据本身就是认证），但**有限速**（按 IP 与全局两层）。

### 请求

```json
{
  "enroll_code": "ABCD-EFGH-JKLM-NPQR",
  "machine_id": "0123456789abcdef0123456789abcdef",
  "machine_uuid": "3f2a…",
  "machine_fingerprint": "4c4c4544-0031-3010-8043-b7c04f4d4432",
  "hostname": "exam-pc-01",
  "agent_version": "0.1.0",
  "os_info": "Linux 5.4.0 Ubuntu 20.04.6 LTS"
}
```

**两种凭据二选一**，都传时以 `enroll_code` 为准（单人码是更明确的意图，
"这台机器就是某个具体选手"，不该被镜像里那份宽泛的密钥盖过去）：

| 字段 | 换回来的机器 |
|---|---|
| `enroll_code` | **直接有归属**（注册码里就写着是谁） |
| `bootstrap_key` | **没有归属**，要短码配对认领到人 |

其余字段：

- `machine_id` 稳定标识本机。优先取 `/etc/machine-id`
- `machine_uuid` Agent 首次运行时生成并持久化的 UUID。比 `machine_id` 更适合当
  身份：克隆镜像没做通用化时 `/etc/machine-id` 是整批相同的
- `machine_fingerprint` 硬件指纹（SMBIOS UUID）。**整机快照还原后不变**，
  服务端靠它认回原机器、保留配对关系。**读不到就省略，不要拿主机名或
  machine-id 顶替** —— 那样会在克隆镜像时整批相同，把"认回原机器"
  变成"认错机器"
- 两个凭据都不传：`400`

### 响应 `200`

有归属时（每选手注册码，或指纹认回了原机器）：

```json
{
  "token": "…43 字符…",
  "agent_id": 1,
  "claimed": true,
  "player_no": "S001",
  "player_name": "张三",
  "contest_id": 1,
  "contest_slug": "mock-1",
  "contest_name": "校内模拟赛",
  "config": { "…见 §6…" }
}
```

**没有归属**时（统一密钥注册的新机器）：

```json
{
  "token": "…43 字符…",
  "agent_id": 0,
  "claimed": false,
  "pair_code": "K7QW2M",
  "player_no": "",
  "contest_id": null,
  "contest_slug": "",
  "contest_name": "",
  "config": { "…见 §6…" }
}
```

> ⚠️ `claimed=false` **不是失败**。把它当成"注册失败"去反复重试，服务端每次都
> 会换一个新配对码 —— 教师刚在机器上读到的那个立刻失效，而且日志里只看得到
> 注册记录，真正的状态（还没配对）反而看不见。这个 `token` 是**有效的**，
> 只是还没有归属。客户端应当把配对码显示给人看，然后安静地按 tick 等着。

### 语义

**注册码不是一次性的。** 它绑定 `(player_no, machine_id)`，长期有效。

- 首次使用：写入绑定的 `machine_id`
- 同一机器再次注册：**换发新 token，旧 token 立即失效**（快照还原自愈路径）
- 不同机器使用同一注册码：`403`

**统一密钥**：

- 密钥可重复使用、可吊销、可带过期时间。**已注册的机器不受吊销影响** ——
  它们手里是各自的 token，不是这把密钥
- 新机器 → 进待认领列表 + 一次性的 6 位配对码（服务端只存哈希）
- 指纹能认回已有机器、**且那台机器当前离线** → 换发 token、保留配对关系
- 指纹撞了、或对应的机器**正在心跳** → 进待认领，并留下告警。
  宁可让教师人工确认一次，也不要把某个学生的身份白送出去

### 错误

| 状态码 | 含义 |
|---|---|
| `400` | 两种凭据都没传 |
| `403` | 注册码绑定了别的机器 / 已被吊销 / 已过期 / 统一密钥已被吊销 |
| `404` | 注册码或统一密钥不存在（和"存在但被吊销"分开，现场排查时这个区别很值钱） |
| `429` | 触发限速，带 `Retry-After` |

---

## 2. `POST /api/v1/agent/tick`

系统的核心接口。**幂等** —— 重复调用、补发、乱序都不会破坏状态。

### 请求

```json
{
  "agent_version": "0.1.0",
  "machine_id": "0123456789abcdef0123456789abcdef",
  "ts": 1767225600,
  "scan_root": "/home/student/code",
  "scan": [
    { "path": "code/main.cpp", "sha256": "ab12…", "size": 1234, "mtime": 1767225500 }
  ],
  "scan_complete": true,
  "partials": [ { "asset_id": 42, "bytes_done": 3145728 } ],
  "stats": { "disk_free": 10737418240, "last_error": null, "queue": 0 }
}
```

| 字段 | 说明 |
|---|---|
| `scan` | **全量**文件索引，不含内容。路径 = `<扫描根目录名>/<相对路径>` |
| `scan_complete` | 本次扫描是否完整覆盖了所有根目录。**为 `false` 时服务端跳过删除判定** |
| `partials` | 本地未完成下载的已下载字节数。Agent 重启后从磁盘实况重建 |
| `stats.queue` | 待上报审计事件数 |

> ⚠️ `scan` 必须是完整扫描。若 Agent 因权限错误等原因漏扫了一批文件，
> **必须**置 `scan_complete=false`，否则服务端会把漏扫误判成"选手删除了文件"。

### 响应 `200`

```json
{
  "server_time": 1767225600,
  "next_tick_seconds": 20,
  "need_upload": ["code/main.cpp"],
  "deploy_jobs": [
    {
      "asset_id": 42,
      "url": "/api/v1/agent/assets/42",
      "sha256": "cd34…",
      "size": 10485760,
      "dest": "题面.pdf",
      "offset": 3145728,
      "mode": "overwrite"
    }
  ],
  "cancel_assets": [],
  "upgrade": null,
  "config": { "…见 §6…" },
  "claimed": true,
  "player_no": "S001",
  "contest_slug": "mock-1"
}
```

### 未配对机器的 tick

统一密钥注册出来、还没被认领的机器**也会**走这个接口。它的响应形态不同：

```json
{
  "server_time": 1767225600,
  "next_tick_seconds": 60,
  "need_upload": [],
  "deploy_jobs": [],
  "cancel_assets": [],
  "upgrade": null,
  "config": { "…见 §6…" },
  "claimed": false
}
```

此时请求里的 `scan` **一律被丢弃** —— 那台机器没有准考证号，它的相对路径没法
归属到任何人，收下来只会污染台账。客户端也因此应当在未配对时干脆不扫描。

认领完成之后，服务端会在下一次 tick 里带上 `claimed=true` 与身份
（`player_no` / `contest_slug`）。客户端接住它就能转入正常扫描，
**不需要**为此再 enroll 一次 —— 那是多一次可能失败的网络往返，还会多刷一条注册审计。

> 未配对的机器调**除 tick 以外**的接口（上传、下载、事件）一律 `403`，
> 而不是 `401`。401 会让客户端以为凭据坏了去反复重新注册，
> 把真正的原因（还没配对）盖掉。

**`next_tick_seconds` 由服务端决定**：有活干（`need_upload`/`deploy_jobs`/发现删除）
时收紧到 `tick_active_seconds`（默认 2），否则放宽到 `tick_idle_seconds`（默认 60）。
Agent 不得自行决定节奏。

**`need_upload` 是服务端的判断，不是 Agent 的。** Agent 只执行，不自行决定传什么 ——
"谁传过什么"必须只有一个权威来源。

**`dest` 是相对于 Agent 的 `deploy_root` 的相对路径**（默认 `deploy_root` 就是桌面）。
服务端下发前已经把 `{player_no}` 展开成该选手的准考证号 —— 客户端不知道也不可能
知道"这次下发是给谁的"。所以：

- 通用资料（题面、样例、须知）的 `dest` 就是文件名本身，例如 `"题面.pdf"`
- 按题分发的附件是 `"<准考证号>/<题目名>/<文件名>"`

---

## 3. `POST /api/v1/agent/files`

上传单个文件内容。`multipart/form-data`。

| 字段 | 位置 | 说明 |
|---|---|---|
| `path` | form | POSIX 相对路径，须与 tick 中上报的一致 |
| `sha256` | form | **文件内容的** sha256（不是 tick 里那个的简单复制，见下） |
| `file` | file | 文件内容 |

### 响应 `200`

```json
{ "path": "code/main.cpp", "sha256": "ab12…", "revision": 1, "stored": true }
```

### 旧版本保护（重要）

服务端会比对 `sha256` 与台账中该文件当前记录的版本：

- **一致** → 接受，`revision += 1`
- **不一致** → `409`，响应体 `{"detail": {"reason": "stale", "expected": "<服务端记录值>"}}`

这是断网补传 / 乱序重传场景下的正确性保证：**迟到的旧内容绝不能覆盖新版本**。
Agent 收到 409 应当静默丢弃，下一轮 tick 自然会拿到真正需要的版本。

> Agent 实现细节：上传前应当**重新计算**磁盘上文件的 sha256，而不是复用 tick
> 时报的那个。若文件在 tick 与 upload 之间又变了，重算会让服务端返回 409
> （干净地走"迟到版本"路径），而复用旧哈希会导致 400 "哈希不符"（脏的错误）。
>
> Agent 也不应自行判断"这个文件变了，那我直接传新的" —— 服务端台账还是旧版本，
> 直传新内容同样会得到 409。让它走 409 才是对的。

---

## 4. `GET /api/v1/agent/assets/{asset_id}`

下载下发文件。**支持 HTTP Range**，用于断点续传。

| 请求头 | 说明 |
|---|---|
| `Range: bytes=<start>-` | 从指定偏移续传 |

| 响应 | 说明 |
|---|---|
| `200` | 完整内容（未请求 Range，或服务端/代理不支持 Range） |
| `206` | 部分内容，带 `Content-Range` |
| `416` | 范围不合法 |

**Agent 必须处理"请求了 Range 却收到 200"的情况** —— 代理剥离 `Range` 头很常见。
此时必须丢弃本地分片从头下载，不能把完整内容追加到已有分片后面。

**授权**：凭据只能下载**下发给本选手**的资源。仅 token 有效是不够的 ——
否则任一选手可以拖走全场测试点。

---

## 5. `POST /api/v1/agent/events`

批量上报审计事件。刻意用列表：断网期间事件会堆积，逐条上报会在重连瞬间产生请求风暴。

```json
[
  { "level": "warning", "category": "disk_full",
    "message": "磁盘空间不足", "meta": { "free": 1024 } }
]
```

常见的 `category`：

| category | 触发条件 |
|---|---|
| `scan_error` | 扫描过程中出现错误 |
| `scan_incomplete` | 扫描不完整，已跳过删除判定 |
| `oversize_skipped` | 有文件超过大小上限被跳过 |
| `deploy_failed` | 下发失败 |
| `upload_rejected` | 服务端拒绝（通常因超限） |
| `file_deleted`（服务端产生） | 完整扫描中发现文件消失 |
| `bulk_rewrite`（服务端产生） | 单轮内大量文件内容变化 |
| `offline`（服务端产生） | 超时未 tick |

---

## 6. `config` 对象

服务端权威下发，两边有各自的默认值（由契约测试保证一致）。

```json
{
  "extensions": [".c", ".cpp", "…"],
  "exclude_dirs": [".git", "build", "…"],
  "exclude_suffixes": [".swp", ".tmp", "…"],
  "max_file_size": 2097152,
  "scan_interval": 60,
  "max_files": 5000,
  "policy_version": 1,
  "tick_idle_seconds": 60,
  "tick_active_seconds": 2
}
```

Agent 合并时**逐键校验**：某一键不合法只回退该键，不整体退回默认值 ——
否则服务端一个新字段的把关疏漏会让整个策略退化。

---

## 7. 服务端内部约定（Agent 无需关心，但影响目录布局）

```
<data_root>/
├── syncoj.db
├── blobs/<sha[:2]>/<sha>                              # 内容寻址，天然去重
├── source/<contest_slug>/<player_no>/<前缀>/<路径>     # 评测器直接读取
├── judge_result/<contest_slug>/<player_no>/<题目>/     # 成绩回写扫描源
└── logs/
```

**`source/` 的路径就是 Agent 上报的路径原样**，包含扫描根目录名前缀。
评测器（LemonLime / Arbiter）按这个结构配置即可，无需任何转换。
