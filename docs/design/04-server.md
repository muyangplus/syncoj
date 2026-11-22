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
| `player` | `(contest_id, player_no, name, seat)`。**不随机器出现自动创建**，教师应用名单才产生 |
| `roster` / `roster_entry` | 全局可复用的名单（人）；机器永久绑 `roster_entry` |
| `bootstrap_key` | 镜像内置的统一注册密钥（只存哈希、可吊销、带过期） |
| `agent` | 每机 Token 哈希、`machine_uuid`、指纹、`roster_entry_id`、可选 `contest_id`、配对码哈希、吊销状态 |
| `agent_diagnostic` | 每台机器**最新一份**诊断包（解压后的 JSON 文本），新覆盖旧 |
| `source_file` | 回收台账 `(contest, player, path, sha256, scanned_sha256, size, revision, first_seen, last_seen)`：`sha256` 是已存版本，`scanned_sha256` 是最近扫描到的，不等即待上传 |
| `source_version` | 代码版本历史：每份新内容一条 `(player, path, sha256, size, received_at, problem)`，内容不被回收（§12.9） |
| `asset` | 内容寻址的下发文件 |
| `deploy_task` / `deploy_target` | 下发任务与逐选手进度（含 `bytes_done`） |
| `judge_run` | 成绩回写 |
| `event_log` | 审计事件 |
| `agent_release` | Agent 版本与签名 |
| `admin` / `admin_session` | 管理员账号与会话 |

- 无 `enroll_code`（每选手注册码，已删除）与 `machine_claim` 表：未配对 = `agent.roster_entry_id IS NULL`。
- 诊断包每台只留最新一份。否决存历史：按机器数 × 时间无限增长，旧现场几乎无用；需要旧包时自行导出。
- `agent.diagnostics_requested_at`：「要一份诊断」的一次性标记，下一次心跳取走（响应 `diagnostics_request=true`）并清空。落库；否决只放内存：服务端在点击与心跳之间重启会静默丢失请求。

### 4.3 写入策略

负载：50 台、每 30s 一次 tick ≈ 1.7 req/s（20s 时 2.5 req/s）。决定：只靠"在线状态不逐请求落库"收敛高频写；**不设**单写连接或写队列。

- **内存优先**：`AgentRegistry`（`registry.py`）用 dict + `threading.Lock` 持有在线状态，管理端读在线状态走内存；无 SSE，前端轮询。
- **维护循环**：`tasks/flush.py` 每 `flush_interval_seconds`（默认 5s）做离线判定与到点结束场次。在线状态**不落库**（`agent_status` 表无人读，迁移 017 已删）。
- **其余写逐请求提交**：每次 tick 开一个会话（`db.py` 的 `Database.session()`，退出时 commit），对账台账、写审计、收下发回执、更新 `agent.last_seen_at` 都在其中；上传、注册、管理操作同理。连接来自 SQLAlchemy 默认连接池、`check_same_thread=False`，写锁由 SQLite 排队。
- SQLite 参数（`db.py:_install_pragmas`）：`journal_mode=WAL`、`synchronous=NORMAL`、`busy_timeout=5000`、`foreign_keys=ON`、`temp_store=MEMORY`；连接另有 30s 超时。写锁争抢由 WAL + `busy_timeout` 兜住。
- 否决写串行化：高频写已收敛，剩余写都低频（详见 `db.py` 模块说明）；"单写线程 + 队列"在单场次超过 500 台时再议。

### 4.4 评测结果回写

后台 `ResultScanner` 每 `judge_scan_interval`（默认 10s）轮询 `judge_result/`，`mtime` 变化才解析。「成绩」页按「选手 × 题目」矩阵展示分数，可导出 CSV。

解析器可配（`glob` + 格式）：先支持通用 JSON/XML/文本模板，拿到 LemonLime/Arbiter 真实输出后再补精确解析。

- **保守解析**（硬规则）：宁可漏解析（标 `unparsed`，教师手工补录）也不错解析；悄悄算错的分比显眼的空更难发现。
- 手工录入的成绩**永不被自动扫描覆盖**（"强制重扫"也不行）。
- 矩阵区分 `ok` / `unparsed` / `manual` / `missing`；`missing` 再分「**已交未评测**」与「**未交**」，前者由 `source_file` 按当前模式实时算出（`submitted` 字段）。

### 4.5 代码归题与目录约定

目录约定**只是默认值**，三层各自可配：

| 层 | 配置 | 默认 | 管什么 |
|---|---|---|---|
| ① Agent | `deploy_root` / `scan.roots` / `scan.prefix` | `{desktop}` / `{desktop}/{player_no}` / `none` | 下发落哪、从哪收、上报路径加不加前缀 |
| ② 服务端 | `SYNCOJ_DEFAULT_FILE_PATTERN` | `{ident}/{ident}.cpp` | 题目**没配**模式时怎么认领 |
| ③ 每道题 | `problem.file_patterns` | 跟随 ② | 这道题**特意**怎么认领 |

目录树（默认值）：

```
桌面/                              ← deploy_root：下发落地根
├── 题面.zip                        ← 通用资料默认直接落这里（目标目录留空）
├── 样例.zip                        ← 题面/样例本身就是 zip，原样落盘、服务端不解压
└── <准考证号>/                      ← scan.roots：Agent 扫描根
    ├── p1/                        ← 题目的 file_patterns 决定哪个文件算哪道题
    │   └── p1.cpp                 ← 约定：<题目名>/<题目名>.cpp
    └── p2/p2.cpp
```

`{desktop}` 探测顺序：`<运行账号的家>/.config/user-dirs.dirs` 的 `XDG_DESKTOP_DIR`，再试 `~/桌面`、`~/Desktop`、`~/desktop`。

| 占位符 | 何时展开 | 展开成 |
|---|---|---|
| `{desktop}` | 载入配置时 | **运行账号**的桌面（兼容「桌面」与 `Desktop`） |
| `{home}` | 载入配置时 | **运行账号**的家目录 |
| `{player_no}` | 注册后 | 准考证号 |
| `{contest_slug}` | 注册后 | 场次标识 |

- `{desktop}` / `{home}` 恒定解析到**运行账号**（= 执行安装的账号），与当前进程无关。否决按当前进程展开：root 注册单元会得到不存在的 `/root/桌面`，配置校验失败，机器不出现。
- 展开不了的占位符**原样保留**不报错：配置校验在注册之前，报错会把"未注册"误报成"配置错"。

`agent.ini` 写法（`[agent] run_user` 由安装器自动填）：

```ini
[agent]
run_user = student                       ; 路径模板按它解析
deploy_root = {desktop}                  ; 或写死 /home/student/桌面

[scan]
; 下面几行任选其一
roots = {desktop}/{player_no}            ; 默认：桌面/<准考证号>
roots = {home}/我的代码                    ; 家目录下
roots = {desktop}/{contest_slug}/{player_no}   ; 一个考点跑多场次时分开
prefix = none                            ; none / auto / 字面量
```

- `scan.prefix` 默认 `none`：扫描根名即准考证号，再加前缀会在 `source/` 里出现两次；多个扫描目录时用 `auto` 区分同名文件。
- **改 `prefix` 须同步改题目的「代码路径」**：`prefix = auto` 时上报路径是 `<准考证号>/p1/p1.cpp`，默认的 `{ident}/{ident}.cpp` 认不出，需改成 `**/{ident}/{ident}.cpp`。

归题硬规则：

1. **glob 语义**：`*` 不跨 `/`，`**` 跨，`?` 单字符，`[abc]`/`[!abc]` 字符类。否决 `fnmatch`：其 `*` 跨 `/`，无法表达"只在这个目录下"。
2. **第一条命中的题目胜**，顺序即题目清单顺序。否决"最具体者优先"：归错时无法从规则直接解释。
3. `{ident}` / `{title}` 是**活占位符**，匹配时才展开，改标识/标题后模式自动跟随；`{title}` 留空时退回标识。
4. **归属现算、不落库**：改模式立即生效，无需重收文件，无"旧归属"脏数据。
5. 认不出即 `None`，不兜底归到某题：假归属比无归属更难发现。
6. 界面「路径试算」调服务端接口，**前端不实现 glob**：两份实现会不一致。

模式示例（第一条胜出）：

```
p1,签到题,{ident}/{ident}.cpp             ← 默认：只认 p1/p1.cpp（最窄的那条）
p1,签到题,{ident}/**                      ← 想收整个 p1/ 目录时显式写这个
p2,图论,{ident}/src/*.cpp;{ident}/**.h    ← 多条模式用 ; 或 | 分隔
p3,字符串,**/p3.cpp                       ← 不限目录层级
```

**回收按同一套模式过滤**：场次配了题目后，扫描到的文件须匹配某题才入台账、才要求上传；不匹配的不入账、不上传，机器列表显示「有 N 个文件不符合题目预设，已跳过」。边界：

* 场次**未配任何题目**时全收。
* 已入台账的条目**不因模式变更被删**：不再上传、不归任何题；只有从选手磁盘消失才记「选手删除」（避免改模式产生虚假删除审计）。
* 被挡数量只在"有无被挡"变化时记审计（否决按轮记：30 秒一条）。
* 被挡文件不入台账，无「未归类」一档；要计入就加进该题的模式。

### 4.6 注册与配对：一条路

- 决定：机房镜像放**一份**统一密钥，机器开机用它换本机凭据。
- 否决每选手注册码（已删除，不并存）：整间机房同一镜像时逐台发码不可规模化；两种模式并存易选错。

```
装机（root 一次性）        机器首次开机              教师
bootstrap.key  ──换凭据──▶  桌面「配对码.txt」 ──读码/输入──▶ 绑到名单里的**人**
```

| 单元 | 身份 | 职责 |
|---|---|---|
| `syncoj-agent-enroll.service` | **root**（oneshot，装机时 enable 并立刻跑一次） | 读密钥、换凭据、写进状态目录并交给运行账号 |
| `syncoj-agent.service` | 运行账号（= 执行安装的账号） | 只读凭据；**无凭据时等待**，不读密钥 |

- 服务本体拿到凭据前每 `REGISTRATION_POLL_SECONDS`（默认 60s）检查一次凭据，日志写明该查哪个单元。
- 否决服务本体自己注册：选手身份读不到 root 只读的密钥 → `PermissionError` → 退出 1 → 反复重启 → 5 次后撞 `StartLimit`，注册单元从未运行。此约束同时保证选手账号拿不到注册密钥。
- 代价：服务端不知道机器是谁，由**配对**补回身份（全链路唯一需人到场的环节）。

规则：

1. **密钥只给 root 读**（`/etc/syncoj/bootstrap.key`，0600）；不放 chown 给选手的 `agent.ini`（泄露即无限注册）。
2. **配对码六位数字**，只存哈希，限时 30 分钟（`pair_code_ttl_seconds`），**一次性**，绑定即作废；配对后认机器只靠 `machine_uuid`。否决更长的码：人工在屏幕与教师之间传递，越长越易抄错；"限时 + 一次性 + 管理员鉴权"已消除爆破空间。
   `claimed=false` 的机器**每次 tick 都回发此码**，明文在服务端内存留到过期或配对（库里只有哈希）；缓存规则见 [`../reference/protocol.md`](../reference/protocol.md) §3。
3. **未配对不另立表**：`agent.roster_entry_id IS NULL`。
4. **未配对的凭据是有效凭据**（无归属）。否决按注册失败处理：Agent 会反复重注册、配对码不断更换。
5. **一人一机、一机一人**，服务端双向强制（否则两台机器静默写同一目录）。覆盖已有绑定须走显式「改派」，再输一次配对码不能换人。
6. **场次动态解析**：找"含此 `player_no` 且 `is_active`"的场次；0 个 →「还没有含你的场次」，多于 1 个 →「需要指定场次」，**不自动挑**。教师可在机器上钉 `Agent.contest_id` 覆盖。

**快照还原自愈**：注册时带**硬件指纹**（SMBIOS UUID，还原后不变），服务端据此认回原机、保留配对。

- 硬前提：**原机器当前离线**。否决无条件认回：克隆镜像的机器报同一指纹，母机在线时来的同指纹机器更可能是克隆，认回即静默送出学生身份。原机在线时转人工配对并告警。
- 多台共用同一指纹在「机器配对」页显示克隆告警（通常意味着镜像在某台机器**运行过之后**才克隆）。

**认机器顺序**：

| 顺序 | 依据 | 适用情形 |
|---|---|---|
| 1 | `machine_uuid` | 配对后的常态；镜像通用化后 UUID 各不相同 |
| 2 | `machine_fingerprint`（要求原机器离线） | 快照还原抹掉了 UUID |
| 3 | 都对不上 → 新机器 + 配对码 | 由人确认，不猜 |

`machine_id`（`/etc/machine-id`）**不参与身份判定**，只用于显示（镜像未通用化时整批相同）。

#### 配对一次，之后 N 场比赛零人工

| 情况 | 要不要再配 | 靠什么 |
|---|---|---|
| 换到另一场比赛 | **不用** | 绑的是名单库里的**人**，场次动态解析；新场次应用了含此 `player_no` 的名单即可 |
| 系统重装 / 凭据被删 | **不用** | 重注册时按 `machine_uuid` 认回（指纹只在原机离线时兜底） |
| 快照还原（UUID 也没了） | **不用** | 硬件指纹 + 原机器必须离线 |
| 升级 Agent | **不用** | 升级包与凭据无关；验签公钥装机时随包带走 |
| **换了人**（改派 / 解绑） | 要 | 唯一有意的重配 |
| 全新机器 | 要 | `machine_uuid` 没见过 |
| **名单条目被删** | 要 | 绑定落在 `roster_entry_id` 上，条目没了绑定就没了；护栏见 ../design/04-server.md §4.7 |

**配对之后不下发任何密钥**：

* 机器**注册时已拿到 token**；配对只把该机器绑到某个人，之后一律用 token。
* 升级验签用的**发布公钥**在打包时随离线包带走（../design/07-ops-and-install.md §7.1），与配对无关。
* 全链路两样凭据：**统一注册密钥**（装机，一次性）与**本机 token**（注册换得，长期）。**配对码不是凭据**，只证明"教师此刻在场"。

### 4.7 名单：模板，不是引用

- `Roster`：跨场次复用的人员名单；`Player`：本场参赛者，成绩、代码、下发挂在它上面。
- 决定：应用名单时**一次性复制**。否决场次引用名单：改名单会连带改掉历史场次的参赛者（成绩矩阵列、代码目录、下发目标、成绩外键）。
- 应用名单时 `prune` 默认关；**显式打开也不删已有代码或成绩的选手**，并在结果里列出。否决默认 prune：连带删提交不可逆且不报错。
- **「默认名单」只是预设，选了不自动补人**。界面在选名单处给动作按钮「按这份名单补人」（只补不删），「选手状态」空状态里给同一按钮。`test_setting_the_default_roster_does_not_touch_players` 钉住：设/改默认名单后 `player_count` 仍为 0，显式应用后才变 1。

**删名单会解绑机器，须拦截。** 机器绑 `agent.roster_entry_id`，条目随名单删除（`cascade="all, delete-orphan"`），外键 `ON DELETE SET NULL`：

```
删一份名单  →  它的 roster_entry 全被删  →  绑在这些条目上的机器 roster_entry_id 变 NULL
                                        →  那批机器回到"未配对"，重新显示配对码
```

- `DELETE /rosters/{id}` 在名单里**有机器绑着的人**时**拒绝**，409 `roster_in_use`，给出台数、前几个考号和出路（先到「机器配对」改派或解绑）。否决"允许但警告"：不可逆的批量解绑，确认框会被习惯性点掉（同 ../design/05-api-and-frontend.md §5.4）。
- 否决把 `player_no` 冗余到机器上永久保留绑定：学年末机器换一批学生，旧绑定留着更糟；要求是不静默解绑，解绑须显式操作。
- 删**单条**条目（`DELETE /roster-entries/{id}`）**允许**，响应写明副作用：解除了 1 台机器的配对，它下一轮心跳显示新配对码。

### 4.8 库结构演进

- `Base.metadata.create_all()` 只建缺失的表，不补列；补列与改结构由 `migrations.py` 完成：`PRAGMA user_version` + 一串"N → N+1"步骤，幂等、每步一个事务、**失败不推进版本号**（下次启动重试同一步）。
- 已有数据的库迁移前**先用 SQLite 在线备份整库**到同目录 `<库名>.bak-v<旧版本>-<时间>`（新库、已是最新的库不备份；备份失败只告警、不阻止启动）。

**重建表（rebuild）的安全配方**（SQLite 删列、改可空性只能"建新表 → 搬数据 → 换名"；配方固定并有看守测试）：

```python
raw = engine.raw_connection()
raw.isolation_level = None          # 自动提交：PRAGMA 才不是"事务里的空操作"
raw.execute("PRAGMA foreign_keys=OFF")
raw.execute("PRAGMA legacy_alter_table=ON")   # 否则 RENAME 会重写引用方
raw.execute("BEGIN"); ... ; raw.execute("COMMIT")
raw.execute("PRAGMA foreign_keys=ON")
```

三条须同时成立：

1. **`isolation_level = None`（自动提交）**：`PRAGMA foreign_keys` 在事务里是空操作；外键开着时 `DROP TABLE` 会顺 `ON DELETE CASCADE` 清掉子表。`test_legacy_rebuild_keeps_child_table_rows` 断言子表数据重建后仍在。
2. **`legacy_alter_table=ON`**：否则新版 SQLite 的 `ALTER TABLE ... RENAME` 改写所有引用旧名的外键/视图/触发器。
3. **DDL 用文本改写**（`CREATE TABLE x (` → `CREATE TABLE x__new (`）。否决 `to_metadata()`：会静默漏拷部分定义，只发一条警告。

看守测试：

- 模型新增的列既不在老结构里、也未声明为迁移要补 → 变红。
- 老库升级：数据不少、关联表未被级联删除、跑第二遍是空操作、全新库与升级库结构一致。

开发期：`syncoj-server db reset --yes` 重建空库（可选 `--keep-admin`），迁移不需兼容全部旧结构。
