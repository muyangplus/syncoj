# 部署与安装

## 注册方式：只有一种

**镜像内置的统一密钥。** 整间机房共用一份镜像，装机时把同一把密钥写进每台机器，
机器首次开机各自换回**自己的**凭据。

> 统一密钥能注册**整间机房**，所以它绝不写进 `agent.ini` —— 那个文件 chown 给了
> 选手账号，学生读得到里面的每一个字节。装机时它落到
> `/etc/syncoj/bootstrap.key`（0600、属主 root），由 root 的一次性单元换回凭据。

注册上来的机器**没有归属**：它会把一个六位配对码写到桌面上的「配对码.txt」，
然后安静等着。教师在管理界面「机器配对」页输入那个码、选一位选手 ——
**配对是永久的，而且绑的是"人"**，所以同一个学生换一场比赛不用重新配对，
只要新场次应用了那份名单。配对成功后文件自动消失。

> 早先还有一条"每选手注册码"（`--enroll-code`）的路，**已经删掉了**：
> 逐台发码在"一份镜像装遍整间机房"的现实里根本不可行，而两种模式并存的代价是
> 每种都要维护、测试，并且迟早有人选错。`agent.ini` 里若还留着 `enroll_code =`，
> 它会被静默忽略（升级过来的机器不该因为这个起不来）。

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

> **重复执行安装器时不必再传一遍密钥。** 它已经在那台机器上了，而"
> 没传 `--bootstrap-key`"不等于"这台机器没有密钥" —— 安装器会认出现有的密钥文件。
> 这条很重要：误判的后果是上一次装好的开机注册单元被删掉，表现是"什么都没改，
> 但下次开机不再注册了"。

### 装完会发生什么

装好后多了一个单元 `syncoj-agent-enroll.service`：开机时**以 root 身份**跑一次
`run_agent.py --provision`，用 `/etc/syncoj/bootstrap.key` 换回本机凭据，
再 chown 给选手账号。之后 Agent 本体（选手身份）只读凭据，碰不到密钥。

装完的机器有三种状态，桌面上会写出来，别把它们混为一谈：

| 状态 | 桌面上 | 含义 |
|---|---|---|
| 还没配对 | `配对码.txt` | 把六位码告诉老师，老师在「机器配对」页绑定到人 |
| 已配对、没场次 | `等待场次.txt` | 配对好了，但那份名单还没被应用到场次里 |
| 正常 | 两个文件都没有 | 正在收代码、收下发 |

排障时可以直接看配对码，不用翻日志：

```bash
python3 /opt/syncoj/current/run_agent.py --config /etc/syncoj/agent.ini --pair-code
```

### ⚠️ 建镜像时最容易犯的错

**在母机上装好并跑过一次 Agent，然后把整机做成镜像。**

那样每一台克隆机都带着**同一个身份**开机（同一份 `credential.json`、
同一个 `machine_uuid`），服务端会看到一批同 ID 的机器互相覆盖 ——
而文件是**静默错的**：看起来一切正常，只有成绩会对不上人。

安装器会在状态目录里发现残留身份文件时**大声告警**并给出命令，但请从流程上避免：

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

## 运行身份（重要）

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

**下面是默认值，不是硬编码**（见「改路径」一节）。

```
桌面/                               ← deploy_root = {desktop}
├── 题面.pdf                         ← 通用资料：目标目录留空 = 直接落桌面
└── <准考证号>/                      ← scan.roots = {desktop}/{player_no}
    ├── p1/                        ← 按题下发时目标目录填 {player_no}/<题目名>
    │   ├── 题面.pdf
    │   └── p1.cpp                 ← 选手写代码的位置
    └── p2/p2.cpp
```

`{desktop}` 会自动探测：先读 `<运行账号的家>/.config/user-dirs.dirs` 里的
`XDG_DESKTOP_DIR`，再依次试 `~/桌面`、`~/Desktop`、`~/desktop`。

**`{desktop}` / `{home}` 一律按「运行账号」解析 —— 也就是跑安装的那个账号**
（`[agent] run_user`，安装器会自动写进去），**与当前是谁在跑这个进程无关**：
注册单元 `syncoj-agent-enroll.service` 以 root 跑，按当前进程的用户算就会得到
`/root/桌面`，那台机器上不存在 —— 注册直接失败退出 2、journal 里还看不到原因。

### 改路径

四个占位符，前两个载入配置时就展开，后两个等注册成功后展开：

| 占位符 | 何时展开 | 展开成 |
|---|---|---|
| `{desktop}` | 立即 | 运行账号的桌面（兼容「桌面」与 `Desktop`） |
| `{home}` | 立即 | 运行账号的家目录 |
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

> 展开不了的占位符会**原样保留**而不是报错 —— 配置校验发生在注册之前，
> 把"还没注册"报成"配置错了"只会让人白折腾。注册完成后 Agent 会在启动日志里
> 打出最终展开的扫描目录，对着它核对最快。

改完路径不用重装，重启服务即可：

```bash
sudo systemctl restart syncoj-agent
journalctl -u syncoj-agent -n 30     # 第一行就会打印实际扫描目录
```

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

# 顺便用服务端私钥签一份 .sig（供手工核对）
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
