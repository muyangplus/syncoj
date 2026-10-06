# 装 Agent

## 机器注册与配对

**只有一条路**：镜像里放一份统一密钥，机器开机换回本机凭据，教师读机器屏幕上的
六位配对码把它绑到名单里的**人**。

```
装机（root 一次性）        机器首次开机              教师
bootstrap.key  ──换凭据──▶  桌面「配对码.txt」 ──读码/输入──▶ 绑到名单里的某个人
```

| | 说明 |
|---|---|
| 密钥在哪 | `/etc/syncoj/bootstrap.key`（**root 只读**，0600）。`agent.ini` 选手账号可读，密钥绝不能写进去 |
| 签发 | `syncoj-server bootstrap-key issue --out /etc/syncoj/bootstrap.key`（明文只显示一次） |
| 配对码 | **六位数字**，限时 30 分钟，一次性。只在"绑定那一刻"用 |
| 配对之后 | 认机器靠 `machine_uuid`，**不再需要配对码** |
| 配对对象 | 名单条目（**人**），不是某场比赛的选手 —— 所以配对是**永久**的，换场次不用重配 |

**配对一次，之后 N 场比赛零人工**：换场次、系统重装、快照还原、升级 Agent 都不用
重配；换人（改派 / 解绑）与删掉名单条目要重配。各种情况与依据见
[`design/04-server.md §4.6`](design/04-server.md)。

> **配对之后不会再下发任何密钥。** 机器在**注册那一步**就已经拿到了自己的凭据（token），
> 配对只是把这枚凭据对应的机器绑到名单里的某个人；之后收代码、收文件、记成绩、收升级
> 都用它。整条链路只有两样凭据：装机时的**统一注册密钥**和注册换来的**本机 token**。
> **配对码不是凭据**，它是"教师此刻在场"的一次性证明。
>
> 也就是说，管理页「机器配对」上那两样东西回答的是两个不同的问题：
> **统一注册密钥**管"这台机器是不是我们机房的"（装 Agent **之前**烤进镜像，全机房一把），
> **配对码**管"这台机器前面站着的是谁"（服务端发给这台机器、教师读码）。**现场要做的是配对。**

## 注册是谁做的：一个 root 一次性单元，不是 Agent 本体

| | 谁 | 干什么 |
|---|---|---|
| `syncoj-agent-enroll.service` | **root**（oneshot；装机时 enable，并立刻跑一次） | 读 `/etc/syncoj/bootstrap.key`，换回本机凭据，写进状态目录并交给运行账号 |
| `syncoj-agent.service` | **运行账号**（= 跑安装的那个账号） | 只读凭据干活；**没有凭据就等**，绝不自己去读那把密钥 |

密钥是 0600、root 只读，服务以选手身份跑**读不到**（设计如此）。所以"服务自己注册"
必然失败，表现是"机器永远不出现"：服务退出 1、重启 5 次后被 systemd 判成"反复起不来"
而罢手，而注册单元从来没被跑过。

现场遇到"机器不出现"或服务反复重启，按顺序看这四样：

```bash
systemctl status syncoj-agent-enroll.service --no-pager -l    # 注册那一步成没成
journalctl -u syncoj-agent-enroll.service -n 30 --no-pager
journalctl -u syncoj-agent -n 30 --no-pager                   # 服务本体
sudo tail -n 40 /var/lib/syncoj/agent.log                     # 两者共用同一个日志文件
```

`Start request repeated too quickly` 说明 systemd 已经罢手 —— 先修掉真正的错误，再清限流状态：

```bash
sudo systemctl reset-failed syncoj-agent
sudo systemctl start syncoj-agent-enroll.service    # 需要重新注册时；已有凭据则它直接返回
sudo systemctl start syncoj-agent
```

注册失败的常见几种（都在 `journalctl -u syncoj-agent-enroll.service` 里看得到原文）：

| 日志里的话 | 含义 | 怎么办 |
|---|---|---|
| `HTTP 403 bootstrap_key_revoked` / `bootstrap_key_invalid` | **机器上那把密钥服务端不认**（吊销过、或换了库/服务器） | 在服务端「机器配对 → 装机设置」重新签发一把，放到 `/etc/syncoj/bootstrap.key`（0600 属主 root，**显式覆盖**旧的那把 —— 安装器默认不替换机器上已有的密钥），再重跑注册单元 |
| `HTTP 403 pairing_required` 之类 | 凭据有效、但还没绑到人 | 教师到「机器配对」页认领 |
| `Permission denied: '/etc/syncoj/agent.ini'` | 旧安装留下的配置属主不是当前运行账号 | 重跑一次安装器（它会顺手把属主改成运行账号），再按上面清一次限流状态 |
| `注册被限速（… 秒后可重试）` | 装机时多台机器同时注册，把服务端限速顶满 | 等它说的秒数；新版注册单元会自己有限重试 |
| `扫描目录的父目录不存在: /root/...` | 注册单元以 root 跑、却按 root 的家展开路径（老版本的 bug） | 升级到修好的版本；临时可 `sudo HOME=/home/<运行账号> … run_agent.py --provision` 跑一次 |

**配对之后不等于马上能干活。** 场次有没有这个人，取决于那份名单有没有被应用进
场次，所以客户端的凭据有三个状态：

| 状态 | 机器上的表现 | 原因 |
|---|---|---|
| 未配对 | 桌面出现「配对码.txt」 | 还没人认领它 |
| 已配对、无场次 | 桌面出现「等待场次.txt」 | 应用了名单才会有这个 `player_no` 的选手 |
| 正常 | 两个文件都被删掉，开始收代码 | —— |

**快照还原能自愈**：机器上的凭据会随还原消失，所以注册时还会带硬件指纹
（SMBIOS UUID，还原后不变）。服务端按 `machine_uuid`（首选）或指纹（UUID 也没了
时）认回原来那台机器、**保留配对关系**，不需要教师重新配对一次。但指纹那条只在
**那台机器当前离线**时才自动认回 —— 指纹只证明硬件相同，母机还在心跳时来了一台
同指纹的机器，那更可能是克隆，认回就等于把某个学生的身份白送出去。这种情况会
进待配对并留下告警（「机器配对」页的克隆告警）。

## 部署与安装

## 注册方式：只有一种

注册模型见本文开头「机器注册与配对」。这里只记一条升级兼容规则：「每选手注册码」
（`--enroll-code`）那条路已经删除，而 `agent.ini` 里若还留着 `enroll_code =`，会被
静默忽略（升级过来的机器不该因为这个起不来）。

## 一键安装（四种入口，共用同一份逻辑）

```bash
# 1. 从服务端直接装（推荐：空机器、整间机房一份小镜像）
#    地址不填也行 —— 那就去局域网里广播着找一次（那时要给 --public-key 才能验应答）
sudo python3 install.py \
    --from-server --server http://10.0.0.1:8000 \
    --bootstrap-key <签发出的密钥> \
    --user student

# 2. 镜像预装（把安装包预先放进镜像）
sudo python3 install.py \
    --bundle ./syncoj-agent-0.1.0.tar.gz \
    --sha256 <校验和> \
    --server http://10.0.0.1:8000 \
    --bootstrap-key <签发出的密钥> \
    --user student

# 3. 在线自举（**空机器上的第一条命令**）
curl -fsSL http://10.0.0.1:8000/api/v1/agent/install/bootstrap.sh | sudo sh -s -- \
    --server http://10.0.0.1:8000 --bootstrap-key <签发出的密钥> --user student

# 4. 通用下载（任意 URL，比如内网静态文件服务器）
sudo python3 install.py \
    --download-url http://10.0.0.1/x/syncoj-agent-bundle.tar.gz \
    --sha256 <从服务端界面抄下来的校验和> \
    --server http://10.0.0.1:8000 --user student

# 先看看会做什么（不需要 root，不做任何改动；预览**不联网**）
python3 install.py --from-server --server http://x --dry-run
```

### 从服务端直接装：凭什么信

`--from-server` 走的是服务端那四个**不鉴权**的装机端点
（`/api/v1/agent/install/*`）。不鉴权是有意的：**初次安装时机器手上什么都没有** ——
没有 Agent、没有凭据，要求鉴权就没有入口。装完之后靠**配对**建立信任：机器注册上来是
"待认领"状态，教师必须在管理界面「机器配对」里把它绑到名单里的某个人，它才开始
收代码、收文件、收成绩。

服务端只发**已铺开**的版本 —— 与升级同一条判据。教师刚构建出来、还没敢铺开的包，
装机入口也拿不到。

安装器把**能验的都验上**，并如实报告做了哪些：

| 机器上有没有发布公钥 | 验到什么程度 |
|---|---|
| 有（`--public-key`，或已装好的机器） | sha256 **+ 签名**。这一步才真正挡住"有人替你换了个包" |
| 没有 | 只验 sha256（对照服务端台账）。**明说未验签**，并打印校验和供人跟界面对一遍 |

想把验签也带上：把 `release-key.pub.json` 放进镜像，用
`--public-key <路径>` 或 `RELEASE_PUBLIC_KEY=<路径>`（自举脚本认这个环境变量）。

> **sha256 挡得住什么、挡不住什么。** 台账与包来自同一个未鉴权的地方，所以它挡的是
> 传输损坏与"传了一半"，**挡不住恶意服务端**。要挡住后者只有验签。

> **重复执行安装器时不必再传一遍密钥。** 它已经在那台机器上了，而"没传
> `--bootstrap-key`"不等于"这台机器没有密钥" —— 安装器会认出现有的密钥文件。
> 误判的后果是上一次装好的开机注册单元被删掉，表现是"什么都没改，但下次开机不再
> 注册了"。

### 装完会发生什么

装好后多了一个单元 `syncoj-agent-enroll.service`：开机时**以 root 身份**跑一次
`run_agent.py --provision`，用 `/etc/syncoj/bootstrap.key` 换回本机凭据，
再 chown 给选手账号。之后 Agent 本体（选手身份）只读凭据，碰不到密钥。

装完的机器有三种状态，桌面上会写出来（见上面「配对之后不等于马上能干活」那张表）。

排障时可以直接看配对码，不用翻日志：

```bash
python3 /opt/syncoj/current/run_agent.py --config /etc/syncoj/agent.ini --pair-code
```

### ⚠️ 建镜像时最容易犯的错

**在母机上装好并跑过一次 Agent，然后把整机做成镜像。**

那样每一台克隆机都带着**同一个身份**开机（同一份 `credential.json`、
同一个 `machine_uuid`），服务端会看到一批同 ID 的机器互相覆盖 ——
而文件是**静默错的**：看起来一切正常，只有成绩会对不上人。

安装器会在状态目录里发现残留身份文件时告警并给出命令，但这条要从流程上避免：

```bash
# 建镜像的正确顺序
1. 装 Agent（此时不要启动服务、不要跑 --once、不要跑 --provision）
2. 做镜像
3. 部署到各机器
4. 首次开机 → syncoj-agent-enroll.service 生成**每台各自的**凭据 + machine_uuid
```

> 如果母机必须在建镜像前先验证一遍能不能装上，请在建镜像**之前**删掉状态目录里的
> `credential.json` 与 `machine_uuid`：在母机上跑过一次之后，这两个文件会被烙进
> 镜像，而每一台克隆机都会带着**同一个身份**开机。删除之后每条机器都会生成
> 自己的 UUID；即使是快照还原把状态目录整个抹掉的场景，服务端也还有硬件指纹那
> 第二道依据可以认回原机器。

服务端侧还有第二道防线：多台机器报同一个硬件指纹时会在「机器配对」页
弹克隆告警。但那是事后发现，不是不犯错的理由。

## 运行身份

**Agent 以选手登录用户的身份运行**（`--user`），不是专用账号。原因：代码和下发
文件都在选手自己的桌面上，跨用户授权在现场很容易装成"服务起来了但什么都不传"。

这决定了 systemd 单元的权限模型：

- **不设 `ProtectHome`** —— 设成 `read-only` 会把家目录整个变只读，Agent 一个
  文件都写不出去
- `ReadWritePaths=%h <状态目录>` —— `%h` 由 systemd 展开成 `User=` 的家目录，
  正好覆盖桌面
- `ProtectSystem=strict` 保留：`/usr`、`/etc`、`/boot` 等系统目录仍然全部只读
- `agent.ini` 与状态目录里的文件都 **chown 给这个用户**（0600）。
  漏掉 chown 的表现是"装完起不来"或"一直重新注册"，而且报的是权限错误，
  很难联想到属主问题 —— 所以安装器现在会检查并告警

## 目录约定

默认落点：

```
桌面/                          ← deploy_root = {desktop}
├── 题面.pdf                    ← 通用资料：目标目录留空 = 直接落桌面
└── <准考证号>/                 ← scan.roots = {desktop}/{player_no}
    ├── p1/p1.cpp              ← 选手写代码的位置（按题下发目标目录填 {player_no}/<题目名>）
    └── p2/p2.cpp
```

改完路径不用重装，重启服务即可：

```bash
sudo systemctl restart syncoj-agent
journalctl -u syncoj-agent -n 30     # 第一行就会打印实际扫描目录
```

`{desktop}` 的探测方式、`{desktop}` / `{home}` 按运行账号解析的规则、四个占位符与
`agent.ini` 写法，见 [`design/04-server.md §4.5`](design/04-server.md)。

## 安装后的布局

```
/opt/syncoj/
├── releases/
│   ├── 0.1.0/                   每个版本一份，互不覆盖
│   │   ├── run_agent.py         ← systemd 的 ExecStart 指向它
│   │   └── syncoj_agent/...
│   └── 0.1.1/...
└── current -> releases/0.1.1    原子切换的软链

/etc/syncoj/agent.ini            配置（已存在时**不覆盖**，见下）
/var/lib/syncoj/                 凭据、哈希缓存、日志、未完成的下载
```

版本化目录 + `current` 软链的布局是自更新的前提。若不用安装器而手工部署成
单一目录，`upgrade.mode = apply` 会直接失败。

## 幂等性

安装器可以随便重复执行，结果一致。三条关键保证：

1. **绝不覆盖已存在的 `agent.ini`**。教师很可能已经改过扫描目录，
   覆盖是灾难性的。要重建请显式加 `--force-config`。
2. **同版本不重复解压**。已装过的版本直接复用。
3. **已是当前版本则不重启服务**。避免每次跑安装器都打断正在进行的传输。

## 打包

```bash
# 产出 dist/syncoj-agent-<版本>.tar.gz
python3 build_bundle.py

# 再用服务端私钥签一份 .sig（供手工核对）
python3 build_bundle.py --verify-signature /etc/syncoj/release-key.pem
```

打包是**可复现的**：同样的内容产出逐字节相同的压缩包，与输出文件名无关。
gzip 头里默认嵌的时间戳与文件名、tar 成员里的 mtime 都已被压平 —— 否则
"同一版本两次打包校验和不同"，签名的确定性和"这个包是不是那个包"的判断都会失效。

## 卸载

```bash
sudo systemctl disable --now syncoj-agent
sudo rm -f /etc/systemd/system/syncoj-agent.service
sudo systemctl daemon-reload
sudo rm -rf /opt/syncoj /etc/syncoj
# 状态目录里是凭据与日志，确认不需要留档后再删
sudo rm -rf /var/lib/syncoj
sudo userdel syncoj
```

## 排障

```bash
systemctl status syncoj-agent          # 进程是否在跑
journalctl -u syncoj-agent -n 100      # systemd 侧的启动期输出
tail -f /var/lib/syncoj/agent.log      # Agent 自己的日志（自动轮转）
/usr/bin/python3 -E -s /opt/syncoj/current/run_agent.py \
    --config /etc/syncoj/agent.ini --check      # 只校验配置
/usr/bin/python3 -E -s /opt/syncoj/current/run_agent.py \
    --config /etc/syncoj/agent.ini --once       # 只跑一轮，前台看输出
```

> **必须走 `run_agent.py`，不能是 `syncoj_agent/main.py`。**
> 后者使用包内相对导入，当脚本直接执行会报
> `ImportError: attempted relative import with no known parent package`。
> 而 `-E` 会连 `PYTHONPATH` 一起忽略，没法靠环境变量把包目录告诉解释器 ——
> 所以只能靠启动器显式设置 `sys.path`。
>
> 这也是 `--check` / `--once` 手工调试与 systemd 启动**必须用同一条命令**的原因：
> 换一种启动方式得到的行为就不是真实行为。

> 注意 `-E -s`：忽略所有 `PYTHON*` 环境变量与 user site-packages。选手怎么
> `pip install` 都污染不到 Agent。手工调试时也请带上。

## 发版时附带统一注册密钥（可选）

「Agent 发布」页构建版本时有一个勾选项：**附带统一注册密钥**。勾了之后：

* 服务端在**构建那一刻现场签发一把新密钥**（库里照旧只存哈希），把明文放进包的根目录
  `bootstrap.key`（0600），并把"这一版附带的是哪把"记在版本记录上；
* 装出来的机器不用再手工拷密钥就能注册 —— 第一次开机就会出现在「机器配对」页；
* 版本历史里能看见这把密钥，**就地可以吊销**（吊销只影响还没注册的机器，已注册的不受影响）。

默认**不勾**，而且要理解它的后果：装机入口（`/api/v1/agent/install/*`）是**刻意不鉴权**的
（空机器上没有任何凭据可用），所以**附带密钥的包等于一张"能注册进这台服务端"的通行证** ——
任何能打开装机页的人都能下载它。只在确认局域网里没有外人时用；发完就吊销那把密钥，或者换一个
不带密钥的版本铺开。

## 发布时的「高级选项」

除了「附带统一注册密钥」，构建卡片里还有一个折叠的**高级选项**，决定"机器上已有的东西
要不要跟着改"。这些选择**随包带下去**（也写进装机台账与升级清单），所以机器那一侧真的会照做：

| 选项 | 默认 | 含义 |
|---|---|---|
| **覆盖机器上已有的注册密钥** | 勾了「附带密钥」就**默认覆盖** | 用包里那把替换机器上旧的（旧的会**先备份**成 `bootstrap.key.replaced-<时间戳>`，回执里写明备份路径）。只在勾了「附带密钥」时才有意义 |
| **升级模式** | **`apply`**（自动） | `apply` = 机器下载→验签→切换→**自动重启**；`stage` = 只下载验签、先不切换；`off` = 不升级 |
| **更新机器上的 `agent.ini`** | 全部**不改** | 逐键三态：`不改` / `仅更新默认配置`（机器上缺这个键，或它的值还是上次安装器写下的那个，才更新）/ `强制覆盖`。状态与凭据文件**永远不动** |

两条要记住的边界：

* **升级只换程序，不动配置与注册密钥。** 自升级是运行账号在跑，而注册密钥是 root 只读的
  —— 想换密钥或改配置，就**重跑安装器**（它有 root）。"发一版就把全机房的 `agent.ini`
  一起改掉"不是这套东西的行为。
* **升级模式默认 `apply`，但镜像里没有发布公钥时会降级成 `off` 并明确报出来**：没有信任锚
  的机器验不了包，硬按 `apply` 走会让服务直接起不来、整批机器失联。显式写 `apply` 却没有
  公钥仍然是错误 —— 那就该把 `.key/release-key.pub.json` 打进镜像。

## 卸载 Agent

**那台机器面前的一条命令**（推荐；装机页上也印着它，带复制按钮）：

```bash
curl -fsSL http://<服务端>/api/v1/agent/install/bootstrap.sh \
  | sudo sh -s -- --uninstall --yes
```

它从机器上已有的 `/etc/syncoj/agent.ini` 里读服务端地址（所以不用再抄一遍；
读不到才要求显式给 `--server`），把安装器抓到 `/tmp` 再跑 —— **离线包里刻意不含
安装器**，装完之后机器上只剩 `run_agent.py`，所以想卸载的第一步永远是"先把安装器
弄回来"。想先看要删什么就别加 `--yes`，加 `--dry-run`。

手上有安装器文件时也可以直接跑：

```bash
sudo python3 install.py --uninstall --dry-run   # 先看要删什么，什么都不改
sudo python3 install.py --uninstall --yes       # 真卸
```

它删掉的不只是程序，还有三样**凭据**：`/etc/syncoj/bootstrap.key`（统一注册密钥，一把能注册
整间机房的通行证）、`release-key.pub.json`（升级信任锚）、以及状态目录里的本机身份与 token。
所以非交互时必须显式 `--yes`（`--dry-run` 不需要），交互时要把名字「syncoj-agent」打一遍；
`--keep-state` 可以保留日志与状态（要复盘时用）。选手桌面上的下发文件、`/tmp` 临时目录、运行
用户的家目录都**不动** —— 结尾会逐条说明。

「文件下发」里的目标目录怎么填：

| 要发的东西 | 目标目录 | 落点 |
|---|---|---|
| 题面、样例、须知（通用资料） | **留空** | `桌面/题面.zip` |
| 某道题的附件、额外数据、模板 | `{player_no}/<题目名>` | `桌面/<准考证号>/<题目名>/数据.zip` |
| 考场公告 | 随便（`NOTICE.md` 会被认出来） | 落在上面那个目录，**同时**显示在选手页「考场公告」 |

目标目录是**自己填的模板**（没有"选一道题就自动填好"那套绑定：下发的是文件，不是题目的附属
物，多数时候留空就够了）。「实际落点预览」那行会把模板代入第一位选手算出来。

**考场公告就是一份下发的 `NOTICE.md`**：在「文件下发」页点「写考场公告」写正文，然后
照常「下发」给选手。可选，没下发就没有公告。文件名大小写不敏感（`notice.md` 也算）；
多份就取最后下发的那一份。

**纯文本资产可以直接在页面上改正文**：表格里带「编辑」的那些（`.txt` / `.md` 这类，且
不超过 1 MB）。改完服务端会把**已经发到机器上的那一份重新排队**，机器下一轮心跳就会
拉到新正文 —— 所以别在考试中途改一份正在当测试数据用的文本文件。

**到点自动结束**：场次填了「结束时间」的话，时间一到服务端会把它置为「已结束」，之后
这台机器上的文件修改**不再同步**（上传会被拒绝）。开考时间是**不会**自动触发的 ——
开考是教师的有意动作。把「结束时间」留空就不会自动结束。

**下发资产一律原样落盘，服务端不解压**：题面和样例本身就是 zip，机器上就是
`题面.zip` / `样例.zip`。要密码就把 `password.txt` 当普通资产一起发。
