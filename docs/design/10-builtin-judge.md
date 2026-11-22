## 12. 系统自带沙箱评测

> **状态**：已实现（2026-11），与外部评测器对接**并存、按题选择**。用法见 `reference/judge-result.md` 第 6 节，取舍见 ADR-0016。
> 代码：`server/syncoj_server/judging/`（`testdata` / `sandbox` / `runner` / `worker`，只用标准库、兼容 3.8）、`services/builtin_judge.py`、`api/judge.py`、`api/admin_judge.py`。

### 12.1 目标与边界

* **要**：教师对一个场次（或一道题、一个选手）发起评测，按 NOI 惯例编译、运行、比对，成绩进成绩矩阵；能重测；能看每个测试点。
* **不要**：在线 OJ 那一套（题库、提交队列、实时排行榜）；交互题与通信题；Windows 评测（沙箱只做 Linux）。
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

* worker（`syncoj-judge --server … --token-file … --backend isolate|rlimit`）是**独立进程、独立账号**，经 HTTP 领任务（服务端持有全部状态），碰不到 `.key/`、数据库与 `source/` 目录，只拿到任务所需的代码与数据包。可与服务端同机；条件允许时放另一台（评测时 CPU 满载，避免与收代码的服务端争用）。
* worker 令牌在「评测」→「评测机」签发，只存哈希（`judge_worker`），只授权评测相关端点；吊销 = 删行（需输评测机名称确认）。状态上报、远程动作与一键安装见 §12.10。
* 任务表 `judge_task(contest, player, problem, status, attempt, worker, lease_until, source_path, source_sha256, outcome, error, …)`：状态 `queued → running → done / failed`；领任务带 15 分钟租约（worker 崩了租约过期自动回队列），最多尝试 3 次。任务即历史（§12.9）：重测只替换同一对**还在排队**的任务，跑完的留着。没交代码的选手直接 `done`、不写 `result.json`（矩阵照旧显示未交）。
* 数据包按 sha256 缓存在 worker 的 `--cache-dir`，同题几十个选手只下载一次。

### 12.4 沙箱：可插拔后端，按安全程度排

| 后端 | 实现 | 前提 | 定位 |
|---|---|---|---|
| `isolate` | 调用 IOI 的 isolate（命名空间 + 可选 cgroup） | 机器装了 isolate（setuid root）；同机多个 worker 用不同 `--box-id` | **推荐**，正式考试用 |
| `rlimit` | `fork` + `setrlimit`（CPU / 内存 / 输出 / 进程数）+ 另一账号运行 + 每点前清空工作目录 | 无；worker 以 root 跑时必须给 `--run-as`，否则拒绝 | **只给开发与自测**，无网络与文件系统隔离，界面标红"不隔离" |

* isolate 是**调用**不是链接（它是 GPL-2），不影响本仓库许可证。
* 否决自研 `unshare` + seccomp：安全边界，自研维护成本与出错代价过高；`rlimit` 覆盖无更好选择时的情形。
* 否决 `systemd-run` 后端：统计单次运行的 CPU 时间与峰值内存需绕路；有 root 的机器直接装 isolate。
* 计时：**CPU 时间**判 TLE；墙钟只作硬上限（时限的 2 倍 + 1 秒），防 sleep 卡死。内存取峰值常驻集，isolate 开 `--cgroups` 时取 cgroup 峰值。`rlimit` 后端经一层 shim 取选手程序自己的 rusage（Linux 的 `ru_maxrss` 跨 exec 保留，直接从 worker fork 会把 worker 的内存算到选手头上）。
* 一个 worker 默认一次评一个任务；`--slots N` 并发评 N 个（各占一个 box，§12.10），或起多个 worker（各用不同 `--box-id`）。

### 12.5 评测语义（NOI 惯例）

* **语言**：C++ `g++ -O2 -std=c++14 -lm`（`.cpp/.cc/.cxx`，编译参数可按题改）；C `gcc -O2 -std=c99 -lm`。编译也在沙箱里跑（防模板递归、`#include </dev/random>` 这类编译器炸弹），限 CPU 10 秒、墙钟 30 秒、输出 64 MB。
* **IO**：按题配置文件 IO（默认读 `<题目>.in` 写 `<题目>.out`，文件名可按题改）或标准 IO（默认）；文件 IO 下没有输出文件判 WA 并写明原因（多半忘了 `freopen`）。默认时限 1000 ms、内存 256 MB。
* **数据包**：每题一个 zip（与下发资产同一套上传与 blob 存储），**上传时校验结构**。测试点 `<名字>.in` + `<名字>.ans`（或 `.out`），可在任意子目录，名字唯一、按自然序；可选 `config.json` 写每点分值与子任务（子任务全 AC 才得分，捆绑）；未写分值时 100 分按点均分。LemonLime 的数据目录直接打 zip 即可；整场题目配置的导入导出见 §12.8。
* **比对**：默认逐行比较，忽略行末空白与文末空行；数据包含 `checker.cpp`（连同 `testlib.h`）时用它做 special judge，testlib 风格 `checker <in> <out> <ans>`，退出码 0 为通过，也在沙箱里编译运行。
* **源码预检**：编译前对选手源码做文本检查（先按 C 翻译阶段规范化再匹配）；`#pragma`、内联汇编、系统调用、禁用头文件、题目以外的文件等直接拦截记 CE（不编译），疑似混淆只预警、写进结果的 `precheck_warnings`。规则与按题开关见 `docs/reference/judge-result.md` 第 7 节。
* 每个测试点前清空工作目录，程序不能跨点留东西。
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
* **自动入队**：场次 `auto_judge = off | on_change`（默认 `off`）、`auto_judge_delay`（默认 60 秒）。开启后上传某题新版本 → 对 (选手, 题目) 排 `auto` 任务，带 `not_before = 收到时刻 + 去抖`；同一对已有排队中的自动任务时只把它往后推（领任务时才选代码，评的总是最后一版）。只对自带评测且有数据包、语言后缀认得的题；场次进行中也排（这是它的意义）。
* **timeline**：`GET …/judge/versions?player_id=&problem=` 每版带最近一次评测；界面画「版本 × 分数」阶梯线（内联 SVG），点的颜色是状态、状态变化处标状态字，矩阵采用的那一版加外环，未评的版本画在基线下方。
* **进度**：排队 / 在评全数；完成 / 失败只数每 (选手, 题目) 最新的那条。

### 12.10 评测机管理：状态、远程动作与一键接入

* **一个循环**：评测机每个轮询间隔 `POST /api/v1/judge/poll` 一次，自报 `version` / `backend` / `isolated` / `hostname` / `platform` / `slots` / `poll_seconds`，有空位时 `want = 1` 顺带领一个任务；评测中照样 poll（`want = 0`）。服务端记进 `judge_worker` 的新列（迁移 023），在线 = 最近联系不超过 3 × 轮询间隔（下限 15 秒）。正在评什么不靠自报，直接看任务表里租约在它名下的 `running` 任务。旧评测机只调 `/lease`：照常领任务、算心跳，自报列为空，收不到命令。
* **管理动作**：暂停（`enabled = false`：领不到新任务、照常心跳、手上的评完照常交）；停止 / 重启记进 `desired`，在下一次 poll 的响应 `command` 里带走并复位成 `run`（一次性，评测机日后被重新拉起不会再停一次）。评测机收到后不再领任务，评完手上的：停止 = 退出码 0（systemd 的 `Restart=on-failure` 不再拉起）；重启 = 以同样的参数重启自己（Linux 原地 exec，进程号不变；Windows 起新进程后退出）。只对在线、报过状态的评测机下命令（否则 409），免得命令在库里放到下次开机才生效。吊销 = 删行，它名下在评的任务**立刻**退回队列（令牌已失效，交不回结果，不必等 15 分钟租约）。
* **并发**：`--slots N` 起 N 个评测线程，各领各的任务；isolate 的 box 编号从 `--box-id` 起依次占用。
* **一键接入**：服务端（只在管理端口）发 `GET /judge/worker.pyz`、`/judge/install.sh`、`/judge/install.ps1`。pyz 把 `syncoj_server/judging/`（只用标准库，测试用 AST 守着）连同只有版本号的包根打成 zipapp，按「版本号 + 源码指纹」缓存在 `<数据目录>/cache/judge-worker/`；另一台机器只要有 Python 3.8+ 与编译器，不必装服务端包。安装脚本嵌入请求的 Host 头（形状不安全就不嵌）与 pyz 的 sha256，令牌由教师在命令行给。签发令牌的响应直接带 Linux / Windows 两条一行命令。
  * Linux：`/opt/syncoj-judge/syncoj-judge.pyz`、`/etc/syncoj-judge/token`（0600）、systemd 单元 `syncoj-judge`。有 isolate 用 isolate、服务以专用账号 `syncoj-judge` 运行；没有 isolate 先询问（`--yes` 跳过），用 rlimit、服务以 root 运行、选手程序换成 `syncoj-judge-run` 跑。幂等，重跑 = 换令牌 / 升级。
  * Windows：`%ProgramData%\SyncOJ\judge\`，令牌只给 SYSTEM 与 Administrators（按 SID 授权），计划任务「SyncOJ Judge」开机启动、以 SYSTEM 运行、失败每分钟重启，`--backend winjob`。检查 Python ≥ 3.8 与 g++ 是否在**系统** PATH。脚本是 UTF-8 BOM（PowerShell 5.1 无 BOM 按 ANSI 读），所以一行命令先下载到文件再执行，而不是 `iwr | iex`。
* **本机评测机**：Linux 服务端可在界面一键启停一个子进程评测机（`python -m syncoj_server.judging.worker`，连 `127.0.0.1:<管理端口>`）。它是 `judge_worker` 里 `is_local = 1` 的那一行，每次启动换一枚令牌（写 `<数据目录>/judge-local/token`，0600）；有 isolate 用 isolate，否则 rlimit（服务端是 root 时选手程序以 `SYNCOJ_JUDGE_LOCAL_RUN_AS`，默认 `nobody` 运行）。日志 `<数据目录>/logs/judge-local.log`。服务端退出时一并停掉（SIGTERM，10 秒后 SIGKILL）；不随服务端启动自动拉起。非 Linux 服务端不提供。
