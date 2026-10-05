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
├── docs/protocol.md          tick 协议规范
├── server/                   服务端（FastAPI + SQLite WAL）
│   ├── syncoj_server/
│   └── tests/
├── agent/                    选手端 Agent（Python 3.8 零依赖）
│   ├── syncoj_agent/
│   ├── packaging/systemd/
│   └── tools/                py38 兼容检查、协议 fixture 生成
└── web/                      管理界面（Vue3 + Vite，M3 起）
```

## 快速开始（开发）

```bash
# 服务端
python -m venv server/.venv
server/.venv/Scripts/activate      # Linux: source server/.venv/bin/activate
pip install -e server[dev]
syncoj-server --init-db            # 建库 + 生成管理员口令 + 打印注册码

# Agent（零依赖，直接跑）
python agent/syncoj_agent/main.py --config agent/config.example.ini
```

## 开发约定

- **Agent 侧代码必须是 Python 3.8 兼容**。提交前跑 `python agent/tools/check_py38.py agent/`，CI 也会跑。
- **协议改动必须同时更新** `docs/protocol.md` 与契约测试 fixture。
- 服务端路径安全校验**独立实现**，绝不信任 Agent 上报的相对路径。

## 状态

- [x] M1 骨架：enroll / tick / 在线状态
- [ ] M2 回收：扫描 / 上传 / `source/` 落盘
- [ ] M3 下发：asset / deploy / 断点续传
- [ ] M4 成绩：结果扫描回写 / 审计日志
- [ ] M5 运维：签名自更新 / 离线包 / installer
