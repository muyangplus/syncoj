# SyncOJ Agent ⇄ Server 协议

改协议时同步修改：

- `server/syncoj_server/schemas.py`（pydantic 模型，OpenAPI 来源）
- `server/syncoj_server/errors.py`（错误码清单）
- `server/syncoj_server/protocol.py` 与 `agent/syncoj_agent/client.py` 的协议版本号
- `agent/syncoj_agent/client.py`、`main.py`（报文构造与状态机）
- `server/tests/test_agent_contract.py`（契约测试）
- `docs/reference/api-conventions.md`（管理端同类约定）

## 0. 总则

| 项 | 约定 |
|---|---|
| 传输 | HTTP/1.1；生产环境 HTTPS |
| 编码 | 请求 / 响应体 UTF-8 JSON；文件内容走 multipart 或原始字节流 |
| 认证 | `Authorization: Bearer <token>`；`/enroll`、`/whoami`、`/install*` 不鉴权 |
| 时间 | Unix 秒（UTC）；诊断回执的 `received_at` 为 ISO-8601 |
| 路径 | POSIX 相对路径；拒绝反斜杠、绝对路径、`..` |
| 命名 | JSON 字段 `snake_case` |
| 未知字段 | 请求体多出字段 → `422 validation_error`（`extra="forbid"`） |
| 轮询 | 节奏由服务端 `next_tick_seconds` 决定 |
| 协议版本 | `protocol_version`，当前 = 最低要求 = `2`；只在破坏性变更时加一 |

端点一览（前缀 `/api/v1/agent`）：

| 方法 / 路径 | 鉴权 | 章节 |
|---|---|---|
| `POST /enroll` | 统一密钥（报文内） | 2 |
| `POST /tick` | 任意有效凭据 | 3 |
| `POST /files` | 能干活的凭据 | 4 |
| `GET /assets/{asset_id}` | 能干活的凭据 | 5 |
| `POST /events` | 任意有效凭据 | 6 |
| `POST /diagnostics` | 任意有效凭据 | 6.1 |
| `GET /releases/{release_id}` | 任意有效凭据 | 升级包下载；只给已铺开且未撤回的版本 |
| `GET /me` | 能干活的凭据 | 自检：身份、场次、在线状态、`config` |
| `GET /whoami?nonce=` | 无 | 9.1 |
| `GET /install.json`、`/install/bundle`、`/install/installer`、`/install/bootstrap.sh` | 无 | 装机入口 |

"任意有效凭据" = 未配对 / 无场次的机器也可调用；"能干活的凭据" = 已解析出场次与选手，否则 `403`。

### 0.1 错误响应

所有错误响应同一形状：

```json
{ "detail": "给人看的中文", "code": "pairing_required", "details": { } }
```

| 字段 | 类型 | 必填 | 含义 |
|---|---|---|---|
| `detail` | string | 是 | 可直接显示的中文句子 |
| `code` | string | 是 | 稳定短标识符；分支判断只用它，不比较 `detail` |
| `details` | object | 否 | 结构化补充信息 |

Agent 的分支（先看状态码，再看 `code`）：

| 条件 | Agent 动作 |
|---|---|
| `429` | 按 `Retry-After` 退避，上限 300 秒 |
| `code` ∈ `bootstrap_key_invalid` / `bootstrap_key_revoked` / `bootstrap_key_expired` | 记 `bootstrap_key_rejected` 事件，按最长间隔（300 秒）重试 |
| `401`（`unauthorized` / `token_expired` / `machine_revoked`） | 清本地凭据，重新 `/enroll` |
| `403`（`pairing_required` / `no_active_contest` / `ambiguous_contest` / `contest_missing` / `contest_player_missing`） | 不重新注册；更新状态，等下一轮 tick |
| `409` | 静默丢弃 |
| `413` | 跳过该文件 |
| `426` / `agent_outdated` | 记日志，按协议错误退避；不重新注册 |
| `5xx` | 指数退避（5 秒起，上限 300 秒） |
| 其他 | 记日志，退避后下一轮 |

- 只有 `401` 清凭据重新注册；`403` 只更新状态。
- `pair_code_invalid` 只发给管理端浏览器，Agent 收不到。

### 0.2 状态码语义（Agent 视角）

| 状态码 | `code` | 含义 | Agent 处理 |
|---|---|---|---|
| 400 | `bad_request` | 路径 / 哈希格式不合法，或上传内容与声明的 `sha256` 不符 | 记日志，不重试；等下一轮 |
| 401 | `unauthorized` / `machine_revoked` | 凭据无效 / 机器已作废 | 清空本地凭据，重新 `/enroll` |
| 403 | 见 0.1 | 凭据有效，但未配对 / 无场次 / 资源未下发给本选手 | 不重新注册；等下一轮 |
| 404 | `not_found` 等 | 资源不存在 | 记日志，不重试 |
| 409 | `stale_upload` / `upload_not_requested` / `contest_not_started` / `contest_ended` / `machine_mismatch` | 上传被拒，或 `machine_id` 与凭据不符 | 静默丢弃；下一轮 tick 给出当前需要 |
| 410 | `gone` | 服务端资源内容缺失 | 上报审计事件 |
| 411 | `bad_request` | `/files` 或 `/tick` 未带 `Content-Length` | — |
| 413 | `payload_too_large` | 文件超过策略上限；或请求体超过 `max_upload_request_bytes`（4 MiB）/ `max_tick_request_bytes`（8 MiB），按 `Content-Length` 在读体之前判定 | 跳过该文件，不重试 |
| 416 | — | Range 不合法，带 `Content-Range: bytes */<size>` | 记日志，等下一轮 |
| 422 | `validation_error` | 报文缺字段 / 超长 / 多字段 | 记日志 |
| 426 | `agent_outdated` | `/enroll` / `/tick` 的 `protocol_version` 低于最低要求（缺省按 0）；优先于 422 判定；`details.protocol_version` = 上报值 | 只能重装 |
| 429 | `rate_limited` | 限速，带 `Retry-After` | 按 `Retry-After` 退避 |
| 5xx | `internal_error` / `unavailable` | 服务端故障 | 指数退避 |

---

## 1. 配对模型

```
装机（root 一次性）          机器首次开机              教师
bootstrap.key ──换凭据──▶  桌面「配对码.txt」 ──读码输入──▶ 绑到名单里的人
```

- 配对永久有效，绑定对象是 `roster_entry`（名单条目 = 人），不是 `player`（某场次的参赛者）。
- 同一学生换场次不重新配对；新场次应用该名单即可。
- 配对码只在绑定时使用；之后按 `machine_uuid` 认机器。

### 三个状态

| `claimed` | `bound` | 含义 | Agent 动作 |
|---|---|---|---|
| `false` | `false` | 未配对 | 把 `pair_code` 写到桌面配对码文件；不扫描；等 tick |
| `true` | `false` | 已配对，无含本人的场次 | 写 `等待场次.txt`（内容 = `reason`）；不扫描；等 tick |
| `true` | `true` | 正常 | 扫描代码、接收下发 |

### 机器身份的三个要素

| 字段 | 来源 | 作用 |
|---|---|---|
| `machine_uuid` | Agent 首次运行生成并持久化 | 首选身份；换凭据、重新注册按它匹配 |
| `machine_fingerprint` | SMBIOS UUID | 第二依据；整机快照还原后不变 |
| `machine_id` | `/etc/machine-id` | 仅供人工辨认，不参与身份判定 |

- `machine_fingerprint` 读不到时省略；不用主机名或 machine-id 顶替。

### 认机器的顺序（服务端）

1. `machine_uuid` 命中 → 该机器。
2. UUID 未命中，指纹命中一台离线机器 → 认回该机器，记录新 UUID。
3. 指纹命中在线机器（事件 `enroll_conflict`）或多台机器（事件 `enroll_ambiguous`）→ 不自动认回，按新机器处理。
4. 均未命中 → 新机器，发六位配对码。

"在线"阈值 = 运行参数 `offline_after_seconds`（默认 90）。

---

## 2. `POST /api/v1/agent/enroll`

用统一密钥换长期凭据。不鉴权；限速：单 IP 10 次/秒、全局 50 次/秒。

### 请求

```json
{
  "protocol_version": 2,
  "bootstrap_key": "43 字符的镜像内置密钥",
  "machine_id": "0123456789abcdef0123456789abcdef",
  "machine_uuid": "3f2a…",
  "machine_fingerprint": "4c4c4544-0031-3010-8043-b7c04f4d4432",
  "hostname": "exam-pc-01",
  "agent_version": "0.1.0",
  "os_info": "Linux 5.4.0 Ubuntu 20.04.6 LTS"
}
```

| 字段 | 类型 | 必填 | 限制 / 含义 |
|---|---|---|---|
| `protocol_version` | int | 否 | 0–10000，缺省 0 |
| `bootstrap_key` | string | 是 | 1–256 字符；从 root 只读文件读取（默认 `/etc/syncoj/bootstrap.key`） |
| `machine_id` | string | 是 | 1–128 字符 |
| `machine_uuid` | string | 否 | ≤64 |
| `machine_fingerprint` | string | 否 | ≤128 |
| `hostname` | string | 否 | ≤128 |
| `agent_version` | string | 否 | ≤32 |
| `os_info` | string | 否 | ≤200 |

### 响应 `200`：三态

| 字段 | 类型 | 含义 |
|---|---|---|
| `token` | string | 长期凭据（32 字节随机数，约 43 字符） |
| `agent_id` | int | 机器 id |
| `claimed` | bool | 已绑定名单条目 |
| `bound` | bool | 已解析出场次与选手 |
| `pair_code` | string \| null | 未配对时的六位配对码 |
| `reason` | string \| null | 未就绪原因 |
| `player_no` | string | 默认 `""` |
| `player_name` | string \| null | |
| `contest_id` | int \| null | |
| `contest_slug` | string | 默认 `""` |
| `contest_name` | string | 默认 `""` |
| `config` | object | 见第 7 节 |

未配对：

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
  "config": { "…": "见第 7 节" }
}
```

已配对，无场次：

```json
{
  "token": "…", "agent_id": 7, "claimed": true, "bound": false,
  "pair_code": null,
  "reason": "还没有包含你的场次",
  "config": { "…": "见第 7 节" }
}
```

正常：

```json
{
  "token": "…", "agent_id": 7, "claimed": true, "bound": true,
  "pair_code": null,
  "player_no": "S001", "player_name": "张三",
  "contest_id": 1, "contest_slug": "mock-1", "contest_name": "校内模拟赛",
  "config": { "…": "见第 7 节" }
}
```

### 规则

- `claimed=false` 时 `token` 有效；Agent 写配对码后按 tick 等待，不重新注册。
- 统一密钥可重复使用、可吊销、可设过期时间；吊销不影响已注册机器。
- 重新注册（UUID 或指纹认回）→ 换发新 token，旧 token 立即失效。
- 未配对机器每次注册换新配对码。
- 配对码：六位数字，与所有有效码不重复；服务端只存哈希；有效期 30 分钟；一次性，绑定后作废。

### 错误

| 状态码 | `code` | 含义 |
|---|---|---|
| `404` | `bootstrap_key_invalid` | 统一密钥不存在 |
| `403` | `bootstrap_key_revoked` | 统一密钥已吊销 |
| `403` | `bootstrap_key_expired` | 统一密钥已过期 |
| `422` | `validation_error` | 报文缺字段 / 字段过长 |
| `426` | `agent_outdated` | 协议版本过低 |
| `429` | `rate_limited` | 限速，带 `Retry-After` |

---

## 3. `POST /api/v1/agent/tick`

核心接口；幂等（重复、补发、乱序不破坏状态）。请求体上限 8 MiB。

### 请求

```json
{
  "protocol_version": 2,
  "agent_version": "0.1.0",
  "machine_id": "0123456789abcdef0123456789abcdef",
  "hostname": "exam-pc-01",
  "ts": 1767225600,
  "scan_root": "/home/student/桌面",
  "scan": [
    { "path": "code/main.cpp", "sha256": "ab12…", "size": 1234, "mtime": 1767225500 }
  ],
  "scan_complete": true,
  "deploy_progress": [ { "target_id": 42, "bytes_done": 3145728 } ],
  "deploy_done": [ { "target_id": 7, "sha256": "cd34…" } ],
  "deploy_failed": [ { "target_id": 9, "reason": "写盘失败: [Errno 28] No space left on device" } ],
  "release_public_key": true,
  "scanned_for": { "player_no": "S001", "contest_slug": "mock-1" },
  "stats": { "disk_free": 10737418240, "last_error": null, "queue": 0, "scan_missing": [] }
}
```

| 字段 | 类型 | 必填 | 限制 / 含义 |
|---|---|---|---|
| `protocol_version` | int | 否 | 0–10000，缺省 0 |
| `agent_version` | string | 否 | ≤32 |
| `machine_id` | string | 是 | 1–128；与凭据不符 → `409 machine_mismatch` |
| `hostname` | string | 否 | ≤128；未配对 / 无场次时每次 tick 更新服务端记录的主机名 |
| `ts` | int | 是 | ≥0 |
| `scan_root` | string | 否 | ≤512 |
| `scan` | array | 否 | 全量文件索引，不含内容；≤50000 条。元素：`path`（≤1024）、`sha256`（64 位十六进制）、`size`（≥0）、`mtime`（≥0） |
| `scan_complete` | bool | 是 | 本轮是否完整覆盖全部扫描根；`false` → 服务端跳过删除判定 |
| `deploy_progress` | array | 否 | ≤1000；`{target_id, bytes_done≥0}`：本机分片已有字节数，只用于进度条 |
| `deploy_done` | array | 否 | ≤1000；`{target_id, sha256(64)}`：已落地目标及落地内容的 sha256 |
| `deploy_failed` | array | 否 | ≤1000；`{target_id, reason(≤300)}`：本轮未下成的目标与原因 |
| `release_public_key` | bool | 否 | 本机有无发布公钥（升级信任锚）；缺省 `false` |
| `scanned_for` | object | 否 | 本轮扫描根按哪个身份展开：`player_no`（≤64）、`contest_slug`（≤128）；缺省按"一致" |
| `stats.disk_free` | int | 否 | 字节 |
| `stats.last_error` | string | 否 | ≤1024 |
| `stats.queue` | int | 否 | 待上报审计事件数，默认 0 |
| `stats.scan_missing` | array | 否 | 本机不存在的扫描根（绝对路径，最多 5 条） |

#### `scan` 规则

- 路径 = `<扫描根目录名>/<相对路径>`（受 `scan.prefix` 影响），NFC 归一化；服务端按 NFC 存、按 NFC 回 `need_upload`；Agent 上传时按本地真实文件名打开。
- 文件名非合法 UTF-8 的文件不进 `scan`，只写入 `stats.last_error`。
- 漏扫（权限错误等）必须置 `scan_complete=false`。
- `max_files`（默认 5000）是整台机器的总数，不是每个根各一份。
- 服务端截断超过 `max_files_per_scan`（默认 5000）的 `scan` 并按 `scan_complete=false` 处理，记事件 `scan_too_large`。

#### 对账规则

- `scanned_for` 与服务端当前身份不同 → 整份不对账：台账不动、`need_upload` 为空、记事件 `scan_identity_mismatch`；响应带新身份。Agent 发现身份变化后重算扫描根、清哈希缓存，本轮不上传不下载。
- 场次设了时间窗时，`[starts_at, ends_at)` 之外的 tick 不对账：台账不动、`need_upload` 为空、按空闲节奏回 `next_tick_seconds`；下发与在线状态照常。
- 服务端只认与资产当前内容一致的 `deploy_done`。
- 连续 5 次 `deploy_failed` → 目标判 `failed`、不再派发，原因显示在管理端；内容变更、手工重试、`deploy_progress` 比上次长时计数清零。

### 响应 `200`

```json
{
  "server_time": 1767225600,
  "next_tick_seconds": 2,
  "need_upload": ["code/main.cpp"],
  "deploy_jobs": [
    {
      "target_id": 42,
      "asset_id": 42,
      "url": "/api/v1/agent/assets/42",
      "sha256": "cd34…",
      "size": 10485760,
      "dest": "题面.zip",
      "mode": "overwrite"
    }
  ],
  "upgrade": null,
  "uninstall_token": null,
  "diagnostics_request": false,
  "config": { "…": "见第 7 节" },
  "claimed": true,
  "bound": true,
  "pair_code": null,
  "reason": null,
  "player_no": "S001",
  "contest_slug": "mock-1"
}
```

| 字段 | 类型 | 含义 |
|---|---|---|
| `server_time` | int | 服务端 Unix 秒 |
| `next_tick_seconds` | int | 下次 tick 间隔 |
| `need_upload` | string[] | 待上传路径，最多 500 条 |
| `deploy_jobs` | array | 下发作业，字段见下表 |
| `upgrade` | object \| null | 已铺开且未撤回的版本；服务端无签名私钥时为 `null` |
| `uninstall_token` | string \| null | 一次性卸载授权令牌；仅当教师请求过卸载且本机报过 `release_public_key=true` |
| `diagnostics_request` | bool | 教师请求过诊断包 |
| `config` | object | 见第 7 节 |
| `claimed` / `bound` / `pair_code` / `reason` | | 三态，见第 1 节 |
| `player_no` / `contest_slug` | string \| null | 当前身份 |

`deploy_jobs[]`：

| 字段 | 类型 | 含义 |
|---|---|---|
| `target_id` | int | 下发目标 id；进度与回执按它报 |
| `asset_id` | int | 资产 id |
| `url` | string | 下载地址 |
| `sha256` | string | 内容哈希 |
| `size` | int | 字节数 |
| `dest` | string | 相对 `deploy_root` 的落地路径 |
| `mode` | string | 默认 `overwrite` |

`upgrade`：`version`、`url`（`/api/v1/agent/releases/{id}`）、`sha256`、`signature`、`size`（默认 0）、`notes`。安装策略以包内 `install_policy.json` 为准，不在清单里。

### 规则

- `next_tick_seconds`：本轮有 `need_upload`、`deploy_jobs` 或发现删除 → `tick_active_seconds`（默认 2，范围 1–60）；否则 `tick_idle_seconds`（默认 30，范围 5–3600）。两者与 `offline_after_seconds`（默认 90，范围 10–86400）在管理端「运行参数」修改，下一轮生效。
- `need_upload` 由服务端决定，Agent 只执行。
- 下发作业按 `target_id` 派发；作业不带续传偏移，续传位置只看本地分片 `deploy_root/.syncoj-parts/<sha256>.part`。
- 收到 `deploy_done` 之前每轮都派发，即使进度已满长；已取消的目标不派发、不可下载。
- `diagnostics_request=true` 只出现一次，服务端返回时即清标记，不重发。
- `uninstall_token` 在多轮心跳里原样重发；教师再次请求换新令牌。
- `dest` 中的 `{player_no}` 已由服务端展开。通用资料：`"题面.zip"`；按题附件：`"<准考证号>/<题目名>/<文件名>"`。
- 下发资产一律按文件落地，不解压；密码以普通资产（如 `password.txt`）下发。

### 未配对 / 无场次的机器

响应形状相同，此时：

- 请求里的 `scan` 一律丢弃；Agent 发空 `scan` 且 `scan_complete=false`。
- `need_upload`、`deploy_jobs` 为空；`next_tick_seconds` = `tick_idle_seconds`。
- `upgrade`、`uninstall_token`、`diagnostics_request` 照常给。
- `claimed=false` 时每次都带 `pair_code`：有效期内为同一个码；过期、缓存丢失或被解绑后发新码。明文只存于服务端内存（`services/paircodes.py`），不落库、不落盘。
- 配对完成后下一次 tick 带 `claimed=true` 与身份；Agent 直接转入正常扫描，不再 enroll。
- 调用 `/files`、`/assets` 返回 `403`，`code` 为 `pairing_required` / `no_active_contest` / `ambiguous_contest` / `contest_missing` / `contest_player_missing` 之一，`details` = `{agent_id, paired}`。

---

## 4. `POST /api/v1/agent/files`

上传单个文件内容。`multipart/form-data`。请求体上限 4 MiB（`max_upload_request_bytes`）；文件内容上限 `max_file_size`（默认 2 MiB）。

| 字段 | 位置 | 必填 | 含义 |
|---|---|---|---|
| `path` | form | 是 | POSIX 相对路径，与 tick 上报一致，且此刻在待上传列表中 |
| `sha256` | form | 是 | 文件内容的 sha256（上传前重新计算） |
| `file` | file | 是 | 文件内容 |

### 响应 `200`

```json
{ "path": "code/main.cpp", "sha256": "ab12…", "revision": 1, "stored": true }
```

### 旧版本保护

台账分别记"最近一次扫描到的"（`scanned_sha256`，tick 更新）与"已存下的"（`sha256`，上传成功更新）；两者不等时路径持续出现在 `need_upload`。

| 情况 | 结果 |
|---|---|
| 路径不在台账 | `409 upload_not_requested`，`details.reason="not_requested"`，不落盘 |
| 场次时间窗之外 | `409 contest_not_started` / `contest_ended`，`details` = `{reason, starts_at, ends_at}`，不落盘 |
| 与已存下的同一份内容 | `200`，幂等，`revision` 不变 |
| `sha256` ≠ 最近一次扫描到的版本 | `409 stale_upload`，`details` = `{reason: "stale", expected: <最近一次扫描到的 sha256>}` |
| 内容与声明的 `sha256` 不符 | `400` |
| 一致 | 先落到 `source/`，再更新台账，`revision += 1` |
| 落盘失败 | `500`，台账不动，下一轮仍要求上传 |

- Agent 收到 `409` 静默丢弃。
- Agent 只上传 `need_upload` 里的路径。

---

## 5. `GET /api/v1/agent/assets/{asset_id}`

下载下发文件，支持 HTTP Range。

| 请求头 | 含义 |
|---|---|
| `Range: bytes=<start>-` | 从指定偏移续传（也接受 `bytes=<start>-<end>`、`bytes=-<suffix>`） |

| 响应 | 含义 |
|---|---|
| `200` | 完整内容（未带 Range，或代理剥离了 Range） |
| `206` | 部分内容，带 `Content-Range` |
| `403` | 资源未下发给本选手（或目标 / 任务已取消） |
| `404` | 资源不存在或不属于本场次 |
| `410` | 资源内容缺失 |
| `416` | 范围不合法 |
| `500` | 内容大小与台账不符 |

- 请求了 Range 却收到 `200` → 丢弃本地分片，从头写入。
- 只能下载下发给本选手的资源。

---

## 6. `POST /api/v1/agent/events`

批量上报审计事件。鉴权与 tick 相同；未配对时只记 `agent_id`，场次与选手留空。单次最多处理前 200 条。

```json
[
  { "level": "warning", "category": "disk_full",
    "message": "磁盘空间不足", "meta": { "free": 1024 } }
]
```

| 字段 | 类型 | 必填 | 限制 |
|---|---|---|---|
| `level` | string | 否 | ≤16，默认 `info` |
| `category` | string | 是 | ≤32 |
| `message` | string | 是 | ≤2000 |
| `meta` | object | 否 | 序列化后截断到 8000 字符 |

响应：`{"ok": true, "detail": "已记录 N 条"}`。

常见 `category`：

| category | 产生方 | 触发条件 |
|---|---|---|
| `scan_error` | Agent | 扫描出错 |
| `oversize_skipped` | Agent | 文件超过大小上限被跳过 |
| `deploy_failed` | Agent | 下发失败 |
| `upload_rejected` | Agent | 上传被服务端拒绝 |
| `pairing_wait` | Agent | 已配对但无可用场次，带 `reason` |
| `bootstrap_key_rejected` | Agent | 统一密钥被拒 |
| `upgrade_failed` / `upgrade_activate_failed` / `upgrade_rollback_failed` | Agent | 升级失败 / 激活失败 / 回滚失败 |
| `uninstall_handoff` / `uninstall_failed` | Agent | 卸载交接 / 卸载失败 |
| `scan_incomplete` | 服务端 | 扫描不完整，已跳过删除判定 |
| `scan_rejected` | 服务端 | 扫描结果中有条目被拒 |
| `scan_too_large` | 服务端 | `scan` 超过 `max_files_per_scan` 被截断 |
| `scan_identity_mismatch` | 服务端 | `scanned_for` 与当前身份不符 |
| `file_deleted` | 服务端 | 完整扫描中发现文件消失 |
| `bulk_rewrite` | 服务端 | 单轮 ≥20 个文件内容变化 |
| `offline` | 服务端 | 超时未 tick |
| `enroll` | 服务端 | 注册 / 重新注册 |
| `enroll_conflict` | 服务端 | 指纹撞上在线机器，拒绝自动认回 |
| `enroll_ambiguous` | 服务端 | 多台机器共用同一指纹 |
| `bind` | 服务端 | 机器被配对到名单条目 |

- `scan_incomplete`、`scan_rejected` 只在状态变化时记录。

---

## 6.1 `POST /api/v1/agent/diagnostics`

回传 gzip 压缩的诊断包（Agent 每 600 秒、连续 2 轮失败后、或收到 `diagnostics_request` 时）。

### 请求

| 项 | 值 |
|---|---|
| 认证 | 与 tick 相同（未配对机器可调用） |
| `Content-Type` | `application/gzip` |
| body | 原始 gzip 字节；解压后为 `diagnostics.build_bundle(...)` 生成的 JSON 对象 |

### 上限

| 项 | 上限 | 超出 |
|---|---|---|
| 压缩后 | 256 KB | `413 payload_too_large` |
| 解压后 | 1 MB | `413 payload_too_large` |

- 解压用带上限的流式解压（`zlib.decompressobj(16 + zlib.MAX_WBITS)` + `max_length`）。
- 解压结果必须是 JSON 对象；不逐字段校验。坏 gzip / 非 UTF-8 / 非 JSON / 非对象 → `400 bad_request`。

### 限速

- 按 `agent_id` 每 60 秒一份；超出 → `429` + `Retry-After`（秒）。
- Agent 对 `429` 不重试，下一周期再传。

### 响应 `200`

```json
{ "received_at": "2026-10-07T10:00:00Z", "bytes": 4096 }
```

| 字段 | 含义 |
|---|---|
| `received_at` | 服务端接收时刻（ISO-8601，UTC） |
| `bytes` | 解压后 JSON 文本字节数 |

### 存储

- 每台机器只留最新一份（按 `agent_id` 覆盖），存解压后的 JSON 文本。

---

## 7. `config` 对象

服务端下发；Agent 内置等价默认值（契约测试对账）。

```json
{
  "extensions": [".c", ".cpp", "…"],
  "exclude_dirs": [".git", "build", "…"],
  "exclude_suffixes": [".swp", ".tmp", "…"],
  "max_file_size": 2097152,
  "scan_interval": 30,
  "max_files": 5000,
  "policy_version": 1,
  "tick_idle_seconds": 30,
  "tick_active_seconds": 2
}
```

| 字段 | 默认值 |
|---|---|
| `extensions` | `.c .cc .cpp .cxx .h .hpp .pas .pp .java .py .pyw .kt .cs .go .rs .js .ts` |
| `exclude_dirs` | `.git .svn .hg .idea .vscode __pycache__ node_modules build dist cmake-build-debug cmake-build-release .cache target` |
| `exclude_suffixes` | `.swp .swx .tmp .temp .bak .orig .rej .pyc .pyo ~` |
| `max_file_size` | 服务端 `max_file_size`（默认 2097152） |
| `scan_interval` | = `tick_idle_seconds` |
| `max_files` | 服务端 `max_files_per_scan`（默认 5000） |
| `policy_version` | `1` |
| `tick_idle_seconds` / `tick_active_seconds` | 运行参数（默认 30 / 2） |

- Agent 逐键校验合并：某键不合法只回退该键。

---

## 8. Agent 本地状态

### 文件

| 文件 | 内容 |
|---|---|
| `/etc/syncoj/agent.ini` | 配置，`0600`，属主 = 运行账号；逐键说明见 `docs/reference/config-keys.md` |
| `/etc/syncoj/bootstrap.key` | 统一注册密钥，root 只读 |
| `/etc/syncoj/machine_uuid` | 权威机器 UUID；读不到时退回 `<state_dir>/machine_uuid` |
| `<state_dir>/credential.json` | token 与身份 |
| `<state_dir>/hash_cache.json` | 哈希缓存 |
| `<state_dir>/agent.log` | 日志（`log.file` 留空时） |
| `<deploy_root>/.syncoj-parts/<sha256>.part` | 未完成的下载分片 |

桌面提示文件（写在 `deploy_root` 下，状态变为可干活后删除）：

| 文件 | 出现条件 | 内容 |
|---|---|---|
| 配对码文件（`pairing.file_name`，默认 `配对码.txt`） | `claimed=false` | 六位配对码 |
| `等待场次.txt`（常量，不可配） | `claimed=true, bound=false` | 服务端 `reason` 原文 |

- `pairing.show_on_desktop=false` 时两个文件都不写。

### 状态机

```
启动
 ├─ 无 token ──────────────────▶ enroll
 └─ 有 token ──▶ tick
                 ├─ 200 claimed=false            ──▶ 写配对码文件，next_tick 秒后再 tick
                 ├─ 200 claimed=true bound=false ──▶ 写等待场次.txt，再 tick
                 ├─ 200 claimed=true bound=true  ──▶ 正常循环（扫描 + 上传 + 下发）
                 ├─ 401 ──▶ 清 token，回到 enroll
                 └─ 403 ──▶ 只更新状态，按原节奏继续 tick（不 enroll）
```

### 命令行参数

| 参数 | 行为 |
|---|---|
| `--check` | 只校验配置；不联网、不运行 |
| `--once` | 只跑一轮后退出 |
| `--dump-config` | 打印解析后的配置（JSON）后退出 |
| `--provision` | 只注册，拿到凭据即退出（装机时 root 一次性单元调用） |
| `--chown-to <用户>` | 配合 `--provision`：把凭据 / 身份文件交给该用户 |
| `--pair-code` | 打印本机配对码（未配对时） |
| `--config` / `-c` | 配置文件路径 |
| `--verbose` / `-v` | 调试日志输出到 stderr |
| `--version` | 打印版本 |

---

## 9. 局域网发现（UDP 广播）

用于包内未带服务端地址或服务端换了 IP 的情况。

```
机器 ──UDP 广播──▶ 255.255.255.255:45871（及各接口的子网广播地址）
                  {"syncoj":"syncoj","v":1,"nonce":"<32 位十六进制>",
                   "machine_id":"...","pad":"<填充，凑到 640 字节>"}

服务端 ──UDP 单播──▶ 来源地址
                  {"syncoj":"syncoj","v":1,"nonce":"<原样带回>",
                   "url":"http://10.0.0.5:8000","key_id":"...","sig":"<base64url>"}
```

签名对象（规范化文本，非 JSON）：

```
syncoj-discovery-v1\n<nonce>\n<url>
```

### 规则

| 规则 | 依据 |
|---|---|
| 应答必须用发布公钥验签（与自更新同一把） | 防止把统一注册密钥交给伪造应答者 |
| 机器上没有发布公钥 → 不发现 | 同上 |
| 每次探测新生成 `nonce`，不匹配不认 | 防重放 |
| 探测 < 640 字节不应答；应答不得大于探测，地址过长时不应答 | 防反射放大 |
| 按来源 IP 限速（默认 5 次/秒） | 防刷 |
| 多个验签通过的地址 → 视为歧义，不选择 | 防连错考场 |

- 服务端端口：`SYNCOJ_DISCOVERY_PORT`（默认 45871）；最小探测长度：`SYNCOJ_DISCOVERY_MIN_PROBE`（默认 640）。

### 机器侧使用时机

- 装机（`install.py`）：`--server` > 包内 `server.json` > 发现 > 默认值 `127.0.0.1`（打印警告）。
- 运行时：连续 3 轮网络失败（约 5 + 10 + 20 秒）后发现一次；新地址只在本进程内生效，不写回 `agent.ini`。

## 9.1 服务端主动通告（UDP 广播 + 挑战应答）

服务端启动后前 10 分钟每 30 秒、之后每 5 分钟，以及管理端「召集机器」时，向各广播地址的 UDP 45872 发送（≤512 字节）：

```
{"syncoj":"syncoj","v":1,"kind":"announce","url":"http://10.0.0.5:8000",
 "key_id":"<发布公钥指纹>","boot_id":"<本次启动的随机数>"}
```

通告不签名。机器经 HTTP 核实（不带凭据）：

```
GET <url>/api/v1/agent/whoami?nonce=<32 位十六进制，机器现生成>
→ 200 {"url":"<服务端认定的对外地址>","nonce":"<原样>","key_id":"...","sig":"<base64url>"}
签名对象：syncoj-announce-v1\n<nonce>\n<url>
```

| 规则 |
|---|
| 切换前必须完成挑战应答 |
| 机器切换到签名里的 `url`，不是通告里的 |
| `key_id` 不是本机信任锚 → 丢弃通告 |
| 同一 `boot_id` 只处理一次；每分钟最多处理一次 |
| `whoami` 按来源 IP 限速（默认 5 次/秒，超出 `429`）；无私钥 → `503 no_signing_key`；不知道对外地址 → `503 no_public_url` |
| 第 9 节的机器主动发现保留 |

- 新地址只在本进程内生效，不写回 `agent.ini`。
- `SYNCOJ_ANNOUNCE=0` 关闭通告；`SYNCOJ_ANNOUNCE_PORT` 改端口（全机房同步修改）。
- 设计 → 见 `docs/design/09-server-announce.md`。

---

## 10. 服务端内部约定（影响目录布局）

```
<data_root>/
├── syncoj.db
├── blobs/<sha[:2]>/<sha>                              # 内容寻址
├── source/<contest_slug>/<player_no>/<前缀>/<路径>     # 评测器读取
├── judge_result/<contest_slug>/<player_no>/<题目>/     # 成绩回写扫描源
└── logs/
```

- `source/` 下的路径 = Agent 上报路径原样（含扫描根目录名前缀）。
- 评测器（LemonLime / Arbiter）按此结构配置，无需转换。
