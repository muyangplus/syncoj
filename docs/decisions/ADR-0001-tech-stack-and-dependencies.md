# ADR-0001：技术栈与依赖

**状态**：已锁定（v1）

## 背景

目标机是 **NOI Linux 2.0**（Ubuntu 20.04 / glibc 2.31 / systemd 245 / Python 3.8.10），
考场通常**离线**，机器是整间机房共用一份镜像装出来的。Python 3.8 早已 EOL，
`pip install` 在那些机器上要么没网、要么装出一份谁也说不清的版本组合。

## 决定

| 层 | 选择 |
|---|---|
| Agent | Python 3.8 + **零第三方依赖**，只用标准库；`python3 -E -s` 启动 |
| 通信 | **HTTP 自适应轮询**（空闲 60s / 有活 2s），无 WebSocket |
| 配置 | **INI**（`configparser`，标准库），不用 TOML |
| Agent 状态 | **无状态**：服务端持有全部状态，Agent 不落本地队列 |
| 服务端 | FastAPI + uvicorn + SQLAlchemy 2.0 + SQLite(WAL)，单机 |
| 前端 | Vue3 + Vite + Pinia，TS 类型从 OpenAPI 生成 |
| 评测对接 | 只投递 `source/` + 扫描结果目录回写成绩，**不触发评测** |

四个替代关系与收益：

| 原方案依赖 | 替代 | 收益 |
|---|---|---|
| `watchdog`（inotify） | 周期 `os.scandir` 全量扫描 | 免依赖；且 tick 本就上报全量索引，inotify 是冗余的 |
| `websockets` | HTTP 轮询 | 免依赖；去掉重连/半开检测/连接生命周期状态机 |
| `requests` / `httpx` | `http.client` + `urllib.request` | 标准库 |
| `pydantic` | 纯 dict + CI 契约测试 | 标准库 |
| `tomli`/`tomllib` | `configparser`（INI） | `tomllib` 是 3.11+，3.8 没有 |
| 本地队列 SQLite | 服务端持有状态，Agent 无状态 | 免依赖；崩溃/重装天然自愈 |

选手折腾 Python 环境的担忧用 `python3 -E -s` 解决（`-E` 忽略所有 `PYTHON*` 环境变量，
`-s` 忽略 user site-packages）—— 选手怎么 `pip install` 都污染不到 Agent。

## 后果

- **每次 tick 上报全量文件索引**：50 个文件约 5KB/次，20s 一次可忽略。换来服务端
  免费的完整文件台账 + 免费的删除检测（比对快照而不是事件流，Agent 停机期间的事件
  也不会丢），还省掉了 `fs.inotify.max_user_watches` 调优。
- 协议一致性**不靠共享模型**，靠 CI 契约测试（见 [`../design/05-api-and-frontend.md`](../design/05-api-and-frontend.md) §5.1）。

## 被否掉的替代方案与理由

* **`watchdog` / inotify 事件流**。多一个依赖，而且事件流在 Agent 停机期间会丢；
  轮询快照反而更可靠。tick 本来就要报全量索引，inotify 是**冗余的**。
* **WebSocket**。50 台机器的规模下它只换来"实时性"（需求只要秒级），却引入重连、
  半开检测、连接生命周期、本地队列状态机一大堆复杂度。轮询让 Agent 变成
  **无状态进程**，崩溃/断网/重装全部天然自愈 —— 这是这套方案最值钱的性质之一。
* **`requests` / `httpx` / `pydantic` / 本地队列 SQLite**。都是"装上就多一个版本
  组合"的东西，而目标机离线。pydantic 的替代不是"不要校验"，是"校验放到 CI 的
  契约测试里"。
* **TOML**。`tomllib` 是 Python 3.11 才进的标准库，目标机 3.8 没有；用 `configparser`
  读 INI 是唯一不需要 pip 的选择。

## 关键设计取舍

下面这一段是重建文档之前 `README.md`「关键设计取舍」一节的**逐字**内容
（只把末尾那句 `详见 DESIGN.md` 换成了新落点）—— 它把四条决定一口气说清楚了，
改任何一条之前都该先读它。

这一版方案和"教科书式"做法有几处刻意不同，都是被约束逼出来的：

1. **Agent 零第三方依赖**。只用 Python 3.8 标准库 —— 不用 `watchdog`、不用 `websockets`、不用 `requests`、不用 `pydantic`。原因是 NOI Linux 上 Python 3.8 已 EOL、考场通常离线，任何第三方依赖都是部署风险。砍掉依赖后，Python 3.8 的版本绑定风险基本归零。

2. **用 HTTP 自适应轮询替代 WebSocket**。50 台机器的规模下，WebSocket 只换来"实时性"（需求其实只要秒级）却引入重连、半开检测、连接生命周期、本地队列状态机一大堆复杂度。轮询让 Agent 变成**无状态进程**，崩溃/断网/重装全部天然自愈。

3. **配置用 INI 而不是 TOML**。`tomllib` 是 Python 3.11+ 才有的，用 `configparser`（标准库）。

4. **每次 tick 上报全量文件索引**。50 个文件约 5KB/次，成本可忽略，换来的是服务端免费的完整文件台账 + 免费的删除检测（不依赖 inotify 事件流，Agent 停机期间的事件也不会丢）。

详细论证见 [`../design/`](../design/) 下的设计文档；本目录里的 ADR 记的是
每一条**被否掉的替代方案**。
