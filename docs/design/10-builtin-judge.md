## 12. 系统自带沙箱评测

> **状态**：已实现（2026-11），与外部评测器对接**并存、按题选择**。用法见 `reference/judge-result.md` 第 6 节，取舍见 ADR-0016。
> 代码：`server/syncoj_server/judging/`（`testdata` / `sandbox` / `runner` / `worker`，只用标准库、兼容 3.8）、`services/builtin_judge.py`、`api/judge.py`、`api/admin_judge.py`。

### 12.1 目标与边界

* **要**：教师对一个场次（或一道题、一个选手）发起评测，按 NOI 惯例编译、运行、比对，成绩进成绩矩阵；能重测；能看每个测试点。
* **不要**：在线 OJ 那一套（题库、提交队列、实时排行榜）；交互题与通信题；Windows 上的网络隔离（`winjob` 限资源 + 低完整性级别，不隔离网络）。
* **硬约束**：选手代码**不可信**；服务端进程持有数据库、发布私钥（`.key/`）、统一注册密钥，**不在服务端进程内、不以服务端账号**运行选手程序。

### 12.2 关键决定：产物写进现有的 `judge_result/`

- 决定：评测机像 LemonLime 一样写 `judge_result/<场次>/<选手>/<题目>/result.json`，由现有 `ResultScanner` 收进来。否决直接写 `judge_run` 表：需另建一套成绩入口。
- 结果：成绩矩阵、`ok/unparsed/manual/missing` 四态、"手工录入永不被自动扫描覆盖"、导出 CSV 原样生效；同一场可混用自带评测与外部评测器。
- `result.json` 用 `judge-result.md` 已支持的 JSON 形状，外加 `cases` 数组放测试点细节（扫描器只读 `score` / `max_score` / `status`，其余原样进 `raw_json`，「成绩」格子点开可看）。

### 12.3 进程与信任边界

```
syncoj-server（服务端账号）         syncoj-judge worker（专用账号，可在另一台机器）
  评测任务表 judge_task  ──HTTP──>  领任务（带 worker 令牌，只能领任务、取代码与数据、交结果）
  source/、测试数据       <──HTTP──  编译 → 沙箱运行 → 比对 → 交回 result.json
  ResultScanner 收结果
```

* worker（`syncoj-judge --server … --token-file … --backend isolate|rlimit|winjob`）是**独立进程、独立账号**，经 HTTP 领任务（服务端持有全部状态），碰不到 `.key/`、数据库与 `source/` 目录，只拿到任务所需的代码与数据包。可与服务端同机；条件允许时放另一台（评测时 CPU 满载，避免与收代码的服务端争用）。
* worker 令牌在「评测」→「评测机」签发，只存哈希（`judge_worker`），只授权评测相关端点；吊销 = 删行（需输评测机名称确认）。状态上报、远程动作与一键安装见 §12.10。
* 任务表 `judge_task(contest, player, problem, status, attempt, worker, lease_until, source_path, source_sha256, outcome, error, …)`：状态 `queued → running → done / failed`；领任务带 15 分钟租约（worker 崩了租约过期自动回队列），最多尝试 3 次。任务即历史（§12.9）：重测只替换同一对**还在排队**的任务，跑完的留着。没交代码的选手直接 `done`、不写 `result.json`（矩阵照旧显示未交）。
* 数据包按 sha256 缓存在 worker 的 `--cache-dir`，同题几十个选手只下载一次。

### 12.4 沙箱：可插拔后端，按安全程度排

| 后端 | 实现 | 前提 | 定位 |
|---|---|---|---|
| `isolate` | 调用 IOI 的 isolate（命名空间 + 可选 cgroup） | 机器装了 isolate（setuid root）；同机多个 worker 用不同 `--box-id` | **推荐**，正式考试用 |
| `rlimit` | `fork` + `setrlimit`（CPU / 内存 / 输出 / 进程数）+ 另一账号运行 + 每点前清空工作目录 | 无；worker 以 root 跑时必须给 `--run-as`，否则拒绝 | **只给开发与自测**，无网络与文件系统隔离，界面标红"不隔离" |
| `winjob` | Windows Job Object（CPU / 内存 / 活动进程数 1 / UI 限制 / 关 Job 即杀）+ 选手程序与 checker 以受限令牌、低完整性级别运行 + 每次运行独立临时目录 | Windows，Python ≥ 3.8，MinGW-w64 `g++` 在系统 `PATH`；不支持 `--run-as` | Windows 上的评测机；以 LOCAL SERVICE 运行；不隔离网络（`isolated: false`） |

* isolate 是**调用**不是链接（它是 GPL-2），不影响本仓库许可证。
* 否决自研 `unshare` + seccomp：安全边界，自研维护成本与出错代价过高；`rlimit` 覆盖无更好选择时的情形。
* 否决 `systemd-run` 后端：统计单次运行的 CPU 时间与峰值内存需绕路；有 root 的机器直接装 isolate。
* 工作目录里的文件一律先删同名条目再 `O_CREAT|O_EXCL|O_NOFOLLOW` 新建、属主与权限经 fd 设置，读输出 `O_NOFOLLOW|O_NONBLOCK` 且只读普通文件：选手程序换成符号链接 / FIFO 也带不走评测机（可能是 root）的权限。rlimit 以 `--run-as` 运行时，每次运行后杀掉该账号在本次运行开始之后起的全部进程（`setsid` 逃出进程组的也算）。
* 计时：**CPU 时间**判 TLE；墙钟只作硬上限（时限的 2 倍 + 1 秒），防 sleep 卡死。内存取峰值常驻集，isolate 开 `--cgroups` 时取 cgroup 峰值。`rlimit` 后端经一层 shim 取选手程序自己的 rusage（Linux 的 `ru_maxrss` 跨 exec 保留，直接从 worker fork 会把 worker 的内存算到选手头上）。
* 一个 worker 默认一次评一个任务；`--slots N` 并发评 N 个（各占一个 box，§12.10），或起多个 worker（各用不同 `--box-id`；rlimit 下要各用不同的 `--run-as` 账号）。
* 平台守卫：Linux 只认 `isolate` / `rlimit`，Windows 只认 `winjob`（默认），其余组合启动即报错。
* `winjob`：自己调 `CreateProcessW`（`CREATE_SUSPENDED`）→ `AssignProcessToJobObject` → `ResumeThread`，程序进 Job 前不执行任何指令；否决 `subprocess.Popen` + 未公开的 `NtResumeProcess`（Popen 不给主线程句柄）。继承句柄用 `PROC_THREAD_ATTRIBUTE_HANDLE_LIST` 限定为标准输入输出三个；限进程数时以 `DETACHED_PROCESS` 启动（否则 conhost.exe 占掉 Job 的活动进程名额），编译用不限进程数、限整个 Job CPU 与内存的 Job。CPU 取 Job 记账的用户 + 内核时间；内存取峰值提交量；墙钟超限 `TerminateJobObject`；运行中轮询工作目录增长判 OLE。崩溃（NTSTATUS / `abort()`）且峰值已达内存限制 90% 记 MLE（Job 挡住的分配不计入峰值）。编译加 `-static`（不依赖 MinGW 的 DLL）、`-Wl,--stack=<内存限制>`（对齐 Linux 栈不限）与 `-pipe`。
  * 降权：选手程序与 checker 用本进程令牌派生的受限令牌（`CreateRestrictedToken` 去掉全部特权）、完整性级别 Low，经 `CreateProcessAsUserW` 启动（同样挂起 → 进 Job → 恢复）；编译器用普通令牌，也在 Job 里。工作目录标 Low（`S:(ML;OICI;NW;;;LW)`），输入输出照常读写；目录以外写不了（no-write-up），评测机进程内存读不了（进程默认 no-read-up）。降权失败时退回评测机令牌并告警，不静默。
  * 令牌文件与数据包缓存（含评测中的选手代码）标 `NO_READ_UP`（Medium，`NRNWNX`），低完整性进程读不了；install.ps1 设，worker 启动时再确认（失败只告警）。
  * 工作目录的根必须是纯 ASCII（MinGW 的 as / ld 用 ANSI API，中文用户名的 `%TEMP%` 会编译失败）：否则依次换到 `SYNCOJ_JUDGE_BOXES`、`%ProgramData%\SyncOJ\judge\boxes`、`%SystemDrive%\syncoj-judge-boxes`。
  * 不隔离网络；选手程序仍能读 LOCAL SERVICE 与低完整性可读的文件（系统文件等）。

### 12.5 评测语义（NOI 惯例）

* **语言**：C++ `g++ -O2 -std=c++14 -lm`（`.cpp/.cc/.cxx`，编译参数可按题改）；C `gcc -O2 -std=c99 -lm`。编译也在沙箱里跑（防模板递归、`#include </dev/random>` 这类编译器炸弹），限 CPU 10 秒、墙钟 30 秒、输出 64 MB。
* **IO**：按题配置文件 IO（默认读 `<题目>.in` 写 `<题目>.out`，文件名可按题改）或标准 IO（默认）；文件 IO 下没有输出文件判 WA 并写明原因（多半忘了 `freopen`）。默认时限 1000 ms、内存 256 MB。
* **数据包**：每题一个 zip（与下发资产同一套上传与 blob 存储），**上传时校验结构**。测试点 `<名字>.in` + `<名字>.ans`（或 `.out`），可在任意子目录，名字唯一、按自然序；可选 `config.json` 写每点分值与子任务（子任务全 AC 才得分，捆绑）；未写分值时 100 分按点均分。LemonLime 的数据目录直接打 zip 即可；整场题目配置的导入导出见 §12.8。
* **比对**：默认逐行比较，忽略行末空白与文末空行；数据包含 `checker.cpp`（连同 `testlib.h`）时用它做 special judge，testlib 风格 `checker <in> <out> <ans>`，退出码 0 为通过，也在沙箱里编译运行。
* **源码预检**：编译前对选手源码做文本检查（先按 C 翻译阶段规范化再匹配）；`#pragma`、内联汇编、系统调用、禁用头文件、题目以外的文件等直接拦截记 CE（不编译），疑似混淆只预警、写进结果的 `precheck_warnings`。规则与按题开关见 `docs/reference/judge-result.md` 第 7 节。
* 每次运行前清空工作目录，从评测机内存重新放入编译产物（选手程序与 checker 各自只在自己运行时在场），程序不能跨点留东西，也改不了 checker。
* **结果状态**：AC / WA / TLE / MLE / RE（带信号）/ OLE / CE（编译信息截断保存）/ SE（评测系统自己的错，**不算选手的分**，界面提示重测）。整题取最严重者（SE > CE > RE > MLE > TLE > OLE > WA > AC）。

### 12.6 界面与触发

* 「评测」页「题目」卡片：每题「设置」——上传数据包、设时空限制与 IO 方式、选择「自带评测 / 外部评测器」。
* 「评测」页「任务」卡片：题目（全部 / 单题）× 选手（全部 / 单人）「评测 / 重测」，可勾「不复用已有结果」；自动评测开关与去抖秒数；失败行「重测本题」；进度沿用已有聚合进度接口形状。顶栏「快捷操作」→「评测全部题目」等价。「评测记录」卡片列出每次评测（含复用的）。
* 「评测」页「评测机」卡片：签发令牌（附一行安装命令）、状态与正在评的任务、暂停 / 停止 / 重启 / 改名 / 吊销、本机评测机启停（§12.10）。
* 「成绩」：格子点开看每个测试点明细，可重测、看版本；选手列「版本与分数」看这位选手全部题目的 timeline。
* 评测用**领任务时 `source/` 里按题目模式选定的那份代码**（记下路径与 sha256）。场次仍在进行（`is_active` 或未到结束时间）时排任务回 409 `contest_in_progress`（评的是考到一半的代码）；教师确认后带 `force` 再排。

### 12.7 落地顺序（已完成）

1. 数据包与题目评测配置（模型 + 迁移 + 管理接口），上传即校验数据包结构。
2. worker + `rlimit` 后端 + C++ 编译与比对 + 写 `result.json`；端到端用例：A+B、TLE、RE、CE、文件 IO 忘写 `freopen`。
3. `isolate` 后端（CI 上没有真 isolate，用假 isolate 脚本覆盖命令行与 meta 解析）。
4. 任务表、租约、重测、进度与界面。
5. special judge、子任务。

取舍（为何不自研 seccomp 沙箱、为何经 `judge_result/` 而不直接写表）记在 ADR-0016。

### 12.8 题目配置导入导出

* **入口**：「评测」页「题目」卡片「导入题目配置」/「导出」；导入先预览（逐题改标识、选新建 / 覆盖 / 跳过）再应用。格式、映射、提示代码、上限 → [`../reference/problem-formats.md`](../reference/problem-formats.md)。
* **格式**：导入 SyncOJ 原生、HOJ、LemonLime（`.cdf` JSON）、Hydro（含旧 `config.ini`、SYZOJ `data.yml`）、QDUOJ、FPS；导出 SyncOJ 原生、HOJ、LemonLime、Hydro。
* **数据包不变**：外来题目规整成 §12.5 的数据包（`<名字>.in/.ans` + `config.json` + `checker.cpp`），走与上传数据包相同的校验与 blob 存储；评测机与计分不改。
* **模型只加两列**：`input_file`、`output_file`（文件 IO 文件名，NULL = `<题目>.in/.out`）。逐点限制、子任务依赖、非捆绑子任务、小数分、栈限制折算成现有语义并逐题给警告。
* **原生格式无损**：`syncoj-problems.json` + 原样的 `testdata.zip`，导出再导入各列与 `testdata_sha256` 不变。
* **checker**：随仓库带 testlib.h，checker 用了而包里没有时补上；实数比较与 HUSTOJ spj 由服务端生成不依赖 testlib 的 checker。
* **YAML** 用自带的受限子集解析器，不引入 PyYAML；XML 流式解析并拒绝 DOCTYPE / ENTITY。
* **只信服务端解析**：应用导入时重新解析暂存包，只采用客户端回传的标识与动作。

取舍记在 ADR-0017。

### 12.9 版本历史、自动评测与分数 timeline

* **版本**：`source_version(player, rel_path, sha256, size, received_at, problem)`，Agent 每传上来一份新内容（指纹变了）记一条；`source_file` 仍只记当前版本。版本内容列入 blob 引用清单，被替换的旧版本不回收；删选手、删场次、删台账行 / 清空台账（含已消失的）时历史随之删除并回收内容。`problem` 是收到时归的题；读的一侧按**当前**题目模式现算。迁移 020 用 `source_file` 里已存下的当前版本补一条初始版本。
* **任务按版本记**：`judge_task` 加 `source_version_id`、`trigger`（`auto` / `manual` / `rejudge`）、`requested_by`、`score` / `max_score` / `result_json`、`applied_at`、`judge_key`、`reused_from`、`rerun`、`not_before`；去掉"每 (选手, 题目) 一条"的唯一约束（迁移 020 重建表）。
  * `auto` / `manual` 评最新版：领任务时才选代码（规则同上一节）。结果写 `result.json`；乱序（旧版本的任务比新版本晚交回）时只进历史。
  * `rejudge` 评指定的历史版本：排任务时钉死版本，结果只进历史与 timeline，教师「采用此版本成绩」才写 `result.json`（手工录入的格子拒绝采用）。不受"场次还在进行"的限制。
* **复用**：`judge_key` = (数据包 sha, 评测配置, 代码 sha) 的指纹；已有同 key 的完成结果（`SE` 除外）时领任务就地抄结果、不占评测机，任务标 `reused_from`。教师勾「不复用已有结果」（`rerun`）时真跑。
* **自动入队**：场次 `auto_judge = off | on_change | smart`（默认 `off`；`smart` 见 §12.12）、`auto_judge_delay`（默认 60 秒）。开启后上传某题新版本 → 对 (选手, 题目) 排 `auto` 任务，带 `not_before = 收到时刻 + 去抖`；同一对已有排队中的自动任务时只把它往后推（领任务时才选代码，评的总是最后一版）。只对自带评测且有数据包、语言后缀认得的题；场次进行中也排（这是它的意义）。
* **timeline**：`GET …/judge/versions?player_id=&problem=` 每版带最近一次评测；界面画「版本 × 分数」阶梯线（内联 SVG），点的颜色是状态、状态变化处标状态字，矩阵采用的那一版加外环，未评的版本画在基线下方。
* **进度**：排队 / 在评全数；完成 / 失败只数每 (选手, 题目) 最新的那条。

### 12.10 评测机管理：状态、远程动作与一键接入

* **一个循环**：评测机每个轮询间隔 `POST /api/v1/judge/poll` 一次，自报 `version` / `backend` / `isolated` / `hostname` / `platform` / `slots` / `poll_seconds`，有空位时 `want = 1` 顺带领一个任务；评测中照样 poll（`want = 0`）。服务端记进 `judge_worker` 的新列（迁移 023），在线 = 最近联系不超过 3 × 轮询间隔（下限 15 秒）。正在评什么不靠自报，直接看任务表里租约在它名下的 `running` 任务。旧评测机只调 `/lease`：照常领任务、算心跳，自报列为空，收不到命令。
* **管理动作**：暂停（`enabled = false`：领不到新任务、照常心跳、手上的评完照常交）；停止 / 重启记进 `desired`，在下一次 poll 的响应 `command` 里带走并复位成 `run`（一次性，评测机日后被重新拉起不会再停一次）。评测机收到后不再领任务，评完手上的：停止 = 退出码 0（systemd 的 `Restart=on-failure` 不再拉起）；重启 = 以同样的参数重启自己（Linux 原地 exec，进程号不变；Windows 起新进程后退出）。只对在线、报过状态的评测机下命令（否则 409），免得命令在库里放到下次开机才生效。吊销 = 删行，它名下在评的任务**立刻**退回队列（令牌已失效，交不回结果，不必等 15 分钟租约）。
* **并发**：`--slots N` 起 N 个评测线程，各领各的任务；isolate 的 box 编号从 `--box-id` 起依次占用。只有 isolate 可以大于 1（每个 box 一个账号）；rlimit / winjob 共用一个运行账号，并发任务能互相读写工作目录，启动即拒绝。
  * **管理端改并发数**：`PATCH /admin/judge-workers/{id}` 带 `slots`（1–64），存进 `judge_worker.desired_slots`（迁移 024），随每次 poll 响应的 `slots` 下发。评测机发现与自己的 `--slots` 不同时不再领新任务，评完手上的、把命令行里的 `--slots` 换成新值重启（复用上面的重启路径；pyz 与 `-m` 两种启动方式都改 argv）。只有报了 `features: ["slots"]` 的评测机能改（旧评测机 409 `judge_worker_unsupported`）；后端不是 isolate 的只能是 1（409 `judge_worker_slots`），服务端也不给它下发大于 1 的值。本机评测机直接以设定的并发数启动。
* **「评测服务」开关**：全局一个（`runtime_setting` 里的 `judge_service`，没有这一行 = 开），`GET / PUT /admin/judge-service`。关 = 所有评测机都领不到新任务（`/poll` 与 `/lease` 都遵守，poll 响应 `service = false`），手上的照常评完交回，排队的任务留着；自动入队照常入队、只是不发出去，打开后按原顺序接着发。
* **一键接入**：服务端（只在管理端口）发 `GET /judge/worker.pyz`、`/judge/install.sh`、`/judge/install.ps1`。pyz 把 `syncoj_server/judging/`（只用标准库，测试用 AST 守着）连同只有版本号的包根打成 zipapp，按「版本号 + 源码指纹」缓存在 `<数据目录>/cache/judge-worker/`；另一台机器只要有 Python 3.8+ 与编译器，不必装服务端包。安装脚本嵌入请求的 Host 头（形状不安全就不嵌）与 pyz 的 sha256，令牌由教师在命令行给。签发令牌的响应直接带 Linux / Windows 两条一行命令。
  * Linux：`/opt/syncoj-judge/syncoj-judge.pyz`、`/etc/syncoj-judge/token`（0600）、systemd 单元 `syncoj-judge`。有 isolate 用 isolate、服务以专用账号 `syncoj-judge` 运行；没有 isolate 先询问（`--yes` 跳过），用 rlimit、服务以 root 运行、选手程序换成 `syncoj-judge-run` 跑。幂等，重跑 = 换令牌 / 升级。
  * Windows：`%ProgramData%\SyncOJ\judge\`（不继承上级权限），令牌只给 SYSTEM、Administrators 读写与 LOCAL SERVICE 读（按 SID 授权）并标 `NO_READ_UP`，`cache\` / `boxes\` / 日志给 LOCAL SERVICE 修改；计划任务「SyncOJ Judge」开机启动、以 LOCAL SERVICE 运行（不是 SYSTEM）、失败每分钟重启，`--backend winjob`。检查 Python ≥ 3.8 与 g++ 是否在**系统** PATH、LOCAL SERVICE 能否读到（Python 装在用户目录下时提示改为「为所有用户安装」）。脚本是 UTF-8 BOM（PowerShell 5.1 无 BOM 按 ANSI 读），所以一行命令先下载到文件再执行，而不是 `iwr | iex`。
* **本机评测机**：Linux 服务端可在界面一键启停一个子进程评测机（`python -m syncoj_server.judging.worker`，连 `127.0.0.1:<管理端口>`）。它是 `judge_worker` 里 `is_local = 1` 的那一行，每次启动换一枚令牌（写 `<数据目录>/judge-local/token`，0600）；有 isolate 用 isolate，否则 rlimit（服务端是 root 时选手程序以 `SYNCOJ_JUDGE_LOCAL_RUN_AS`，默认 `nobody` 运行）。日志 `<数据目录>/logs/judge-local.log`。服务端退出时一并停掉（SIGTERM，10 秒后 SIGKILL）；不随服务端启动自动拉起。非 Linux 服务端不提供。

### 12.11 风险标注与筛选

* **风险事件**：runner 每次运行选手程序后检查工作目录与沙箱的报告，记进结果的 `risk_events`（`{code, case, message}`，预检拦截还带 `line` / `rule` / `excerpt`）：`workdir_symlink`（符号链接、FIFO、设备、套接字、硬链接计数 > 1 的文件）、`tamper_checker`（出现 `chk` / `chk.exe` / `checker` 同名文件，或 `prog` 运行前后 sha256 不同）、`escaped_process`（rlimit 下运行结束后按账号 + 启动时刻清掉的、不在本次进程组里的进程；winjob 的 Job 拒绝建子进程不发通知、要另开完成端口才拿得到，不做；isolate 自己收拾）、`precheck_blocked`（源码预检的每个拦截项）。**只标注，不改变判分**（拦截照旧 CE）。
* **分级放服务端一处**（`judging/precheck.py` 的 `level_of`）：风险 = `risk_events` 全部 + 拦截类规则 code（题目配成 `warn` 时它们出现在预警里，级别不降）；疑似违规 = 预警类规则。明细接口给每条带 `level`，前端不维护 code 表。
* **计数入库**：`judge_task` / `judge_run` 加 `warning_count` / `risk_count`（迁移 024，老数据解析一次 `result_json` / `raw_json` 回填），交结果、扫描入库时算好；列表据此筛选（`flag = any | warning | risk`），不在每次请求里解析结果文档。
* **界面**：评测记录（成绩页、评测页）可筛「有疑似违规 / 有风险」，行上黄 / 红左边框与计数（风险优先）；成绩矩阵格子最新结果有风险加红细边框、有疑似违规加黄细边框，分数颜色不变；明细对话框风险条目在上（红框）、疑似违规在下（黄框），「源码」折叠区按行显示该次评测的代码（`GET …/judge/tasks/{id}/source`，≤ 1 MB），标注的行高亮，点条目滚到对应行。

### 12.12 智能自动评测

`auto_judge = smart`：判断某人某题「可能已经写完」才排评测，考试中评测机不必为每次保存跑一遍。代码：`services/smart_judge.py`（判定是纯函数 `decide`）、`tasks/smart_judge.py`。

* **触发信号**（满足任一即对该 (选手, 题目) 排一次；阈值按场次配，括号内为默认值）：

  | 信号 `reason` | 界面 | 条件 |
  |---|---|---|
  | `moved_on` | 换题 | 该题最新版本已稳定 ≥ `smart_stable_seconds`（180），且此后该选手别的题（含不评测的题）收到过新版本 |
  | `long_stable` | 空闲 | 该题最新版本已稳定 ≥ `smart_idle_seconds`（600） |
  | `near_end` | 将结束 | 距 `ends_at` ≤ `smart_near_end_seconds`（900；0 = 不用）：每对未评的最新版本各排一次，之后再改按冷却 |
  | `offline` | 掉线 | 选手有这一场的机器、且全都离线（内存注册表；服务端刚重启、注册表里没有的不算），最新版本未评 |
  | `contest_end` | 已结束 | 场次已结束（状态 `closed` 或到了 `ends_at`）：全员每题未评的最新版本各排一次。做完记 `contest.smart_end_done_at`，不再重复；场次重新开放时清空 |

* **未评**：该 (选手, 题目) 最新版本（按当前题目模式归题、语言后缀认得、题目自带评测且有数据包）没有任何任务评过它或为它排过（`judge_task.source_version_id` / `target_version_id`，任意状态，失败的也算，免得反复排），且没有还在排队的「评最新版」任务。
* **减压**：相同 `judge_key` 照旧复用结果；同一对两次换题 / 空闲至少隔 `smart_cooldown_seconds`（300）；全局排队任务数 > 在线且没暂停的评测机 `slots` 之和（至少 1）× `smart_queue_factor`（20）时只接受将结束 / 掉线 / 已结束，其余下一轮再判。
* **优先级**：`judge_task.priority`（迁移 025，老任务回填手动 / 重测 100、自动 10），领任务按它降序、同级按 id：手动 100 > 已结束 60 > 掉线 50 > 将结束 40 > 换题 30 > 空闲 20 > 收到新版本即评 10。智能任务 `trigger = smart`，信号记在 `reason`。
* **考后结果进矩阵**：后台成绩扫描平时跳过 `closed` 场次；评测机交回结果时把场次记进 `ctx.judge_dirty_contests`，下一轮扫描照扫它一次（已归档仍不扫）。
* **节奏**：后台每 `smart_tick_seconds`（服务端配置，默认 30 秒）评估一轮，每场一个短事务；归档场次不评；评测服务关着照常入队、只是不发。
* **界面**：「任务」卡片的自动评测三态（关闭 / 每次改动 / 智能），选智能时展开阈值（稳定 / 空闲 / 结束前 / 冷却 / 队列倍数）；评测记录的触发方式显示与筛选带上各信号。
