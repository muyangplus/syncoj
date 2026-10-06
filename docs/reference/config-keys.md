# `agent.ini` 逐键说明

**权威来源是 [`../../agent/config.example.ini`](../../agent/config.example.ini)** ——
这一页是对它的逐键说明，不是另一份定义。示例文件里的注释写得比这张表详细得多，
尤其是"为什么"；这里只是把它整理成一份**可以被机器对账**的清单。

**这张表的键清单与 `server/syncoj_server/services/install_policy.py::CONFIG_POLICY_KEYS`
逐键相同**，两边都由 `server/tests/test_docs.py` 守着（多一个、少一个、改个名字都会红）。
那一份清单又已经被 `test_install_policy.py` 拿去和示例文件对账，所以三方是一致的：

```
agent/config.example.ini  ⇄  CONFIG_POLICY_KEYS  ⇄  本页
                    （test_install_policy）      （test_docs）
```

为什么值得这样对账：这些键就是发布时「高级选项」里那份**逐键策略**能覆盖的全部范围。
清单里多一个，教师能配一条**安装器不会写**的键 —— 界面显示配好了、机器上什么都没发生；
少一个，某个键永远没法被逐键覆盖，而没有任何地方会提示这件事。

## 逐键

| 键 | 示例里的默认值 | 说明 |
|---|---|---|
| `server.url` | `https://10.0.0.1:8443` | 服务端地址。生产环境务必用 https，并配好 `ca_file` |
| `server.verify_tls` | `true` | 校验服务端证书。设为 `false` 意味着中间人可以直接读走本机凭据，**仅用于调试** |
| `server.ca_file` | `/etc/syncoj/ca.pem` | 自签 CA 证书路径（用自签证书时必填） |
| `agent.bootstrap_key_file` | `/etc/syncoj/bootstrap.key` | 统一注册密钥文件（整间机房一份），**root 只读、0600**。密钥的**内容**绝不要写进本文件 |
| `agent.run_user` | 空 | **运行账号**：`{home}` / `{desktop}` 只按它解析，与"当前是谁在跑这个进程"无关。安装器会把跑安装的那个账号写进来；留空退回当前用户 |
| `agent.state_dir` | `/var/lib/syncoj` | 凭据、哈希缓存、日志、未完成的分片 |
| `agent.machine_id` | 空 | 本机标识。留空则自动读 `/etc/machine-id`（推荐留空）。**不参与身份判定**，只供人工辨认 |
| `agent.deploy_root` | `{desktop}` | 下发文件的落地根目录，服务端指定的是相对它的路径 |
| `pairing.show_on_desktop` | `true` | 把状态写到桌面上给教师看（配对码 / 等待场次的原因）。关掉不等于不工作，日志里还有一份 |
| `pairing.file_name` | `配对码.txt` | 桌面上那个文件的名字 |
| `scan.roots` | `{desktop}/{player_no}` | 要回收的代码目录，绝对路径，多个用换行或逗号分隔 |
| `scan.prefix` | `none` | 上报路径的前缀：`none` / `auto`（取根目录名）/ 字面量。**改了它就要跟着改题目的「代码路径」** |
| `scan.interval` | `60` | 扫描间隔下限（秒）。实际节奏由服务端 tick 响应动态决定 |
| `scan.max_file_size` | `2097152` | 单文件大小上限（字节）。超过则跳过并计入审计事件 |
| `log.level` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `log.file` | 空 | 日志文件路径。留空则写到 `<state_dir>/agent.log`，自动轮转（4MB × 3） |
| `log.to_stderr` | `false` | 是否同时输出到 stderr（systemd 会收集进 journal） |
| `upgrade.mode` | `apply` | 自更新模式：`apply`（下载→验签→原子切换并重启）/ `stage`（只下载验签、不激活）/ `off`（只上报有新版本） |
| `upgrade.install_root` | `/opt/syncoj` | 安装根目录（installer 创建；手工部署时可留空） |
| `upgrade.public_key` | 空 | 发布签名公钥。**留空则任何升级都会被拒绝** —— 没有信任锚的签名毫无意义 |

## 路径模板里的占位符

| 占位符 | 何时展开 | 展开成 |
|---|---|---|
| `{desktop}` | **载入配置时** | **运行账号**的桌面（自动探测，兼容「桌面」与 `Desktop`） |
| `{home}` | **载入配置时** | **运行账号**的家目录 |
| `{player_no}` | **注册后** | 准考证号 |
| `{contest_slug}` | **注册后** | 场次标识 |

后两个在注册之前**原样保留、不报错**：配置校验发生在注册之前，把"还没注册"说成
"配置错了"只会让人白折腾。注册完成后 Agent 会在启动日志里打出最终展开的目录。

`{desktop}` / `{home}` 恒按**运行账号**展开的理由，以及"注册单元以 root 跑会把它们
算成 `/root/桌面`"那次真机事故，见
[`../decisions/ADR-0010-run-user-owns-path-templates.md`](../decisions/ADR-0010-run-user-owns-path-templates.md)。

## 安装策略怎么管这些键

发布时的「高级选项」里有一份**逐键三态**策略，键名就是上面这张表里的
`<段>.<键>`（`config_policy`）：

* `keep`（**默认**）—— 一个字节都不动，连缺失的键也不补；
* `default` —— 只更新"没人动过"的键（机器上缺这个键，或它的值等于安装器上次写下的值）；
* `force` —— 无条件写成新值。

状态与凭据文件（`credential.json` / `machine_uuid` / `machine_id` / 缓存）
**永不在作用域内**，也不给它们留策略位。默认值为什么全是 `keep`、以及
`upgrade.mode` 默认 `apply` 但缺信任锚时降级 `off` 的理由，见
[`../decisions/ADR-0013-install-policy-defaults.md`](../decisions/ADR-0013-install-policy-defaults.md)。
