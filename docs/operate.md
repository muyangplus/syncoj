# 服务端运维

## 部署

装机入口（离线包 / 在线自举 / 上传安装包等）见 [`install-agent.md`](install-agent.md)
的「一键安装（四种入口，共用同一份逻辑）」。服务端发布 Agent 版本的流程见本文
「发一个 Agent 版本」。

`install.py` 不接受「每选手注册码」：那条链路已经删除，只有统一密钥一条路。

## 密钥从哪来：`.key/`

为了试一次升级，原本要走一遍"手工 genkey → 手工指私钥 → 手工把公钥拷进包"；
`syncoj-server init` 会把两把密钥都建好：

```
.key/                         ← 默认密钥目录（整个目录不进版本库，两道 gitignore 挡着）
├── release-key.pem           发布签名私钥  0600
├── release-key.pub.json      对应公钥（打包 Agent 时自动内嵌）
└── bootstrap.key             统一注册密钥  0600（哈希同时写进库，开箱即可装机）
```

| 环节 | 行为 |
|---|---|
| `syncoj-server init` | 缺哪把建哪把；**已有的绝不覆盖、绝不轮换**，并把建了什么/找到了什么打出来 |
| 服务端用私钥 | `.key/release-key.pem` 在就用它；`SYNCOJ_RELEASE_KEY` 覆盖一切 |
| `build_bundle.py` | 默认取 `.key/release-key.pub.json`（存在才用）打进包；显式 `--public-key` 给错路径会**报错退出**，不会静默打出个不能升级的包 |
| `install.py` | 统一密钥的优先级：`--bootstrap-key`（明文）> `--bootstrap-key-file` > `.key/bootstrap.key`（存在才用）> 没有。包里的公钥会落到装机器上的配置目录，`agent.ini` 指向那份副本 |

三条默认值**都是"有则生效、无则保持原样"**：没有私钥就照旧不提供任何升级，
没有公钥就打出和以前一样的包，没有 `.key/` 目录就什么都不发生。生产部署请用
`SYNCOJ_KEY_DIR` / `SYNCOJ_RELEASE_KEY` 指到只有服务端账号读得到的目录。

> ℹ️ `db reset` 只删库、不碰 `.key/`，所以"密钥文件还在、库里的哈希没了"是会出现的
> 一态。`init` 会把它**补登记**回去；但**已经被吊销的密钥不会被复活** —— 吊销是
> 有意动作，不该被一次 init 撤销。

> ⚠️ 私钥放在仓库相对路径上是一个明确的取舍：任何拿到这份 checkout 的人都能签出
> 考试机会接受的升级包。`.key/` 由目录自带的 `.gitignore` 与根 `.gitignore`
> **两道**挡着，私钥的安全不依赖单个可被删掉的文件。

## 机器怎么知道服务端在哪

**装 50 台机器不用输地址。** 地址有两个入口，主次分明：

| 层 | 什么时候用 | 凭什么可信 |
|---|---|---|
| **包内嵌地址**（主路径） | 包是这台服务端自己打的 | `build_bundle.py --server-url` 把它写进包内 `server.json`，装机时一个字不用输 |
| **UDP 广播发现**（兜底） | 包是别处打的、或者服务端换了 IP（DHCP 很常见） | 应答用**发布签名私钥**签「随机数 + 地址」，机器用包里内嵌的公钥验签 |

```bash
# 打包时把地址一起带上（界面上的「发布当前版本」会自动做这件事）
python3 agent/packaging/build_bundle.py --server-url http://10.0.0.5:8000

# 装机：什么都不用传；要覆盖就显式给
sudo python3 install.py --bundle ./x.tar.gz --sha256 <校验和>              # 用包里的
sudo python3 install.py --bundle ./x.tar.gz --server http://1.2.3.4:8000  # 覆盖
sudo python3 install.py --bundle ./x.tar.gz --no-discover                 # 不许广播
```

**发现必须验签。** 机器"找到服务端"之后第一件事是把统一注册密钥发过去；应答不验签
时，局域网内任何应答者都能取到这把能注册整间机房的密钥。密钥是 50 台机器共享的，
拿它做 HMAC 也挡不住。没有发布公钥就**不发现**（与"没有密钥就不升级"同一个默认）。

**服务端怎么知道自己的地址：** 优先 `SYNCOJ_PUBLIC_URL` / `serve --public-url`；
没配就从**请求来源反推本机地址**（内核算的，客户端伪造不了）；再退到"教师浏览器
用过的那个非回环 `Host`"。三条都没有就**什么都不做**（不内嵌、不发现），不退回
`127.0.0.1`（那样 50 台机器会各自连自己，现场只看到"注册不上"）。三条来源的排序
理由见 [`design/07-ops-and-install.md §7.3`](design/07-ops-and-install.md)。

**它会把自己的答案直接打出来。** 启动时要么 `对外地址: http://…（显式配置）`，
要么如实说 `对外地址: 还没有`（那一刻确实没有线索）；**第一次从局域网打开管理界面**
时补一行：

```
对外地址: http://10.0.0.5:8000（来源：管理界面访问地址（Host 头，只记成功的请求））
```

来源那一句决定了这个地址有多可信（显式配置 > 请求来源反推 > 教师浏览器用过的那条），
也决定要不要现在就用 `SYNCOJ_PUBLIC_URL` 固定下来。想随时查：`GET /healthz` 里的
`public_url` 就是当前答案（没有时是 `null`，**不会**退回 `127.0.0.1`）。

> UDP `45871` 是个无状态反射点，所以立了一条硬约束：**应答不得大于探测**。探测靠
> `pad` 凑到 640 字节，更短的探测服务端不理、地址太长时宁可**不应答** —— 不然伪造
> 来源地址就能借它放大流量。

## 发一个 Agent 版本

服务端**跑在源码仓库里**时（`.key/` 就是这么找到的），不必先手工打包：管理界面
「Agent 发布」页上直接「发布当前版本」—— 服务端从本机 `agent/` 源码构建、用私钥
签好、进版本历史。

```bash
# 界面上等价的那条命令；产物与界面构建的逐字节相同（可复现打包）
python3 agent/packaging/build_bundle.py --out dist/syncoj-agent-0.1.0.tar.gz
```

三个必须知道的边界：

- **构建 ≠ 铺开。** 构建出来的是草稿，机器看不到它，要再点「铺开」。
- **版本号必须显式提交、且必须与 `agent/syncoj_agent/__init__.py` 里的 `__version__`
  一致。** 不一致时给 409 并同时报出两个版本号。这是为了挡住"改了代码忘了改版本号
  就静默覆盖上一版"——那种情况的现场表现是"发成功了但已升级的机器拒绝升级"。
- **缺私钥或缺公钥都会被拒绝**，不是降级打一个未签名/无信任锚的包。后者更糟：
  界面上一切正常，而所有机器静默不升级。服务端所在机器没有 `agent/` 源码时
  （例如 pip 装出来的生产实例），这一栏会说明原因，仍可走「上传安装包」那条路。

## 命令行等价流程

命令行等价流程（批量部署时更顺手）：

```bash
# 场次与选手
syncoj-server contest create --name "2025 校内模拟赛"
syncoj-server contest import-players --contest 2025 --file roster.csv
syncoj-server contest list

# 统一注册密钥（整间机房一份；明文只显示这一次）
syncoj-server bootstrap-key issue --label "2025 机房镜像" --expires-days 30 \
    --out /srv/syncoj/bootstrap.key      # 同时写成 root 只读的一份
syncoj-server bootstrap-key list
syncoj-server bootstrap-key pending      # 还没配对的机器（含配对码剩余时间）
syncoj-server bootstrap-key revoke --id 1

# 开发阶段库结构还在动，需要重来时
syncoj-server db reset --yes [--keep-admin]
```

名单 CSV 的列顺序是 `选手编号,姓名,座位,分组`（只有编号必填），
逗号或制表符分隔都认，表头与 `#` 注释行会被自动跳过。

测试规模：**服务端 812 项通过**（另 2 项按环境跳过）、**Agent 509 项通过**
（另 16 项按环境跳过）。含端到端集成测试（真实 Agent 代码通过真实 HTTP 打到真实
服务端）、**openssl 交叉验证**（手写密码学代码唯一可信的证据是独立实现能互相
验通，而且要求 openssl 1.1.1 与 3.0 两种默认输出格式都能读）。

## 心跳停了怎么排查

先分清三种"停"：① 真的不再心跳；② 在心跳但被服务端拒了；③ 服务端**故意**让它安静
（场次结束）。三者的证据在不同的地方，按下面顺序走最省时间。

### 第一步：在服务端查

* **机器列表/总览**里那一行的「上次心跳」与「距上次心跳」——超过离线阈值（管理端
  「运行参数」，默认 **90 秒**）才显示离线；
* **事件页**筛 `agent_`：`agent_revoke`（凭据被作废）、`agent_unbind`（被解除配对）、
  `agent_uninstall`（被请求卸载）、`contest_auto_closed`（到点自动结束场次）；
* 服务端日志里搜机器名或 `machine_id`，看有没有：

| 响应 | 含义 | 心跳还在吗 |
|---|---|---|
| `401` | 凭据失效（被作废/换库）——Agent 会清掉本地凭据回到"等注册" | 就此停止 |
| `403 pairing_required` / `contest_player_missing` | 机器还没绑人，或这个场次里没有这个人 | 还在，只是没归属 |
| `409 contest_ended` | 场次时间窗结束 | **设计如此**：安静下来，不再上传 |
| `404` | `agent.ini` 里的地址指错了 | 停止（连错服务端） |

### 第二步：到那台机器上跑这一段

```bash
systemctl status syncoj-agent --no-pager -l | head -20
systemctl status syncoj-agent-enroll.service --no-pager -l | head -20
journalctl -u syncoj-agent -n 50 --no-pager
journalctl -u syncoj-agent-enroll.service -n 50 --no-pager
sudo tail -n 100 /var/lib/syncoj/agent.log        # 两个单元共用这一个日志
sudo ls -l /etc/syncoj/ /var/lib/syncoj/          # 凭据、密钥、配置的属主
df -h /; systemctl show syncoj-agent -p NRestarts -p ExecMainStatus -p User
```

判据表（右列都是在实机上出现过的现象）：

| 现场看到 | 原因 | 修法 |
|---|---|---|
| `226/NAMESPACE`，服务反复起不来 | 单元里 `ReadWritePaths=` 写了不存在的目录 | 升级到每条路径都带 `-` 的版本 |
| `统一密钥文件 … 读不出来` + 服务 exit 1 | 老版本 Agent 自己去读 root 只读的密钥 | 升级：新版**服务不注册、只等待**，注册由 `syncoj-agent-enroll.service` 做 |
| `Start request repeated too quickly` | 上面的失败把 systemd 的重启额度用完了 | 先修真正的原因，再 `systemctl reset-failed syncoj-agent && systemctl start syncoj-agent` |
| 注册单元 `HTTP 403 bootstrap_key_revoked` | 机器上那把统一密钥服务端不认（吊销过/换了库） | 服务端重签发一把 → **覆盖** `/etc/syncoj/bootstrap.key`（0600、属主 root）→ 重跑注册单元 |
| 注册单元 `Permission denied: '/etc/syncoj/agent.ini'` | 旧安装留下的配置属主不是当前运行账号 | 重跑安装器（它会顺手改属主），再清一次限流状态 |
| 服务在跑、日志安静、服务端一个 tick 都没有 | 没有凭据，它正在**等注册**（设计如此） | `systemctl status syncoj-agent-enroll` 看注册那一步 |
| 服务停在 `activating (auto-restart)` | 起不来、或起来就崩 | `journalctl -u syncoj-agent -n 50` |
| 机器时间偏得厉害 | 配对码/令牌时效判断错乱 | 校时（考场机器建议开 NTP） |

**最省事的一条命令**（前台跑一轮，直接看它说什么）：

```bash
sudo -u <运行账号> /usr/bin/python3 -E -s /opt/syncoj/current/run_agent.py \
    --config /etc/syncoj/agent.ini --verbose --once
```

想只验配置对不对，把 `--verbose --once` 换成 `--check`（它会打印**具体哪个键**有问题）。

### 第三步：不是故障的三种安静

* **场次时间窗结束**：Agent 主动安静（409 `contest_ended`），是设计行为，不是故障；
* **离线阈值**：默认 90 秒没有心跳才显示离线，这个值在管理端「运行参数」里改；
* **正在升级**：铺开一版之后它会下载 → 验签 → 切目录 → 重启，`agent.log` 里是
  `upgrade_*` 事件；新版本起不来时启动守卫会自动回滚（回滚点被 `prune` 保护着）；
* **大文件下发中**：有活时服务端把节奏收到 2 秒，此时列表上是"在线"。
