## 5. 接口约定与前端

### 5.1 协议一致性靠契约测试，不靠共享模型

决定：协议一致性由 **CI 契约测试**保证。否决共享模型：Agent 零依赖，不能 import pydantic。

```python
# server/tests/test_agent_contract.py
def test_tick_payload_matches_schema():
    raw = subprocess.run(["python3", "agent/tools/build_fixture.py"],
                         capture_output=True, text=True).stdout
    TickRequest.model_validate(json.loads(raw))     # 不匹配就红
```

`agent/tools/build_fixture.py` 用 Agent 自己的纯标准库构造器生成真实 payload；服务端 schema 改动破坏契约时 CI 变红。

### 5.2 管理端 API：一个信封、一个错误体

完整约定见 [`docs/reference/api-conventions.md`](../reference/api-conventions.md)。

```jsonc
// 所有列表
{ "items": [ … ], "total": 137, "limit": 50, "offset": 0 }

// 所有错误
{ "detail": "这句是给人看的中文", "code": "roster_entry_taken", "details": { } }
```

- 决定：所有列表与错误统一形状，前端只写一套数据层。
- **`detail` 恒为字符串**，可直接显示在提示条上的一句中文。422 由处理器合并成"每页条数太大了（最大 500）；考号不能为空"，原始错误列表放 `details.errors` 供表单标红。
- **500 有兜底处理器**：否则 Starlette 返回纯文本 `Internal Server Error`，前端 `response.json()` 抛 `SyntaxError`，界面只显示 "Unexpected token I"。
- **码表静态可查**：`ERROR_CODES` 是唯一清单，`CODE_BY_STATUS` 是未显式给码时按状态码推断的兜底表。AST 扫描守四条：发出的码须登记、登记的码须真会发出、兜底码须登记、用到的状态码须有兜底。不留死码（会被当成契约写进前端分支与文档）。

### 5.3 前端架构

类型链路单向，唯一真相源是服务端 schema：

```
server/syncoj_server/schemas.py        ← 类型的唯一来源
        │  python server/tools/dump_openapi.py
        ▼
web/openapi.json
        │  npm run gen:types  (openapi-typescript)
        ▼
web/src/api/schema.d.ts                ← 生成物，不要手改
```

页面**只描述长什么样，不描述怎么取数**：

| 文件 | 职责 |
|---|---|
| `api/client.ts` | `request`/`query`/`download`，把错误体转成带 `code` 的 `ApiError` |
| `api/endpoints.ts` | 所有路径的唯一定义，服务端改路径只改这一处 |
| `api/crud.ts` | 分页、信封、删除语义的公共实现 |
| `api/<资源>.ts` | **一个资源一个模块**，只有「路径 + 参数 + 返回类型」映射 |
| `api/index.ts` | 入口，几乎全是 re-export；页面只从这里 import |
| `composables/useList.ts` | 分页、筛选、加载/错误、刷新、批量选择、可中断请求 |
| `composables/useMutation.ts` | 写操作、提示、刷新列表、按钮 pending 态 |
| `components/` | `PageShell` / `DataTable` / `ConfirmByNameDialog` / `FormDialog` |

- 新增资源 = 一个资源模块 + 一个页面（+ `index.ts` 一行转出），不重复 loading/error/分页/轮询。
- 按资源拆模块。否决全部堆进 `index.ts`：几十个方法挤在一个文件里，漏接入口难以发现。
- 侧栏分组：总览（工作台）/ 考务（场次管理、名单库、选手状态）/ 机器（机器配对、Agent 发布）/ 代码与评测（代码台账、文件下发、评测、成绩）/ 系统（审计日志）。顶栏「快捷操作」菜单、Ctrl/⌘+K 命令面板与工作台共用同一份动作清单（`composables/useQuickActions.ts`）。

#### 界面文案：只留动作、后果与实时数据

页面**禁止**声明式/解释式语句。允许的只有：

| 允许 | 例子 |
|---|---|
| 标题、标签、按钮、状态、表头、placeholder | 「统一密钥」「已签名」「sha256」 |
| **一句话**操作指引 | 「在每台空机器上跑它。」 |
| **破坏性动作的后果** | 「会删掉统一注册密钥与本机凭据。」 |
| 服务端原文错误、实时数据 | `detail` 直接显示，不加工成第二种说法 |

- 删除成段说明（设计理由、概念辨析、机制、「上传不等于铺开」类劝告），放进文档。
- 判据：**站在机器前的人此刻能否据此动手**。不能动手的解释句删；能动手的后果句留（删除前须告知会丢什么）。
- 面向用户的字符串不得含 Markdown 标记（如 `**已铺开**`），由 AST 守卫检查（`server/tests/test_errors.py`，含前端文案）。

### 5.4 删除语义

- **结构性数据**（名单/场次/选手/机器/题目）→ 硬删，**输入名称确认**（`confirm` 须逐字等于 `player_no` / `slug` / `name` / `hostname`），服务端校验。否决"是否确定"弹窗：连续操作中会被习惯性点掉。
- **产物性数据**（代码文件/下发资产）→ 教师删除也**硬删**（不要求输名称；清空代码台账要输场次 slug）；内容寻址的 blob 无任何记录引用时才一并删除；`source/` 里已落盘的文件不动。
- 唯一的墓碑：**扫描对账**打的"已消失"（选手在机器上删掉的文件，`deleted_at`），`?include_deleted=true` 可见；导出**默认带上**（记录"选手交过、后来删了"）。见 [`ADR-0003`](../decisions/ADR-0003-delete-semantics.md)。

### 5.5 下发资产一律是文件，不解压

- 决定：服务端**只搬运字节**，不打包不解压，由选手或评测机自己解压。收益：压缩格式零第三方依赖；落盘字节与教师上传的文件逐字节相同。

**纯文本资产可在线改正文**（`GET|PUT .../assets/{id}/text`）：

* 改正文 = **同一个 asset id 换 `sha256`/`size`**，不新建；改名是另一条路（换标签不动内容）。否决合并为一个入口：两者对已落盘文件的后果不同，回执无法说清。
* 改完把该资产**已完成（及已判失败）的下发目标重排回 `PENDING`**（tick 只对 `PENDING/READY` 派活）。分片按**内容 sha256** 命名，续传不错位；完成回执带 sha256，迟到的旧回执不会把新内容标成完成（管理端进度一并清零）。
* "能否在线改"由**服务端**算成布尔给界面（扩展名白名单 + 体积上限，只看元数据）；读写接口要求 UTF-8 可解码，否则 400。

**例外：就地给 zip 打密码 / 改密码**。由 `services/zipcrypto.py`（自写的 ZipCrypto 写实现）完成，**读走标准库**，"写出的包能否读回"由独立实现判定。

* 语义同在线改正文：同一 asset id 换 `sha256`/`size`，已完成/已失败目标重排回 `PENDING`。
* **库里没有密码字段**：密码只在自动生成/更新的 `password.txt` 文本资产里，可读回、可下发。审计事件**不写密码**。
* `password.txt` **单件语义**：更新同一条，不每次新建。
* 决定 **ZipCrypto**；否决 AES：学生用的 **Archive Manager**（GNOME file-roller）打不开 AES 包。口径：挡随手翻看，不挡有心人（`password.txt` 本身是明文），不称"安全加密"。密码按 UTF-8；不支持 ZIP64（>4GB / >65535 条目回 400）。

### 5.6 两个免登录页面

| 页面 | 使用者 | 身份确定方式 |
|---|---|---|
| `/install` | 教师，在未装 Agent 的机器前 | **不需要身份**：只展示从哪装、装哪个版本、怎么核对 |
| `/player` | 选手，在考场机器上 | **服务端按来源 IP 认机器** → 配对到的人 → 场次 → 该选手 |

**`/install`**：

- 地址取**当前页面的 `origin`**（页面能打开即说明局域网可达），不调需鉴权的"我是谁"接口。
- 未铺开任何版本时不给安装命令，只给一句"去铺开"的指引。

**`/player` 身份**：

- 链条「机器 → 人 → 场次里的选手」与 `/tick` **共用同一个解析函数**（../design/04-server.md §4.6 的 `resolve_machine`），两者结论一致。
- 匹配不上（Agent 未起、教师用笔记本打开）时给**手填场次 + 考号**的兜底入口。否决 URL 参数预填："显示的是谁"会取决于可随手改的链接。

**`/player` 内容**：只有**考场公告**、发给本人的文件清单、身份卡；**不含代码、成绩、其他选手**（有测试：其他选手的资产不得出现）。

- 顺序：**场次卡 → 考场公告 → 下发文件**。
- 场次卡显示**开考与结束时间**（留空 = 不限）；到点后服务端拒收文件。时间按服务端存值原样下发（UTC ISO），浏览器按本机时区显示。
- **公告 = 下发文件里的 `NOTICE.md`**：教师在「文件下发」写一个 `NOTICE.md` 照常下发。否决单独的公告字段：要写两遍，且须说明哪个作数（同 ../design/05-api-and-frontend.md §5.5）。
- 判据：**本场 + 发给本人**的下发记录中 basename 大小写不敏感匹配 `notice.md`，多份取最后下发的；非 UTF-8 或超过 64 KB 的跳过并记日志。
- 正文按 **Markdown 渲染**（`marked` + `DOMPurify`，见 `web/src/utils/markdown.ts`）。否决不消毒：正文常从别处粘贴，`<img onerror=…>` 会执行任意脚本。
- 开 **`breaks: true`**（GFM 行为）。否决 CommonMark 软换行合并：公告写在纯文本框里，两行应显示为两行。
- 否决从题目模式推导"把代码存到 X"：服务端不知道每台机器的 `scan.roots` 与前缀，错误的操作指引比没有更坏。

### 5.7 公开端口：默认 80，只放这两页

决定：服务端**默认再开一个监听**（`SYNCOJ_PUBLIC_PORT`，默认 80；考场浏览器敲 `http://10.0.0.5/` 即走 80）：

| 端口 | 内容 |
|---|---|
| `--port`（默认 8000） | 全部：管理界面 + 三套 API + `/docs` |
| `SYNCOJ_PUBLIC_PORT`（默认 80） | 只有 `/install` 与 `/player`；管理端、`/docs`、`/openapi.json`、`/healthz` 一律 404 |

- `SYNCOJ_PUBLIC_PORT=0` 关掉第二个监听；`0` 是哨兵（留空 = 未设，退回 80；见 `config._env_opt_int`）。
- 否决同一端口按路径分：学生访问的原点上会有登录框，`/docs` 可免登录列出全部管理接口。
- 门禁用**黑名单**（挡 `/api/v1/admin`、`/docs`、`/redoc`、`/openapi.json`、`/healthz`）。否决白名单：漏一条即两页静默失效（白屏、转圈）；黑名单漏一条只多暴露一个仍有 `require_admin` 兜底的接口。
- 被挡接口回 **404**。否决 403：等于提示"去别的端口试"。
- **"是否来自公开端口"由服务端启动时写进 `app.state.public_port`**，判据是实际在该端口监听，不是配置数字：
  * `serve` 绑不上 80 时**降级**（只留管理端口并打印两种修法），此时不会有请求被当成公开端口、管理界面不会 404。
  * `TestClient` 默认端口是 80，取配置默认值会让整个测试套件被误判为公开端口。
- **前端识别公开端口**：服务端在公开端口返回的 `index.html` 注入 `window.__SYNCOJ__={"publicOnly":true,"adminPort":8000}`（见 `web/src/publicMode.ts`）。前端据此把默认与兜底路由压到 `/player`，**不碰鉴权状态**（该接口被挡），`/login` 也送到 `/player`。否决前端自己数端口：80 端口时 `location.port === ""`，且同一事实不应定义两遍。
- `adminPort` 供**装机命令**使用：命令里的地址长期写进机器配置，须指向 API 端口而非可关闭的公开端口；主机名取当前页面（见 ../design/07-ops-and-install.md §7.3 三条来源的排序）。
