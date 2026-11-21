# SyncOJ — Online Sync Judge

<p align="center"><a href="https://github.com/muyangplus/syncoj/releases/latest"><img alt="正式版" src="https://img.shields.io/github/v/release/muyangplus/syncoj?label=%E6%AD%A3%E5%BC%8F%E7%89%88&color=2F80ED"></a> <a href="https://github.com/muyangplus/syncoj/releases"><img alt="测试版" src="https://img.shields.io/github/v/release/muyangplus/syncoj?include_prereleases&label=%E6%B5%8B%E8%AF%95%E7%89%88&color=6C5CE7"></a> <a href="https://github.com/muyangplus/syncoj/releases"><img alt="下载量" src="https://img.shields.io/github/downloads/muyangplus/syncoj/total?label=%E4%B8%8B%E8%BD%BD"></a></p>
<p align="center"><a href="https://github.com/muyangplus/syncoj/actions/workflows/check.yml"><img alt="CI" src="https://img.shields.io/github/actions/workflow/status/muyangplus/syncoj/check.yml?branch=main&label=CI"></a> <a href="https://github.com/muyangplus/syncoj/actions/workflows/release.yml"><img alt="发布" src="https://img.shields.io/github/actions/workflow/status/muyangplus/syncoj/release.yml?label=%E5%8F%91%E5%B8%83"></a> <a href="LICENSE"><img alt="许可证" src="https://img.shields.io/github/license/muyangplus/syncoj?label=%E8%AE%B8%E5%8F%AF%E8%AF%81"></a> <a href="https://github.com/muyangplus/syncoj/commits/main"><img alt="最近提交" src="https://img.shields.io/github/last-commit/muyangplus/syncoj/main?label=%E6%9C%80%E8%BF%91%E6%8F%90%E4%BA%A4"></a></p>
<p align="center"><img alt="NOI Linux" src="https://img.shields.io/badge/NOI_Linux-2.0-E95420?logo=ubuntu&logoColor=white"> <img alt="systemd" src="https://img.shields.io/badge/systemd-245-30D475?logo=linux&logoColor=white"> <img alt="Python" src="https://img.shields.io/badge/Python-3.8%2B-3776AB?logo=python&logoColor=white"></p>
<p align="center"><img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-0.100%2B-009688?logo=fastapi&logoColor=white"> <img alt="SQLAlchemy" src="https://img.shields.io/badge/SQLAlchemy-2-D71F00?logo=sqlalchemy&logoColor=white"> <img alt="SQLite" src="https://img.shields.io/badge/SQLite-WAL-003B57?logo=sqlite&logoColor=white"> <img alt="Vue" src="https://img.shields.io/badge/Vue-3-4FC08D?logo=vuedotjs&logoColor=white"> <img alt="TypeScript" src="https://img.shields.io/badge/TypeScript-5-3178C6?logo=typescript&logoColor=white"> <img alt="Vite" src="https://img.shields.io/badge/Vite-5-646CFF?logo=vite&logoColor=white"> <img alt="Element Plus" src="https://img.shields.io/badge/Element_Plus-2-409EFF?logo=element&logoColor=white"></p>

考场在线同步评测：考试机静默回收代码、接收下发文件；服务端落盘到 `source/<场次>/<选手编号>/`，
由 LemonLime / Arbiter 或自带沙箱评测（`syncoj-judge`）出成绩并回写。考试机：NOI Linux 2.0（Ubuntu 20.04 / systemd 245 /
Python 3.8.10）；服务端：内网单机 Python 3.8+，管理端口 8000，公开端口 80（装机页、选手页）。

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
5. 浏览器打开 `http://<服务端IP>:8000`，用 `admin` 登录。Windows 上把 `.venv/bin/` 换成 `.venv/Scripts/`。数据目录默认 `runtime/server`（`SYNCOJ_DATA_ROOT` 或 `--data-root` 改）。

## 接着做

| 要做的事 | 文档 |
|---|---|
| 建场次、导名单、配对机器、收成绩 | [`docs/quickstart.md`](docs/quickstart.md) |
| 给考试机装 Agent | [`docs/install-agent.md`](docs/install-agent.md) |
| 部署、发版、自带评测机、排障 | [`docs/operate.md`](docs/operate.md) |
| 协议、接口、配置项 | [`docs/reference/`](docs/reference/) · 设计 [`docs/design/`](docs/design/) · 决策 [`docs/decisions/`](docs/decisions/) |

全部文档清单：[`docs/README.md`](docs/README.md)。开发：[`CONTRIBUTING.md`](CONTRIBUTING.md)。安全：[`SECURITY.md`](SECURITY.md)。
