# 装 Agent

## 机器注册与配对

```
装机（root 一次性）        机器首次开机              教师
bootstrap.key  ──换凭据──▶  桌面「配对码.txt」 ──读码/输入──▶ 绑到名单条目
```

| 项 | 值 |
|---|---|
| 统一注册密钥 | `/etc/syncoj/bootstrap.key`，0600，属主 root；不得写进 `agent.ini`（选手账号可读） |
| 签发 | `syncoj-server bootstrap-key issue --out /etc/syncoj/bootstrap.key`（明文只显示一次） |
| 本机凭据（token） | 注册时换得；之后收代码、收文件、记成绩、收升级都用它 |
| 配对码 | 六位数字，30 分钟有效，一次性；不是凭据，配对后不再使用 |
| 配对对象 | 名单条目（人）；之后按 `machine_uuid` 认机器 |
| 无需重配 | 换场次、系统重装、快照还原、升级 Agent |
| 需要重配 | 换人（改派 / 解绑）、删除名单条目 |

→ 见 [`design/04-server.md §4.6`](design/04-server.md)、[ADR-0002](decisions/ADR-0002-communication-and-identity.md)。

### 机器状态

| 状态 | 机器上的表现 | 处理 |
|---|---|---|
| 未配对 | 桌面出现「配对码.txt」 | 教师在「机器配对」认领 |
| 已配对、无场次 | 桌面出现「等待场次.txt」 | 把该名单应用到场次 |
| 正常 | 两个文件被删除，开始收代码 | — |

查看配对码：

```bash
python3 /opt/syncoj/current/run_agent.py --config /etc/syncoj/agent.ini --pair-code
```

### 快照还原与克隆

| 情况 | 服务端行为 |
|---|---|
| 状态目录被还原、`machine_uuid` 仍在 | 按 `machine_uuid` 认回，保留配对 |
| `machine_uuid` 也没了、原机当前离线 | 按硬件指纹（SMBIOS UUID）认回，保留配对 |
| 同指纹、原机仍在心跳 | 进待配对，「机器配对」页显示克隆告警 |

## 一键安装（四种入口，共用同一份逻辑）

```bash
# 1. 从服务端直接装（空机器 / 小镜像）
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

# 3. 在线自举（空机器上的第一条命令；--server 省略时用发脚本的那台服务端）
curl -fsSL http://10.0.0.1:8000/api/v1/agent/install/bootstrap.sh | sudo sh -s -- \
    --server http://10.0.0.1:8000 --bootstrap-key <签发出的密钥> --user student

# 4. 任意 URL 下载
sudo python3 install.py \
    --download-url http://10.0.0.1/x/syncoj-agent-bundle.tar.gz \
    --sha256 <从服务端界面抄下来的校验和> \
    --server http://10.0.0.1:8000 --user student

# 预览（不需要 root，不改动、不联网）
python3 install.py --from-server --server http://x --dry-run
```

### `install.py` 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--bundle` / `--download-url` / `--from-server` / `--from-dir` | — | 安装包来源：本地 tar.gz / URL / 服务端当前已铺开版本 / 已解开的目录 |
| `--sha256` | 空 | 安装包校验和 |
| `--server` | 包内 `server.json`，否则局域网发现 | 显式给出则只用它 |
| `--no-discover` | 关 | 不做局域网发现 |
| `--discover-address` | 广播 | 改为单播询问该地址 |
| `--discover-timeout` | `1.5` 秒 | 等发现应答的时间 |
| `--bootstrap-key` / `--bootstrap-key-file` | — | 统一注册密钥；优先级见下表 |
| `--user` | `SUDO_USER`，否则当前用户 | 运行账号；以 root 直接运行时必须显式给 |
| `--scan-root` | `{desktop}/{player_no}` | 扫描目录，逗号分隔多个 |
| `--scan-prefix` | `none` | 上报路径前缀：`none` / `auto` / 字面量 |
| `--deploy-root` | `{desktop}` | 下发落地根目录 |
| `--ca-file` / `--insecure` | 空 / 关 | 自签 CA；`--insecure` 不校验证书（凭据可被中间人读走） |
| `--force-config` | 关 | 覆盖已有 `agent.ini`（会丢失手工修改） |
| `--upgrade-mode` | 包内策略，否则 `apply` | `off` / `stage` / `apply` |
| `--public-key` | — | 发布签名公钥路径；给了才验签 |
| `--prefix` / `--config-dir` / `--state-dir` / `--unit-dir` | `/opt/syncoj` / `/etc/syncoj` / `/var/lib/syncoj` / `/etc/systemd/system` | 布局 |
| `--dry-run` / `--quiet` / `--skip-user` / `--skip-service` / `--skip-python-check` | 关 | 行为开关 |
| `--uninstall` / `--yes` / `--keep-user` / `--keep-state` | 关 | 卸载，见「卸载 Agent」 |

统一注册密钥来源优先级：`--bootstrap-key` > `--bootstrap-key-file` > `<仓库>/.key/bootstrap.key`（存在才用）> 机器上已有 > 包内附带。重复安装时省略 `--bootstrap-key` 不影响已有密钥与注册单元。

`bootstrap.sh` 认识的参数：`--server`、`--bootstrap-key`、`--scan-root`、`--deploy-root`、`--sha256`、`--uninstall`、`--yes`、`--dry-run`、`--keep-state`、`--keep-user`；其余原样透传给 `install.py`。

### 从服务端直接装的校验

装机端点 `/api/v1/agent/install/*` 不鉴权，只发**已铺开**的版本 → 见 [`design/07-ops-and-install.md`](design/07-ops-and-install.md)。

| 机器上有发布公钥 | 校验内容 |
|---|---|
| 有（`--public-key`，或已装好的机器） | sha256 + 签名 |
| 无 | 仅 sha256（对照服务端台账）；输出"未验签"与校验和，需人工对照界面 |

带验签自举：

```bash
curl -fsSL http://10.0.0.1:8000/api/v1/agent/install/bootstrap.sh \
  | sudo RELEASE_PUBLIC_KEY=/path/to/release-key.pub.json sh -s -- \
    --server http://10.0.0.1:8000 --bootstrap-key <签发出的密钥> --user student
```

⚠️ 仅 sha256 时挡不住恶意服务端。

### 建镜像

⚠️ 母机上跑过 Agent 再做镜像，所有克隆机共用同一身份（`credential.json` + `machine_uuid`），成绩会对错人。

1. 装 Agent。不要启动服务、不要跑 `--once`、不要跑 `--provision`。
2. 母机已跑过时，删除状态目录里的 `credential.json` 与 `machine_uuid`。
3. 做镜像。
4. 部署到各机器。
5. 首次开机，`syncoj-agent-enroll.service` 为每台生成各自的凭据与 `machine_uuid`。

## 注册单元与服务

| 单元 | 身份 | 作用 |
|---|---|---|
| `syncoj-agent-enroll.service` | root，oneshot；安装时 enable 并立即运行一次 | 运行 `run_agent.py --provision`：读 `/etc/syncoj/bootstrap.key` 换本机凭据，写入状态目录并 chown 给运行账号；失败最多重试 5 次、间隔 30 秒 |
| `syncoj-agent.service` | 运行账号 | 只读凭据；无凭据时等待，不读密钥 |
| `syncoj-agent-admin.path` / `.service` | root | 处理升级切换与远程卸载请求 |

→ 见 [ADR-0009](decisions/ADR-0009-registration-is-a-root-unit.md)。

### 机器不出现 / 服务反复重启

1. 查看状态与日志：

   ```bash
   systemctl status syncoj-agent-enroll.service --no-pager -l
   journalctl -u syncoj-agent-enroll.service -n 30 --no-pager
   journalctl -u syncoj-agent -n 30 --no-pager
   sudo tail -n 40 /var/lib/syncoj/agent.log
   ```

2. 按下表修复根因。
3. 出现 `Start request repeated too quickly` 时清限流状态：

   ```bash
   sudo systemctl reset-failed syncoj-agent
   sudo systemctl start syncoj-agent-enroll.service
   sudo systemctl start syncoj-agent
   ```

| 日志 | 原因 | 处理 |
|---|---|---|
| `HTTP 403 bootstrap_key_revoked` / `bootstrap_key_invalid` | 服务端不认机器上的密钥（已吊销 / 换库 / 换服务器） | 「机器配对 → 装机设置」重新签发 → 覆盖 `/etc/syncoj/bootstrap.key`（0600、属主 root；安装器默认不替换已有密钥）→ 重跑注册单元 |
| `HTTP 403 pairing_required` | 凭据有效，未绑人 | 「机器配对」认领 |
| `Permission denied: '/etc/syncoj/agent.ini'` | 配置属主不是运行账号 | 重跑安装器（会改属主）→ 清限流状态 |
| `注册被限速（… 秒后可重试）` | 多台同时注册触发服务端限速 | 等待提示的秒数 |
| `扫描目录的父目录不存在: /root/...` | 旧版注册单元按 root 的家目录展开路径 | 升级；临时：`sudo HOME=/home/<运行账号> … run_agent.py --provision` |
| 服务在跑、日志安静、服务端无 tick | 无凭据，等待注册 | 查 `syncoj-agent-enroll.service` |

## 运行身份

Agent 以 `--user` 指定的选手登录账号运行 → 见 [`design/03-agent.md`](design/03-agent.md)。

| 单元设置 | 值 |
|---|---|
| `ProtectHome` | 不设 |
| `ReadWritePaths=` | `-/home/<运行账号> <状态目录> …`，每条带 `-`，家目录安装时解析为绝对路径 → 见 [ADR-0007](decisions/ADR-0007-unit-path-whitelist.md) |
| `ProtectSystem` | `strict` |
| `agent.ini`、状态目录文件 | 属主运行账号，0600；安装器检查并告警 |

## 目录约定

```
桌面/                          ← deploy_root = {desktop}
├── 题面.pdf                    ← 目标目录留空 = 落桌面
└── <准考证号>/                 ← scan.roots = {desktop}/{player_no}
    ├── p1/p1.cpp              ← 按题下发目标目录填 {player_no}/<题目名>
    └── p2/p2.cpp
```

改路径后重启服务，无需重装：

```bash
sudo systemctl restart syncoj-agent
journalctl -u syncoj-agent -n 30     # 第一行打印实际扫描目录
```

占位符、`{desktop}` 探测与 `agent.ini` 写法 → 见 [`design/04-server.md §4.5`](design/04-server.md)、[ADR-0010](decisions/ADR-0010-run-user-owns-path-templates.md)。

### 文件下发

| 下发内容 | 目标目录 | 落点 |
|---|---|---|
| 题面、样例、须知 | 留空 | `桌面/题面.zip` |
| 某题附件、额外数据、模板 | `{player_no}/<题目名>` | `桌面/<准考证号>/<题目名>/数据.zip` |
| 考场公告 | 任意 | 同上，并显示在选手页「考场公告」 |

| 操作 | 结果 |
|---|---|
| 「实际落点预览」 | 用第一位选手代入模板 |
| 「写考场公告」→ 下发 | 生成 `NOTICE.md`；文件名大小写不敏感；多份取最后下发的；不下发则无公告 |
| 「编辑」（`.txt` / `.md` 等，≤ 1 MB） | 已下发的副本重新排队，下一轮心跳生效；⚠️ 考试中修改会替换机器上的测试数据 |
| 场次填「结束时间」 | 到点自动置为「已结束」，之后拒收上传；开考不自动触发；留空不自动结束 |
| 下发 zip | 原样落盘，不解压；密码用 `password.txt` 作为普通资产一起发 |

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
/etc/syncoj/bootstrap.key        统一注册密钥
/etc/syncoj/release-key.pub.json 升级信任锚
/var/lib/syncoj/                 凭据、哈希缓存、日志、未完成的下载
/usr/lib/syncoj/                 root 管理单元代码
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
python3 build_bundle.py                                             # 产出 dist/syncoj-agent-<版本>.tar.gz
python3 build_bundle.py --verify-signature /etc/syncoj/release-key.pem   # 另产出 .sig
```

| 参数 | 说明 |
|---|---|
| `--out` | 输出路径 |
| `--public-key` | 打进包的公钥；默认 `.key/release-key.pub.json`（存在才用） |
| `--no-public-key` | 不打公钥 |
| `--server-url` | 写入包内 `server.json` |
| `--print-sha256-only` | 只打印 sha256 |

同样内容产出逐字节相同的包。

## 发版时附带统一注册密钥（可选）

「Agent 发布」→ 构建时勾「附带统一注册密钥」，默认不勾：

| 效果 | 说明 |
|---|---|
| 构建时现场签发新密钥 | 明文放进包根 `bootstrap.key`（0600），库里只存哈希，版本记录上可见、可就地吊销（只影响未注册机器） |
| 装机入口拒发 | `/api/v1/agent/install/*` 返回 409 `install_bundle_has_key`；只能登录管理端下载做离线包 / 镜像 |

⚠️ 附带密钥的包可注册进本服务端；离线包发完即吊销该密钥。在线装机用不带密钥的版本，密钥由 `bootstrap.sh --bootstrap-key …` 给出。

## 发布时的「高级选项」

随包下发，写进装机台账与升级清单：

| 选项 | 默认 | 取值 |
|---|---|---|
| 覆盖机器上已有的注册密钥 | 勾「附带密钥」时覆盖 | 旧密钥备份为 `bootstrap.key.replaced-<时间戳>`，回执写明路径 |
| 升级模式 | `apply` | `apply`：下载→验签→切换→自动重启；`stage`：只下载验签；`off`：不升级 |
| 更新机器上的 `agent.ini` | 全部「不改」 | 逐键：`不改` / `仅更新默认配置`（缺该键或值仍是上次安装器写的）/ `强制覆盖`；状态与凭据文件不动 |

| 边界 | 处理 |
|---|---|
| 自升级只换程序，不改配置与注册密钥 | 改密钥 / 配置需重跑安装器 |
| 镜像无发布公钥 | 默认 `apply` 降级为 `off` 并报出；显式 `apply` 无公钥报错 → 把 `.key/release-key.pub.json` 打进镜像 |

## 重装与卸载

重装 = 再跑一次安装器（保留 `agent.ini`、凭据、配对、已有密钥；版本变化才重启服务）：

```bash
curl -fsSL http://<服务端>/api/v1/agent/install/bootstrap.sh \
  | sudo sh -s -- --server http://<服务端> --user <运行账号>
```

| 参数 | 说明 |
|---|---|
| `--user` | 必须显式给；`sudo` 下默认 `SUDO_USER`，常为管理员而非选手 |
| `--force-config` | 按新默认重建 `agent.ini`；⚠️ 丢失教师改过的扫描目录 |

### 协议不兼容升级后全部重装

服务端协议变更（如 tick 新增 `scanned_for`、`scan_complete` 必填、下发进度改为 `deploy_progress` / `deploy_done` / `deploy_failed` 按 `target_id`）后，旧 Agent 每次 tick 返回 422，不收代码、不收下发、收不到升级 → 见 [`design/08-milestones-and-open.md`](design/08-milestones-and-open.md) §10.5。

1. 「Agent 发布」构建并铺开新版本。
2. 每台考试机运行上面的重装命令。
3. 重做镜像：替换镜像里的旧离线包（重新打包或从「Agent 发布」下载）；已固化 Agent 的镜像 / 还原卡母机重装后重新做镜像（先按「建镜像」删除身份文件）。
4. 「机器配对」核对各机 Agent 版本与最近心跳。

## 卸载 Agent

```bash
# 机器上一条命令（装机页有复制按钮；服务端地址与安装器 sha256 已嵌入脚本）
curl -fsSL http://<服务端>/api/v1/agent/install/bootstrap.sh \
  | sudo sh -s -- --uninstall --yes

# 已有 install.py 时
sudo python3 install.py --uninstall --dry-run   # 只列出要删的内容
sudo python3 install.py --uninstall --yes       # 执行卸载
```

| 参数 | 说明 |
|---|---|
| `--yes` | 非交互时必需；交互时改为输入「syncoj-agent」确认 |
| `--dry-run` | 预览，无需 `--yes` |
| `--keep-state` | 保留状态目录与日志 |
| `--keep-user` | 保留安装器用 `--user` 新建的系统账号 |

⚠️ 会删除 `/etc/syncoj/bootstrap.key`、`release-key.pub.json`、状态目录里的本机身份与 token。不删除：选手桌面上的下发文件、`/tmp` 临时目录、运行账号及其家目录。

离线包不含安装器；服务端已下线时手工卸载：

```bash
sudo systemctl disable --now syncoj-agent syncoj-agent-enroll
sudo rm -f /etc/systemd/system/syncoj-agent.service \
           /etc/systemd/system/syncoj-agent-enroll.service
sudo systemctl daemon-reload
sudo systemctl disable --now syncoj-agent-admin.path
sudo rm -f /etc/systemd/system/syncoj-agent-admin.path /etc/systemd/system/syncoj-agent-admin.service
sudo rm -rf /usr/lib/syncoj                     # root 管理单元代码
sudo rm -f /etc/sudoers.d/syncoj-uninstall      # 旧版本的 sudoers 规则
sudo rm -rf /usr/local/lib/syncoj               # 旧版本的远程卸载脚本
sudo rm -rf /opt/syncoj /etc/syncoj             # 程序、配置、统一注册密钥、升级信任锚
sudo rm -rf /var/lib/syncoj                     # ⚠️ 凭据与日志，确认无需留档后再删
```

不要删除运行账号（默认即选手登录账号）。

## 排障

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
| 经 `run_agent.py` 启动，不直接运行 `syncoj_agent/main.py` | `ImportError: attempted relative import with no known parent package` |
| 手工调试带 `-E -s`，与 systemd 用同一条命令 | 行为与线上不一致（`PYTHON*` 环境变量、user site-packages 生效） |

服务端侧排查与更多现象 → 见 [`operate.md`](operate.md)「心跳停了怎么排查」。
