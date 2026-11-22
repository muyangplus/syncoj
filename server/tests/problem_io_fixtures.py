"""题目导入导出测试用的夹具：全部在内存里用代码生成，仓库里不放二进制包。

约定（各格式的测试都按这个来）：

* 每个 ``make_<格式>_*`` 返回 **zip 字节**（FPS 返回 XML 字节），参数全有默认值，默认值
  就是一个能被识别、能被成功解析的最小真实包；用关键字参数覆盖其中的字段。
* ``shell="外壳目录"`` 把全部内容套进一层目录（测剥壳）；``extra={路径: 内容}`` 追加文件、
  ``drop=[路径]`` 删掉文件（测缺文件）。
* :func:`stage` 把字节写进 ``tmp_path`` 下的文件并返回路径，给 ``problem_io.parse_archive`` 用。

格式 agent 可以在本文件里补充/扩展自己那一节（保持函数名不变）。
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Union

Content = Union[bytes, str]


# --------------------------------------------------------------------------- #
# 通用
# --------------------------------------------------------------------------- #


def _bytes(value: Content) -> bytes:
    return value.encode("utf-8") if isinstance(value, str) else value


def make_zip(
    files: Dict[str, Content],
    *,
    shell: Optional[str] = None,
    extra: Optional[Dict[str, Content]] = None,
    drop: Iterable[str] = (),
) -> bytes:
    """``{路径: 内容}`` → zip 字节。"""
    merged = dict(files)
    merged.update(extra or {})
    for name in drop:
        merged.pop(name, None)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, content in merged.items():
            zf.writestr(("%s/%s" % (shell, name)) if shell else name, _bytes(content))
    return buffer.getvalue()


def stage(tmp_path: Path, data: bytes, name: str = "upload.zip") -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def aplusb_cases(count: int = 3) -> List[tuple]:
    """``[(输入, 答案)]``：a+b 的几组数据。"""
    return [("%d %d\n" % (i, i * 10), "%d\n" % (i * 11)) for i in range(1, count + 1)]


def make_testdata_zip(
    count: int = 3,
    *,
    scores: Optional[Sequence[int]] = None,
    subtasks: Optional[List[dict]] = None,
    checker: Optional[str] = None,
    support: Optional[Dict[str, Content]] = None,
) -> bytes:
    """SyncOJ 现有格式的单题数据包（``1.in``/``1.ans``…、可选 config.json 与 checker）。"""
    files: Dict[str, Content] = {}
    for index, (inp, ans) in enumerate(aplusb_cases(count), 1):
        files["%d.in" % index] = inp
        files["%d.ans" % index] = ans
    config: Dict[str, object] = {}
    if scores is not None:
        config["cases"] = [{"name": str(i), "score": s} for i, s in enumerate(scores, 1)]
    if subtasks is not None:
        config["subtasks"] = subtasks
    if config:
        files["config.json"] = json.dumps(config)
    if checker is not None:
        files["checker.cpp"] = checker
    files.update(support or {})
    return make_zip(files)


# --------------------------------------------------------------------------- #
# SyncOJ 原生 syncoj-judge/1
# --------------------------------------------------------------------------- #


def native_problem(ident: str = "sum", **overrides: object) -> dict:
    """清单里的一道题（不含 testdata 元信息；``testdata`` 键放内层 zip 字节或 None）。"""
    entry: Dict[str, object] = {
        "ident": ident,
        "title": "求和",
        "order_index": 1,
        "note": None,
        "file_patterns": None,
        "judge_mode": "builtin",
        "time_limit_ms": 1000,
        "memory_limit_mb": 256,
        "io_mode": "file",
        "input_file": None,
        "output_file": None,
        "compile_flags": "-std=c++17",
        "testdata": make_testdata_zip(),
    }
    entry.update(overrides)
    return entry


def make_native_zip(
    problems: Optional[List[dict]] = None,
    *,
    version: int = 1,
    format_name: str = "syncoj-judge",
    tamper_sha: bool = False,
    shell: Optional[str] = None,
    extra: Optional[Dict[str, Content]] = None,
    drop: Iterable[str] = (),
) -> bytes:
    problems = problems if problems is not None else [native_problem()]
    files: Dict[str, Content] = {}
    entries = []
    for problem in problems:
        entry = dict(problem)
        data = entry.pop("testdata", None)
        if data is None:
            entry["testdata"] = None
        else:
            path = "%s/testdata.zip" % entry["ident"]
            files[path] = data
            entry["testdata"] = {
                "path": path,
                "name": "%s.zip" % entry["ident"],
                "sha256": "0" * 64 if tamper_sha else sha256_hex(data),
                "size": len(data),
            }
        entries.append(entry)
    files["syncoj-problems.json"] = json.dumps(
        {"format": format_name, "version": version, "problems": entries}, ensure_ascii=False
    )
    return make_zip(files, shell=shell, extra=extra, drop=drop)


# --------------------------------------------------------------------------- #
# HOJ：根目录 problem_<k>.json + problem_<k>/
# --------------------------------------------------------------------------- #


def hoj_problem_json(problem_id: str = "P1000", count: int = 3, **problem_overrides: object) -> dict:
    problem: Dict[str, object] = {
        "problemId": problem_id,
        "title": "A+B",
        "type": 1,
        "judgeMode": "default",
        "judgeCaseMode": "default",
        "timeLimit": 1000,
        "memoryLimit": 256,
        "stackLimit": 256,
        "isFileIO": False,
        "ioReadFileName": None,
        "ioWriteFileName": None,
        "isRemoveEndBlank": True,
        "ioScore": 100,
        "description": "",
    }
    problem.update(problem_overrides)
    base, rest = divmod(100, count)
    samples = [
        {"input": "%d.in" % i, "output": "%d.out" % i, "score": base + (1 if i > count - rest else 0)}
        for i in range(1, count + 1)
    ]
    return {
        "problem": problem,
        "languages": ["C", "C++", "C++ With O2", "C++ 17"],
        "samples": samples,
        "tags": [],
        "codeTemplates": [],
        "judgeMode": problem["judgeMode"],
    }


def make_hoj_zip(
    problems: Optional[List[dict]] = None,
    *,
    shell: Optional[str] = None,
    extra: Optional[Dict[str, Content]] = None,
    drop: Iterable[str] = (),
) -> bytes:
    """``problems`` 是 :func:`hoj_problem_json` 的结果列表；数据文件按 samples 生成。"""
    problems = problems if problems is not None else [hoj_problem_json()]
    files: Dict[str, Content] = {}
    for k, doc in enumerate(problems, 1):
        key = "problem_%d" % k
        files[key + ".json"] = json.dumps(doc, ensure_ascii=False)
        data = aplusb_cases(len(doc["samples"]))
        for (inp, ans), sample in zip(data, doc["samples"]):
            name = str(sample["input"]).replace("\\", "/").rsplit("/", 1)[-1]
            files["%s/%s" % (key, name)] = inp
            out = sample.get("output") or name.split(".")[0] + ".out"
            files["%s/%s" % (key, str(out).replace("\\", "/").rsplit("/", 1)[-1])] = ans
    return make_zip(files, shell=shell, extra=extra, drop=drop)


# --------------------------------------------------------------------------- #
# LemonLime：<contest>.cdf（JSON）+ data/<pid>/<pid><i>.in/.out
# --------------------------------------------------------------------------- #


def lemon_task(
    pid: str = "game",
    count: int = 3,
    *,
    time_limit: int = 1000,
    memory_limit: int = 512,
    **overrides: object,
) -> dict:
    """与真实样例 .cdf 的 task 同形（文件 IO、每点 1 对、时限 ms、内存 MB）。"""
    base, rest = divmod(100, count)
    task: Dict[str, object] = {
        "answerFileExtension": "out",
        "comparisonMode": 1,
        "compilerConfiguration": {"g++": "C++14 O2"},
        "diffArguments": "--ignore-space-change --text --brief",
        "inputFileName": "%s.in" % pid,
        "outputFileName": "%s.out" % pid,
        "problemTitle": pid,
        "realPrecision": 3,
        "sourceFileName": pid,
        "specialJudge": "",
        "standardInputCheck": False,
        "standardOutputCheck": False,
        "subFolderCheck": False,
        "taskType": 0,
        "testCases": [
            {
                "fullScore": base + (1 if i > count - rest else 0),
                "inputFiles": ["%s/%s%d.in" % (pid, pid, i)],
                "outputFiles": ["%s/%s%d.out" % (pid, pid, i)],
                "memoryLimit": memory_limit,
                "timeLimit": time_limit,
            }
            for i in range(1, count + 1)
        ],
    }
    task.update(overrides)
    return task


def make_lemon_zip(
    tasks: Optional[List[dict]] = None,
    *,
    contest: str = "sample",
    version: Optional[str] = "1.0",
    contestants: Optional[List[dict]] = None,
    cdf: Optional[bytes] = None,
    shell: Optional[str] = None,
    extra: Optional[Dict[str, Content]] = None,
    drop: Iterable[str] = (),
) -> bytes:
    """数据文件按每个 testCase 的 inputFiles/outputFiles 生成（依赖标记跳过）。``cdf`` 直接给字节时原样写。"""
    tasks = tasks if tasks is not None else [lemon_task()]
    doc: Dict[str, object] = {"contestTitle": contest, "contestants": contestants or [], "tasks": tasks}
    if version is not None:
        doc["version"] = version
    files: Dict[str, Content] = {
        "%s.cdf" % contest: cdf if cdf is not None else json.dumps(doc, ensure_ascii=False, separators=(",", ":")),
        "README.txt": "比赛说明",
    }
    counter = 0
    for task in tasks:
        for case in task.get("testCases", []):  # type: ignore[union-attr]
            inputs = [p for p in case["inputFiles"] if "_lemon_SUbtaskDEPENDENCE_fLAg" not in p]
            for inp, out in zip(inputs, case["outputFiles"]):
                counter += 1
                files["data/" + inp.replace("\\", "/")] = "%d %d\n" % (counter, counter)
                files["data/" + out.replace("\\", "/")] = "%d\n" % (counter * 2)
    return make_zip(files, shell=shell, extra=extra, drop=drop)


#: 真实 LemonLime 比赛包结构 的六道题与时限（ms）；内存都是 512 MB，每题 20 点 × 5 分
LEMON_SAMPLE_TASKS = (("game", 1000), ("rect", 1000), ("match", 1500), ("digit", 1000), ("queue", 1000), ("number", 2000))
LEMON_SAMPLE_CONTESTANTS = ("std", "p1", "p2", "p3")


def lemon_contestant(name: str, pids: Sequence[str]) -> dict:
    """真实 .cdf 里 contestants[] 的一项（按题的数组；``compileMesaage`` 是原文拼写）。"""
    n = len(pids)
    return {
        "checkJudged": [True] * n,
        "compileMesaage": [""] * n,
        "compileState": [0] * n,
        "contestantName": name,
        "inputFiles": [[] for _ in pids],
        "judgingTime_date": "2024-07-01",
        "judgingTime_time": "10:00:00.000",
        "judgingTime_timespec": 1,
        "memoryUsed": [[[1024]] for _ in pids],
        "message": [[[""]] for _ in pids],
        "result": [[[0]] for _ in pids],
        "score": [[[5]] for _ in pids],
        "sourceFile": ["%s.cpp" % pid for pid in pids],
        "timeUsed": [[[10]] for _ in pids],
    }


def make_lemon_sample_zip(shell: Optional[str] = "sample") -> bytes:
    """复刻真实包：sample/sample.cdf + data/<pid>/<pid><i>.in/.out + source/<选手>/<pid>.cpp + README.txt。"""
    pids = [pid for pid, _ in LEMON_SAMPLE_TASKS]
    tasks = [lemon_task(pid, 20, time_limit=limit, memory_limit=512) for pid, limit in LEMON_SAMPLE_TASKS]
    sources: Dict[str, Content] = {
        "source/%s/%s.cpp" % (who, pid): "int main(){return 0;}\n" for who in LEMON_SAMPLE_CONTESTANTS for pid in pids
    }
    return make_lemon_zip(
        tasks,
        contest="sample",
        contestants=[lemon_contestant(who, pids) for who in LEMON_SAMPLE_CONTESTANTS],
        shell=shell,
        extra=sources,
    )


# --------------------------------------------------------------------------- #
# Hydro：<dir>/problem.yaml + <dir>/testdata/config.yaml
# --------------------------------------------------------------------------- #

HYDRO_CONFIG = """type: default
time: 1s
memory: 256m
subtasks:
  - id: 1
    score: 30
    type: min
    cases:
      - input: 1-1.in
        output: 1-1.out
      - input: 1-2.in
        output: 1-2.out
  - id: 2
    score: 70
    type: min
    cases:
      - input: 2-1.in
        output: 2-1.out
"""


def make_hydro_zip(
    *,
    folder: str = "1001",
    problem_yaml: str = "pid: P1001\nowner: 1\ntitle: A+B Problem\ntag:\n  - 入门\n",
    config: Optional[str] = HYDRO_CONFIG,
    config_name: str = "config.yaml",
    data: Optional[Dict[str, Content]] = None,
    shell: Optional[str] = None,
    extra: Optional[Dict[str, Content]] = None,
    drop: Iterable[str] = (),
) -> bytes:
    """``folder=""`` 时 problem.yaml 放在根目录（单题包）。``data`` 是 testdata/ 下的数据文件。"""
    prefix = folder + "/" if folder else ""
    files: Dict[str, Content] = {prefix + "problem.yaml": problem_yaml, prefix + "problem_zh.md": "# A+B"}
    if config is not None:
        files[prefix + "testdata/" + config_name] = config
    if data is None:
        data = {}
        for index, name in enumerate(("1-1", "1-2", "2-1"), 1):
            data[name + ".in"] = "%d %d\n" % (index, index)
            data[name + ".out"] = "%d\n" % (index * 2)
    for name, content in data.items():
        files[prefix + "testdata/" + name] = content
    return make_zip(files, shell=shell, extra=extra, drop=drop)


SYZOJ_DATA_YML = """subtasks:
  - score: 30
    type: min
    cases: [1, 2]
  - score: 70
    type: sum
    cases: [3, 4]
inputFile: 'data#.in'
outputFile: 'data#.out'
"""


def make_syzoj_zip(
    *,
    folder: str = "",
    data_yml: Optional[str] = SYZOJ_DATA_YML,
    data: Optional[Dict[str, Content]] = None,
    shell: Optional[str] = None,
    extra: Optional[Dict[str, Content]] = None,
    drop: Iterable[str] = (),
) -> bytes:
    """SYZOJ 的平铺数据目录：``data.yml`` 与 ``data<i>.in`` / ``data<i>.out`` 同级（默认 4 个点）。"""
    prefix = folder + "/" if folder else ""
    files: Dict[str, Content] = {}
    if data_yml is not None:
        files[prefix + "data.yml"] = data_yml
    if data is None:
        data = {}
        for i in range(1, 5):
            data["data%d.in" % i] = "%d %d\n" % (i, i)
            data["data%d.out" % i] = "%d\n" % (2 * i)
    for name, content in data.items():
        files[prefix + name] = content
    return make_zip(files, shell=shell, extra=extra, drop=drop)


# --------------------------------------------------------------------------- #
# QDUOJ：<i>/problem.json + <i>/testcase/<k>.in/.out
# --------------------------------------------------------------------------- #


def qduoj_problem_json(display_id: str = "1001", count: int = 2, **overrides: object) -> dict:
    base, rest = divmod(100, count)
    doc: Dict[str, object] = {
        "display_id": display_id,
        "title": "A+B",
        "description": {"format": "html", "value": "<p>a+b</p>"},
        "tags": ["easy"],
        "test_case_score": [
            {"score": base + (1 if i > count - rest else 0), "input_name": "%d.in" % i, "output_name": "%d.out" % i}
            for i in range(1, count + 1)
        ],
        "time_limit": 1000,
        "memory_limit": 256,
        "samples": [{"input": "1 2", "output": "3"}],
        "template": {},
        "spj": None,
        "rule_type": "OI",
        "source": "",
        "answers": [],
    }
    doc.update(overrides)
    return doc


def make_qduoj_zip(
    problems: Optional[List[dict]] = None,
    *,
    shell: Optional[str] = None,
    extra: Optional[Dict[str, Content]] = None,
    drop: Iterable[str] = (),
) -> bytes:
    problems = problems if problems is not None else [qduoj_problem_json()]
    files: Dict[str, Content] = {}
    for i, doc in enumerate(problems, 1):
        files["%d/problem.json" % i] = json.dumps(doc, ensure_ascii=False, indent=4)
        for k, item in enumerate(doc["test_case_score"], 1):  # type: ignore[arg-type]
            files["%d/testcase/%s" % (i, item["input_name"])] = "%d %d\n" % (k, k)
            if doc.get("spj") is None:
                files["%d/testcase/%s" % (i, item["output_name"])] = "%d\n" % (2 * k)
    return make_zip(files, shell=shell, extra=extra, drop=drop)


# --------------------------------------------------------------------------- #
# FPS：单个 XML
# --------------------------------------------------------------------------- #


def fps_tests(count: int = 2, *, layout: str = "interleaved", named: bool = True) -> str:
    """``count`` 组 a+b 测试数据的 XML 片段。``layout``：``interleaved``（HUSTOJ 导出的
    in,out,in,out）或 ``grouped``（DTD 的先全部输入、再全部输出）；``named`` 控制 name 属性。"""

    def tag(kind: str, i: int, body: str) -> str:
        attr = ' name="test_%02d"' % i if named else ""
        return "<%s%s><![CDATA[%s]]></%s>" % (kind, attr, body, kind)

    inputs = [tag("test_input", i, "%d %d\n" % (i, i)) for i in range(1, count + 1)]
    outputs = [tag("test_output", i, "%d\n" % (2 * i)) for i in range(1, count + 1)]
    if layout == "grouped":
        return "".join(inputs) + "".join(outputs)
    return "".join(a + b for a, b in zip(inputs, outputs))


def fps_item(
    title: str = "A+B Problem",
    count: int = 2,
    *,
    extra_xml: str = "",
    time: str = "1",
    time_unit: Optional[str] = "s",
    memory: str = "256",
    memory_unit: Optional[str] = "mb",
    tests: Optional[str] = None,
) -> str:
    """一个 ``<item>``。``tests`` 给了就整段替换测试数据（默认 :func:`fps_tests` 交错带名）；
    单位传 None 则不写 ``unit`` 属性。"""
    tests = tests if tests is not None else fps_tests(count)

    def unit(value: Optional[str]) -> str:
        return ' unit="%s"' % value if value is not None else ""

    return (
        "<item><title><![CDATA[%s]]></title>"
        "<time_limit%s><![CDATA[%s]]></time_limit>"
        "<memory_limit%s><![CDATA[%s]]></memory_limit>"
        "<description><![CDATA[<p>a+b</p>]]></description>"
        "<sample_input><![CDATA[1 2]]></sample_input><sample_output><![CDATA[3]]></sample_output>"
        "%s%s</item>" % (title, unit(time_unit), time, unit(memory_unit), memory, tests, extra_xml)
    )


def make_fps_xml(items: Optional[List[str]] = None, *, version: str = "1.2", prolog: str = "") -> bytes:
    """``items`` 是 :func:`fps_item` 的结果；``prolog`` 插在 ``<fps>`` 前（测 DOCTYPE）。"""
    items = items if items is not None else [fps_item()]
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n%s'
        '<fps version="%s" url="https://github.com/zhblue/freeproblemset/">\n'
        '<generator name="HUSTOJ" url="https://github.com/zhblue/hustoj/"/>\n%s\n</fps>\n'
        % (prolog, version, "\n".join(items))
    ).encode("utf-8")


def make_fps_zip(items: Optional[List[str]] = None, *, name: str = "problem.xml", **kwargs: object) -> bytes:
    """HUSTOJ 勾选 zip 时的样子：同一份 XML 以 problem.xml 为名放进 zip。"""
    return make_zip({name: make_fps_xml(items)}, **kwargs)  # type: ignore[arg-type]


def corrupt_member(data: bytes, name: str) -> bytes:
    """把 zip 里 ``name`` 的压缩数据中间翻转一个字节（成员目录不动，读的时候才出错）。"""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        info = zf.getinfo(name)
    raw = bytearray(data)
    name_len = int.from_bytes(raw[info.header_offset + 26:info.header_offset + 28], "little")
    extra_len = int.from_bytes(raw[info.header_offset + 28:info.header_offset + 30], "little")
    start = info.header_offset + 30 + name_len + extra_len
    raw[start + info.compress_size // 2] ^= 0xFF
    return bytes(raw)
