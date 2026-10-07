# 管理端 API 约定

> 本文件管的是**形状**（信封、错误体、分页、命名、删除语义），不是每个接口的语义。
> 每个接口的字段以 `web/openapi.json`（由 `server/tools/dump_openapi.py` 生成）为准。
> Agent 侧对应的是 `docs/reference/protocol.md`。

## 1. 为什么要有约定

约定是**前端写一次数据层，所有资源共用**：集合统一走信封，错误统一带 `code`。

同一件事在 74 个接口里有好几套写法：列表有的返回裸数组、有的返回
`{"items": ...}`；错误有的 `{"detail": "文本"}`、有的 `{"detail": {"reason": ...}}`、
422 又是 pydantic 的英文结构。前端只能逐个接口写适配，于是"少写一个入口"和
"这个接口忘了处理"成了常态。

---

## 2. 列表信封

**所有返回集合的 `GET` 接口**都用同一个形状：

```json
{ "items": [ … ], "total": 137, "limit": 50, "offset": 0 }
```

| 字段 | 说明 |
|---|---|
| `items` | 当前页的数据 |
| `total` | **忽略 `limit`/`offset` 后的总条数**（不是 `items.length`） |
| `limit` | 本次生效的每页条数 |
| `offset` | 本次生效的起始位置 |

> **信封只管 GET 集合**。`POST /contests/{id}/players`（批量导入）这类**写操作**
> 返回的是一个动作结果，不是一页数据 —— 它有自己的形状（创建/更新了几条、
> 哪些被拒绝），硬套 `{items,total,limit,offset}` 会让字段名与含义不符
> （导入 30 条时 `total=30, limit=50` 没有意义）。写操作的返回值见各接口
> 的 schema，命名规则是 `XxxImportOut` / `XxxResultOut`，字段用
> `created` / `updated` / `skipped` 这类动词。

查询参数（**每个** GET 列表接口都支持，名字固定）：

| 参数 | 默认 | 约束 | 说明 |
|---|---|---|---|
| `limit` | `50` | `ge=1, le=500` | 每页条数 |
| `offset` | `0` | `ge=0` | 起始位置 |

> `total` 必须是真实总数。前端的分页器与"清空 N 条"的确认文案都靠它，
> 拿 `items.length` 充数会让第二页显示"共 50 条"。

就算某个集合天然很小（场次、名单），**也照样用信封**：前端因此只需要一个表格
组件、一套分页逻辑。参数给宽一点就行（`limit=500`）。

按资源定制的过滤参数（`contest_id`、`player_id`、`status`、`q` …）一律走 query
string，不塞进请求体 —— 否则分页链接、收藏、刷新就都没法用了。

### 分页实现

- 数据在 SQL 里就能分页的（选手、代码文件、事件、评测记录）→ 真的 `LIMIT/OFFSET`
  加一条独立的 `COUNT(*)`，不要先全查出来再切
- 数据在 Python 里拼出来的（场次列表要算在线人数、名单要算条目数）→ 允许切
  Python 列表，但集合规模必须是有界的（场次数、名单数都是几十）

---

## 3. 错误体

**所有**错误响应都是同一个形状（与管理端、Agent 侧一致）：

```json
{ "detail": "这句是给人看的中文", "code": "roster_entry_taken", "details": { "player_no": "S001" } }
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `detail` | 是 | **永远是字符串**，直接显示给人看 |
| `code` | 是 | 稳定的短标识符，**永远存在**。要分支判断就用它 |
| `details` | 否 | 结构化补充信息（字段名、冲突对象 id…） |

实现见 `server/syncoj_server/errors.py`：

- 业务错误优先抛 `ApiError(409, "roster_entry_taken", "…")`，而不是 `HTTPException`
- 仍然在抛的 `HTTPException` 由 handler 兼容：`detail` 压成字符串，
  `code` 从状态码**推导**（见下面的 `CODE_BY_STATUS`）
- 需要别人分支判断的地方**显式给码**，其余一律用兜底码

### 码表怎么维护

`ERROR_CODES` 是唯一的码清单，`CODE_BY_STATUS` 是"没显式给码时按状态码推"的兜底表。
四条规矩由 `server/tests/test_errors.py` 静态守住（都是 AST 扫描，不是正则）：

| 规矩 | 违反了会怎样 |
|---|---|
| 源码里发出的每个码都登记在 `ERROR_CODES` 里 | 码只给一半时 `ApiError` 抛 `ValueError`，接口变成 500 |
| `ERROR_CODES` 里的每个码源码里真的会发 | 死码**被当成契约**：前端照着写分支、文档照着写章节 |
| `CODE_BY_STATUS` 的每个取值都登记在 `ERROR_CODES` 里 | 兜底码没文档 —— 而 400 恰是最常见的错误状态 |
| 每个用得到的状态码都在 `CODE_BY_STATUS` 里 | 落到 `.get(status, "error")`，客户端拿到毫无含义的 `error` |

上表最后一条有实例：`/releases` 的"该版本已撤回"和"内容缺失"三处在用 **410**，
`CODE_BY_STATUS` 里却没有兜底码，这类响应带的是 `code: "error"` —— 既不在文档里，
也没法 grep。

> **不许登记服务端根本发不出来的码**。这里曾登记过 8 个
> （`contest_frozen`、`roster_in_use`、`last_admin`、`machine_unbound` …），
> 它们描述的是"设计时觉得以后会有"的场景，而对应的校验要么压根不存在，
> 要么当初就选了更宽容的做法（删名单是被允许的，只是把引用它的场次置空）。
> 要加新码，请**连同产生它的那条分支一起加**。

> `agent_not_found` 与 `diagnostics_not_found` 是"具名码比兜底码有用"的两个例子：
> 前者是"这台机器已经不在台账里"，后者是"这台机器还没回传过诊断包" ——
> 后者的界面表现是「诊断」对话框里的"还没有收到诊断包"这一**正常初态**，
> 不是一句"找不到这个对象"。

### 422 校验错误

pydantic 的原始错误**不直接暴露给人**。处理器把它揉成一句话：

```json
{
  "detail": "每页条数太大了（最大 500）；考号不能为空",
  "code": "validation_error",
  "details": { "errors": [ { "loc": ["query","limit"], "type": "less_than_equal", "message": "…" } ] }
}
```

- `detail` 只列**前 3 条**：教师改完第一处再提交，剩下的通常也跟着消失了，
  而一屏滚不完的报错反而让人不知道该先动哪个
- `details.errors` 保留原始列表，前端表单据此把红框标到具体输入框上

### 500

兜底的 `Exception` handler **必须存在**。没有它时 Starlette 返回一坨纯文本
`Internal Server Error`，前端的 `response.json()` 当场抛 `SyntaxError`，
界面上显示的是"Unexpected token I"而不是任何有用的东西。

---

## 4. 命名

| 项 | 约定 | 例子 |
|---|---|---|
| 前缀 | 管理端 `/api/v1/admin`、Agent `/api/v1/agent` | |
| 集合 | **复数**名词 | `/players`、`/machines`、`/assets` |
| 单对象 | `/{集合}/{id}` | `/players/12` |
| 嵌套 | 父资源在前，最多两层 | `/contests/{id}/players` |
| 字段 | `snake_case` | `player_no`、`machine_uuid` |
| 时间 | ISO-8601 字符串（`*_at`）或 Unix 秒（`ts`） | `last_seen_at` |
| 布尔 | 陈述句 | `claimed`、`bound`、`is_active` |

**状态变更用子路径动词，不用 `PATCH` 改状态字段。** `PATCH` 只用于改一堆普通
字段（改名字、改备注）。原因：状态变更有前置条件、有副作用（作废机器要解绑、
封榜要停回收），塞进 `PATCH` 会让"这次到底想干什么"从 URL 上消失，日志和
权限也就无从下手。

| 动作 | 形状 |
|---|---|
| 改普通字段 | `PATCH /players/{id}` |
| 改状态 | `POST /players/{id}/withdraw`、`POST /contests/{id}/freeze` |
| 绑定关系 | `POST /machines/{id}/bind` |
| 解绑关系 | `DELETE /machines/{id}/bind` |
| 要一份诊断 | `POST /agents/{id}/diagnostics/request`（机器下一次心跳取走，一次性） |
| 读最新诊断包 | `GET /agents/{id}/diagnostics`（没有收到过是 404 `diagnostics_not_found`） |

---

## 5. 删除语义

**分两类，规则不同。** 判断标准是"删掉之后还有没有人需要它"。

### 5.1 结构性数据 —— 硬删除 + 输入名称确认

名单、场次、选手、机器、题目。

删错了重建即可（名单还能重新导入），但误删的后果很重（成绩矩阵连同代码一起
消失），所以**必须二次确认，而且确认内容不是"是否确定"而是把名字打一遍**。
"是否确定"这种弹窗在连续操作里会被手指肌肉记忆点掉，打名称不会。

- 单个：`DELETE /api/v1/admin/players/12?confirm=S001`
- 批量/清空：`POST /api/v1/admin/contests/3/players/clear`，体 `{"confirm": "mock-1"}`

`confirm` 必须**逐字等于**该对象的标识（选手用 `player_no`，场次用 `slug`，
名单用 `name`，机器用 `hostname`），否则 `400` + `code=name_mismatch`。

"清空所有"这类操作没有单个对象可对，用一个**固定的字面量** `all` 作为
`confirm`：`{"confirm": "all"}`。它看起来像个魔法值，但那正是重点 ——
打 `all` 是一个明确的、需要读一眼才能完成的动作，而 `{"confirm": true}` 不是。

> `confirm` 走 query 还是 body，取决于这个动作有没有别的东西要传：
> **单个对象删除**放 query（`?confirm=X`），**批量与清空**放 body
> （`{"confirm": "X"}`，可以和 `keep_manual` 这类选项并列）。
> 别把 body 塞进 DELETE —— 它对代理和缓存都是未定义行为。

> 校验放在**服务端**，不只是前端弹窗。前端弹窗是给人看的提示，服务端校验是
> 保证"没经过界面的调用也一样安全"。

### 5.2 产物性数据 —— 软删除（墓碑）

代码文件、下发资产。

- 删除只置 `deleted_at`，行还在，`blobs/` 里的内容**只有在没有任何引用时**才回收
- 列表默认不显示，`?include_deleted=true` 可以看到
- **导出默认包含已删除的代码**：考场现实是"选手确实交过，只是后来被清了"，
  成绩复核时这份历史比干净的数据重要

### 5.3 清空

"清空"是"范围删除"，语义与被删对象的类别一致：清代码 = 软删（留墓碑），
清选手 = 硬删 + 必须确认。**每个清空入口都要有对应的确认**，不能因为
"它在设置页角落"就省掉。

---

## 6. 前端怎么接

`web/src/api/` 一层负责把上面的约定吃掉，页面代码不应该再碰信封和错误码。
**一个资源一个模块**，`index.ts` 只是入口（几乎全是 re-export）：

| 文件 | 职责 |
|---|---|
| `index.ts` | 入口。页面只从这里 import，所以"方法在哪个文件里"是这一层的内部事 |
| `endpoints.ts` | **所有路径的唯一定义**（含全局确认字面量 `GLOBAL_CONFIRM`） |
| `types.ts` | 从生成物里挑出页面要用的类型别名 |
| `schema.d.ts` | **生成物**，来自 `web/openapi.json`，不要手改 |
| `client.ts` | `request`/`query`/`download`，把错误体转成 `ApiError` |
| `crud.ts` | 分页、信封、删除语义的公共实现（`listPage`/`clearCollection`…） |
| `contests.ts`、`files.ts`… | 一个资源一个：只有「路径 + 参数 + 返回类型」的映射 |

页面只描述"这一页长什么样"，不描述"怎么取数"。新增一个资源 = 一个资源模块
+ 一个页面（外加在 `index.ts` 里加一行转出），而不是再抄一遍
loading/error/分页逻辑。

> 列表数据层（分页、筛选、加载、刷新、批量选择）在
> `web/src/composables/useList.ts`，不在 `api/` 里 —— 它管的是"页面怎么用数据"，
> 而不是"接口长什么样"。

### 6.1 选择必须按稳定键，不能按对象引用

批量操作 + 轮询放在一起时，有一个很容易写错的地方：列表每次刷新拿到的是**全新的
对象**，所以"把上一轮的选中项在新数据里过滤一遍"这种写法（`new Set(rows).has(row)`）
永远过滤到空 —— 于是每轮轮询都把教师刚勾好的十个选手清掉，批量删除根本没法用。

规矩是：

- 选择状态存的是**资源的稳定键**（`id`，或 `player_no` 这类天然主键），
  要用的时候再去当前页里捞回整行
- 表格组件必须带 `row-key` 且开启跨数据保留选择的能力
- **有选中项或批量对话框打开时，后台轮询不得刷新列表**。静默地丢掉选择，
  在破坏性批量流程里就是"删错东西"的前一步

---

## 7. 生成物

```
server/syncoj_server/schemas.py        ← 类型的唯一来源
        │  python server/tools/dump_openapi.py
        ▼
web/openapi.json
        │  npx openapi-typescript
        ▼
web/src/api/schema.d.ts
```

改了接口就必须重跑这两步并把结果提交，否则前端拿到的是旧类型。

守这条链路的测试有两层，分工不同：

| 测试 | 守什么 |
|---|---|
| `server/tests/test_api_conventions.py` | 路由**形状**：GET 不返回裸数组、信封四个字段齐全、每个信封都收 `limit`/`offset`、8 个单对象删除与 7 个清空必须带 `confirm`、路径前缀与命名。它还有一组 `test_selfcheck_*`，拿合成出来的坏 spec 证明每个检测器**真的会红** |
| `server/tests/test_frontend_contract.py` | 前端声明与服务端实际**双向**对齐：`endpoints.ts` 里的路径服务端都认，服务端有的路径前端都声明了。它还盯着 `npm run check:routes` 还在、且仍挂在 `build` 的第一步 |
| `web/scripts/check-routes.mjs` | 反向的那一半：声明的接口方法**必须有界面入口**（这正是"很多功能没有入口"的防复发机制） |
