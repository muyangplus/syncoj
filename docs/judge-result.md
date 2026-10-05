# 评测器对接：成绩怎么进 SyncOJ

> SyncOJ **不触发评测**。开考、点评测仍然由教师在自己的 LemonLime / Arbiter
> 界面里操作。SyncOJ 只做一件事：把评测器写出来的成绩收拢成一张表。
>
> 这是刻意的边界。LemonLime 和 Arbiter 都是 Qt GUI 程序，无头调用方式不稳定、
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

## 3. 三条保守规则（重要）

我们无法预先覆盖所有评测器格式，所以解析器一律保守。理解这三条能省掉很多困惑：

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
  "problems": ["p1", "p2"],
  "rows": [
    {"player_no": "S001", "total": 150,
     "cells": [
       {"problem": "p1", "score": 100, "max_score": 100, "parse_status": "ok"},
       {"problem": "p2", "score": 50,  "max_score": 100, "parse_status": "unparsed",
        "detail": "没有解析器认得这个格式"}
     ]}
  ],
  "unparsed": 1,
  "complete": false
}
```

**`parse_status` 必须看**：

| 值 | 含义 | 界面该怎么显示 |
|---|---|---|
| `ok` | 解析成功 | 正常显示分数 |
| `unparsed` | 有文件但看不懂 | **醒目标记**，不要显示成 0 |
| `manual` | 教师手工录入 | 可以加个标记区分 |
| `missing` | 从未收到结果 | 显示成"—"而不是 0 |

`0 分` 和 `没有成绩` 是完全不同的两件事。都显示成 0 会让教师以为选手考砸了，
进而做出错误的排名判断。
