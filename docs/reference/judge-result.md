# 评测器对接：成绩怎么进 SyncOJ

两条路，同一场内可按题混用：

| 方式 | 触发 | 结果来源 |
|---|---|---|
| 外部评测器（默认） | 教师在 LemonLime / Arbiter 界面里评测 | 评测器写入 `judge_result/` 的文件（第 1–5 节） |
| 自带评测 | 题目设为"自带评测"并上传数据包 | SyncOJ 评测机写入同一目录的 `result.json`，走同一套扫描（第 6 节） |

→ 不接管外部评测器的触发：见 `docs/design/00-decisions.md`；自带评测设计：见 `docs/design/10-builtin-judge.md`、[ADR-0016](../decisions/ADR-0016-builtin-judge.md)。

## 1. 唯一要配的东西：输出目录

```
<数据目录>/judge_result/<场次 slug>/<选手编号>/<题目标识>/
```

例：场次 `mock-1`、选手 `S001`、题目 `p1`：

```
judge_result/mock-1/S001/p1/
├── result.json
└── ...其他文件
```

- `<场次 slug>`：创建场次时的短标识。
- `<选手编号>`：与导入的 `player_no` 完全一致。
- `<题目标识>`：自定（`p1` / `t1` / `A`），即成绩矩阵列名；未在题目清单登记的显示为 `declared=false`。
- 题目目录下文件格式不限（第 2 节）。

扫描：

- 服务端每 10 秒扫描一次（`judge_scan_interval`）。
- 每个（选手, 题目）目录最多看 40 个文件；按后缀 `.json .xml .txt .result .log .csv .ini .out .res`、其余后缀的顺序，同序按修改时间新到旧尝试。
- 跳过：空文件、超过 4 MiB 的文件、含 NUL 字节的二进制文件。
- 编码依次尝试 UTF-8、GB18030、UTF-16、Latin-1；去掉 BOM。

手工触发：

```bash
curl -X POST -H "Authorization: Bearer <管理员令牌>" \
  http://127.0.0.1:8000/api/v1/admin/contests/1/judge/rescan
```

响应字段：`parsed`、`unparsed`、`unchanged`、`skipped`、`manual`（因手工录入保留的条数）、`errors`。

## 2. 支持的成绩格式

依次尝试 JSON → XML → key=value 文本，第一个解析成功的生效（`PARSERS` 顺序）。

### JSON

```json
{"score": 100, "max_score": 100, "status": "AC"}
```

键名规范化：去掉空白、`_`、`-`，转小写后比较。

| 含义 | 认得的键 |
|---|---|
| 得分 | `score` `totalscore` `total` `points` `got` `sum` `scoresum` `scoretotal` `得分` `分数` `总分` `成绩` |
| 满分 | `maxscore` `fullscore` `full` `max` `totalmax` `maxpoints` `满分` `总分上限` |
| 状态 | `status` `result` `verdict` `state` `outcome` `结果` `状态` `评测结果` |
| 测试点容器 | `cases` `case` `tasks` `task` `results` `result` `testcases` `testcase` `tests` `subtasks` `subtask` `details` `case_results` `caseresults` `测试点` `子任务` |

示例（均可识别）：

```json
{"total_score": 88}    {"totalScore": 88}    {"总分": 88}    {"得分": 88}
```

分值：

| 写法 | 读作 |
|---|---|
| `100`、`"100"`、`100.0` | 100 |
| `"100/100"` | 100 |
| `"100分"`、`"100 分"` | 100 |
| 布尔值、非整数小数 | 不认 |

逐测试点结果求和：

```json
{"cases": [
  {"score": 10, "max_score": 10, "status": "AC"},
  {"score": 0,  "max_score": 10, "status": "WA"}
]}
```

→ 10 分，状态 `WA`。顶层为数组时同样求和。

整题状态取最严重的测试点状态：

| 严重度 | 状态（规范化后） |
|---|---|
| 0 | `ac` `ok` `passed` `pass` `correct` `accepted` `skipped` `judging` `pending` 空 |
| 1 | `pe` `presentationerror` `format` |
| 2 | `wa` `wrong` `wronganswer` `fail` `failed` |
| 3 | `tle` `timeout` `timelimitexceeded` `mle` `memorylimitexceeded` `ole` `outputlimitexceeded` |
| 4 | `re` `runtimeerror` `crash` `abort` |
| 5 | `ce` `compileerror` `compilefailed` |
| 6 | `se` `systemerror` `internalerror` |
| 7 | 其他未知状态 |

### XML

```xml
<result score="100" max_score="100" status="AC"/>
```

```xml
<tests>
  <test score="10" status="AC"/>
  <test score="0"  status="WA"/>
</tests>
```

### key = value 文本

```
题目: p1
得分: 88
满分: 100
状态: WA
```

分隔符 `=` 与 `:` 均可。

## 3. 三条保守规则

### 规则一：看不懂就说看不懂，绝不猜

- 没有解析器认得的文件 → `parse_status="unparsed"`，界面标出，等待手工录入。

### 规则二：一个测试点读不出来，整题就不给分

```json
[{"score": 10}, {"verdict": "WA"}, {"score": 30}]
```

- 任一测试点读不出分数 → 整份拒绝解析（不返回 40）。

### 规则三：不会盲目全树搜数字

```json
{"meta": {"time_limit": 1000}, "config": {"score": 999}}
```

- 只沿已知结果容器键取分 → 上例拒绝解析。

## 4. 解析不出来怎么办

**A. 手工录入**

```bash
curl -X PUT -H "Authorization: Bearer <管理员令牌>" \
     -H "Content-Type: application/json" \
     -d '{"player_id": 1, "problem": "p1", "score": 88, "max_score": 100, "status": "WA"}' \
     http://127.0.0.1:8000/api/v1/admin/contests/1/judge/score
```

| 字段 | 类型 | 必填 | 限制 |
|---|---|---|---|
| `player_id` | int | 是 | |
| `problem` | string | 是 | 1–64 |
| `score` | int | 否 | ≥0 |
| `max_score` | int | 否 | ≥0 |
| `status` | string | 否 | ≤32 |

- 手工录入 → `parse_status="manual"`；自动扫描与"重新扫描"都不覆盖。
- 清除手工录入（恢复自动扫描），`confirm` = 该选手考号：

```bash
curl -X DELETE -H "Authorization: Bearer <管理员令牌>" \
  "http://127.0.0.1:8000/api/v1/admin/contests/1/judge/score?player_id=1&problem=p1&confirm=S001"
```

**B. 加一个解析器**

在 `server/syncoj_server/services/judge.py` 里加函数并注册到 `PARSERS`：

```python
def parse_lemonlime(text: str) -> Optional[ParsedScore]:
    # 按 LemonLime 的真实格式解析
    ...
    return ParsedScore(score=..., max_score=..., status=..., detail="LemonLime")

PARSERS = (
    ("lemonlime", parse_lemonlime),   # 放在最前，优先于通用解析器
    ("json", parse_json_text),
    ("xml", parse_xml_text),
    ("keyvalue", parse_keyvalue_text),
)
```

- 待补：LemonLime / Arbiter 真实输出样例与对应精确解析器。
- 私有二进制格式只能手工录入。

## 5. 成绩矩阵怎么看

```bash
curl -H "Authorization: Bearer <管理员令牌>" \
  http://127.0.0.1:8000/api/v1/admin/contests/1/scores
```

```json
{
  "contest_id": 1,
  "columns": [
    {"ident": "p1", "title": null, "declared": false},
    {"ident": "p2", "title": null, "declared": false}
  ],
  "rows": [
    {"player_id": 1, "player_no": "S001", "player_name": "张三", "total": 100,
     "cells": [
       {"problem": "p1", "score": 100, "max_score": 100, "status": "AC",
        "parse_status": "ok", "submitted": false, "detail": "json：JSON 直接字段",
        "updated_at": "2026-10-07T09:58:45Z"},
       {"problem": "p2", "score": null, "max_score": null, "status": null,
        "parse_status": "unparsed", "submitted": false,
        "detail": "没有解析器认得这个格式", "updated_at": "2026-10-07T09:58:45Z"}
     ]}
  ],
  "unparsed": 1,
  "complete": false
}
```

| 字段 | 含义 |
|---|---|
| `columns[].declared` | `false` = 只在评测结果里出现、未在题目清单登记 |
| `cells[].parse_status` | `ok` / `unparsed` / `manual` / `missing`（从未收到结果） |
| `cells[].submitted` | 该题是否收到过代码；`missing` 时区分"已交未评测"与"未交" |
| `unparsed` | 尚未解析出成绩的单元格数 |
| `complete` | `unparsed == 0` |

巡场进度（只数题目清单里登记的题）：`GET /api/v1/admin/contests/{id}/scores/progress`，每行 `submitted` / `pending` / `missing` / `total`。

## 6. 自带评测（§12）

### 题目怎么配

`PUT /api/v1/admin/problems/{id}/judge`：

| 字段 | 取值 | 默认 |
|---|---|---|
| `judge_mode` | `external` / `builtin` | `external` |
| `time_limit_ms` | 100–60000 | 1000 |
| `memory_limit_mb` | 16–4096 | 256 |
| `io_mode` | `stdio` / `file`（读输入文件、写输出文件） | `stdio` |
| `input_file` / `output_file` | 文件 IO 的文件名，≤64 字符：字母或数字开头，只含字母、数字、`.` `_` `-`；不能用评测机自己的文件名（`prog`、`main.cpp`、`chk`、`in.txt` 等）；两者生效名不能相同；空 = `<题目>.in` / `<题目>.out`。`PUT` 整体覆盖，不传即回到默认名 | 空 |
| `compile_flags` | ≤200 字符；只收 `-std=` / `-O` / `-D` / `-W` / `-f…`（不含 `-fplugin`）/ `-g` / `-static` / `-m…` | 空 |

数据包：`POST /api/v1/admin/problems/{id}/testdata` 上传，`DELETE` 同路径删除。

### 数据包

zip，结构同 LemonLime 数据目录，上传时校验：

```
1.in  1.ans          # 答案也可以叫 .out；可放在子目录里
2.in  2.ans
config.json          # 可选
checker.cpp          # 可选：special judge（testlib 风格），testlib.h 放同一目录
```

| 规则 | 值 |
|---|---|
| 大小上限 | 上传 512 MiB；解压后 2 GiB |
| 测试点数上限 | 500 |
| 测试点名 | 去掉扩展名的相对路径，唯一；按自然序排列 |
| 答案后缀 | `.ans` 优先于 `.out` |
| checker 文件名 | `checker.cpp` / `spj.cpp` / `chk.cpp`；调用 `checker <in> <out> <ans>`，退出码 0 为通过 |
| 结构错误 | `400 validation_error` |
| 超大 | `413 payload_too_large` |

`config.json`（全部可选）：

```json
{"cases": [{"name": "1", "score": 10}], "subtasks": [{"score": 40, "cases": ["1", "2"]}],
 "precheck": {"mode": "block", "disabled": ["file_access"]}}
```

- 未写分值：总分 100 按测试点平均，余数给最后几个点。
- `precheck`：源码预检配置（第 7 节）；不合法时上传报 `400 validation_error`。
- 子任务：全部 AC 才得该子任务分；子任务内的点不单独计分。

### 评测机

「评测」→「评测机」卡片 →「添加评测机」签发令牌（只显示一次；`POST /api/v1/admin/judge-workers`）。评测机只支持 Linux（Windows 上用 WSL2 或另一台 Linux）；装 `server/` 包后在评测机上以专用账号运行：

```bash
syncoj-judge --server http://10.0.0.5:8000 --token-file token --backend isolate
```

| 参数 | 含义 |
|---|---|
| `--server` | 服务端管理端口地址（必填） |
| `--token-file` | 令牌文件（必填） |
| `--backend` | `isolate`（默认，需先装 [isolate](https://github.com/ioi/isolate)）/ `rlimit`（只用 `setrlimit`，不隔离网络与文件系统，仅开发自测；结果 `isolated: false`） |
| `--box-id` | isolate box 编号，同机多 worker 各不相同（默认 0） |
| `--cgroups` | isolate 用 cgroup 记内存 |
| `--isolate` | isolate 可执行文件路径（默认 `isolate`） |
| `--run-as` | rlimit 后端以 root 运行时必填：选手程序的运行账号 |
| `--cache-dir` | 数据包缓存目录（默认 `~/.cache/syncoj-judge`） |
| `--poll` | 无任务时轮询间隔（默认 5 秒） |
| `--once` | 评一个任务后退出 |
| `--verbose` / `-v` | 调试日志 |

- 评测机令牌只能领任务、取本任务的代码与数据包、交结果。

### 怎么评、结果长什么样

| 项 | 规则 |
|---|---|
| 编译 | `.cpp` / `.cc` / `.cxx`：`g++ -O2 -std=c++14 -lm`（可按题改）；`.c`：`gcc -O2 -std=c99 -lm`；编译在沙箱内 |
| 运行 | 每个测试点前清空工作目录；TLE 按 CPU 时间判 |
| 比对 | 默认逐行比较，忽略行末空白（空格、`\t`、`\r`）与文末空行；有 checker 时用 checker |
| 文件 IO 无输出文件 | WA |
| 状态 | `AC` / `WA` / `TLE` / `MLE` / `RE` / `OLE` / `CE`；整题取最严重（`SE` > `CE` > `RE` > `MLE` > `TLE` > `OLE` > `WA` > `AC`） |

写出的 `result.json`（原子写入）：

```json
{"score": 70, "max_score": 100, "status": "WA", "judge": "syncoj-builtin", "isolated": true,
 "language": "cpp", "compile_message": "", "source_path": "ab/ab.cpp",
 "cases": [{"name": "1", "status": "AC", "score": 10, "time_ms": 3, "memory_kb": 1540, "message": ""}],
 "subtasks": [{"index": 1, "score": 0, "max_score": 40, "cases": ["2", "3"]}]}
```

- 有源码预检预警时多一个 `precheck_warnings`：`[{"code", "line", "message", "excerpt"}]`（第 7 节）；没有预警或旧评测机时不带。
- 扫描器只读 `score` / `max_score` / `status`，其余进 `raw_json`。
- 评测机自身故障（沙箱起不来、checker 编不过，记 `SE`）：不写成绩，任务退回队列；第 3 次仍失败记失败，进度里显示原因。
- 代码选择：只看仍在机器上、内容已存下的文件，按题目代码路径归题；多份候选时取文件名与题目同名者；仍无法区分 → 记失败。
- 未交代码：不写结果，矩阵显示"未交"。
- 场次进行中：`POST /api/v1/admin/contests/{id}/judge/builtin` 返回 `409 contest_in_progress`；带 `"force": true` 后执行。
- 重测 = 再次排任务：同一对还在排队的任务被替换，跑完的留作历史；新结果覆盖 `result.json`（若已写进去的是更新版本的结果则只进历史）；手工录入的成绩不被覆盖。
- 同一 (数据包, 评测配置, 代码) 评过（`SE` 除外）：直接复用结果，不占评测机；`rerun: true` 时真跑。
- 自带评测写的 `result.json` 多带 `task_id`、`source_version_id`（复用的还有 `reused_from`）。

排任务：`POST /api/v1/admin/contests/{id}/judge/builtin`，体 `{problem?, player_id?, force, version_id?, rerun}`，都不填 = 全场所有自带评测题 × 所有选手（评最新版）；`version_id` = 重测那个历史版本（结果只进历史，不受场次进行中限制）；响应 `{queued, problems}`。进度：`GET` 同路径，返回 `total` / `queued` / `running` / `done` / `failed` / `delayed`（去抖中）/ `failures[]`；完成 / 失败只数每 (选手, 题目) 最新那条。

### 版本、自动评测与 timeline（§12.9）

| 接口 | 说明 |
|---|---|
| `PUT /api/v1/admin/contests/{id}/judge/auto` | `{auto_judge: "off"\|"on_change", auto_judge_delay: 0–3600}`；场次列表回显这两项 |
| `GET /api/v1/admin/contests/{id}/judge/versions?player_id=&problem=` | 选手的代码版本（旧的在前，列表信封）：`id` / `rel_path` / `sha256` / `size` / `received_at` / `problem`（按当前题目模式）/ `current` / `tasks` / `judge{task_id, status, trigger, score, max_score, outcome, reused, applied, finished_at}` / `in_matrix` |
| `GET /api/v1/admin/contests/{id}/judge/tasks?player_id=&problem=&status=&trigger=` | 评测任务（新的在前，列表信封），含复用的 |
| `GET /api/v1/admin/contests/{id}/judge/tasks/{task_id}` | 任务与测试点明细 |
| `POST /api/v1/admin/contests/{id}/judge/tasks/{task_id}/adopt` | 采用此版本成绩：写成当前 `result.json` 并立即重扫；无结果 `409 judge_task_no_result`，手工录入的格子 `409 manual_score` |

### 评测机接口

均带 `Authorization: Bearer <评测机令牌>`：

| 接口 | 说明 |
|---|---|
| `POST /api/v1/judge/lease` | 领任务（租约 15 分钟，过期回队列）；无任务 `204`。返回 `id`、`contest_slug`、`player_no`、`problem`（`ident` / `time_limit_ms` / `memory_limit_mb` / `io_mode` / `input_file` / `output_file` / `compile_flags`；文件名为 `null` = 用 `<ident>.in` / `<ident>.out`）、`source`（`path` / `sha256`）、`testdata`（`sha256` / `name`）、`attempt` |
| `GET /api/v1/judge/tasks/{id}/blobs/{sha256}` | 取本任务的代码或数据包；其他内容 `404` |
| `POST /api/v1/judge/tasks/{id}/result` | `{score, max_score, status, detail}`（`score` / `max_score` 0–1000000，`status` 1–16 字符），写 `result.json`；`detail` 里认 `judge` / `isolated` / `language` / `compile_message` / `cases` / `subtasks` / `precheck_warnings`，其余丢弃 |
| `POST /api/v1/judge/tasks/{id}/fail` | `{message}`（≤2000），退回队列 |

评测机与服务端出自同一个包，需一起升级：旧评测机不认 `input_file` / `output_file`，仍按 `<ident>.in` / `<ident>.out` 读写，设了自定义文件名的题会被评错。

## 7. 源码预检

自带评测编译前检查选手源码（`server/syncoj_server/judging/precheck.py`，只用标准库，评测机与服务端共用）。

- 有拦截项：不编译，记 `CE`；`compile_message` 每项一行 `源码检查未通过：<说明>（第 N 行）`，下一行缩进给原文片段。
- 预警：照常评测，写进 `result.json` 的 `precheck_warnings`（`code` / `line` / `message` / `excerpt`，片段 ≤80 字符）；评测记录详情 `GET .../judge/runs/{run_id}` 同名字段。
- 只查选手代码，不查 checker。

### 规范化

匹配在规范化后的文本上做，按 C 翻译阶段：

1. 换行统一为 `\n`；三字符序列替换（`??=` → `#` 等 9 个）。
2. 反斜杠续行拼接（反斜杠与换行间允许空白）；行号记原始行。
3. 去注释（块注释换成一个空格，可把指令并成一行）；字符串、字符字面量、C++ 原始字符串 `R"x(...)x"` 的内容换成占位符另存，`1'000` 数字分隔符不当字符字面量。原始字符串内部不做 1、2 步，直接取原文。
4. 二合字母替换（`%:` → `#`、`%:%:` → `##`、`<:` `:>` `<%` `%>`；C++ 的 `<::` 除外）；代码里的 `\uXXXX` 解码。

`#include` 路径另在规范化前的原文指令上再查一遍。源码按 UTF-8 解码，失败按 GB18030。

### 拦截

| code | 拦截 |
|---|---|
| `pragma` | `#pragma`（含 `#/**/pragma`、续行、`%:pragma`、`??=pragma`）、`_Pragma`、`__pragma` |
| `attribute_optimize` | `__attribute__((...))` 里的 `optimize` / `target` / `target_clones`；`[[gnu::optimize]]` / `[[gnu::target]]` |
| `inline_asm` | `asm` / `__asm__` / `__asm` |
| `syscall` | `system` `popen` `fork` `vfork` `exec*` `posix_spawn` `ptrace` `syscall` `dlopen` `PROT_EXEC` `CreateProcess*` `ShellExecute*` `WinExec`（作为标识符出现即拦，成员访问除外）；`kill` `socket` `connect` `bind` 只拦调用，源码里有同名函数定义或赋值（lambda）时放行，`std::bind` 放行、`::connect(` 照拦；有 `##` 时，能由源码里的记号拼出上述名字也拦 |
| `forbidden_header` | `<windows.h>` `<winsock2.h>` `<process.h>` `<sys/socket.h>` `<sys/un.h>` `<netdb.h>` `<netinet/…>` `<arpa/…>` `<linux/…>` `<sys/ptrace.h>` `<sys/syscall.h>` `<dlfcn.h>` `<sys/mman.h>` `<sys/shm.h>` `<spawn.h>` `<sys/wait.h>` `<sys/prctl.h>` 等；`#include` / `#include_next` / `#import` 用绝对路径、盘符、`..`、含 `/dev/` `/proc/` `/etc/` `/sys/`，或用宏 |
| `file_access` | `fopen` `freopen` `open` `creat` 与 `ifstream` / `ofstream` / `fstream` 构造的文件名字面量含 `/` `\` `..` 或盘符，或不是 `<题目>.in` / `<题目>.out` / 题目配置的输入输出文件名；`NULL` 与 `/dev/stdin` `/dev/stdout` `/dev/stderr` 放行 |
| `bidi` | U+202A–202E、U+2066–2069、U+200E、U+200F、U+061C |

### 预警

| code | 条件（默认阈值） |
|---|---|
| `macro_heavy` | `#define` 超过 30 个；宏名是关键字或常用标准名（`if` `return` `main` `printf` …）；类型关键字（`#define int long long`）不算 |
| `token_paste` | `#define` 里有 `##` |
| `trigraph_digraph` | 用了三字符序列或二合字母 |
| `line_splice` | 续行拆开标识符 |
| `long_line` | 单行超过 500 字符；非空行 ≥10 时平均行长超过 200 |
| `big_table` | 字符串与数字字面量共超过 64 KiB；单个初始化列表超过 5000 个元素 |
| `encoded_blob` | 字符串里连续 base64 / 十六进制字符或 `\x` 转义超过 200 个 |
| `non_ascii_ident` | 代码（字面量、注释外）里有非 ASCII 标识符字符 |
| `huge_source` | 源码超过 256 KiB |
| `file_dynamic` | 打开文件的文件名不是字面量 |

每条规则最多报 20 处。

### 配置

数据包 `config.json` 的 `precheck`（可选，缺省按默认）：

| 字段 | 取值 | 默认 |
|---|---|---|
| `mode` | `block`：拦截项拦截、预警项预警；`warn`：拦截项降为预警；`off`：不查 | `block` |
| `disabled` | 关闭的规则 code 数组 | `[]` |

不认识的字段、模式或 code：上传时报错。
