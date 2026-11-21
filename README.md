# SyncOJ — Online Sync Judge

考场在线同步评测：考试机静默回收代码、接收下发文件；服务端落盘到 `source/<场次>/<选手编号>/`，
由 LemonLime / Arbiter 或自带沙箱评测（`syncoj-judge`）出成绩并回写。

| 角色 | 环境 |
|---|---|
| 考试机 | NOI Linux 2.0（Ubuntu 20.04 / systemd 245 / Python 3.8.10） |
| 服务端 | 内网单机，Python 3.8+；管理端口 8000，公开端口 80（装机页、选手页） |

## 启动服务端

1. 取代码：下载 [Releases](https://github.com/muyangplus/syncoj/releases) 里的 `syncoj-<版本>.tar.gz` 并解压（自带前端），或 `git clone` 本仓库（前端需 `cd web && npm ci && npm run build`）。
2. 安装（必须 `-e`，服务端按目录找 `web/dist` 与 `agent/`）：
   ```bash
   python3 -m venv .venv && .venv/bin/pip install -e server
   ```
3. 初始化（建库、建管理员、生成 `.key/` 下的发布私钥与统一注册密钥；不给密码则随机生成并打印）：
   ```bash
   .venv/bin/syncoj-server init --admin-password <密码>
   ```
4. 启动（`--public-url` 填考试机能访问到的地址）：
   ```bash
   .venv/bin/syncoj-server serve --host 0.0.0.0 --public-url http://<服务端IP>:8000
   ```
5. 浏览器打开 `http://<服务端IP>:8000`，用 `admin` 登录。

Windows 上把 `.venv/bin/` 换成 `.venv/Scripts/`。数据目录默认 `runtime/server`（`SYNCOJ_DATA_ROOT` 或 `--data-root` 改）。

## 接着做

| 要做的事 | 文档 |
|---|---|
| 建场次、导名单、配对机器、收成绩 | [`docs/quickstart.md`](docs/quickstart.md) |
| 给考试机装 Agent | [`docs/install-agent.md`](docs/install-agent.md) |
| 部署、发版、自带评测机、排障 | [`docs/operate.md`](docs/operate.md) |
| 协议、接口、配置项 | [`docs/reference/`](docs/reference/) · 设计 [`docs/design/`](docs/design/) · 决策 [`docs/decisions/`](docs/decisions/) |

全部文档清单：[`docs/README.md`](docs/README.md)。开发：[`CONTRIBUTING.md`](CONTRIBUTING.md)。安全：[`SECURITY.md`](SECURITY.md)。许可证 Apache-2.0。
