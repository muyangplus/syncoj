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
│   └── judge-result.md       评测器对接：成绩怎么进 SyncOJ
├── server/                   服务端（FastAPI + SQLite WAL）
│   ├── syncoj_server/
│   └── tests/
├── agent/                    选手端 Agent（Python 3.8 零依赖）
│   ├── syncoj_agent/
│   ├── packaging/            幂等安装器 / 打包器 / 在线自举
│   ├── tools/                py38 兼容门禁、协议 fixture 生成
│   └── tests/
├── scripts/check.sh          一键跑全部检查
└── web/                      管理界面（Vue3 + TS + Vite + Element Plus）
    ├── src/api/              client + 从 OpenAPI 生成的类型
    ├── src/views/            7 个页面
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
python agent/run_agent.py --config agent/config.example.ini --check
python agent/run_agent.py --config agent/config.example.ini --once
```

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
./scripts/check.sh
```

## 状态

| 里程碑 | 状态 |
|---|---|
| M1 骨架：enroll / tick / 在线状态 / 管理后台 | ✅ 完成 |
| M2 回收：扫描 / 上传 / 内容寻址存储 / `source/` 落盘 / 删除审计 | ✅ 完成 |
| M3 下发：资产管理 / 任务编排（全员·按人·按分组）/ Range 断点续传 / 进度聚合 | ✅ 完成 |
| M4 成绩：结果扫描回写 / 成绩矩阵 / 手工补录 / 原始记录 | ✅ 完成 |
| M5 运维：RSA 签名自更新 / 安全解包 / 自动回滚 / 幂等安装器 / 可复现打包 | ✅ 完成 |
| 管理界面：8 个页面 + 初始化闭环（建场次 / 导选手 / 维护题目 / 批量签注册码） | ✅ 完成 |

## 目录约定

**下面这套只是默认值**，三层都可以改，不改也能直接用。

```
桌面/                              ← ① deploy_root：下发落地根（自动探测「桌面」或 Desktop）
├── 题面.pdf                        ← 通用资料默认直接落这里（目标目录留空）
├── 样例/                           ← 也可以自己填相对路径
└── <准考证号>/                      ← ② scan.roots：Agent 扫描根
    ├── p1/                        ← ③ 题目的 file_patterns 决定哪个文件算哪道题
    │   ├── 题面.pdf                ←   按题下发时目标目录填 {player_no}/<题目名>
    │   └── p1.cpp                 ←   约定：<题目名>/<题目名>.cpp
    └── p2/
        └── p2.cpp
```

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
# 4. 场次管理 → 题目                  → 登记题目（标识会用作目录名与代码文件名）
#                                      按需填「代码路径」，拿不准就留空
# 5. 选手状态 → 导入选手                （可直接从 Excel 粘贴）
# 6. 选手状态 → 批量签发注册码          → 复制或导出 CSV
# 7. 文件下发 → 上传题面                （目标目录留空 = 直接落桌面）→ 下发
# 8. 装 Agent（见 agent/packaging/README.md），把注册码传给它
```

「文件下发」里的目标目录怎么填：

| 要发的东西 | 目标目录 | 落点 |
|---|---|---|
| 题面、样例、须知（通用资料） | **留空** | `桌面/题面.pdf` |
| 某道题的附件、额外数据、模板 | `{player_no}/<题目名>` | `桌面/<准考证号>/<题目名>/数据.zip` |

选了「所属题目」会自动填好第二种；不选题就是第一种。「实际落点预览」那行会把
模板代入第一位选手算给你看，不用自己在脑子里展开。

命令行等价流程（批量部署时更顺手）：

```bash
syncoj-server contest create --name "2025 校内模拟赛"
syncoj-server contest import-players --contest 2025 --file roster.csv
syncoj-server contest enroll-codes --contest 2025 > codes.csv
syncoj-server contest list
```

名单 CSV 的列顺序是 `选手编号,姓名,座位,分组`（只有编号必填），
逗号或制表符分隔都认，表头与 `#` 注释行会被自动跳过。

测试规模：**服务端 352 项、Agent 187 项**，含端到端集成测试（真实 Agent 代码
通过真实 HTTP 打到真实服务端）与 **openssl 交叉验证**（手写密码学代码唯一可信的
证据是独立实现能互相验通）。

## 部署

见 [`agent/packaging/README.md`](agent/packaging/README.md)。三条安装入口：

```bash
# 离线包
sudo python3 install.py --bundle ./syncoj-agent-0.1.0.tar.gz \
     --sha256 <校验和> --server https://10.0.0.1:8443 --enroll-code XXXX-XXXX-XXXX-XXXX

# 在线自举
curl -fsSL https://10.0.0.1:8443/dist/bootstrap.sh | sudo sh -s -- --server ...

# 先预览（不需要 root）
python3 install.py --bundle ./x.tar.gz --server https://x --dry-run
```
