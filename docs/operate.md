## 部署

见 [`install-agent.md`](install-agent.md)。三条安装入口：

```bash
# 离线包
sudo python3 install.py --bundle ./syncoj-agent-0.1.0.tar.gz \
     --sha256 <校验和> --server https://10.0.0.1:8443

# 在线自举
curl -fsSL https://10.0.0.1:8443/dist/bootstrap.sh | sudo sh -s -- --server ...

# 先预览（不需要 root）
python3 install.py --bundle ./x.tar.gz --server https://x --dry-run
```

`install.py` 不接受「每选手注册码」——那条链路已经删除了，只有统一密钥一条路。

## 密钥从哪来：`.key/`

为了不用"手工 genkey → 手工指私钥 → 手工把公钥拷进包"走一遍才能试一次升级，
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
> **两道**挡着 —— 私钥的安全不该只挂在一个可以被删掉的文件上。

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

**发现要验签，这不是洁癖。** 机器"找到服务端"之后第一件事是把统一注册密钥发过去，
所以一个不验签的应答等于把"能注册整间机房"的密钥交给局域网里任何一个应答者 ——
而那个密钥是 50 台机器共享的，拿它做 HMAC 也挡不住。没有发布公钥就**不发现**
（与"没有密钥就不升级"同一个默认）。

**服务端怎么知道自己的地址：** 优先 `SYNCOJ_PUBLIC_URL` / `serve --public-url`；
没配就从**请求来源反推本机地址**（内核算的，客户端伪造不了）；再退到"教师浏览器
用过的那个非回环 `Host`"。三条都没有就**什么都不做** —— 退回 `127.0.0.1` 是最坏的
一种，50 台机器会各自连自己，而现场只看到"注册不上"。

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

> 这一节是原 `README.md` 对应的几段**逐字**内容：批量部署用的命令行等价流程、
> 名单 CSV 的列顺序、以及测试规模汇总。

命令行等价流程（批量部署时更顺手）：

```bash
# 场次与选手
syncoj-server contest create --name "2025 校内模拟赛"
syncoj-server contest import-players --contest 2025 --file roster.csv
syncoj-server contest list

# 统一注册密钥（整间机房一份；明文只显示这一次）
syncoj-server bootstrap-key issue --label "2025 机房镜像" --expires-days 30 \
    --out /srv/syncoj/bootstrap.key      # 顺便写成 root 只读的一份
syncoj-server bootstrap-key list
syncoj-server bootstrap-key pending      # 还没配对的机器（含配对码剩余时间）
syncoj-server bootstrap-key revoke --id 1

# 开发阶段库结构还在动，需要重来时
syncoj-server db reset --yes [--keep-admin]
```

名单 CSV 的列顺序是 `选手编号,姓名,座位,分组`（只有编号必填），
逗号或制表符分隔都认，表头与 `#` 注释行会被自动跳过。

测试规模：**服务端 741 项通过**（另 2 项按环境跳过）、**Agent 363 项通过**
（另 13 项按环境跳过）。含端到端集成测试（真实 Agent 代码通过真实 HTTP 打到真实
服务端）、**openssl 交叉验证**（手写密码学代码唯一可信的证据是独立实现能互相
验通，而且要求 openssl 1.1.1 与 3.0 两种默认输出格式都能读）。
