# 部署与安装

本目录：`install.py`（幂等安装器）、`build_bundle.py`（打包器）、`bootstrap.sh`（在线自举）。镜像预装、离线包、在线安装共用同一份逻辑。
完整参数、排障与卸载 → 见 [`docs/install-agent.md`](../../docs/install-agent.md)。

## 注册

| 项 | 值 |
|---|---|
| 注册方式 | 唯一一种：镜像内置的统一注册密钥；每台机器首次开机各自换回本机凭据 |
| 密钥位置 | `/etc/syncoj/bootstrap.key`，0600、属主 root；不得写进 `agent.ini`（该文件 chown 给选手账号） |
| 换凭据 | root 一次性单元 `syncoj-agent-enroll.service` 运行 `run_agent.py --provision`，凭据 chown 给运行账号；Agent 本体只读凭据，不读密钥 |
| 配对 | 机器在桌面写「配对码.txt」（六位）；教师在「机器配对」绑定到名单条目（人）；配对永久，换场次只需新场次应用同一份名单 |
| 已删除的方式 | 每选手注册码（`--enroll-code`）；`agent.ini` 中残留的 `enroll_code =` 被静默忽略 |

机器状态：

| 状态 | 桌面 | 处理 |
|---|---|---|
| 未配对 | `配对码.txt` | 「机器配对」→ 该机器行「配对」，或「按码配对」输入六位码 |
| 已配对、无场次 | `等待场次.txt` | 把该名单应用到场次 |
| 正常 | 两个文件都没有 | — |

查看配对码：

```bash
python3 /opt/syncoj/current/run_agent.py --config /etc/syncoj/agent.ini --pair-code
```

## 一键安装（四种入口）

```bash
# 1. 从服务端直接装（空机器、小镜像）
#    省略 --server 时在局域网广播发现服务端（须给 --public-key 才能验应答）
sudo python3 install.py \
    --from-server --server http://10.0.0.1:8000 \
    --bootstrap-key <签发出的密钥> \
    --user student

# 2. 镜像预装离线包
sudo python3 install.py \
    --bundle ./syncoj-agent-0.1.0.tar.gz \
    --sha256 <校验和> \
    --server http://10.0.0.1:8000 \
    --bootstrap-key <签发出的密钥> \
    --user student

# 3. 在线自举（空机器上的第一条命令）
curl -fsSL http://10.0.0.1:8000/api/v1/agent/install/bootstrap.sh | sudo sh -s -- \
    --server http://10.0.0.1:8000 --bootstrap-key <签发出的密钥> --user student

# 4. 任意 URL 下载（如内网静态文件服务器）
sudo python3 install.py \
    --download-url http://10.0.0.1/x/syncoj-agent-bundle.tar.gz \
    --sha256 <从服务端界面抄下来的校验和> \
    --server http://10.0.0.1:8000 --user student

# 预览（不需要 root，不改动、不联网）
python3 install.py --from-server --server http://x --dry-run
```

### `--from-server` 的校验

- 装机端点 `/api/v1/agent/install/*` 不鉴权；只发**已铺开**的版本（与升级同一判据）。
- 装完的机器处于"待配对"，教师在「机器配对」绑定后才开始收代码、收文件、记成绩。

| 机器上有发布公钥 | 校验内容 |
|---|---|
| 有（`--public-key`，或已装好的机器） | sha256 + 签名 |
| 无 | 仅 sha256（对照服务端台账）；输出"未验签"与校验和，需人工对照界面 |

- 带验签：把 `release-key.pub.json` 放进镜像，用 `--public-key <路径>`，或自举脚本的环境变量 `RELEASE_PUBLIC_KEY=<路径>`。
- ⚠️ 仅 sha256 时只挡传输损坏与半截下载，挡不住恶意服务端（台账与包同源且不鉴权）。
- 重复安装时省略 `--bootstrap-key`：安装器识别机器上已有的密钥文件，保留注册单元。

## 建镜像

⚠️ 母机上跑过 Agent 再做镜像，所有克隆机共用同一身份（`credential.json` + `machine_uuid`），成绩会对错人；安装器发现残留身份文件时告警并给出命令。

1. 装 Agent。不要启动服务、不要跑 `--once`、不要跑 `--provision`。
2. 母机已跑过时，删除状态目录里的 `credential.json` 与 `machine_uuid`。
3. 做镜像。
4. 部署到各机器。
5. 首次开机，`syncoj-agent-enroll.service` 为每台生成各自的凭据与 `machine_uuid`。

多台机器报同一硬件指纹时，「机器配对」页显示克隆告警。

## 运行身份

Agent 以 `--user` 指定的选手登录账号运行。

| 单元设置 | 值 |
|---|---|
| `ProtectHome` | 不设（`read-only` 会使家目录只读，Agent 无法写入） |
| `ReadWritePaths=` | `-/home/<运行账号> <安装根> <状态目录>`，每条带 `-`，家目录为安装时解析的绝对路径 → 见 [ADR-0007](../../docs/decisions/ADR-0007-unit-path-whitelist.md) |
| `ProtectSystem` | `strict`（`/usr`、`/etc`、`/boot` 等只读） |
| `agent.ini`、状态目录文件 | 属主运行账号，0600；属主错误表现为"装完起不来"或"一直重新注册"，安装器检查并告警 |

## 目录约定

默认值，可改：

```
桌面/                               ← deploy_root = {desktop}
├── 题面.pdf                         ← 目标目录留空 = 落桌面
└── <准考证号>/                      ← scan.roots = {desktop}/{player_no}
    ├── p1/                        ← 按题下发目标目录填 {player_no}/<题目名>
    │   ├── 题面.pdf
    │   └── p1.cpp                 ← 选手代码
    └── p2/p2.cpp
```

- `{desktop}` 探测顺序：`<运行账号的家>/.config/user-dirs.dirs` 的 `XDG_DESKTOP_DIR`，再试 `~/桌面`、`~/Desktop`、`~/desktop`。
- `{desktop}` / `{home}` 恒按运行账号（`[agent] run_user`，安装器自动写入）解析，与当前进程用户无关；按 root 解析会得到 `/root/桌面`，注册退出 2 → 见 [ADR-0010](../../docs/decisions/ADR-0010-run-user-owns-path-templates.md)。

| 占位符 | 何时展开 | 展开成 |
|---|---|---|
| `{desktop}` | 载入配置时 | 运行账号的桌面（兼容「桌面」与 `Desktop`） |
| `{home}` | 载入配置时 | 运行账号的家目录 |
| `{player_no}` | 注册后 | 准考证号 |
| `{contest_slug}` | 注册后 | 场次标识 |

```ini
[agent]
run_user = student                       ; 路径模板按它解析（安装器自动填）
deploy_root = {desktop}                  ; 或写死 /home/student/桌面

[scan]
; 下面几行任选其一
roots = {desktop}/{player_no}            ; 默认：桌面/<准考证号>
roots = {home}/我的代码                    ; 家目录下
roots = {desktop}/{contest_slug}/{player_no}   ; 一个考点跑多场次时分开
prefix = none                            ; none / auto / 字面量（支持上面两个注册后占位符）
```

- 展开不了的占位符原样保留，不报错。
- 改路径后重启服务，无需重装：

  ```bash
  sudo systemctl restart syncoj-agent
  journalctl -u syncoj-agent -n 30     # 第一行打印实际扫描目录
  ```

## 安装后的布局

```
/opt/syncoj/
├── releases/
│   ├── 0.1.0/                   每个版本一份
│   │   ├── run_agent.py         ← systemd ExecStart
│   │   └── syncoj_agent/...
│   └── 0.1.1/...
└── current -> releases/0.1.1    原子切换的软链

/etc/syncoj/agent.ini            配置（已存在时不覆盖）
/var/lib/syncoj/                 凭据、哈希缓存、日志、未完成的下载
```

⚠️ 手工部署成单一目录时 `upgrade.mode = apply` 失败。

## 幂等性

| 保证 | 例外 |
|---|---|
| 不覆盖已有 `agent.ini` | `--force-config` |
| 同版本不重复解压 | — |
| 已是当前版本则不重启服务 | — |

## 打包

```bash
python3 build_bundle.py                                                  # 产出 dist/syncoj-agent-<版本>.tar.gz
python3 build_bundle.py --verify-signature /etc/syncoj/release-key.pem   # 另产出 .sig（供手工核对）
```

可复现：同样内容产出逐字节相同的包，与输出文件名无关（gzip 头时间戳与文件名、tar 成员 mtime 均已归零）。

## 卸载与排障

卸载 → 见 [`docs/install-agent.md`](../../docs/install-agent.md)「卸载 Agent」（不要删除运行账号）。

```bash
systemctl status syncoj-agent          # 进程状态
journalctl -u syncoj-agent -n 100      # systemd 启动期输出
tail -f /var/lib/syncoj/agent.log      # Agent 日志（自动轮转）
/usr/bin/python3 -E -s /opt/syncoj/current/run_agent.py \
    --config /etc/syncoj/agent.ini --check      # 只校验配置
/usr/bin/python3 -E -s /opt/syncoj/current/run_agent.py \
    --config /etc/syncoj/agent.ini --once       # 前台跑一轮
```

| 规则 | 违反后果 |
|---|---|
| 经 `run_agent.py` 启动，不直接运行 `syncoj_agent/main.py` | `ImportError: attempted relative import with no known parent package`（`-E` 忽略 `PYTHONPATH`，包路径由启动器设置 `sys.path`） |
| 手工调试带 `-E -s`，与 systemd 用同一条命令 | 行为与线上不一致（`PYTHON*` 环境变量、user site-packages 生效） |
