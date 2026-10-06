# SyncOJ Agent ⇄ Server 协议

> 本文件是协议的**权威描述**。改协议必须同时更新：
> - `server/syncoj_server/schemas.py`（pydantic 模型，OpenAPI 的来源）
> - `server/syncoj_server/errors.py`（错误码清单）
> - `agent/syncoj_agent/client.py` 与 `main.py`（报文体构造与状态机）
> - `server/tests/test_agent_contract.py`（契约测试会自动发现不一致）
> - `docs/api-conventions.md`（管理端 API 的同类约定）

## 0. 总则

| 项 | 约定 |
|---|---|
| 传输 | HTTP/1.1，生产环境走 HTTPS |
| 编码 | 请求/响应体统一 UTF-8 JSON；文件内容走 multipart 或原始字节流 |
| 认证 | `Authorization: Bearer <token>`（`/enroll` 除外） |
| 时间 | 全部 Unix 秒（UTC） |
| 路径 | **只使用 POSIX 相对路径**，不接受反斜杠、绝对路径、`..` |
| 命名 | JSON 字段用 `snake_case` |
| 轮询 | 服务端用 `next_tick_seconds` 决定节奏，Agent **不得自行决定** |

### 0.1 错误响应

**所有**错误响应都是同一个形状：

```json
{ "detail": "这句是给人看的中文", "code": "pairing_required", "details": { } }
```

| 字段 | 说明 |
|---|---|
| `detail` | **永远是字符串**，可以直接显示给教师/选手 |
| `code` | 稳定的短标识符。要分支判断就用它，**不要拿 `detail` 做字符串比较** |
| `details` | 结构化补充信息，可缺省 |

Agent 只应对下面这几个 `code` 做分支，其余一律"记日志 + 等下一轮"：

| `code` | Agent 的动作 |
|---|---|
| `unauthorized` / `token_expired` | 凭据坏了 → 清掉本地 token，重新走 `/enroll` |
| `machine_revoked` | 这台机器已被作废 → 停止重试，等人处理 |
| `pairing_required` | 还没配对 → **不要重新注册**，显示配对码，等下一轮 tick |
| `no_active_contest` / `ambiguous_contest` / `contest_missing` / `contest_player_missing` | 配对好了但还干不了活 → 写「等待场次.txt」，等下一轮 tick |
| `bootstrap_key_invalid` / `bootstrap_key_revoked` / `bootstrap_key_expired` | 镜像里的密钥不对 → 停止重试，写明确日志等人来修 |
| `rate_limited` | 按 `Retry-After` 退避 |
| `internal_error` | 指数退避 |

其他 `code` 一律"记日志 + 等下一轮"。

> **`pair_code_invalid` 不在这张表里，因为 Agent 收不到它。** 配对码是教师在管理
> 界面里输的，所以"码不对/过期"是发给**浏览器**的响应。机器那侧只管把码显示在
> 桌面上、等对方输对 —— 它甚至不知道有人正在试。

> **Agent 不需要记住这张表也能做对。** 真正要守住的分界线只有一条：**401 才清
> 凭据重新注册，403 一律只更新状态**。`code` 是用来把"卡在哪一档"写进日志和事件
> 的，不是用来决定要不要重新注册的 —— 服务端将来加一个新 code 时，按状态码判
> 的客户端不会因此走错路。

### 0.2 状态码语义（Agent 视角）

| 状态码 | 含义 | Agent 应如何处理 |
|---|---|---|
| 400 | 请求不合法（路径、哈希格式） | 记日志，不重试；等下一轮 |
| **401** | **凭据本身无效** | 清空本地凭据，重新 `/enroll` |
| **403** | **凭据有效，但还没配对/无权访问** | **不要重新注册**。显示配对码，等下一轮 |
| 404 | 资源不存在 | 记日志，不重试 |
| 409 | **内容已不是当前版本**（迟到旧版本） | **静默丢弃**，下一轮 tick 会拿到正确版本 |
| 410 | 服务端资源内容缺失 | 上报审计事件 |
| 413 | 文件超过策略上限 | 跳过该文件，不重试 |
| 429 | 触发限速，带 `Retry-After` | 按 `Retry-After` 退避 |
| 5xx | 服务端故障 | 指数退避重试 |

> 401 与 403 的分工是**故意的**。把"还没配对"报成 401，客户端会以为凭据坏了去
> 反复重新注册，把真正的原因盖掉；而每次重新注册都会换一个新配对码，教师刚在
> 屏幕上读到的那个当场失效。

---

## 1. 配对模型（先读这一节，它决定了后面所有接口的形态）

```
装机（root 一次性）          机器首次开机              教师
bootstrap.key ──换凭据──▶  桌面「配对码.txt」 ──读码输入──▶ 绑到名单里的**人**
```

**配对是永久的，而且绑的是"人"，不是"某场比赛的选手"。**

- 绑定的对象是 `roster_entry`（名单条目 = 一个人），不是 `player`（某场次的参赛者）
- 同一个学生换一场比赛**不用重新配对**，只要新场次应用了那份名单
- 配对码**只在绑定的那一刻用**。配对之后认机器一律靠 `machine_uuid`

### 三个状态

`claimed` 与 `bound` 是两个正交的布尔量，它们组合出三种状态。客户端必须分清 ——
三个状态看起来都像"注册成功了"，但该做的事完全不同：

| `claimed` | `bound` | 含义 | Agent 该做什么 |
|---|---|---|---|
| `false` | `false` | 还没配对到人 | 把 `pair_code` 写到桌面 `配对码.txt`，**不扫描**，安静等 tick |
| `true` | `false` | 配对好了，但还没有含你的场次 | 写 `等待场次.txt`（内容 = `reason`），**不扫描**，等 tick |
| `true` | `true` | 正常 | 扫代码、收下发 |

第二档是"机器绑的是人"带来的必然结果：某个场次有没有这个人，取决于那份名单
有没有被应用到场次里。所以**配对成功不等于马上能干活** —— 这个中间状态必须
说得出来，否则客户端只能猜（猜错的后果是它去扫一个展开不出准考证号的目录）。

### 机器身份的三个要素

| 字段 | 来源 | 作用 |
|---|---|---|
| `machine_uuid` | Agent 首次运行生成并持久化 | **首选身份**。换凭据、重新注册都按它匹配 |
| `machine_fingerprint` | SMBIOS UUID | 第二道依据。**整机快照还原后不变**，UUID 会一起消失 |
| `machine_id` | `/etc/machine-id` | **仅供人工辨认**（列表里显示），不参与身份判定 |

> `machine_fingerprint` **读不到就省略，不要拿主机名或 machine-id 顶替** ——
> 那样会在克隆镜像时整批相同，把"认回原机器"变成"认错机器"。
>
> 克隆镜像时 `/etc/machine-id` 也常常整批相同，这正是它不当身份用的原因。

### 认机器的顺序（服务端逻辑）

1. `machine_uuid` 能对上 → 就是它。配对之后的常态，一次索引命中
2. UUID 新（快照还原把它抹了）但指纹对上一台**离线**的机器 → 认回它，记下新 UUID
3. 都对不上 → 新机器，发一个六位配对码等教师绑人

第 2 步的"离线"条件是**安全要求**：克隆镜像时母机多半还开着，一台新机器却报出
同样的指纹。如果让它把身份拿走了，那个学生的成绩就会被另一台机器的代码污染，
而且完全静默。所以原机器在线 / 指纹撞多台时都不自动认回，改走人工配对并留告警。

---

## 2. `POST /api/v1/agent/enroll`

换长期凭据。**无需认证**（凭据本身就是认证），但**有限速**（按 IP 与全局两层）。
这是**唯一**的注册路径 —— 每选手注册码已经被"机器永久绑定名单条目"取代。

### 请求

```json
{
  "bootstrap_key": "43 字符的镜像内置密钥",
  "machine_id": "0123456789abcdef0123456789abcdef",
  "machine_uuid": "3f2a…",
  "machine_fingerprint": "4c4c4544-0031-3010-8043-b7c04f4d4432",
  "hostname": "exam-pc-01",
  "agent_version": "0.1.0",
  "os_info": "Linux 5.4.0 Ubuntu 20.04.6 LTS"
}
```

`bootstrap_key` 必填。它从 **root 只读**的单独文件读（默认
`/etc/syncoj/bootstrap.key`），**不要**写进选手账号可读的 `agent.ini`。

### 响应 `200`：三态

**未配对** —— 把码显示给人看：

```json
{
  "token": "…43 字符…",
  "agent_id": 7,
  "claimed": false,
  "bound": false,
  "pair_code": "482913",
  "reason": "这台机器还没有配对到人，请把配对码告诉老师",
  "player_no": "",
  "contest_id": null,
  "contest_slug": "",
  "contest_name": "",
  "config": { "…见 §7…" }
}
```

**已配对，但还没有含你的场次**：

```json
{
  "token": "…", "agent_id": 7, "claimed": true, "bound": false,
  "pair_code": null,
  "reason": "还没有包含你的场次",
  "config": { "…见 §7…" }
}
```

**正常**：

```json
{
  "token": "…", "agent_id": 7, "claimed": true, "bound": true,
  "pair_code": null,
  "player_no": "S001", "player_name": "张三",
  "contest_id": 1, "contest_slug": "mock-1", "contest_name": "校内模拟赛",
  "config": { "…见 §7…" }
}
```

> ⚠️ `claimed=false` **不是失败**。把它当成"注册失败"去反复重试，服务端每次都
> 会换一个新配对码 —— 教师刚读到的那个立刻失效。这个 `token` 是**有效的**，
> 只是还没有归属。客户端应当写下配对码，然后安静地按 tick 等着。

### 语义

- 密钥可重复使用、可吊销、可带过期时间。**已注册的机器不受吊销影响** ——
  它们手里是各自的 token，不是这把密钥
- 重新注册（UUID 认回或指纹认回）→ **换发新 token，旧 token 立即失效**。
  这就是整机快照还原之后的自愈路径
- 未配对的机器重新注册时**换一个新配对码**（教师手上那个可能早就过期了）
- 配对码：**六位数字**，服务端只存哈希，有效期 30 分钟，**一次性**，
  绑定成功即刻作废

### 错误

| 状态码 | `code` | 含义 |
|---|---|---|
| `404` | `bootstrap_key_invalid` | 统一密钥不存在（和"存在但被吊销"分开，现场排查时这个区别很值钱） |
| `403` | `bootstrap_key_revoked` | 统一密钥已被吊销 |
| `403` | `bootstrap_key_expired` | 统一密钥已过期 |
| `422` | `validation_error` | 报文体缺字段/字段过长 |
| `429` | `rate_limited` | 触发限速，带 `Retry-After` |

---

## 3. `POST /api/v1/agent/tick`

系统的核心接口。**幂等** —— 重复调用、补发、乱序都不会破坏状态。

### 请求

```json
{
  "agent_version": "0.1.0",
  "machine_id": "0123456789abcdef0123456789abcdef",
  "hostname": "exam-pc-01",
  "ts": 1767225600,
  "scan_root": "/home/student/code",
  "scan": [
    { "path": "code/main.cpp", "sha256": "ab12…", "size": 1234, "mtime": 1767225500 }
  ],
  "scan_complete": true,
  "partials": [ { "asset_id": 42, "bytes_done": 3145728 } ],
  "completed_assets": [42],
  "stats": { "disk_free": 10737418240, "last_error": null, "queue": 0 }
}
```

| 字段 | 说明 |
|---|---|
| `scan` | **全量**文件索引，不含内容。路径 = `<扫描根目录名>/<相对路径>` |
| `scan_complete` | 本次扫描是否完整覆盖了所有根目录。**为 `false` 时服务端跳过删除判定** |
| `partials` | 本地未完成下载的已下载字节数。Agent 重启后从磁盘实况重建 |
| `completed_assets` | 本地已确认完整的下发资源 id。**显式上报**而不由服务端从 `partials` 推断 —— "分片不见了"既可能是下完了，也可能是被清理了 |
| `stats.queue` | 待上报审计事件数 |

> ⚠️ `scan` 必须是完整扫描。若 Agent 因权限错误等原因漏扫了一批文件，
> **必须**置 `scan_complete=false`，否则服务端会把漏扫误判成"选手删除了文件"。

### 响应 `200`

```json
{
  "server_time": 1767225600,
  "next_tick_seconds": 2,
  "need_upload": ["code/main.cpp"],
  "deploy_jobs": [
    {
      "asset_id": 42,
      "url": "/api/v1/agent/assets/42",
      "sha256": "cd34…",
      "size": 10485760,
      "dest": "题面.zip",
      "offset": 3145728,
      "mode": "overwrite"
    }
  ],
  "cancel_assets": [],
  "upgrade": null,
  "config": { "…见 §7…" },
  "claimed": true,
  "bound": true,
  "pair_code": null,
  "reason": null,
  "player_no": "S001",
  "contest_slug": "mock-1"
}
```

未配对 / 无场次的机器**也走这个接口**，响应形态相同但三态不同（见 §1），
并且此时：

- 请求里的 `scan` **一律被丢弃** —— 那台机器没有准考证号，相对路径没法归属到
  任何人，收下来只会污染台账。客户端也因此应当在未配对上时干脆不扫描
- 客户端此时应当报**空 `scan` 且 `scan_complete=false`**，而不是 `true`。
  理由不是服务端要读它（它确实丢弃），而是"空 `scan` + `true`"在报文里与
  "选手把所有文件都删了"**一模一样**。今天靠服务端记得丢弃才没出事，而
  "记得丢弃"恰好是重构中最容易被忘掉的那一行。报 `false` 让这层歧义在协议层
  就不存在 —— 代价是零。
- `need_upload` 与 `deploy_jobs` 一律为空
- `claimed=false` 时响应里**带上 `pair_code`**（每次 tick 都会确认它还有效；
  过期就发一个新的，理由见 §1 —— 心跳是未配对机器唯一稳定的上行通道）

> **"每次 tick 都带同一个码"对服务端有个不那么显然的要求。** 库里只存哈希，
> 而哈希推不回明文，所以服务端要把明文**在内存里**留到过期或配对为止
> （`services/paircodes.py`，随进程消失）。
>
> 为什么不让 Agent 自己记住上次收到的码、只在"有变化"时下发？那也能工作，但会把
> "桌面上那个码是不是还有效"变成一个客户端状态问题：Agent 的状态目录被抹掉
> （快照还原的常态）之后它就再也说不出码是什么了，而它连自己**曾经**说过什么是
> 都不知道。现在这条规则是"每次 tick 都给你一个当前有效的码"，客户端只负责显示，
> 不负责保管有效性。
>
> 代价是明文会在服务端内存里活最多 30 分钟 —— 和它同时活在选手桌面的文件里是
> 同一量级，而它**绝不落库、绝不落服务端磁盘**。进程重启后缓存没了，下一次 tick
> 直接发一个新码，桌面上的文件随之更新，不需要人工介入。

配对完成之后，服务端会在下一次 tick 里带上 `claimed=true` 与身份。
客户端接住它就能转入正常扫描，**不需要**为此再 enroll 一次 —— 那是多一次可能
失败的网络往返，还会多刷一条注册审计。

> 未配对的机器调**除 tick 以外**的接口（上传、下载、事件）一律 `403`，而不是 401。
> 401 会让客户端以为凭据坏了去反复重新注册，把真正的原因（还没配对）盖掉。
> 403 的 `code` 会告诉你卡在哪一档（`pairing_required` / `no_active_contest` /
> `ambiguous_contest` / `contest_missing` / `contest_player_missing`），
> `detail` 是一句能直接显示的中文。

**`next_tick_seconds` 由服务端决定**：有活干（`need_upload`/`deploy_jobs`/发现删除）
时收紧到 `tick_active_seconds`（默认 2），否则放宽到 `tick_idle_seconds`（默认 60）。

**`need_upload` 是服务端的判断，不是 Agent 的。** Agent 只执行，不自行决定传什么 ——
"谁传过什么"必须只有一个权威来源。

**`dest` 是相对于 Agent 的 `deploy_root` 的相对路径**（默认 `deploy_root` 就是桌面）。
服务端下发前已经把 `{player_no}` 展开成该选手的准考证号 —— 客户端不知道也不可能
知道"这次下发是给谁的"。所以：

- 通用资料（题面、样例、须知）的 `dest` 就是文件名本身，例如 `"题面.zip"`、`"样例.zip"`
- 按题分发的附件是 `"<准考证号>/<题目名>/<文件名>"`

**下发资产一律是文件，不解压。** 题面与样例本身就是 zip，服务端只负责搬运字节，
选手/评测机自己解压。密码不进系统：需要密码时，教师把它当普通资产一起下发
（一个 `password.txt`），它会像别的文件一样落到桌面上。

---

## 4. `POST /api/v1/agent/files`

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
- **不一致** → `409` + `code=conflict`，`details.reason = "stale"`，
  `details.expected` = 服务端记录值

这是断网补传 / 乱序重传场景下的正确性保证：**迟到的旧内容绝不能覆盖新版本**。
Agent 收到 409 应当静默丢弃，下一轮 tick 自然会拿到真正需要的版本。

> Agent 实现细节：上传前应当**重新计算**磁盘上文件的 sha256，而不是复用 tick
> 时报的那个。若文件在 tick 与 upload 之间又变了，重算会让服务端返回 409
> （干净地走"迟到版本"路径），而复用旧哈希会导致 400 "哈希不符"（脏的错误）。
>
> Agent 也不应自行判断"这个文件变了，那我直接传新的" —— 服务端台账还是旧版本，
> 直传新内容同样会得到 409。让它走 409 才是对的。

---

## 5. `GET /api/v1/agent/assets/{asset_id}`

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

## 6. `POST /api/v1/agent/events`

批量上报审计事件。刻意用列表：断网期间事件会堆积，逐条上报会在重连瞬间产生请求风暴。

```json
[
  { "level": "warning", "category": "disk_full",
    "message": "磁盘空间不足", "meta": { "free": 1024 } }
]
```

> **未配对的机器发不出事件。** 这个接口对未配对机器也是 `403`（和上传、下载
> 一样），所以那段时间攒下的事件只能留在本地队列里，等配对成功后的第一轮 tick
> 一起上报。这是有意的：给它开一个例外，就等于给一台"身份未知"的机器开了一条
> 写入通道。本地队列要封顶（当前实现是 200 条），否则一台长期跑不出去的机器
> 会把磁盘写满 —— 而它本来就是因为没人来配对才卡在那儿的。

常见的 `category`：

| category | 触发条件 |
|---|---|
| `scan_error` | 扫描过程中出现错误 |
| `scan_incomplete` | 扫描不完整，已跳过删除判定 |
| `oversize_skipped` | 有文件超过大小上限被跳过 |
| `deploy_failed` | 下发失败 |
| `upload_rejected` | 服务端拒绝（通常因超限） |
| `pairing_wait` | 已配对但没有可用场次（**客户端产生**，带上 `reason`） |
| `file_deleted`（服务端产生） | 完整扫描中发现文件消失 |
| `bulk_rewrite`（服务端产生） | 单轮内大量文件内容变化 |
| `offline`（服务端产生） | 超时未 tick |
| `enroll_conflict`（服务端产生） | 指纹撞上在线的机器，拒绝自动认回 |
| `enroll_ambiguous`（服务端产生） | 多台机器共用同一指纹，无法自动认回 |
| `bind`（服务端产生） | 机器被配对到某个名单条目 |

---

## 7. `config` 对象

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

## 8. Agent 本地状态

### 凭据文件

`agent.ini`（默认 `/etc/syncoj/agent.ini`，0600，属主 = 运行 Agent 的账号）：

| 键 | 说明 |
|---|---|
| `server_url` | 服务端地址 |
| `bootstrap_key_file` | 统一密钥路径，**root 只读**，默认 `/etc/syncoj/bootstrap.key` |
| `state_dir` | 本地状态目录，默认 `/var/lib/syncoj`。token、machine_uuid、下载分片都在这里 |
| `deploy_root` | 下发落盘根目录，默认 `{desktop}` |
| `scan_roots` | 扫描根目录列表 |
| `pairing_show_on_desktop` | 是否把配对码写到桌面，默认 `true` |
| `pairing_file_name` | 配对码文件名，默认 `配对码.txt` |

桌面上可能出现的两个提示文件（配对成功后都要删掉，留着会让下一场的人以为那是
新的码）：

| 文件 | 什么时候出现 | 内容 |
|---|---|---|
| 配对码文件（`pairing_file_name`） | `claimed=false` | 六位配对码 |
| `等待场次.txt`（**常量**，不可配） | `claimed=true, bound=false` | 服务端给的 `reason` 原话 |

第二个刻意不做成配置键：它是"机器已配对但还不能干活"的说明，名字必须能被教师
一眼看懂是同一类东西，而没有第二个使用场景需要定制它。内容必须是**服务端给的
原话**，不能由客户端兜底 —— 客户端编不出一句准确的"为什么还没有你的场次"。

**不再有 `enroll_code`。** 它属于被取代的每选手注册码链路；`config.py` 里
残留的 `enroll_code` 字段、`SYNCOJ_ENROLL_CODE` 环境变量映射、以及
`config.example.ini` 里的注释**都要删掉**，否则运维会照着配置一个不存在的功能。

### 状态机

```
启动
 ├─ state_dir 里没有 token ──────────────▶ enroll
 └─ 有 token ──▶ tick
                 ├─ 200 claimed=false        ──▶ 写配对码.txt，next_tick 秒后再 tick
                 ├─ 200 claimed=true bound=false ──▶ 写等待场次.txt，再 tick
                 ├─ 200 claimed=true bound=true  ──▶ 正常循环（扫描 + 上传 + 下发）
                 ├─ 401 ──▶ 清 token，回到 enroll
                 └─ 403 pairing_required / no_active_contest
                       ──▶ 只更新状态，按原节奏继续 tick
                           （**不要 enroll**；提示文件和配对码由下一次 200 带回来）
```

配对码文件在**配对成功后必须删掉** —— 留在桌面上会让下一场的人以为那是新的码。

### `--provision` 与 `--check`

- `--provision`：一次性装机动作（写 systemd unit、建状态目录、chown 凭据文件），
  **不注册**
- `--check`：只做本地自检（配置能不能解析、密钥文件读不读得到、桌面目录在不在），
  **不联网**

---

## 9. 局域网发现（UDP 广播）

装 50 台机器时，"服务端地址"是唯一还得人手输的一项。主路径是**包内嵌地址**
（见 §10），这一节是兜底：包没带地址、或者服务端换了 IP 时，机器喊一声。

```
机器 ──UDP 广播──▶ 255.255.255.255:45871  （以及各接口的子网广播地址）
                  {"syncoj":"syncoj","v":1,"nonce":"<32 位十六进制>",
                   "machine_id":"...","pad":"<填充，凑到 640 字节>"}

服务端 ──UDP 单播──▶ 来源地址
                  {"syncoj":"syncoj","v":1,"nonce":"<原样带回>",
                   "url":"http://10.0.0.5:8000","key_id":"...","sig":"<base64url>"}
```

**签名对象是一段规范化文本，不是 JSON** —— 两边各自 `json.dumps` 出来的字节可能
不一样（键序、空格、转义），而签名是逐字节比的：

```
syncoj-discovery-v1\n<nonce>\n<url>
```

版本前缀的作用是**跨协议隔离**：将来若有另一处也用这把私钥签东西，两边的签名对象
不会长得一样，一个签名就不能被搬到另一个场景里用。

### 规矩

| 规矩 | 为什么 |
|---|---|
| 应答必须能**验签**（发布公钥，与自更新同一把） | 机器找到服务端后立刻要发**统一注册密钥**过去。不验签就等于把它交给局域网里任何一个应答者 —— 而那个密钥是这间机房所有机器共享的，拿它做 HMAC 也挡不住 |
| 机器上**没有公钥就不发现** | 分不出"服务端"和"随便一个应答者"时不猜。与"没有密钥就不升级"同一个默认 |
| 每次探测现生成 `nonce`，对不上就不认 | 防重放：录下一条真应答原样发回来没用 |
| **应答不得大于探测** | 这是个无状态反射点，伪造来源地址就能借它放大流量。探测靠 `pad` 凑到 640 字节；比这短的探测服务端一律不理，地址太长时宁可**不应答** |
| 按来源 IP 限速 | 挡"一个来源反复刷" |
| 多个**都验得过**的地址 = 歧义，不许猜 | 两个考场共用一层楼时很常见；猜错的表现是"代码交上去了但成绩是空的"，且现场看不出来 |

### 机器这边的使用时机

- **装机时**（`install.py`）：优先级 `--server` > 包内 `server.json` > 发现 > 默认值。
  落到默认值时会打一条显眼的警告 —— 它是 `127.0.0.1`，意味着机器去连自己
- **运行时**：连续若干轮连不上服务端之后才问一次（约 5+10+20 秒），因为常见原因是
  服务端重启或网线抖动，那些情况下地址并没有变，而广播会打扰整个局域网。
  问到的新地址**只在这个进程内生效**，不写回 `agent.ini`

---

## 10. 服务端内部约定（Agent 无需关心，但影响目录布局）

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
