# SyncOJ — Online Sync Judge

通用编程考试与竞赛模拟的**在线同步评测**系统。不绑定任何特定赛事。

- **代码自动回收**：选手端静默扫描代码目录，自动同步到服务端 `source/<场次>/<选手编号>/`
- **文件主动下发**：教师上传题面、测试点，按选手或全量下发
- **在线状态管理**：实时维护在线/离线状态与最后心跳
- **统一管理界面**：选手列表、同步进度、下发进度、日志
- **静默客户端**：无 GUI、无弹窗、无通知，开机自启，后台常驻
- **评测对接**：`source/` 目录供 LemonLime / Arbiter 直接读取，成绩自动回写

## 目标环境

| | |
|---|---|
| 考试机 | **NOI Linux 2.0**（Ubuntu 20.04 / glibc 2.31 / systemd 245 / Python 3.8.10） |
| 服务端 | 单机内网部署，Python 3.8+ |
| 通信 | 内网 HTTPS，HTTP 自适应轮询（无 WebSocket） |

## 关键设计取舍

这一版方案和"教科书式"做法有几处刻意不同，都是被约束逼出来的：

1. **Agent 零第三方依赖**。只用 Python 3.8 标准库 —— 不用 `watchdog`、不用 `websockets`、不用 `requests`、不用 `pydantic`。原因是 NOI Linux 上 Python 3.8 已 EOL、考场通常离线，任何第三方依赖都是部署风险。砍掉依赖后，Python 3.8 的版本绑定风险基本归零。

2. **用 HTTP 自适应轮询替代 WebSocket**。50 台机器的规模下，WebSocket 只换来"实时性"（需求其实只要秒级）却引入重连、半开检测、连接生命周期、本地队列状态机一大堆复杂度。轮询让 Agent 变成**无状态进程**，崩溃/断网/重装全部天然自愈。

3. **配置用 INI 而不是 TOML**。`tomllib` 是 Python 3.11+ 才有的，用 `configparser`（标准库）。

4. **每次 tick 上报全量文件索引**。50 个文件约 5KB/次，成本可忽略，换来的是服务端免费的完整文件台账 + 免费的删除检测（不依赖 inotify 事件流，Agent 停机期间的事件也不会丢）。

详见 [`DESIGN.md`](DESIGN.md)。

## 仓库结构

```
SyncOJ/
├── DESIGN.md                 完整技术方案
├── docs/
│   ├── protocol.md           Agent ⇄ Server 协议规范
│   ├── api-conventions.md    管理端 API 形状约定（信封 / 错误体 / 删除语义）
│   └── judge-result.md       评测器对接：成绩怎么进 SyncOJ
├── server/                   服务端（FastAPI + SQLite WAL）
│   ├── syncoj_server/
│   └── tests/
├── agent/                    选手端 Agent（Python 3.8 零依赖）
│   ├── syncoj_agent/
│   ├── packaging/            幂等安装器 / 打包器 / 在线自举
│   ├── tools/                py38 兼容门禁、协议 fixture 生成
│   └── tests/
├── scripts/                  一键跑全部检查（check.sh / check.ps1，步骤一一对应）
└── web/                      管理界面（Vue3 + TS + Vite + Element Plus）
    ├── src/api/              client + resource 声明 + 从 OpenAPI 生成的类型
    ├── src/composables/      useList / useMutation —— 所有页面共用的数据层
    ├── src/components/       通用页面外壳、表格、确认对话框
    ├── src/views/            9 个页面
    └── openapi.json          由 server/tools/dump_openapi.py 导出（纳入版本管理）
```

## 快速开始（开发）

```bash
# 服务端
python -m venv server/.venv
server/.venv/Scripts/activate      # Linux: source server/.venv/bin/activate
pip install -e "server[dev]"
syncoj-server init                 # 建库 + 生成管理员口令
syncoj-server serve --host 0.0.0.0

# 管理界面（开发模式，热更新）
cd web && npm install
npm run dev                        # http://localhost:5173，API 自动代理到 :8000

# 管理界面（构建后由服务端托管 —— 单进程部署，不需要额外的 Caddy/nginx）
npm run build                      # 产出 web/dist
# 服务端启动时自动探测 web/dist 并挂载；探测不到就只提供 API，/docs 仍可用

# Agent（零依赖）
python agent/run_agent.py --check                          # 用内置默认配置自检
python agent/run_agent.py --config agent/config.example.ini --check
python agent/run_agent.py --once
```

> `--check` **不带 `--config`** 时用的是内置默认值（`ca_file` 留空、服务端
> `https://127.0.0.1:8000`），所以开箱就能跑。带上 `config.example.ini` 会去
> 找 `/etc/syncoj/ca.pem` —— 那是个**部署模板**，填好真实路径再用。

> **Agent 必须通过 `run_agent.py` 启动，不能是 `syncoj_agent/main.py`。**
> 后者使用包内相对导入，当脚本直接执行会报
> `ImportError: attempted relative import with no known parent package`。
> `-E` 会连 `PYTHONPATH` 一起忽略，所以只能靠启动器显式设置 `sys.path`。

## 开发约定

- **Agent 侧代码必须是 Python 3.8 兼容**（目标机是 NOI Linux 2.0）。
  提交前跑 `python agent/tools/check_py38.py agent/`，CI 也会跑。
- **协议改动必须同步三处**：`server/syncoj_server/schemas.py`、
  `agent/syncoj_agent/` 的报文体构造、`docs/protocol.md`。
  契约测试 `server/tests/test_agent_contract.py` 会自动发现不一致。
- **服务端的路径安全校验必须独立实现**，绝不信任 Agent 上报的路径。
  `agent/syncoj_agent/safepath.py` 是**另一份独立实现**（纵深防御），
  两者的一致性由契约测试中的 `test_path_validation_parity` 守住。
- **前端类型绝不手写**。改完服务端模型后依次跑：
  ```bash
  python server/tools/dump_openapi.py && (cd web && npm run gen:types)
  ```
  `scripts/check.sh` 会校验 `web/openapi.json` 是否已同步，不同步直接失败。

## 跑全部检查

```bash
./scripts/check.sh          # Linux / CI；Windows 的 Git bash 里也能直接跑
```

```powershell
pwsh -File scripts/check.ps1   # Windows 原生入口
```

两个脚本**步骤一一对应**，只换执行方式 —— 改了其中一个就要改另一个，否则两个
平台给出的"通过"含义不同，那比只有一个入口更糟。

- `check.sh` 自动挑可用的 Python：先 `python3`，不行就退回仓库里的 `.venv`。
  Windows 上 `python3` 常常指向应用商店的占位程序（`command -v` 找得到、执行直接
  失败），所以判据是"**真的能跑起来**且 ≥3.8"，不是"命令存在"。实测在 Git bash 里
  可以直接跑通全程。
- `check.ps1` 是 Windows 原生入口，少跨一层 msys 路径翻译，出错信息更直白。

检查内容：py38 兼容门禁 → Agent 测试 → 服务端测试 → `openapi.json` 是否已同步
→ `schema.d.ts` 是否与 `openapi.json` 一致（**重新生成一遍再逐字节比对**）
→ 前端入口对账 + `typecheck` + `build`。

> 生成物那条检查刻意**不用 `git diff`**：git diff 比的是工作树和 HEAD，而开发中
> 工作树本来就有未提交的改动，于是它在最常见的场景下恒红。假警报比没有检查更糟，
> 它会训练人无视这一步。判据是"重新生成的结果和仓库里那份是否一致"。

## 状态

| 里程碑 | 状态 |
|---|---|
| M1 骨架：enroll / tick / 在线状态 / 管理后台 | ✅ 完成 |
| M2 回收：扫描 / 上传 / 内容寻址存储 / `source/` 落盘 / 删除审计 | ✅ 完成 |
| M3 下发：资产管理 / 任务编排（全员·按人·按分组）/ Range 断点续传 / 进度聚合 | ✅ 完成 |
| M4 成绩：结果扫描回写 / 成绩矩阵 / 手工补录 / 原始记录 | ✅ 完成 |
| M5 运维：RSA 签名自更新 / 安全解包 / 自动回滚 / 幂等安装器 / 可复现打包 | ✅ 完成 |
| 管理界面：9 个页面 + 初始化闭环（建场次 / 导名单 / 维护题目 / 机器配对） | ✅ 完成 |
| 代码归题：题目 glob 模式 / 文件台账归类 / 路径试算 / 三层可配置 | ✅ 完成 |
| 交付视图：区分「未交」「已待评测」「已出成绩」，列表页给交题进度 | ✅ 完成 |
| 名单库：全局可复用名单，场次引用默认名单并一键应用 | ✅ 完成 |
| 永久配对：镜像一份统一密钥 + 六位配对码绑定名单条目 + 指纹自愈 + 克隆告警 | ✅ 完成 |
| API 形状统一：列表信封 / 统一错误体 / 输入名称确认的删除 | ✅ 完成 |

## 机器注册与配对

**只有一条路**：镜像里放一份统一密钥，机器开机换回本机凭据，教师读机器屏幕上的
六位配对码把它绑到名单里的**人**。

```
装机（root 一次性）        机器首次开机              教师
bootstrap.key  ──换凭据──▶  桌面「配对码.txt」 ──读码/输入──▶ 绑到名单里的某个人
```

| | 说明 |
|---|---|
| 密钥在哪 | `/etc/syncoj/bootstrap.key`（**root 只读**，0600）。`agent.ini` 选手账号可读，密钥绝不能写进去 |
| 签发 | `syncoj-server bootstrap-key issue --out /etc/syncoj/bootstrap.key`（明文只显示一次） |
| 配对码 | **六位数字**，限时 30 分钟，一次性。只在"绑定那一刻"用 |
| 配对之后 | 认机器靠 `machine_uuid`，**不再需要配对码** |
| 配对对象 | 名单条目（**人**），不是某场比赛的选手 —— 所以配对是**永久**的，换场次不用重配 |

**配对之后不等于马上能干活。** 场次有没有这个人，取决于那份名单有没有被应用进
场次，所以客户端的凭据有三个状态：

| 状态 | 机器上的表现 | 原因 |
|---|---|---|
| 未配对 | 桌面出现「配对码.txt」 | 还没人认领它 |
| 已配对、无场次 | 桌面出现「等待场次.txt」 | 应用了名单才会有这个 `player_no` 的选手 |
| 正常 | 两个文件都被删掉，开始收代码 | —— |

**快照还原能自愈**：机器上的凭据会随还原消失，所以注册时还会带硬件指纹
（SMBIOS UUID，还原后不变）。服务端按 `machine_uuid`（首选）或指纹（UUID 也没了
时）认回原来那台机器、**保留配对关系**，不需要教师重新配对一次。但指纹那条只在
**那台机器当前离线**时才自动认回 —— 指纹只证明硬件相同，母机还在心跳时来了一台
同指纹的机器，那更可能是克隆，认回就等于把某个学生的身份白送出去。这种情况会
进待配对并留下告警（「机器配对」页的克隆告警）。

> ⚠️ **建镜像时不要在母机上跑过 Agent 再克隆。** 那样每台克隆机都带着
> **同一个身份**开机、互相覆盖，而文件是静默错的。安装器会在状态目录里发现
> 残留身份文件时告警；「机器配对」页也会对"多台机器共用同一硬件指纹"报警，
> 但那是事后发现，不是不犯错的理由。

## 目录约定

**下面这套只是默认值**，三层都可以改，不改也能直接用。

```
桌面/                              ← ① deploy_root：下发落地根（自动探测「桌面」或 Desktop）
├── 题面.zip                        ← 通用资料默认直接落这里（目标目录留空）
├── 样例.zip                        ← 题面/样例本身就是 zip，原样落盘、服务端不解压
└── <准考证号>/                      ← ② scan.roots：Agent 扫描根
    ├── p1/                        ← ③ 题目的 file_patterns 决定哪个文件算哪道题
    │   ├── 题面.zip                ←   按题下发时目标目录填 {player_no}/<题目名>
    │   └── p1.cpp                 ←   约定：<题目名>/<题目名>.cpp
    └── p2/
        └── p2.cpp
```

题面需要密码时，密码**不经过系统**：把 `password.txt` 当普通资产一起下发，
它会像别的文件一样落到桌面上。

回收后在服务端的落点：`source/<场次>/<准考证号>/p1/p1.cpp`。

### 三层可配置

| 层 | 配置项 | 默认值 | 决定什么 |
|---|---|---|---|
| ① Agent | `agent.deploy_root` | `{desktop}` | 服务端下发的文件落在哪 |
| ① Agent | `scan.roots` | `{desktop}/{player_no}` | 从哪回收代码 |
| ① Agent | `scan.prefix` | `none` | 上报路径要不要加前缀 |
| ② 服务端 | `SYNCOJ_DEFAULT_FILE_PATTERN` | `{ident}/**` | 题目**没配**模式时怎么认领文件 |
| ③ 每道题 | 「代码路径」列 | 跟随 ② | 这道题**特意**怎么认领文件 |

Agent 侧路径模板支持四个占位符 —— 前两个载入配置时就展开，后两个等注册拿到
凭据再展开（所以注册之前配错也不会当场炸，只是校验不了最后一段）：

| 占位符 | 何时展开 | 展开成 |
|---|---|---|
| `{desktop}` | 立即 | 当前用户桌面（兼容「桌面」与 `Desktop` 两种命名） |
| `{home}` | 立即 | 当前用户家目录 |
| `{player_no}` | 注册后 | 准考证号 |
| `{contest_slug}` | 注册后 | 场次标识 |

> `prefix` 默认 `none` 是有原因的：扫描根的名字就是准考证号，再加前缀会让
> `source/` 里准考证号出现两次。配了多个扫描目录时改用 `auto` 区分同名文件。
>
> ⚠️ **改了 `prefix` 就要跟着改题目的「代码路径」** —— 这两层是叠在一起的。
> 比如 `prefix = auto` 时上报路径变成 `<准考证号>/p1/p1.cpp`，默认的
> `{ident}/**` 就认不出来了（它只匹配 `p1/...`）。这时把代码路径改成
> `**/{ident}/**` 即可。改完用「路径试算」粘一条真实路径确认。

### 代码归题（③）

题目里配的是一组 **glob 模式**，决定「哪个文件算哪道题」。留空就用服务端默认。

- 语义是**标准 glob**：`*` 不跨 `/`，`**` 跨，`?` 单字符，`[abc]` / `[!abc]` 字符类。
  （不是 `fnmatch` —— 它的 `*` 会吞掉 `/`，那就没法表达「只在根目录下」。）
- **第一条命中的题目胜出**，顺序就是题目清单的顺序。不做「最具体者优先」
  那种隐式判断 —— 出问题时得能一眼解释清楚。
- `{ident}` / `{title}` 是**活占位符**，每次匹配时才展开。所以改了标识或标题，
  模式自动跟着走；`{title}` 在标题留空时退回标识。
- 归属是**算出来的，不落库**：改模式立刻生效，不需要重收文件。
- 界面里「题目清单 → 路径试算」可以粘一条相对路径问服务端它会归到哪道题
  （走的是同一份匹配实现，不是前端再算一遍）。

```
p1,签到题,{ident}/**                      ← 默认：p1/ 下的一切
p2,图论,{ident}/src/*.cpp;{ident}/**.h    ← 多条模式用 ; 或 | 分隔
p3,字符串,**/p3.cpp                       ← 不限目录层级
```

> 少配一个模式，代码照样收得上来（只是不归到任何题目），成绩矩阵会显示成
> 「未交」。这两件事在界面上分得很清楚：文件台账里标「未归类」，成绩页的
> 「未交」提示里也专门说了可能是模式配错。

## 教师上手流程

界面里就能走完，命令行也可以（便于脚本化批量部署）：

```bash
# ── 界面 ──────────────────────────────────────────────
# 1. syncoj-server init                建库 + 建管理员
# 2. 打开 http://<服务端>:8000          登录
# 3. 场次管理 → 新建场次
#                                      可选：指定默认名单
# 4. 名单库 → 新建名单 → 粘贴导入        （从 Excel 直接粘）
#                                      再回场次管理点「设置」选这份名单
# 5. 场次管理 → 进入 → 「应用名单」      （把名单落到这个场次）
# 6. 场次管理 → 题目                  → 登记题目（标识会用作目录名与代码文件名）
#                                      按需填「代码路径」，拿不准就留空
# 7. 装 Agent（见 agent/packaging/README.md）
#      syncoj-server bootstrap-key issue --out /etc/syncoj/bootstrap.key
#      → 把密钥放进镜像（root 只读）→ 机器开机
# 8. 机器配对 → 对着机器上显示的六位配对码，认领到名单里的人
```

配对是**永久**的：绑的是**人**（名单条目），不是某场比赛的选手。换一场比赛不用
重配，只要新场次也应用了那份名单；机器下一次 tick 就会自己拿到新的场次身份。

「文件下发」里的目标目录怎么填：

| 要发的东西 | 目标目录 | 落点 |
|---|---|---|
| 题面、样例、须知（通用资料） | **留空** | `桌面/题面.zip` |
| 某道题的附件、额外数据、模板 | `{player_no}/<题目名>` | `桌面/<准考证号>/<题目名>/数据.zip` |

选了「所属题目」会自动填好第二种；不选题就是第一种。「实际落点预览」那行会把
模板代入第一位选手算给你看，不用自己在脑子里展开。

**下发资产一律原样落盘，服务端不解压**：题面和样例本身就是 zip，机器上就是
`题面.zip` / `样例.zip`。要密码就把 `password.txt` 当普通资产一起发。

命令行等价流程（批量部署时更顺手）：

```bash
# 场次与选手
syncoj-server contest create --name "2025 校内模拟赛"
syncoj-server contest import-players --contest 2025 --file roster.csv
syncoj-server contest list

# 统一注册密钥（整间机房一份；明文只显示这一次）
syncoj-server bootstrap-key issue --label "2025 机房镜像" --expires-days 30 \
    --out /srv/syncoj/bootstrap.key      # 顺便写成 root 只读的一份
syncoj-server bootstrap-key list
syncoj-server bootstrap-key pending      # 还没配对的机器（含配对码剩余时间）
syncoj-server bootstrap-key revoke --id 1

# 开发阶段库结构还在动，需要重来时
syncoj-server db reset --yes [--keep-admin]
```

名单 CSV 的列顺序是 `选手编号,姓名,座位,分组`（只有编号必填），
逗号或制表符分隔都认，表头与 `#` 注释行会被自动跳过。

测试规模：**服务端 561 项通过**（另 2 项按环境跳过）、**Agent 288 项通过**
（另 13 项按环境跳过）。含端到端集成测试（真实 Agent 代码通过真实 HTTP 打到真实
服务端）、**openssl 交叉验证**（手写密码学代码唯一可信的证据是独立实现能互相
验通，而且要求 openssl 1.1.1 与 3.0 两种默认输出格式都能读）。

## 部署

见 [`agent/packaging/README.md`](agent/packaging/README.md)。三条安装入口：

```bash
# 离线包
sudo python3 install.py --bundle ./syncoj-agent-0.1.0.tar.gz \
     --sha256 <校验和> --server https://10.0.0.1:8443

# 在线自举
curl -fsSL https://10.0.0.1:8443/dist/bootstrap.sh | sudo sh -s -- --server ...

# 先预览（不需要 root）
python3 install.py --bundle ./x.tar.gz --server https://x --dry-run
```

`install.py` 不接受「每选手注册码」——那条链路已经删除了，只有统一密钥一条路。

### 密钥从哪来：`.key/`

为了不用"手工 genkey → 手工指私钥 → 手工把公钥拷进包"走一遍才能试一次升级，
`syncoj-server init` 会把两把密钥都建好：

```
.key/                         ← 默认密钥目录（整个目录不进版本库，两道 gitignore 挡着）
├── release-key.pem           发布签名私钥  0600
├── release-key.pub.json      对应公钥（打包 Agent 时自动内嵌）
└── bootstrap.key             统一注册密钥  0600（哈希同时写进库，开箱即可装机）
```

| 环节 | 行为 |
|---|---|
| `syncoj-server init` | 缺哪把建哪把；**已有的绝不覆盖、绝不轮换**，并把建了什么/找到了什么打出来 |
| 服务端用私钥 | `.key/release-key.pem` 在就用它；`SYNCOJ_RELEASE_KEY` 覆盖一切 |
| `build_bundle.py` | 默认取 `.key/release-key.pub.json`（存在才用）打进包；显式 `--public-key` 给错路径会**报错退出**，不会静默打出个不能升级的包 |
| `install.py` | 统一密钥的优先级：`--bootstrap-key`（明文）> `--bootstrap-key-file` > `.key/bootstrap.key`（存在才用）> 没有。包里的公钥会落到装机器上的配置目录，`agent.ini` 指向那份副本 |

三条默认值**都是"有则生效、无则保持原样"**：没有私钥就照旧不提供任何升级，
没有公钥就打出和以前一样的包，没有 `.key/` 目录就什么都不发生。生产部署请用
`SYNCOJ_KEY_DIR` / `SYNCOJ_RELEASE_KEY` 指到只有服务端账号读得到的目录。

> ℹ️ `db reset` 只删库、不碰 `.key/`，所以"密钥文件还在、库里的哈希没了"是会出现的
> 一态。`init` 会把它**补登记**回去；但**已经被吊销的密钥不会被复活** —— 吊销是
> 有意动作，不该被一次 init 撤销。

> ⚠️ 私钥放在仓库相对路径上是一个明确的取舍：任何拿到这份 checkout 的人都能签出
> 考试机会接受的升级包。`.key/` 由目录自带的 `.gitignore` 与根 `.gitignore`
> **两道**挡着 —— 私钥的安全不该只挂在一个可以被删掉的文件上。

### 发一个 Agent 版本

服务端**跑在源码仓库里**时（`.key/` 就是这么找到的），不必先手工打包：管理界面
「Agent 发布」页上直接「发布当前版本」—— 服务端从本机 `agent/` 源码构建、用私钥
签好、进版本历史。

```bash
# 界面上等价的那条命令；产物与界面构建的逐字节相同（可复现打包）
python3 agent/packaging/build_bundle.py --out dist/syncoj-agent-0.1.0.tar.gz
```

三个必须知道的边界：

- **构建 ≠ 铺开。** 构建出来的是草稿，机器看不到它，要再点「铺开」。
- **版本号必须显式提交、且必须与 `agent/syncoj_agent/__init__.py` 里的 `__version__`
  一致。** 不一致时给 409 并同时报出两个版本号。这是为了挡住"改了代码忘了改版本号
  就静默覆盖上一版"——那种情况的现场表现是"发成功了但已升级的机器拒绝升级"。
- **缺私钥或缺公钥都会被拒绝**，不是降级打一个未签名/无信任锚的包。后者更糟：
  界面上一切正常，而所有机器静默不升级。服务端所在机器没有 `agent/` 源码时
  （例如 pip 装出来的生产实例），这一栏会说明原因，仍可走「上传安装包」那条路。

