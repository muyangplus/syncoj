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
* worker 令牌在「评测」→「评测机」签发，只存哈希（`judge_worker`），只授权评测相关端点；吊销 = 删行（需输评测机名称确认）。
* 任务表 `judge_task(contest, player, problem, status, attempt, worker, lease_until, source_path, source_sha256, outcome, error)`：状态 `queued → running → done / failed`；领任务带 15 分钟租约（worker 崩了租约过期自动回队列），最多尝试 3 次；同一 (选手, 题目) 只留最新一条，重测 = 删旧排新。没交代码的选手直接 `done`、不写 `result.json`（矩阵照旧显示未交）。
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
* 一个 worker 一次评一个任务；要并发就起多个 worker（各用不同 box）。

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
* 「评测」页「任务」卡片：全部自带评测题目或单题「评测 / 重测」，失败行「重测本题」；进度沿用已有聚合进度接口形状。顶栏「快捷操作」→「评测全部题目」等价。
* 「评测」页「评测机」卡片：签发与吊销 worker 令牌。
* 「成绩」：格子点开看每个测试点明细，可重测。
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
