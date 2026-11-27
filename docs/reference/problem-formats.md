# 题目配置导入导出：格式参考

实现：`server/syncoj_server/services/problem_io/`；接口：`server/syncoj_server/api/admin_problem_io.py`；设计：[`../design/10-builtin-judge.md`](../design/10-builtin-judge.md) §12.8；取舍：[`../decisions/ADR-0017-problem-format-import.md`](../decisions/ADR-0017-problem-format-import.md)。

| 格式 id | 名称 | 导入 | 导出 |
|---|---|---|---|
| `syncoj` | SyncOJ 原生 `syncoj-judge/1` | ✅ 无损 | ✅ 无损 |
| `hoj` | HOJ（HimitZH/HOJ 后台导入导出 zip） | ✅ | ✅ |
| `lemonlime` | LemonLime 比赛目录（`.cdf` JSON + `data/`） | ✅ | ✅ |
| `hydro` | Hydro（含旧 `config.ini`、SYZOJ `data.yml`） | ✅ | ✅ |
| `qduoj` | QDUOJ 导出 zip | ✅ | — |
| `fps` | FPS XML（HUSTOJ 等） | ✅ | — |

## 1. 接口

| 接口 | 作用 |
|---|---|
| `POST /contests/{id}/judge-import/preview?format=auto\|<格式 id>` | multipart `file`；暂存、识别、解析、试构建；返回 `token`、逐题预览与本场次已有题目标识 `existing_idents` |
| `POST /contests/{id}/judge-import/apply` | `{token, items:[{key, ident, action}], set_builtin}`；按暂存包重新解析，只采用回传的 `ident` 与 `action` |
| `GET /contests/{id}/judge-export/preview?format=…&problem_id=…` | 逐题 `included` 与警告 |
| `GET /contests/{id}/judge-export?format=…&problem_id=…` | 下载 zip |
| `GET /problems/{id}/judge-export?format=…` | 单题导出 |
| `GET /problems/{id}/testdata/file` | 原样下载数据包 |

* 预览默认动作：有错误 → `skip`；ident 已存在 → `update`；否则 `create`。有错误的题只能 `skip`（否则 400）。界面改 ident 后按 `existing_idents` 在 `create` / `update` 间切换（手选的 `skip` 不动）。
* 试构建：预览只算摘要，不写出完整数据包（测试点写成空文件的骨架包回读自检，测试数据按成员各解压一遍查损坏，同一成员每次请求只读一遍）；apply 把数据包写到暂存目录下的临时文件再流式入库，原生包的内层 zip 直接从导入包流式入库。
* apply：token 不存在 / 过期 / 属于别的场次 → 404 `import_token_expired`；同一 token 的 apply 串行，后到的在前一个完成后得 404；本批 ident 重复、`create` 撞已有 ident 或写库时撞唯一约束（别处同时建了同名题）→ 409 `ident_conflict`。
* 新建题目 `judge_mode=builtin`；覆盖已有题目时 `set_builtin=true`（默认）才改成 builtin。覆盖时 `title` 只在包里有值、且不等于来源名或 ident（不分大小写）时覆盖，`file_patterns` 不动（原生格式除外）。
* 导出不传 `problem_id`：`syncoj` 取全部题目；其他格式取全部带数据包的自带评测题。非原生格式跳过没有数据包或 blob 丢失的题（`included=false`）。
* 整包级错误：400 `validation_error`，`details.code_detail` = 下表代码；`too_many_problems` 为 413。

## 2. 公共规则（导入）

| 项 | 规则 | 提示 |
|---|---|---|
| ident 建议 | NFKC → 非 `[A-Za-z0-9_-]` 换 `_` → 去首尾 `_` → 截 64 → 空则 `p<序号>` → 本批去重（不分大小写） | `ident_renamed` |
| ident 来源 | 文件 IO 题取输入文件主名；其次格式自己的 ID；来源名为空时取上传文件主名 | |
| 时限 / 内存 | 夹到 100–60000 ms、16–4096 MB | `limit_clamped` |
| 分值 | 小数向下取整；全部为 0 或缺失 → 100 分平分到计分单元（子任务 + 散点） | `score_truncated`、`scores_missing` |
| IO 文件名 | `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`，非保留名，输入输出生效名不同；等于 `<ident>.in/.out` 时存 NULL（原生格式不改） | `io_name_invalid`（错误） |
| 编译参数 | 过 `PUT /judge` 的白名单，不合法的参数丢弃 | `flags_dropped` |
| checker | 只收 `.cpp/.cc/.cxx/.c`（按 C++ 编译）；`#include "testlib.h"` 而包里没有时补随仓库的 testlib.h（副本最前面加注释：来源、版本、MIT 许可全文） | `checker_language`（错误）、`testlib_injected`、`testlib_missing` |
| 数据包 | 规整成 `<名字>.in/.ans` + `config.json` + `checker.cpp`，名字 `[A-Za-z0-9._-]`、撞名加 `-2`；写完用 `parse_testdata` 回读自检 | `testdata_invalid`（错误） |
| 不支持的语义 | 逐点 / 逐组时限内存取最大值；子任务依赖丢弃；max / mul / average 子任务降级；栈限制丢弃 | 见第 9 节 |

保留的 IO 文件名：`prog`、`main.c`、`main.cpp`、`compile.out`、`compile.err`、`chk`、`checker.err`、`chk.out`、`chk.err`、`in.txt`、`out.txt`、`ans.txt`。

## 3. 识别顺序

预处理：忽略 `__MACOSX/`、`.DS_Store`、`.` 开头的文件；根目录只有一个目录时另试剥壳视图。首个命中即采用：

1. 非 zip：`<` / `<?xml` 开头 → `fps`；`0x20111127` 开头 → `cdf_binary`；`{` 开头 → `cdf_without_data`
2. 根目录 `syncoj-problems.json` → `syncoj`
3. 深度 ≤ 2 处有 `*.cdf` → `lemonlime`（`data/` 相对 `.cdf` 所在目录）
4. 根目录 `X.json` + 同名目录 `X/`，JSON 含 `problem` 与 `samples` → `hoj`
5. `<n>/problem.json` 含 `test_case_score` 或 `rule_type` → `qduoj`
6. `problem.yaml` / `*/problem.yaml` / `[*/]testdata/config.{yaml,yml,ini}` / `data.yml` → `hydro`
7. 只有一个 `*.xml`，根元素 `<fps>` → `fps`
8. 只有成对 `.in` / `.ans` → `plain_testdata`
9. 其他 → `unknown_format`

## 4. 各格式导入映射

### HOJ

| HOJ | SyncOJ | 提示 |
|---|---|---|
| `problem.problemId` | 来源名；非文件 IO 时作 ident（小写） | |
| `timeLimit`(ms) / `memoryLimit`(MB) | 时限 / 内存；缺失或 ≤ 0 的那一项取 1000 ms / 256 MB | `limits_defaulted` |
| `stackLimit` ≠ `memoryLimit` | 丢弃 | `stack_limit_dropped` |
| `isFileIO` + `ioReadFileName` / `ioWriteFileName` | 文件 IO + 文件名；ident 取输入主名 | |
| `problem.judgeMode`（顶层 `judgeMode` 只校验） | `default` 默认比较；`spj` → `spjCode` 写成 checker.cpp（`checker_points: percent`）；`interactive` → 交互题，`spjCode` 写成 interactor.cpp，一律标准 IO | `judge_mode_mismatch` |
| `isRemoveEndBlank=false` | `compare_mode: strict` | |
| `spjLanguage` | 只收 C / C++ | `checker_language` |
| `spjCode` 没用 testlib | 照样导入 | `checker_not_testlib` |
| `judgeExtraFile` / `userExtraFile` | testlib.h 丢弃；checker 用到的 `.h` 作支持文件；其余丢弃 | `extra_files_dropped` |
| `samples[]` | 测试点（先按相对路径，再按 basename 找）；`output` 缺省 = input 第一个 `.` 前 + `.out` | `missing_file` |
| `type=0`（ACM） | 全部测试点一个捆绑子任务，100 分 | `acm_as_bundle` |
| `judgeCaseMode=default` | 各点自己的分 | |
| `subtask_lowest` | 按 `groupNum` 分组，组分取组内最小；单点组当普通点 | `subtask_scores_uneven` |
| `subtask_average` | 拆成独立点，组分 `round(Σ/n)` 均分 | `subtask_average_split` |
| `ergodic_without_error` | 按 default | `ergodic_as_default` |
| `languages` 含 `C++ 20` / `C++ 17` | `-std=c++2a` / `-std=c++17`（取最高；NOI Linux 2.0 的 g++ 9.3 不认 `-std=c++20`）；只有 `C++` 时不设 | `languages_mapped` |
| 题面、tags、codeTemplates | 忽略 | `statement_dropped` |

### LemonLime

| `.cdf` | SyncOJ | 提示 |
|---|---|---|
| `version` ≠ `"1.0"` | 整包错误 | `version_unsupported` |
| `sourceFileName` / `problemTitle` | ident / 标题 | |
| `taskType` 0 / 1 / 2 | 传统 / 提交答案（答案文件名 = 各测试点输出文件名）/ 函数交互（`grader` 写成 grader.cpp，`interactor` 头文件按 `interactorName` 改名作附带文件） | `checker_missing_source`（没有 grader） |
| `taskType` 3、4 | 错误 | `communication_unsupported` |
| `standardInputCheck`、`standardOutputCheck` 都为 true | 标准 IO | |
| 都为 false | 文件 IO，`inputFileName` / `outputFileName` | |
| 两者不同 | `io_mode: auto`（输入同时是 stdin 与文件，输出优先取文件） | |
| `testCases[k]` 1 对文件 / 多对文件 | 普通测试点 / 捆绑子任务，分值 `fullScore` | |
| `inputFiles` 里的 `*_lemon_SUbtaskDEPENDENCE_fLAg` | 剔除后按下标配对 | `dependency_dropped` |
| `timeLimit`(ms) / `memoryLimit`(MB) | 取各组最大值 | `limits_collapsed` |
| `comparisonMode` 1 / 0 / 2 | 默认比较 / `compare_mode: strict` / 默认比较 | — / — / `diff_compare_relaxed` |
| `comparisonMode` 3 | 生成实数 checker，eps = 10^-`realPrecision`，绝对或相对误差 | `real_checker_generated` |
| `comparisonMode` 4 | Lemon 协议 spj（`checker_protocol: lemon`），找源码的规则同 5 | `checker_missing_source` |
| `comparisonMode` 5 | `specialJudge` 同目录依次找 `<spj>`(源码)、`<spj>.cpp/.cc`、`<主名>.cpp/.cc`、`checker.cpp`、`chk.cpp`、`spj.cpp`；同目录 `.h` 作支持文件 | `checker_missing_source` |
| 测试点总数 > 500 | 错误（解析时就报） | `too_many_cases` |
| `compilerConfiguration`、`diffArguments`、`contestants`、`subFolderCheck` | 忽略 | |

路径：`\` 换 `/`；`..`、绝对路径、盘符 → `unsafe_path`。

### Hydro / SYZOJ

| Hydro | SyncOJ | 提示 |
|---|---|---|
| 目录 | 根目录有 `problem.yaml` / `testdata/` / `data.yml` 时整包一题，否则根下每个目录一题；`testdata/` 子目录展开一层 | |
| `problem.yaml: pid` / `title` | ident / 标题；解析失败只提示 | `problem_yaml_ignored` |
| `problem.yaml: limits.time_limit`(s) / `memory`(MB) | 覆盖 config | |
| `type: interactive` + `interactor` | 交互题，interactor 写成 interactor.cpp；一律标准 IO | `checker_missing_source` |
| `type: submit_answer` | 提交答案题，答案文件名 = 各测试点输出文件名 | |
| `type` 其他、`multi_pass` | 错误 | `communication_unsupported`、`problem_type_unsupported` |
| `user_extra_files` | 带 `main` 的 C / C++ 源码 → grader，其余 `.h` / 源码 → 附带文件，题型 grader；没有带 main 的源码、或与交互 / 提交答案同时出现 → 错误 | `grader_unsupported` |
| `time` | 数字按 ms；`s`/`ms`/`us` 换算后向下取整；非有限数 → 错误 | `bad_manifest` |
| `memory` | 数字按 MB；`k`/`m`/`g` 换算后向上取整 | |
| `filename` | 文件 IO，`<filename>.in/.out`；ident 取 `filename`（覆盖 pid） | |
| `checker_type` default / strict | 默认比较 / `compare_mode: strict` | |
| `checker_type: testlib` + `checker: x.cpp` | checker.cpp（`checker_points: auto`）；`judge_extra_files` 里的 `.h` 作支持文件 | `extra_files_dropped` |
| `checker_type: lemon` + `checker: x.cpp` | checker.cpp，`checker_protocol: lemon` | |
| `checker: rcmp4` / `rcmp6` / `rcmp9` | 实数 checker，1e-4 / 1e-6 / 1e-9，绝对或相对误差 | `real_checker_generated` |
| `checker: rcmp` | 实数 checker，1.5e-6，只比绝对误差 | `real_checker_generated` |
| `checker: wcmp` / `ncmp` / `lcmp` / `fcmp` / `hcmp` | 默认比较 | `builtin_checker_approx` |
| 其他内置 checker | 错误 | `checker_unknown` |
| `checker_type` hustoj / qduoj / syzoj / kattis | 错误 | `checker_protocol_unsupported` |
| 子任务没写 `score`（含 0） | 平分 `100 − 已写分之和`，余数给后面几个 | |
| `subtasks[].type` min（缺省） | 捆绑；单点当普通点 | `subtask_scores_uneven` |
| `sum` / 平铺 `cases` | 拆成独立点，没写分的点平分子任务分 | |
| `max` | 捆绑 | `max_as_bundle` |
| `subtasks[].if` | 丢弃 | `dependency_dropped` |
| 子任务 / 测试点的 `time`、`memory` | 与全局一起取最大 | `limits_collapsed` |
| 没有测试点配置 | 按文件名识别：`X.in` 配 `X.out/.ans`，`N-M.in` 归 min 子任务 N | |
| `config.ini` | 首行点数，之后 `in\|out\|time(s)\|score\|memory(KB)`；点数 > 500 直接报错 | `too_many_cases` |
| `langs` 含 `cc17`、`cc.cc17o2` 等 | `-std=c++17` 等（取最高） | `languages_mapped` |
| SYZOJ `data.yml` | `inputFile` / `outputFile` 只替换第一个 `#`；min 捆绑、mul 捆绑、sum 拆点；`specialJudge` → 错误；时限内存取默认 | `mul_as_bundle`、`limits_defaulted` |

### QDUOJ

| QDUOJ | SyncOJ | 提示 |
|---|---|---|
| `display_id` / `title` | ident / 标题 | |
| `time_limit`(ms) / `memory_limit`(MB) | 直接对应；缺失的那一项取默认 | `limits_defaulted` |
| `test_case_score[k]` | 第 k 个 `testcase/k.in` / `k.out`；缺号报错，多出未列的点报错 | `missing_file`、`cases_unlisted` |
| `rule_type=OI` / `ACM` | 各点自己的分 / 一个捆绑子任务 100 分 | `acm_as_bundle` |
| `spj` 非 null | `spj.code` 写成 checker.cpp，`checker_protocol: exitcode`（QDUOJ 以 `spj <输入> <选手输出>` 调用，多传的答案参数它不读；没有 `k.out` 时按空答案）；只收 C / C++ | `checker_language` |
| IO | 一律标准 IO；没有 spj 时 `compare_mode: trailing_newlines`（QDUOJ 只对整个文件去尾部空白） | |
| `template` | 丢弃 | `template_dropped` |

### FPS

| FPS | SyncOJ | 提示 |
|---|---|---|
| `<item>` | 一题；ident 建议 `p<序号>` | |
| `title` | 标题 | |
| `time_limit@unit` | `s`（缺省）× 1000；`ms` 直接用 | `limits_defaulted` |
| `memory_limit@unit` | `kb` ÷ 1024 向上取整；`gb` × 1024；其余按 MB | `limits_defaulted` |
| `test_input` / `test_output` | 内嵌数据；全都带不重复的 `name` 时按 name 配对，否则按序号（分组式按下标，交错式按紧随的输出） | `missing_answer` |
| 分值 | 100 分平分 | `scores_missing` |
| `<spj language=C/C++>` | HUSTOJ 参数顺序（in std user），套外壳对调 | `wrapped_hustoj_spj` |
| `<tpj>` | 按 testlib checker | `tpj_order_uncertain` |
| `<spj language=Text>` / `<interactor>` | 错误 | `text_judge_unsupported` / `interactive_unsupported` |
| `prepend` / `template` / `append` | 丢弃 | `template_dropped` |
| `sample_*`、题面、`solution` | 忽略 | `statement_dropped` |

XML：拒绝 DOCTYPE / ENTITY（`xml_unsafe`）；按 1 MB 一块流式解析，只建 `fps` → `item` → 上表元素。

## 5. 原生格式 `syncoj-judge/1`、`/2`

```
syncoj-problems.json
<ident>/testdata.zip          # 原样的 blob 字节
```

`syncoj-problems.json`：`{"format": "syncoj-judge", "version": 1, "problems": [...]}`；有非传统题、比较方式不是 `both` 或 `io_mode: auto` 的题时写 `version: 2`（旧服务端读到 2 会 `version_unsupported` 拒收，不会当传统题导入）。每题字段：

| 字段 | 说明 |
|---|---|
| `ident`、`title`、`order_index`、`note` | 题目列原值 |
| `file_patterns` | 库里原值；`null` = 服务端默认模式 |
| `judge_mode`、`time_limit_ms`、`memory_limit_mb`、`io_mode`、`input_file`、`output_file`、`compile_flags` | 题目列原值 |
| `problem_type`、`compare_mode` | 题型与比较方式（版本 2 起；缺省 `traditional` / `both`）；内层 zip 按题型过 `parse_testdata` |
| `testdata` | `{path, name, sha256, size}`；没有数据包时省略。导入时 `name` 只取基本名、去掉控制字符、截到 255 字符，为空用 `<ident>.zip` |

导入：`format` 必须是 `syncoj-judge`，`version > 2` → `version_unsupported`；内层 zip sha256 不符 → `sha_mismatch`；内层 zip 过 `parse_testdata`；各列按原值写回，只校验（ident、IO 文件名、编译参数白名单、代码路径模式）。导出再导入：各列（id 除外）与 `testdata_sha256` 相同。

## 6. 各格式导出映射

### HOJ

* `problem_<k>.json` + `problem_<k>/<i>.in|.out`，k、i 从 1 起。
* `problemId = ident.upper()`，`type=1`，`stackLimit = memoryLimit`，`ioScore` = 满分，`isRemoveEndBlank=true`，题面字段空串。
* 有 checker：`judgeMode=spj`（顶层与 `problem` 内各一份）、`spjCode`、`spjLanguage="C++"`；testlib.h 以外的 `.h` 写进 `judgeExtraFile`（`hoj_extra_headers`）。
* 有子任务：`judgeCaseMode=subtask_lowest`，组内每点 score = 子任务分，`groupNum` = 子任务序号；散点各自成组。
* `languages`：`C`、`C With O2`、`C++`、`C++ With O2`；编译参数带 `-std=c++17`、`-std=c++2a` 或 `-std=c++20` 时加 `C++ 17` 两项，`-std=c++2a` / `-std=c++20` 再加 `C++ 20` 两项。其余参数（含 `-O2`、`-std=c++14`）→ `flags_dropped`。
* checker 没用 testlib → `checker_not_testlib`。
* 题型：交互题 `judgeMode=interactive`，interactor 写进 `spjCode`；函数交互、提交答案题 HOJ 没有对应 → 跳过（`type_not_exportable`）。`compare_mode: strict` → `isRemoveEndBlank=false`；`io_mode: auto` 按文件 IO 写出（`io_auto_as_file`）；checker 协议不是 testlib → `checker_protocol_approx`。

### LemonLime

* `contest/contest.cdf`（紧凑 JSON，`version "1.0"`）+ `contest/data/<ident>/`。
* 散点一个 testCase，子任务一个 testCase（含全部文件对），都带整题时限与内存。
* `subFolderCheck`：按收代码时生效的代码路径模式（没配时是服务端的 `default_file_pattern`）判断：展开后全是 `<ident>.<扩展名>` → false；全是 `<ident>/<ident>.<扩展名>` → true；其他写法按有无子目录取值并给 `lemon_layout_mismatch`。
* 有 checker：`comparisonMode=5`、`specialJudge="<ident>/checker"`，源码写成 `data/<ident>/checker.cpp`（`checker_needs_build`）；没用 testlib → `checker_not_testlib`。
* 编译参数 → `flags_dropped`。
* 题型：提交答案 `taskType=1`（输出文件名取答案文件名）；函数交互 `taskType=2`，grader 写成 `data/<ident>/grader.cpp`，第一个头文件作 `interactor` / `interactorName`（其余 `extra_files_dropped`）；交互题 LemonLime 没有对应 → 跳过（`type_not_exportable`）。Lemon 协议 checker → `comparisonMode=4`；`compare_mode: strict` → `comparisonMode=0`，只忽略其中一种空白 → `compare_mode_approx`；`io_mode: auto` 按文件 IO 写出（`io_auto_as_file`）。

### Hydro

* `<ident>/problem.yaml`（`pid`、`title`、`tag: []`）+ `<ident>/testdata/config.yaml` 与扁平化数据。
* `pid` 不满足 `^[a-z][0-9a-z]*$`（不分大小写）→ 省略（`pid_omitted`）。
* `time: <n>ms`、`memory: <n>m`；文件 IO 且输入输出主名相同、扩展名为 `.in/.out` 时写 `filename`，否则写 ident（`io_names_lost`）。
* checker → `checker_type: testlib`、`checker: checker.cpp`，`.h` 写进 `judge_extra_files`（testlib.h 不写）。
* 子任务与散点都写成 `{id, score, type: min, cases}`。
* 题型：交互 `type: interactive` + `interactor: interactor.cpp`；提交答案 `type: submit_answer`；函数交互 `user_extra_files: [grader.cpp, 附带文件…]`。`compare_mode: strict` → `checker_type: strict`；Lemon 协议 checker → `checker_type: lemon`。
* 提示：`hydro_zero_score_filled`（满分 < 100 且有 0 分单元）、`hydro_total_not_100`（其余满分 ≠ 100）、`hydro_case_limit`（> 100 点）、`hydro_total_time`（点数 × 时限 > 60 s）、`flags_dropped`。

往返：HOJ 文件 IO 题导回后 ident 取输入文件主名；Hydro 文件 IO 题导回后 ident 取 `filename`。文件名不变，ident 与原题不同时默认动作是新建。

## 7. 大小与安全

| 项 | 上限 / 规则 | 代码 |
|---|---|---|
| 上传 | 512 MiB | 413 `payload_too_large` |
| 成员名 | `\` 先换成 `/`；再过 `safe_member`；拒 NUL、盘符 | `unsafe_path` |
| 成员名重复（含 `\` 换 `/` 之后） | 拒绝 | `duplicate_member` |
| 成员名每段 | ≤ 255 字节 | `name_too_long` |
| 成员数 | ≤ 20000 | `too_many_members` |
| 解压总量（按 `file_size`） | ≤ 2 GiB | `archive_too_large` |
| 单成员压缩比 | > 1000 且 > 64 MiB → 拒绝 | `zip_bomb` |
| 描述文件（json / yaml / cdf） | ≤ 16 MiB，按实际读到的字节截断 | `member_too_large` |
| JSON 描述文件嵌套过深（解码器递归溢出） | 拒绝 | `bad_manifest` |
| 压缩数据损坏 | 描述文件 → 整包错误；测试数据 → 该题错误 | `member_corrupt`、`testdata_invalid` |
| FPS XML | ≤ 256 MiB | `member_too_large` |
| YAML | 块映射 / 块序列 / 行内 `[..]` `{..}` / 标量 / 注释 / `---`；`& * ! \| > ? %` 拒绝；深度 ≤ 32；≤ 1 MiB | `yaml_unsupported`、`yaml_invalid` |
| 题目数 | ≤ 200，拿到题目目录列表时就查 | 413 `too_many_problems` |
| 每题测试点 | ≤ 500，解析时就查 | `too_many_cases` |
| 每题数据包解压量 | 按引用（含重复引用）累计 ≤ 2 GiB，写之前查 | `testdata_invalid` |
| 一次 preview / apply 的数据包解压量 | 全部题目按引用累计 ≤ 4 GiB，超出的题报错 | `testdata_invalid` |
| 暂存 | `<data_dir>/tmp/judge-import/<token>.bin`，token 为 32 位十六进制；2 小时过期；每场次最多 5 份 | 404 `import_token_expired` |

## 8. 提示代码

### 整包错误

| 代码 | 消息 |
|---|---|
| `bad_zip` | 不是合法的 zip |
| `unknown_format` | 无法识别的格式 |
| `plain_testdata` | 单题数据包：在题目「设置」中上传 |
| `cdf_binary` | 旧版二进制 .cdf，请在 LemonLime 中重新保存 |
| `cdf_without_data` | 缺少 data/ 目录，请把整个比赛目录打包为 zip |
| `unsafe_path` | 不安全的路径：{path} |
| `name_too_long` | 文件名超过 255 字节：{path} |
| `duplicate_member` | {path} 重复出现 |
| `too_many_members` | 文件数超过 {limit} 个 |
| `archive_too_large` | 解压后超过 {limit} |
| `zip_bomb` | {path} 压缩比异常 |
| `member_too_large` | {path} 超过 {limit} |
| `member_corrupt` | {path} 的压缩数据已损坏 |
| `too_many_problems` | 题目超过 {limit} 道 |
| `no_problems` | 包里没有题目 |
| `format_unimplemented` | 暂不支持{action} {format} |
| `bad_manifest` | {path} 无法解析：{reason}（也用作单题错误） |
| `version_unsupported` | {path} 的版本 {version} 不受支持 |
| `yaml_unsupported` | {path} 第 {line} 行：不支持的 YAML 写法 |
| `yaml_invalid` | {path} 第 {line} 行：{reason} |
| `xml_unsafe` | XML 含 DOCTYPE / ENTITY，已拒绝 |
| `no_exportable` | 没有可导出的题目 |

### 单题错误（只能跳过）

| 代码 | 消息 |
|---|---|
| `missing_file` | 缺少文件 {path} |
| `missing_answer` | 测试点 {name} 缺少答案 |
| `no_cases` | 没有测试点 |
| `cases_unlisted` | 测试点 {files} 不在分值表中 |
| `sha_mismatch` | {path} 的 sha256 与清单不符 |
| `testdata_invalid` | 数据包不对：{reason} |
| `ident_invalid` | 题目标识 {ident} 不合法 |
| `io_name_invalid` | 文件名 {name} 不合法 |
| `interactive_unsupported` | 交互题无法导入 |
| `communication_unsupported` | 通信题无法导入 |
| `answers_only_unsupported` | 提交答案题无法导入 |
| `problem_type_unsupported` | 题目类型 {type} 无法导入 |
| `lemon_spj_unsupported` | Lemon 旧式 spj 无法导入 |
| `checker_missing_source` | checker {path} 没有源码 |
| `checker_language` | checker 语言 {language} 无法导入 |
| `checker_protocol_unsupported` | {checker} 协议的 checker 无法导入 |
| `checker_unknown` | 内置 checker {name} 无法导入 |
| `grader_unsupported` | 带 grader 的题无法导入 |
| `qduoj_spj_unsupported` | 两参数 spj（无标准答案）无法导入 |
| `text_judge_unsupported` | 文本评测（人工判题）无法导入 |
| `too_many_cases` | 测试点 {count} 个，超过 {limit} 个 |

### 导入警告

| 代码 | 消息 |
|---|---|
| `ident_renamed` | 标识「{source}」改为 {ident} |
| `limit_clamped` | 时限/内存超出范围，已改为 {time} ms / {memory} MB |
| `limits_defaulted` | {what}未知，已设为 {value} |
| `limits_collapsed` | 各测试点时限/内存不同，已统一为 {time} ms / {memory} MB |
| `score_truncated` | 小数分值已向下取整 |
| `scores_missing` | 未给分值，按 100 分平分 |
| `flags_dropped` | 编译参数 {flags} 已丢弃 |
| `languages_mapped` | 编译参数取 {flags} |
| `stack_limit_dropped` | 栈限制 {mb} MB 已丢弃 |
| `judge_mode_mismatch` | 两处评测方式不一致，按 {mode} 导入 |
| `checker_not_testlib` | checker 未使用 testlib，退出码 0 以外均判 WA |
| `extra_files_dropped` | 附加文件 {files} 已丢弃 |
| `acm_as_bundle` | ACM 题改为整体捆绑，任一测试点错误得 0 分 |
| `subtask_scores_uneven` | 子任务 {group} 内分值不一致，取最小值 {score} |
| `subtask_average_split` | 平均分子任务已拆成独立测试点 |
| `ergodic_as_default` | 「遇错即停」已改为逐点计分，非满分时得分可能偏高 |
| `dependency_dropped` | 子任务依赖已丢弃，得分可能偏高 |
| `max_as_bundle` | max 子任务按捆绑计分，得分可能偏低 |
| `mul_as_bundle` | mul 子任务按捆绑计分，得分可能偏低 |
| `mixed_io` | {source}已按文件 IO 导入，{affected}的程序全部判错 |
| `strict_compare_relaxed` | 原为严格逐行比较，现忽略行末空白 |
| `compare_mode_approx` | 比较方式 {mode} 在目标格式里没有对应，按忽略行末空白写出 |
| `checker_protocol_approx` | {protocol} 协议的 checker 在目标格式里没有对应，按 testlib 写出，退出码以外的判定可能不同 |
| `io_auto_as_file` | 自动输入输出在目标格式里没有对应，按文件 IO 写出，读标准输入的程序全部判错 |
| `type_not_exportable` | {type}无法导出为 {format}，已跳过 |
| `diff_compare_relaxed` | 原为 diff 比较，现忽略行末空白 |
| `real_checker_generated` | 已生成实数比较 checker（误差 {eps}） |
| `builtin_checker_approx` | 内置 checker {name} 改用默认比较 |
| `wrapped_hustoj_spj` | HUSTOJ spj 已套外壳，参数顺序已对调 |
| `tpj_order_uncertain` | tpj 按 testlib 参数顺序导入，需人工核对 |
| `testlib_injected` | 已补入 testlib.h |
| `testlib_missing` | 缺少 testlib.h，checker 编译会失败 |
| `statement_dropped` | 题面、标签等未导入（整包一次） |
| `template_dropped` | 代码模板已丢弃，选手须提交完整程序 |
| `problem_yaml_ignored` | problem.yaml 无法解析，标题与 pid 未导入 |
| `file_patterns_dropped` | 代码路径模式不合法，已改用默认模式 |

### 导出警告

| 代码 | 消息 |
|---|---|
| `no_testdata` | 无数据包，已跳过 |
| `missing_file` | 缺少文件 {path}（blob 丢失，这道题未导出） |
| `flags_dropped` | 编译参数 {flags} 已丢弃 |
| `checker_not_testlib` | checker 未使用 testlib，退出码 0 以外均判 WA |
| `checker_needs_build` | checker 需在 LemonLime 机器上编译为 {path} |
| `hoj_extra_headers` | {files} 写进 judgeExtraFile，HOJ 再次导出时会丢失 |
| `lemon_layout_mismatch` | 代码路径 {patterns} 不是 <题目>.cpp 或 <题目>/<题目>.cpp，LemonLime 可能找不到选手代码 |
| `io_names_lost` | 文件名 {input} / {output} 未导出，按 {ident}.in / {ident}.out |
| `pid_omitted` | pid {ident} 未导出 |
| `hydro_total_not_100` | 满分 {total}，Hydro 不换算成 100 |
| `hydro_zero_score_filled` | {count} 组 0 分测试点在 Hydro 中共得 {score} 分，满分变为 100 |
| `hydro_case_limit` | 测试点 {count} 个，Hydro 默认评测机会拒绝 |
| `hydro_total_time` | 测试点时限合计 {seconds} s，超过 60 s，Hydro 默认评测机会拒绝 |

## 9. 不做的事

逐点 / 逐组时限内存、子任务依赖、sum / max 子任务策略、小数分、checker 部分分（退出码 7）、栈限制、交互 / 通信 / 提交答案题、QDUOJ 与 FPS 导出。理由见 ADR-0017。
