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

## 三条命令

```bash
python -m venv .venv && .venv/Scripts/pip install -e "server[dev]"   # Linux: .venv/bin/pip
.venv/Scripts/syncoj-server init            # 建库 + 建管理员 + 建好 .key/ 两把密钥
.venv/Scripts/syncoj-server serve --host 0.0.0.0
```
打开 `http://<服务端>:8000` 登录。前端热更新用 `cd web && npm install && npm run dev`；
`npm run build` 产出 `web/dist` 后由服务端托管（单进程部署，不需要 nginx）。

## 文档地图

**不知道从哪看起就进 [`docs/README.md`](docs/README.md)** —— 它按"我是谁"分流。

| 我是谁 | 去哪 |
|---|---|
| 教师 / 装机 / 运维 | [`quickstart.md`](docs/quickstart.md) · [`install-agent.md`](docs/install-agent.md) · [`operate.md`](docs/operate.md) |
| 开发者：接口、字段与配置 | [`docs/reference/`](docs/reference/) |
| 想改设计、想知道"当初否掉了什么" | [`docs/design/`](docs/design/) · [`docs/decisions/`](docs/decisions/) |
老文档里的 `DESIGN.md §X.Y` 引用仍然有效：[`DESIGN.md`](DESIGN.md) 只剩一张「§ 编号
→ 新文件」的映射表。这一版刻意不同于"教科书式"做法的地方（零依赖、轮询、每次 tick
上报全量索引…）与它们的理由都在 ADR 里；`docs/**` 下每个文件都列在 `docs/README.md`。
