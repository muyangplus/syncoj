# 管理端 API 约定

- 范围：信封、错误体、分页、命名、删除语义（形状），不含各接口语义。
- 各接口字段以 `web/openapi.json`（`server/tools/dump_openapi.py` 生成）为准。
- Agent 侧协议见 `docs/reference/protocol.md`。

## 1. 适用范围

| 规则 | 内容 |
|---|---|
| 集合 | 所有返回集合的 `GET` 用列表信封（第 2 节） |
| 错误 | 所有错误响应用统一错误体，必带 `code`（第 3 节） |
| 前端 | `web/src/api/` 一层消化信封与错误码（第 6 节） |

→ 背景见 `docs/design/05-api-and-frontend.md`。

---

## 2. 列表信封

```json
{ "items": [ … ], "total": 137, "limit": 50, "offset": 0 }
```

| 字段 | 类型 | 含义 |
|---|---|---|
| `items` | array | 当前页数据 |
| `total` | int | 忽略 `limit` / `offset` 后的总条数（不是 `items.length`） |
| `limit` | int | 本次生效的每页条数 |
| `offset` | int | 本次生效的起始位置 |

查询参数（每个 GET 列表接口都支持）：

| 参数 | 默认 | 约束 | 含义 |
|---|---|---|---|
| `limit` | `50` | `ge=1, le=500` | 每页条数 |
| `offset` | `0` | `ge=0` | 起始位置 |

规则：

- 信封只用于 GET 集合；集合再小（场次、名单）也用信封。
- 写操作（如 `POST /contests/{id}/players` 批量导入）返回动作结果，schema 命名 `XxxImportOut` / `XxxResultOut`，字段用 `created` / `updated` / `skipped` 等。
- 资源过滤参数（`contest_id`、`player_id`、`status`、`q` …）走 query string，不放请求体。

### 分页实现

- SQL 可分页的集合（选手、代码文件、事件、评测记录、名单、统一密钥、资产、下发任务）：`LIMIT/OFFSET` + 独立 `COUNT(*)`。
- 统计列（名单条目数、选手的机器数与文件数、资产的未完成目标数、下发任务各状态台数）：只对当前页主键做一次 `GROUP BY`。
- Python 拼出的集合（场次列表，需算在线人数）：允许切 Python 列表，集合规模须有界。

---

## 3. 错误体

所有错误响应（管理端、Agent 侧）同一形状：

```json
{ "detail": "给人看的中文", "code": "roster_entry_taken", "details": { "player_no": "S001" } }
```

| 字段 | 类型 | 必填 | 含义 |
|---|---|---|---|
| `detail` | string | 是 | 可直接显示的中文句子；不含 Markdown 标记 |
| `code` | string | 是 | 稳定短标识符；分支判断用它 |
| `details` | object | 否 | 结构化补充信息（字段名、冲突对象 id…） |

实现：`server/syncoj_server/errors.py`。

- 业务错误抛 `ApiError(409, "roster_entry_taken", "…")`。
- 遗留的 `HTTPException`：`detail` 压成字符串，`code` 按 `CODE_BY_STATUS` 推导。
- 需要分支判断的地方显式给码，其余用兜底码。

### 码表怎么维护

- `ERROR_CODES`：唯一的具名码清单（码 → 默认中文句子）。
- `CODE_BY_STATUS`：未显式给码时的兜底表。

| 状态码 | 兜底 `code` |
|---|---|
| 400 | `bad_request` |
| 401 | `unauthorized` |
| 403 | `forbidden` |
| 404 | `not_found` |
| 409 | `conflict` |
| 410 | `gone` |
| 413 | `payload_too_large` |
| 422 | `validation_error` |
| 426 | `agent_outdated` |
| 429 | `rate_limited` |
| 500 | `internal_error` |
| 503 | `unavailable` |

`server/tests/test_errors.py` 静态检查（AST 扫描）：

| 规则 | 违反后果 |
|---|---|
| 源码发出的每个码都登记在 `ERROR_CODES` | `ApiError` 抛 `ValueError`，接口变 500 |
| `ERROR_CODES` 里的每个码源码都会发出 | 死码被当成契约 |
| `CODE_BY_STATUS` 的每个取值都登记在 `ERROR_CODES` | 兜底码无文档 |
| 用到的每个状态码都在 `CODE_BY_STATUS` | 响应落到 `code: "error"` |
| `detail` 不夹带 Markdown 标记 | — |

- 新增码与产生它的分支一起提交。
- 具名码示例：`agent_not_found`（机器不在台账）、`diagnostics_not_found`（机器尚未回传诊断包，界面显示为正常初态）。

### 422 校验错误

```json
{
  "detail": "每页条数太大了（最大 500）；考号不能为空",
  "code": "validation_error",
  "details": { "errors": [ { "loc": ["query","limit"], "type": "less_than_equal", "message": "…" } ] }
}
```

- `detail` 只列前 3 条，中文。
- `details.errors` 保留完整列表，前端据此标红具体输入框。

### 500

- 兜底 `Exception` handler 必须存在，返回 JSON：`{"detail": …, "code": "internal_error"}`。

---

## 4. 命名

| 项 | 约定 | 例子 |
|---|---|---|
| 前缀 | 管理端 `/api/v1/admin`、Agent `/api/v1/agent`、评测机 `/api/v1/judge` | |
| 集合 | 复数名词 | `/players`、`/agents`、`/assets` |
| 单对象 | `/{集合}/{id}` | `/players/12` |
| 嵌套 | 父资源在前，最多两层 | `/contests/{id}/players` |
| 字段 | `snake_case` | `player_no`、`machine_uuid` |
| 时间 | ISO-8601 字符串（`*_at`）或 Unix 秒（`ts`） | `last_seen_at` |
| 布尔 | 陈述式 | `claimed`、`bound`、`is_active` |

- `PATCH` 只改普通字段；状态变更用子路径动词。

| 动作 | 形状 |
|---|---|
| 改普通字段 | `PATCH /players/{id}` |
| 改状态 | `POST /bootstrap-keys/{id}/revoke`、`POST /deploys/{id}/cancel`、`POST /releases/{id}/yank` |
| 绑定关系 | `POST /machines/{agent_id}/bind` |
| 解绑关系 | `DELETE /agents/{agent_id}/bind` |
| 要一份诊断 | `POST /agents/{id}/diagnostics/request`（机器下一次心跳取走，一次性） |
| 读最新诊断包 | `GET /agents/{id}/diagnostics`（未收到过 → `404 diagnostics_not_found`） |

---

## 5. 删除语义

两类数据，管理端删除都是硬删；区别在确认方式与连带回收。→ 见 [ADR-0003](../decisions/ADR-0003-delete-semantics.md)。

### 5.1 结构性数据 —— 硬删除 + 输入名称确认

名单、名单条目、场次、选手、机器、题目、成绩记录、评测机。

- 单个删除：`confirm` 放 query，如 `DELETE /api/v1/admin/players/12?confirm=S001`。
- 批量 / 清空：`confirm` 放 body，如 `POST /api/v1/admin/contests/3/players/clear`，体 `{"confirm": "mock-1"}`；可与 `keep_*` 等选项并列。
- DELETE 不带请求体。
- `confirm` 去掉首尾空格后逐字比较；不符 → `400 name_mismatch`，`details` = `{expected, given, label}`。
- 校验在服务端完成。

单个删除的确认值：

| 接口 | `confirm` |
|---|---|
| `DELETE /rosters/{id}` | 名单 `name` |
| `DELETE /roster-entries/{id}` | 考号 `player_no` |
| `DELETE /contests/{id}` | 场次 `slug` |
| `DELETE /players/{id}` | 考号 `player_no` |
| `DELETE /problems/{id}` | 题目 `ident` |
| `DELETE /agents/{id}` | 机器 `hostname`（无则 `machine_id`） |
| `DELETE /machines/pending/{id}` | 机器 `hostname`（无则 `machine_id`） |
| `DELETE /contests/{id}/judge/score?player_id=&problem=` | 考号 `player_no` |
| `DELETE /judge-workers/{id}` | 评测机 `name` |

全局范围（无单个对象）用固定字面量 `all`（`schemas.GLOBAL_CONFIRM` / 前端 `GLOBAL_CONFIRM`）。

### 5.2 产物性数据 —— 硬删，内容按引用回收

代码文件、下发资产。不要求 `confirm`。

- `DELETE /files/{id}`：删该行；提交后回收不再被任何记录引用的 blob（代码、资产、Agent 发布共用内容寻址存储）。
- `DELETE /assets/{id}`：删资产，下发任务与逐选手进度外键级联；已落到机器上的文件不撤回。
- `source/<场次>/<考号>/` 下已落盘的文件不随任何删除改动（硬链接）。
- `POST /contests/{id}/players/clear` 不回收 blob。

扫描对账墓碑：

- 选手机器上删除 / 改名的文件，在 `scan_complete=true` 的一轮被置 `deleted_at`（界面"已消失"），行与内容保留；文件重新出现时自动复原。
- 列表接口 `?include_deleted=` 默认 `false`。
- 导出 `GET /contests/{id}/files/export` 的 `include_deleted` 默认 `true`。

### 5.3 清空

清空 = 范围删除，一律硬删、一律在 body 带 `confirm`。

| 接口 | `confirm` | 选项 |
|---|---|---|
| `POST /rosters/{id}/entries/clear` | 名单 `name` | |
| `POST /contests/{id}/players/clear` | 场次 `slug` | `keep_with_submissions`（默认 `true`） |
| `POST /contests/{id}/files/clear` | 场次 `slug` | `purge`（默认 `false` 只删"已消失"的行；`true` 全删，机器仍在上报的文件下一轮重新收回） |
| `POST /contests/{id}/problems/clear` | 场次 `slug` | |
| `POST /contests/{id}/judge/runs/clear` | 场次 `slug` | `player_id`；`keep_manual`（默认 `true`） |
| `POST /events/clear` | 带 `contest_id` 时场次 `slug`，否则 `all` | query：`contest_id`、`level`、`older_than_days` |
| `POST /machines/pending/clear` | `all` | |

---

## 6. 前端怎么接

`web/src/api/`：一个资源一个模块，页面只从 `index.ts` 导入。

| 文件 | 职责 |
|---|---|
| `index.ts` | 入口（re-export） |
| `endpoints.ts` | 所有路径的唯一定义（含 `GLOBAL_CONFIRM`） |
| `types.ts` | 从生成物挑出的类型别名 |
| `schema.d.ts` | 生成物，来自 `web/openapi.json`，不手改 |
| `client.ts` | `request` / `query` / `download`；错误体转成 `ApiError` |
| `crud.ts` | 分页、信封、删除语义的公共实现（`listPage` / `clearCollection` …） |
| `contests.ts`、`files.ts` … | 每资源一个：路径 + 参数 + 返回类型 |

- 新增资源 = 一个资源模块 + 一个页面 + `index.ts` 一行转出。
- 列表数据层（分页、筛选、加载、刷新、批量选择）：`web/src/composables/useList.ts`。

### 6.1 选择必须按稳定键，不能按对象引用

- 选择状态存资源稳定键（`id` 或 `player_no` 等），使用时从当前页取回整行。
- 表格组件带 `row-key` 并开启 `reserve-selection`。
- 有选中项或批量对话框打开时，后台轮询不刷新列表。

---

## 7. 生成物

```
server/syncoj_server/schemas.py        ← 类型的唯一来源
        │  python server/tools/dump_openapi.py
        ▼
web/openapi.json
        │  cd web && npm run gen:types（openapi-typescript）
        ▼
web/src/api/schema.d.ts
```

- 改接口后重跑两步，生成物一并提交。

| 测试 | 检查内容 |
|---|---|
| `server/tests/test_api_conventions.py` | 路由形状：GET 不返回裸数组、信封四字段齐全、信封接口收 `limit`/`offset`、8 个单对象删除与 7 个清空带必填 `confirm`、路径前缀与命名；`test_selfcheck_*` 用合成坏 spec 自证检测器会报错 |
| `server/tests/test_frontend_contract.py` | `endpoints.ts` 与服务端路径双向对齐；`npm run check:routes` 存在且是 `build` 第一步 |
| `web/scripts/check-routes.mjs` | 声明的接口方法都有界面入口 |
