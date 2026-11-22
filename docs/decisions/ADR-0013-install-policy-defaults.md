# ADR-0013：安装策略三条的默认值

**状态**：已锁定（v1，真机故障后补上）

## 背景

- 发版时须决定**机器上已有内容是否随之改动**；不同机器要求不同（"包里新钥须覆盖已吊销的旧钥" vs "教师手工配过，保持不动"）。
- 策略位于「Agent 发布」→「发布当前版本」卡片的「高级选项」，**随包（及装机台账、升级清单）带下去**，字段名冻结；出现在三处（装机台账、升级清单、离线包根目录 `install_policy.json`），须**单一出口**生成，防止装机与升级策略不一致。
- 真机故障：机器上留有**已吊销的旧钥**，包内新钥用不上，机器无法注册。

## 决定

| 字段 | 取值 | 默认 | 管什么 |
|---|---|---|---|
| `bootstrap_key_policy` | `keep` / `replace` | **随包内有没有密钥**：带 → `replace`，不带 → 忽略 | 机器上已有的统一注册密钥是否被包内那把覆盖 |
| `config_policy` | `{"<section>.<key>": "keep"｜"default"｜"force"}` | 全部 `keep` | 机器上 `agent.ini` 的**逐键**更新策略 |
| `upgrade_mode` | `apply` / `stage` / `off` | `apply` | 拿到新版本后：自动切换重启 / 只下载验签 / 不动 |

- **`bootstrap_key_policy` 带密钥时默认 `replace`**。替换前先备份旧钥（`bootstrap.key.replaced-<时间戳>`，0600 root），回执写明备份路径与旧钥指纹；显式 `--bootstrap-key` 永远最优先。
- **`config_policy` 默认全 `keep`**：扫描根、下发根、服务端地址都是现场数据。三态语义、快照文件与作用域见 [`../design/07-ops-and-install.md §7.2.1`](../design/07-ops-and-install.md)。
- **`upgrade_mode` 默认 `apply`**（产品决定）。例外：**镜像/包里无发布公钥时降级为 `off` 并提示**（否则 `validate()` 报错、服务起不来、整批机器失联）；显式写 `apply` 而无公钥是**硬错误**。
- **优先级**：显式 CLI 参数 > 服务端台账 > 包内 `install_policy.json` > 模板默认（台账反映服务端当前状态，包可能是早先打的）。
- **生效时机**：装机，以及经 root 管理单元的升级切换（`--admin-handoff` 走安装器同一个 `run()`，读暂存包内的 `install_policy.json`，不带台账；见 [ADR-0012](ADR-0012-remote-uninstall-authorization.md)）。Agent 进程本身不改 `agent.ini`、不碰注册密钥。

## 后果

- `config_policy` 的键有**规范清单** `CONFIG_POLICY_KEYS`，须与 `agent/config.example.ini` 逐键一致（多一个：界面可配而机器不变；少一个：该键无法逐键覆盖且无提示）。逐键说明见 [`../reference/config-keys.md`](../reference/config-keys.md)。
- `apply` 的前提：Agent 装在"版本化目录 + `current` 软链"布局下。

## 被否掉的替代方案

* **`bootstrap_key_policy` 默认 `keep`**：即该真机故障的成因。
* **`upgrade_mode` 默认 `off`**：铺开后无人逐台升级，自更新形同虚设。
* **无公钥时对显式 `apply` 静默降级**：配置错误应报错。
* **Agent 进程（运行账号）升级时自己改配置**：运行账号改不了 root 只读的密钥，且影响面过大；改配置走 root 安装器路径。
