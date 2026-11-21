# ADR-0001：技术栈与依赖

**状态**：已锁定（v1）；评测对接一行由 [ADR-0016](ADR-0016-builtin-judge.md) 扩充（外部评测器仍不触发评测）。

## 背景

目标机是 **NOI Linux 2.0**（Ubuntu 20.04 / glibc 2.31 / systemd 245 / Python 3.8.10），考场通常**离线**，整间机房共用一份镜像。
Python 3.8 已 EOL，在那些机器上 `pip install` 要么没网，要么装出说不清的版本组合。

## 决定

| 层 | 选择 |
|---|---|
| Agent | Python 3.8 + **零第三方依赖**，只用标准库；`python3 -E -s` 启动 |
| 通信 | **HTTP 自适应轮询**（空闲 30s / 有活 2s），无 WebSocket |
| 配置 | **INI**（`configparser`），不用 TOML |
| Agent 状态 | **无状态**：服务端持有全部状态，Agent 不落本地队列 |
| 服务端 | FastAPI + uvicorn + SQLAlchemy 2.0 + SQLite(WAL)，单机 |
| 前端 | Vue3 + Vite + Pinia，TS 类型从 OpenAPI 生成 |
| 评测对接 | 只投递 `source/` + 扫描结果目录回写成绩，**不触发评测** |

依赖替代：

| 原方案依赖 | 替代 | 收益 |
|---|---|---|
| `watchdog`（inotify） | 周期 `os.scandir` 全量扫描 | 免依赖；tick 本就上报全量索引 |
| `websockets` | HTTP 轮询 | 免依赖；没有重连/半开检测/连接状态机 |
| `requests` / `httpx` | `http.client` + `urllib.request` | 标准库 |
| `pydantic` | 纯 dict + CI 契约测试 | 标准库 |
| `tomli`/`tomllib` | `configparser`（INI） | `tomllib` 要 3.11+ |
| 本地队列 SQLite | 服务端持有状态 | 免依赖；崩溃/重装天然自愈 |

`-E` 忽略 `PYTHON*` 环境变量、`-s` 忽略 user site-packages，选手的 `pip install` 污染不到 Agent。

## 后果

- **每次 tick 上报全量文件索引**（50 文件约 5KB/次，可忽略），换来完整文件台账与删除检测（比对快照，Agent 停机期间的变化不丢），也免去 `fs.inotify.max_user_watches` 调优。
- 协议一致性靠 CI 契约测试而非共享模型（[`../design/05-api-and-frontend.md`](../design/05-api-and-frontend.md) §5.1）。

## 被否掉的替代方案

* **`watchdog` / inotify 事件流**：多一个依赖，且停机期间事件会丢；tick 本就报全量索引，inotify 冗余。
* **WebSocket**：需求只要秒级，它却引入重连、半开检测、连接生命周期、本地队列状态机；轮询让 Agent 无状态，崩溃/断网/重装天然自愈。
* **`requests` / `httpx` / `pydantic` / 本地队列 SQLite**：目标机离线，每个都多一个版本组合；校验改放 CI 契约测试。
* **TOML**：`tomllib` 3.11 才进标准库，INI 是唯一不需要 pip 的选择。
