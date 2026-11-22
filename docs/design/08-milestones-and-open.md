## 8. 里程碑

| | 内容 | 验收 |
|---|---|---|
| M1 | 服务端骨架 + enroll + tick + 在线列表 | 50 台机器能在 Web 上看到在线状态 |
| M2 | 扫描 + 上传 + `source/` 落盘 + 文件树 | 选手改代码 → 20s 内出现在 Web |
| M3 | asset + deploy + Range 续传 + 进度条 | 下发 500MB 测试点，中途断网能续 |
| M4 | 结果扫描 + 成绩矩阵 + 审计日志 + 资源限制 | 教师手动跑 LemonLime → 成绩自动汇总 |
| M5 | 签名自更新 + 离线包 + 幂等 installer | 一条命令升级 50 台 |
| M6 | 永久配对（统一密钥 + 六位码，取代注册码）+ 名单库 | 一份镜像装 50 台，教师读码配对即可开考 |
| M7 | API 形状统一（信封/错误体）+ 前端共享数据层 + 补齐全部增删改查入口 | 新增一个资源的成本是"一条声明 + 一个页面" |
| M8 | 「发布当前版本」：从服务端本机源码构建并签发 Agent 包 | 界面上点一下，机器真能升级上去（`test_release_build` 全链路） |
| M9 | 局域网自动发现服务端（包内嵌地址 + UDP 广播发现，签名鉴权） | 装 50 台不用输地址；服务端换 IP 后机器自己找回来（`test_discovery` 两侧） |
| M10 | 免登录两页 + 一键装机入口（`/install` 从服务端直接装） | 空机器上一条 curl 装完；没铺开版本时装机页给"去铺开"而非一条没用的命令 |
| M11 | 公开端口（默认 80）只放装机页与选手页 | 学生敲 `http://10.0.0.5/` 看不到登录框与 `/docs`；绑不上 80 时管理界面不受影响 |
| M12 | 选手页考场公告（正文 = 下发的 `NOTICE.md`） | 教师写一份 `NOTICE.md` 并下发，选手页当场显示；没下发就是没有 |
| M13 | 考试时间窗：窗口外拒收 + 到点自动结束场次 | 到点后机器安静下来（不再重试上传），且**不做**任何违规判定 |

## 9. 待补信息

1. 真实 NOI Linux 机器上的探测输出：`python3 -V`、`fs.inotify.max_user_watches`、权限模型、`cat /sys/class/dmi/id/product_uuid` 是否可读（决定硬件指纹认回在目标机上是否可用）。
2. LemonLime / Arbiter 的结果输出路径与格式样例（阻塞 M4 精确解析）。
3. 浏览器里的实际界面验收：前端只经过 `vue-tsc` 与 `vite build`，没人真正点过。

选手代码目录的规范位置是产品决定（默认运行账号的 `~/Desktop` 下），见 ../design/00-decisions.md §0 与 ../design/04-server.md §4.5。

## 10. 待办

「发布当前版本」已实现，见 ../design/07-ops-and-install.md §7.2。

### 10.1 内置评测

**已实现，见 §12（[`10-builtin-judge.md`](10-builtin-judge.md)，ADR-0016）**。与外部评测器（只收 `judge_result/`，[`../reference/judge-result.md`](../reference/judge-result.md)）并存、按题选择；外部评测那一路仍"不触发评测"。

原列的岔路定案如下：

| 岔路 | 定案 |
|---|---|
| 1. 评测跑在哪 | 独立评测机进程 `syncoj-judge`（专用账号，可与服务端同机或另一台），不派给考场 Agent |
| 2. 隔离程度 | isolate（推荐）；`rlimit` 仅开发；不自研 seccomp，未做 `systemd-run` |
| 3. 测试数据来源 | Web 上传 zip，走与下发资产同一套 blob 存储，上传时校验 |
| 4. 谁触发 | 教师点「评测」/「重测」；场次进行中需显式确认（`force`） |
| 5. 语言 | C++ 与 C |
| 6. 判分范围 | 逐点 + 子任务捆绑 + testlib 风格 SPJ；不做交互题 |

随之落地：成绩仍经 `ResultScanner` 收进 `judge_run`（§4.4 不变），测试点细节放在 `result.json` 的 `cases` 并原样进 `raw_json`；评测所用代码记在 `judge_task.source_path` / `source_sha256`。

### 10.2 客户机同步服务端的时区与时间

**目标**：机房机器与服务端的时区、时间对齐，由服务端**一个开关**控制（关闭时不做任何事）。
**现状**：服务端只按 `SYNCOJ_TZ`（默认 UTC+8）**显示**时间（[`04-server.md`](04-server.md)）；机器时区与时钟由装机镜像决定，各自漂移。

**约束**：

* 改时区、改时钟要 root，常驻 Agent 以普通账号运行。现有提权通道：**root 管理单元**（Agent 往 `<状态目录>/handoff/` 写请求，root 单元验签后执行，[ADR-0012](../decisions/ADR-0012-remote-uninstall-authorization.md)）；root 一次性单元先例 `syncoj-agent-enroll.service`（[ADR-0009](../decisions/ADR-0009-registration-is-a-root-unit.md)）。
* **时钟跳变影响**：日志时间戳、`source_file.mtime`（台账按它记最后看到的版本）、结果扫描的"mtime 未变即跳过"。考场常规做法是机器跟 NTP 走，源可以是服务端（第 2 条）。

**待定岔路**（未定不动手）：

1. 同步到哪一步：A. 只设时区（`timedatectl set-timezone`）；B. 时区 + 时钟；C. 只比对偏差并显示在机器列表，改不改由教师定。
2. 时钟怎么校：A. 直接 `timedatectl set-time` / `date -s`（简单、立即，但跳变）；B. 服务端做 NTP 源，客户端 `chrony` / `systemd-timesyncd` 指向它（平滑标准，但服务端多一个常驻进程、装机要带配置）；C. B 优先、A 兜底。
3. 提权从哪来：A. 在 root 管理单元的交接请求里加一种"设时区 / 校时"请求；B. 另加 root 一次性单元，服务端下指令时触发（照注册单元）；C. 不加提权 —— 只剩第 1 条的 C。
4. 时区名从哪来：显示时区是**固定偏移**（`SYNCOJ_TZ=+08:00`），`set-timezone` 要 IANA 名（`Asia/Shanghai`）。要么加一个只给客户机用的时区名配置项，要么规定"`SYNCOJ_TZ` 写名字才下发、写偏移不下发"。
5. 开关默认值与粒度：默认**关**（改时钟风险高，要教师显式打开）还是开？整间机房一个开关（同「运行参数」）还是按机器？
6. 时间以谁为准：服务端系统时钟，还是服务端先与上游 NTP 对齐后的值（服务端的钟同样可能不准）？

**落地时要一起改的**：

- 「运行参数」（「机器配对」页）现只有三个整数秒（`services/runtime_settings.py`，整间机房粒度、非法值拒绝）；加布尔开关需同步界面与校验。
- `docs/reference/protocol.md` 的 tick 响应加下发项（时区名 / 时间基准 / 偏差回执），`agent/tools/build_fixture.py` 契约样本重生成。
- 时区名若走配置项：`docs/reference/config-keys.md` 与 `CONFIG_POLICY_KEYS` 同步（文档守卫逐键对账）。
- 客户机时间**不参与**考试时间窗判定（服务端算，选手页倒计时也取服务端时间），要写进协议文档。

### 10.3 已知限制

主动留下的取舍：

* 上传时让服务端生成随机密码，密码只落在 `password.txt` 文本资产里，上传回执不为它加字段；教师要当场拿密码就走「文件下发」页「打包成 zip」。
* 「运行参数」（空闲心跳 / 有活心跳 / 离线判定）**整间机房**生效，没有按机器、按场次的粒度。
* 安装时换密钥留下的 `bootstrap.key.replaced-<时间戳>` 备份不清理。
* 同版本重装是就地替换，没有同版本回滚点。
* 诊断包**每台机器只留最新一份**：不存历史，不提供原始 gzip 下载。按机器的 60 秒限速窗口在内存里，服务端重启即重置（至多多收一份）。

### 10.4 被明确否决的想法

不再重新提案：

* **违规判定 / 迟到上传告警**（如结束后上传记 `late_upload` 并点名）：否决理由——把技术状态变成对人的指控，网络抖动、机器卡顿会让正常选手撞线。决定：到点只做两件事，拒收（409 `contest_ended`）+ 场次自动置「已结束」（`contest_auto_closed`，info 级审计，不点名）。
* **从题目模式推导"保存规则"给选手看**：否决理由——服务端不知道每台机器的 `scan.roots` 与前缀，错误指引比没有更坏。决定：公告由教师下发 `NOTICE.md`（../design/05-api-and-frontend.md §5.6）。

### 10.5 重构遗留（2026-11 四路审计 + 方案盲审）

已拍板：特权模型改 root 管理单元；删除语义**按现状硬删**、文档承认；传输层如实写明文；一人一机按考号查重（不建 person 表；2026-11 改为按名单条目查重 + 场次按所用名单取人，见 [`ADR-0018`](../decisions/ADR-0018-multi-roster-binding.md)）；选手页手填保留但降级。

**已完成**（细节见各提交与对应文档）：

* 阶段 0：检查链路全绿、自更新止血。
* 阶段 1：回收 / 下发 / 成绩 / 配对正确性。
* 阶段 2 中：发布签名覆盖版本号与大小；未配对机器也能升级；带密钥的包经公开装机入口下发（铺开期间密钥公开，装完即吊销）；`bootstrap.sh` 加固；协议版本 + 426；迁移前备份；单元家目录不再用 `%h`；辅助件路径可配置并收回 `needs_layout`；`--skip-service` 管住注册单元；**root 管理单元**（升级切换与远程卸载经交接目录由 root 验签执行，Agent 恢复 `NoNewPrivileges=yes`，删 sudoers 与 `/usr/local/lib/syncoj`，见 ADR-0012）。
* 阶段 3 中：边沿触发的审计事件；删 `agent_status`；`DeployTask` 状态读时聚合 + `cancelled_at`；blob 提交后回收（含删资产 / 选手 / 场次）；删除语义前端文案；注册表身份随解析更新、待配对心跳记在线；tick 运行参数每轮只读一次。
* 阶段 4：列表 SQL 分页与聚合、审计索引、批量清理、前端轮询与概览聚合接口。
* 阶段 5：文档对账。
* **systemd 245 冒烟**：`scripts/smoke_systemd245.py` 在 Ubuntu 20.04 容器（真 systemd 245 当 PID 1、Python 3.8.10）里以 umask 0 安装、经 root 管理单元升级、远程卸载未配对机器，CI 的 `systemd245` 一路每次跑。它抓出并已修复（均有回归测试）：安装器沿用调用方 umask（装出 0777）；root 注册单元在属选手的状态目录 / 桌面里按路径写文件、chown，顺符号链接可改写任意文件；root 建的 `agent.log` 让选手服务没有日志；未配对机器不处理卸载令牌、事件上报恒 403；管理单元重写正在运行的自身单元文件。**仍未覆盖**：真 NOI Linux 2.0 桌面环境（容器没有图形会话与「桌面」目录的本地化名）。
* **启动守卫与回滚移到 root 一侧**：Agent 单元加 `ExecStartPre=-+…install.py --boot-guard`（`+` 以 root 跑，`-` 失败不挡启动），计数文件仍是安装根下的 `upgrade-state.json`（真机上属 root）；root 管理单元切换时记回滚点，Agent 平稳跑几轮（**含未配对时的心跳**）后经交接报 `healthy`，root 清状态；回滚过的版本记进 `rolled-back.json`，root 拒绝再切、Agent 不再下载（否则"切过去→崩→回滚"循环）。主单元改 `Restart=always`；冒烟加"一启动就崩的版本被切回去"。顺带修了：交接槽被后来的升级请求冲掉卸载请求；root 升级时当场跑注册单元（新版本坏时挂住两分钟）。
* **发布（CD）**：推 `v<版本号>` 标签 → 全量检查（含 systemd 245 冒烟）→ 构建部署包 / Agent 离线包 / 安装器 / 校验和 → GitHub Release，见 07 的 §7.5。
* 小项：配置里空的 `pass` 告警分支删掉；安装器"三选一"改"四选一"；导出接口 `include_deleted` 默认与界面一致为 True。
* **服务端主动通告**（新想法，已实现）：见 §11（`09-server-announce.md`）。服务端周期通告 + 「召集机器」，Agent 挑战应答验签后切换；Agent 自己探测保留为兜底（ADR-0015）。systemd 245 冒烟里关掉局域网发现、服务端换端口重启，机器靠通告转过去。
* **系统自带沙箱评测**（新想法，已实现）：见 §12（`10-builtin-judge.md`）。按题选外部评测器 / 自带评测；数据包上传时校验；独立账号的评测机（`syncoj-judge`）经 HTTP 领任务，碰不到私钥与数据库；沙箱 isolate（推荐）/ rlimit（仅开发）；结果写进 `judge_result/` 走现有扫描（ADR-0016）。

**还没做**：

* **按 uuid 认回已配对机器**需原机离线或出示旧 token（与还原卡场景、注册重试窗口对齐），可由 root 管理单元持有凭据；`agent.ini` 改 `root:root 0644`；删 `config_policy` 逐键合并器；注册单元不再以 root 执行发布目录里的代码。
* **安装器拆包**：`syncoj_install/` 包 + zipapp 单文件分发，先 plan 后 apply；顺带 root 对选手目录递归 chown 改安全实现（`fwalk` + `follow_symlinks=False`）、重跑不再无条件重启服务、`--from-dir` 与 `build_bundle` 共用文件清单、`mkdtemp` 泄漏、删 `--service-name`；届时测试宿主沙箱（`conftest._installer_never_touches_the_host`）可收回（现在留作安全网）。
* **结构**：`api/agent.py` 拆出 `services/tick.py`（返回 `TickOutcome`）与 `services/upload.py`；tar 安全解包、RSA 验签、`parse_version` 各留一份源码，安装器用构建生成的 vendored 拷贝 + 逐字节一致测试（路径校验除外，按硬规则保持两份独立实现）。
* 小项：选手删掉的文件在 `source/` 里仍留着、照样被评测（要不要清待定）。

**推迟（本轮不做）**：`api/admin.py` 与 `agent/main.py` 的整体拆分；zip 改密码改后台任务（纯 Python 约 1.9 MB/s、同步跑在请求线程）；TLS + pinning；全局 person 表；"blob 已有就直接物化免上传"（要限定在本人历史出现过的 sha，需单独设计）。
