# `agent.ini` 逐键说明

- 权威来源：[`../../agent/config.example.ini`](../../agent/config.example.ini)。
- 本页键清单 = `agent/config.example.ini` = `server/syncoj_server/services/install_policy.py::CONFIG_POLICY_KEYS`。
- 对账测试：`server/tests/test_docs.py`（本页 ⇄ 示例文件、本页 ⇄ `CONFIG_POLICY_KEYS`）、`test_install_policy.py`（示例文件 ⇄ `CONFIG_POLICY_KEYS`）。
- 本页的键 = 发布「高级选项」里逐键策略（`config_policy`）可覆盖的全部键。
- 默认路径：`/etc/syncoj/agent.ini`，属主 = 运行账号，权限 `0600`。

## 逐键

| 键 | 示例里的默认值 | 说明 |
|---|---|---|
| `server.url` | `https://10.0.0.1:8443` | 服务端地址。生产环境用 https，并配 `ca_file` |
| `server.verify_tls` | `true` | 校验服务端证书。`false` 仅用于调试 |
| `server.ca_file` | `/etc/syncoj/ca.pem` | 自签 CA 证书路径；用自签证书时必填 |
| `agent.bootstrap_key_file` | `/etc/syncoj/bootstrap.key` | 统一注册密钥文件（整间机房一份），root 只读、`0600`。密钥内容不写进 `agent.ini` |
| `agent.run_user` | 空 | 运行账号；`{home}` / `{desktop}` 只按它展开。安装器写入执行安装的账号；留空 = 当前用户 |
| `agent.state_dir` | `/var/lib/syncoj` | 状态目录：凭据、哈希缓存、日志、未完成的分片 |
| `agent.machine_id` | 空 | 本机标识；留空读 `/etc/machine-id`（推荐留空）。不参与身份判定，只供人工辨认 |
| `agent.deploy_root` | `{desktop}` | 下发文件落地根目录；服务端下发的 `dest` 相对于它 |
| `pairing.show_on_desktop` | `true` | 把配对码 / 等待场次原因写到桌面。`false` 时只写日志 |
| `pairing.file_name` | `配对码.txt` | 桌面配对码文件名 |
| `scan.roots` | `{desktop}/{player_no}` | 回收的代码目录，绝对路径；多个用换行或逗号分隔 |
| `scan.prefix` | `none` | 上报路径前缀：`none` / `auto`（取根目录名）/ 字面量（可含 `{player_no}` / `{contest_slug}`）。改动后同步修改题目的「代码路径」 |
| `scan.interval` | `30` | 扫描 / 心跳间隔（秒）的本地兜底值；服务端下发的策略与 `next_tick_seconds` 优先 |
| `scan.max_file_size` | `2097152` | 单文件大小上限（字节）；超过则跳过并记审计事件 |
| `log.level` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `log.file` | 空 | 日志文件路径；留空 = `<state_dir>/agent.log`，轮转 4 MB × 3 |
| `log.to_stderr` | `false` | 同时输出到 stderr（由 journal 收集） |
| `upgrade.mode` | `apply` | `apply`（下载 → 验签 → 原子切换并重启）/ `stage`（下载验签、不激活）/ `off`（只上报有新版本） |
| `upgrade.install_root` | `/opt/syncoj` | 安装根目录（安装器创建；手工部署可留空） |
| `upgrade.public_key` | 空 | 发布签名公钥；留空则拒绝一切升级 |

## 路径模板里的占位符

| 占位符 | 何时展开 | 展开成 |
|---|---|---|
| `{desktop}` | 载入配置时 | 运行账号的桌面（自动探测，兼容「桌面」与 `Desktop`） |
| `{home}` | 载入配置时 | 运行账号的家目录 |
| `{player_no}` | 注册后 | 准考证号 |
| `{contest_slug}` | 注册后 | 场次标识 |

- `{player_no}` / `{contest_slug}` 在注册前原样保留，不报错。
- 注册完成后，启动日志打印最终展开的目录。
- `{desktop}` / `{home}` 按运行账号展开 → 见 [`ADR-0010`](../decisions/ADR-0010-run-user-owns-path-templates.md)。

## 环境变量覆盖

| 环境变量 | 覆盖 |
|---|---|
| `SYNCOJ_SERVER_URL` | `server.url` |
| `SYNCOJ_CA_FILE` | `server.ca_file` |
| `SYNCOJ_VERIFY_TLS` | `server.verify_tls` |
| `SYNCOJ_STATE_DIR` | `agent.state_dir` |
| `SYNCOJ_MACHINE_ID` | `agent.machine_id` |
| `SYNCOJ_SCAN_ROOTS` | `scan.roots`（同样按运行账号展开占位符） |
| `SYNCOJ_LOG_STDERR` | `log.to_stderr` |

## 安装策略（`config_policy`）

键名 = 上表的 `<段>.<键>`，每键三态：

| 取值 | 行为 |
|---|---|
| `keep`（默认） | 不改动，缺失的键也不补 |
| `default` | 只更新未被改动过的键（机器上缺这个键，或值等于安装器上次写入的值） |
| `force` | 无条件写成新值 |

- 不在策略范围内：`credential.json`、`machine_uuid`、`machine_id`、缓存等状态与凭据文件。
- `upgrade.mode` 默认 `apply`；安装器在机器上没有发布公钥时写 `off`。
- `upgrade.mode=apply` 要求版本化目录 + `current` 软链布局，且 `upgrade.public_key` 非空，否则升级失败。
- 默认值取舍 → 见 [`ADR-0013`](../decisions/ADR-0013-install-policy-defaults.md)。
