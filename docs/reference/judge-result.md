# 评测器对接：成绩怎么进 SyncOJ

> 两条路，可以在同一场里按题混用：
>
> * **外部评测器**（默认）：教师在自己的 LemonLime / Arbiter 界面里点评测，SyncOJ 只把
>   评测器写出来的成绩收拢成一张表（第 1–5 节）。
> * **自带评测**（§12，第 6 节）：题目设成"自带评测"、传一个数据包，SyncOJ 的评测机编译
>   运行比对，结果写进**同一个目录**、走**同一套**扫描。
>
> 不接管触发评测的理由：LemonLime 和 Arbiter 都是 Qt GUI 程序，无头调用方式不稳定、
> 随版本变化，硬接会把 SyncOJ 的可靠性绑死在别人的界面实现上。

## 1. 唯一要配的东西：输出目录

把评测器的结果输出目录指到：

```
<数据目录>/judge_result/<场次 slug>/<选手编号>/<题目标识>/
```

例如场次 `mock-1`、选手 `S001`、题目 `p1`：

```
judge_result/mock-1/S001/p1/
├── result.json          ← 成绩从这里读
└── ...其他文件随便放
```

- `<场次 slug>` 就是管理界面里创建场次时那个短标识
- `<选手编号>` 必须与导入的 `player_no` **完全一致**
- `<题目标识>` 由你自己定（`p1` / `t1` / `A` 都行），它会成为成绩矩阵的列名
- 题目目录下的文件**放什么格式都可以**，见下一节

服务端每 10 秒扫一次，也可以随时手工触发：

```bash
curl -X POST -H "Authorization: Bearer <管理员令牌>" \
  http://127.0.0.1:8000/api/v1/admin/contests/1/judge/rescan
```

## 2. 支持的成绩格式

按 **JSON → XML → key=value 文本** 的顺序尝试，第一个解析成功的胜出。

### JSON

```json
{"score": 100, "max_score": 100, "status": "AC"}
```

键名大小写不敏感、`_` 与空格会被归一化，所以下面这些都认：

```json
{"total_score": 88}    {"totalScore": 88}    {"总分": 88}    {"得分": 88}
```

值可以带单位：`{"score": "100/100"}` 和 `{"score": "100分"}` 都会被读成 100。

**逐测试点结果自动求和**：

```json
{"cases": [
  {"score": 10, "max_score": 10, "status": "AC"},
  {"score": 0,  "max_score": 10, "status": "WA"}
]}
```

→ 得到 10 分，状态 `WA`（取**最严重**的状态 —— 否则一个错点会被后面的正确掩盖）。
顶层直接是数组时同样求和。

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

评测器导出的人读报表大多是这种形态：

```
题目: p1
得分: 88
满分: 100
状态: WA
```

`=` 和 `:` 都认。

## 3. 三条保守规则

无法预先覆盖所有评测器格式，所以解析器一律保守：

### 规则一：看不懂就说看不懂，绝不猜

没有解析器认得的文件会被标成 `unparsed`，在界面上明示，等你手工补录。
**不会**给出一个"猜测的分数" —— 错误分数比缺失分数严重得多，因为你会拿它当真。

### 规则二：一个测试点读不出来，整题就不给分

```json
[{"score": 10}, {"verdict": "WA"}, {"score": 30}]
```

中间那项读不出分数。此时**不会**返回 40（那是个偏低且错误的分数），
而是拒绝解析。宁可让你手工确认。

### 规则三：不会盲目全树搜数字

只在明确认得的位置取分。下面这种不会被误读：

```json
{"meta": {"time_limit": 1000}, "config": {"score": 999}}
```

→ 拒绝解析（`config.score` 不在已知的结果容器里）。盲目搜数字会把
`time_limit`、`memory_limit` 当成分数。

## 4. 解析不出来怎么办

管理界面会把这类单元格单独标出来。两条出路：

**A. 手工录入**（推荐先这么做，把考试跑完）

```bash
curl -X PUT -H "Authorization: Bearer <管理员令牌>" \
     -H "Content-Type: application/json" \
     -d '{"player_id": 1, "problem": "p1", "score": 88, "max_score": 100, "status": "WA"}' \
     http://127.0.0.1:8000/api/v1/admin/contests/1/judge/score
```

手工录入的成绩会标记成 `manual`，**自动扫描永远不覆盖它**，连点"重新扫描"也不覆盖。
要恢复自动扫描，得显式清除：

```bash
curl -X DELETE -H "Authorization: Bearer <管理员令牌>" \
  "http://127.0.0.1:8000/api/v1/admin/contests/1/judge/score?player_id=1&problem=p1"
```

**B. 加一个解析器**（永久解决）

在 `server/syncoj_server/services/judge.py` 里加一个函数，注册到 `PARSERS`
元组即可，不需要动扫描逻辑：

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

> **待补**：如果你能提供一份 LemonLime / Arbiter 的真实输出样例，就可以写一个
> 精确解析器，替掉现在的通用模板。目前通用模板覆盖的是"导出成 JSON/XML/文本报表"
> 这类常见用法；如果评测器写的是私有二进制格式，只能走手工录入。

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

## 6. 自带评测（§12）

### 题目怎么配

题目清单里「评测设置」：评测方式选"自带评测"，填时限（默认 1000 ms）、内存（默认 256 MB）、
输入输出方式（标准 IO，或 NOI 惯例的文件 IO：读 `<题目>.in`、写 `<题目>.out`）、可选的编译参数
（只收 `-std=` / `-O` / `-D` / `-W` / `-f…` / `-g` / `-static` / `-m…`），再上传数据包。

### 数据包

一个 zip，结构与 LemonLime 的数据目录一致，**上传时就校验**：

```
1.in  1.ans          # 答案也可以叫 .out；可以放在一层子目录里
2.in  2.ans
config.json          # 可选
checker.cpp          # 可选：special judge（testlib 风格），testlib.h 放同一目录
```

`config.json`（全部可选）：`{"cases": [{"name": "1", "score": 10}, …], "subtasks": [{"score": 40, "cases": ["1", "2"]}, …]}`。
没写分值时总分 100 按测试点平均分；子任务里**全部** AC 才得这个子任务的分。

### 评测机

评测**不在服务端进程里跑**。成绩页「自带评测」→「添加评测机」签发一个令牌（只显示一次），
然后在评测机上以**专用账号**运行：

```bash
syncoj-judge --server http://10.0.0.5:8000 --token-file token --backend isolate
```

* `--backend isolate`（推荐）：需要先装好 [isolate](https://github.com/ioi/isolate)；同一台机器上
  跑多个 worker 时用 `--box-id` 区分，有 cgroup 时加 `--cgroups` 让内存统计更准。
* `--backend rlimit`：只用 `setrlimit`，**不隔离网络与文件系统**，只给开发自测；以 root 运行时
  必须 `--run-as <普通账号>`。结果里 `isolated: false`。
* 评测机令牌只能领任务、取**这个任务**的代码与数据包、交结果，碰不到别的选手的代码。

### 怎么评、结果长什么样

编译 `g++ -O2 -std=c++14 -lm`（C 用 `gcc -O2 -std=c99`），每个测试点前清空工作目录；
默认逐行比较、忽略行末空白与文末空行；判 TLE 看 CPU 时间。写出的 `result.json`：

```json
{"score": 70, "max_score": 100, "status": "WA", "judge": "syncoj-builtin", "isolated": true,
 "language": "cpp", "compile_message": "", "source_path": "ab/ab.cpp",
 "cases": [{"name": "1", "status": "AC", "score": 10, "time_ms": 3, "memory_kb": 1540, "message": ""}, …],
 "subtasks": [{"index": 1, "score": 0, "max_score": 40, "cases": ["2", "3"]}]}
```

状态：AC / WA / TLE / MLE / RE / OLE / CE；评测机自己的问题（沙箱起不来、checker 编不过）不写
成绩，任务退回队列，三次都失败记失败并在进度里显示原因。

* **选哪份代码**：只看还在机器上、内容已存下的文件，按题目的代码路径归题；多份候选时优先文件名
  与题目同名的，还分不出来就记失败（不猜）。没交的选手不写结果，矩阵照旧显示"未交"。
* **考试进行中**点评测会先确认：那时评的是考到一半的代码。
* **重测**就是再点一次：旧任务被替换，结果覆盖旧的 `result.json`。手工录入的成绩照旧不被覆盖。

### 评测机接口

都带 `Authorization: Bearer <评测机令牌>`：

| 接口 | 说明 |
|---|---|
| `POST /api/v1/judge/lease` | 领任务（租约 15 分钟，过期自动回到队列）；没有任务 `204` |
| `GET /api/v1/judge/tasks/{id}/blobs/{sha256}` | 取这个任务的代码或数据包 |
| `POST /api/v1/judge/tasks/{id}/result` | `{score, max_score, status, detail}`，写 `result.json` |
| `POST /api/v1/judge/tasks/{id}/fail` | `{message}`，退回队列 |

