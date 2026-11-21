## 12. 系统自带沙箱评测

> **状态**：已实现（2026-11），用法见 `reference/judge-result.md` 第 6 节，取舍见 ADR-0016。
> 与外部评测器对接**并存**，按题选择。
>
> 与下文设计的出入：沙箱后端实现了 `isolate` 与 `rlimit` 两个，`systemd-run` 没做（它统计
> 单次运行的 CPU 时间与峰值内存要绕一圈，而有 root 的机器装 isolate 更直接）；LemonLime
> 工程文件导入没做（数据包直接用它的数据目录打 zip 即可）；`rlimit` 后端经一层小 shim 取
> 选手程序自己的 rusage（Linux 的 `ru_maxrss` 跨 exec 保留，直接从 worker fork 会把 worker
> 的内存算到选手头上）。

### 12.1 目标与边界

* **要**：教师在管理界面对一个场次（或一道题、一个选手）点"评测"，服务端按 NOI 惯例
  编译、运行、比对，成绩直接进成绩矩阵；能重测；能看每个测试点的结果。
* **不要**：在线 OJ 那一套（题库、提交队列、排行榜实时刷新）；交互题与通信题（第一版）；
  Windows 上的评测（沙箱只做 Linux）。
* **硬约束**：选手代码是**不可信代码**。服务端进程手里有数据库、发布私钥（`.key/`）、统一
  注册密钥 —— 评测**绝不能在服务端进程里、也不能以服务端账号**跑选手程序。

### 12.2 关键决定：产物写进现有的 `judge_result/`

自带评测器**不直接写 `judge_run` 表**，而是像 LemonLime 一样往
`judge_result/<场次>/<选手>/<题目>/result.json` 写结果，由现有的 `ResultScanner` 收进来。

这样什么都不用改：成绩矩阵、`ok/unparsed/manual/missing` 四态、"手工录入永不被自动扫描
覆盖"、导出 CSV，全部原样生效；自带评测与外部评测器可以在同一场里混用（某道题用 Arbiter 测）。
`result.json` 用 `judge-result.md` 已支持的 JSON 形状，外加一个 `cases` 数组放每个测试点的
细节（扫描器只读 `score` / `max_score` / `status`，多出来的键原样进 `raw_json`，界面可展开）。

### 12.3 进程与信任边界

```
syncoj-server（服务端账号）         syncoj-judge worker（专用账号 syncoj-judge，可在另一台机器）
  评测任务表 judge_task  ──HTTP──>  领任务（带 worker 令牌，只能领任务、取代码与数据、交结果）
  source/、测试数据       <──HTTP──  编译 → 沙箱运行 → 比对 → 交回 result.json
  ResultScanner 收结果
```

* worker 是**独立进程、独立账号**，经 HTTP 领任务（与 Agent 同一种"服务端持有全部状态"的
  形状），读不到 `.key/`、数据库与 `source/` 目录本身。默认与服务端同机跑，机房条件允许时
  放到另一台机器上（评测时 CPU 跑满，不该和收代码的服务端抢）。
* worker 令牌只授权评测相关的几个端点，与管理员令牌、Agent 凭据都是不同的东西。
* 任务表：`judge_task(contest, player, problem, status, attempt, lease_until, worker)`，
  领任务是带租约的（worker 崩了租约过期自动回到队列），同一 (选手, 题目) 只保留最新一条。

### 12.4 沙箱：可插拔后端，按安全程度排

| 后端 | 怎么实现 | 前提 | 定位 |
|---|---|---|---|
| `isolate` | 调用 IOI 的 isolate（命名空间 + cgroup，竞赛圈事实标准） | 机器上装了 isolate（setuid root） | **推荐**，正式考试用 |
| `systemd-run` | `systemd-run --scope -p MemoryMax= -p CPUQuota= -p PrivateNetwork=yes -p DynamicUser=yes …` | worker 有权起临时单元 | 没装 isolate 时的次选 |
| `rlimit` | `fork` + `setrlimit`（CPU/AS/FSIZE/NPROC）+ 独立 uid + 空工作目录 | 无 | **只给开发与自测**，界面上标红"不隔离" |

* 用外部 isolate 是**调用**而不是链接（它是 GPL-2），不影响本仓库的许可证。
* 自己用 ctypes 拼 `unshare` + seccomp 也做得到，但这是安全边界，自研的维护成本与出错代价都
  不值得；`rlimit` 后端已经覆盖"没有更好的选择"那一档。
* 计时：**CPU 时间**（`wait4` 的 rusage / cgroup `cpu.stat`）判 TLE，墙钟只作硬上限（CPU 时间
  的 2 倍 + 1 秒），防止 sleep 卡死。内存：cgroup `memory.peak`；`rlimit` 后端退回
  `ru_maxrss`（偏小，标注"不精确"）。
* 公平性：worker 并发数默认 `CPU 核数 - 1`，每个评测进程绑一个核（`sched_setaffinity`），
  同一题的所有选手在同一台机器上测，避免机器之间速度差异进入成绩。

### 12.5 评测语义（NOI 惯例）

* **语言**：第一版只做 C++（`g++ -O2 -std=c++14 -static -lm`，参数按题可改，因为 NOI Linux
  2.0 的官方参数就是这一档）；C 与 Python 留接口。编译也在沙箱里跑（编译器炸弹：模板递归、
  `#include </dev/random>`），限时 10 秒、限输出。
* **文件 IO**：NOI 题目默认读 `<题目>.in` 写 `<题目>.out`，按题配置"文件 IO / 标准 IO"；
  选手忘了 `freopen` 时"没有输出文件"判错，原因写清楚。
* **数据**：每道题一个数据包（与下发资产同一套上传与 blob 存储），约定 `<名字>.in` /
  `<名字>.ans`，可选 `config.json` 写子任务（捆绑测试取最小值）、每点分值、时空限制。
  LemonLime 的工程文件能导入的话最好（教师手上现成的题都是那个格式）—— 列为待评估。
* **比对**：默认忽略行末空格与文末空行的逐行比较；可选 testlib 风格的 special judge
  （checker 也在沙箱里编译运行，接口 `checker <in> <out> <ans>`，读退出码与 stderr）。
* **结果状态**：AC / WA / TLE / MLE / RE（带信号）/ OLE / CE（编译信息截断存）/ SE（评测系统
  自己的错，**不算选手的分**，界面显眼地提示重测）。

### 12.6 界面与触发

* 题目页：上传数据包、设时空限制与 IO 方式、选择"自带评测 / 外部评测器"。
* 成绩页：按场次 / 题目 / 选手"评测"与"重测"；进度用已有的进度接口形状
  （`/scores/progress` 那一类聚合接口）；单元格展开看每个测试点。
* 评测用的是**当前 `source/` 里的代码**；开考期间不允许点（防止把考到一半的代码当成绩），
  场次结束后才可用，或者教师显式确认。

### 12.7 落地顺序（如果做）

1. 数据包与题目评测配置（模型 + 迁移 + 管理接口），不评测，只校验数据包结构。
2. worker 骨架 + `rlimit` 后端 + C++ 编译与比对 + 写 `result.json`；端到端测试：一道 A+B、
   一个 TLE、一个 RE、一个 CE、一个文件 IO 忘写 `freopen`。
3. `isolate` 后端（CI 里装 isolate 跑同一组用例）与 `systemd-run` 后端。
4. 任务表、租约、重测、进度与界面。
5. special judge、子任务、LemonLime 工程导入。

每一步都先写 ADR 记取舍（尤其是"为什么不自研 seccomp 沙箱"和"为什么经 `judge_result/`
而不直接写表"）。
