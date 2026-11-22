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
            files["%s/%s" % (key, str(out).rsplit("/", 1)[-1])] = ans
    return make_zip(files, shell=shell, extra=extra, drop=drop)


# --------------------------------------------------------------------------- #
# LemonLime：<contest>.cdf（JSON）+ data/<pid>/<pid><i>.in/.out
# --------------------------------------------------------------------------- #


def lemon_task(pid: str = "game", count: int = 3, **overrides: object) -> dict:
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
                "memoryLimit": 512,
                "timeLimit": 1000,
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
    version: str = "1.0",
    cdf: Optional[bytes] = None,
    shell: Optional[str] = None,
    extra: Optional[Dict[str, Content]] = None,
    drop: Iterable[str] = (),
) -> bytes:
    """数据文件按每个 testCase 的 inputFiles/outputFiles 生成（依赖标记跳过）。``cdf`` 直接给字节时原样写。"""
    tasks = tasks if tasks is not None else [lemon_task()]
    doc = {"contestTitle": contest, "contestants": [], "tasks": tasks, "version": version}
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


def fps_item(title: str = "A+B Problem", count: int = 2, *, extra_xml: str = "") -> str:
    tests = "".join(
        '<test_input name="test_%02d"><![CDATA[%d %d\n]]></test_input>'
        '<test_output name="test_%02d"><![CDATA[%d\n]]></test_output>' % (i, i, i, i, 2 * i)
        for i in range(1, count + 1)
    )
    return (
        "<item><title><![CDATA[%s]]></title>"
        '<time_limit unit="s"><![CDATA[1]]></time_limit>'
        '<memory_limit unit="mb"><![CDATA[256]]></memory_limit>'
        "<description><![CDATA[<p>a+b</p>]]></description>"
        "<sample_input><![CDATA[1 2]]></sample_input><sample_output><![CDATA[3]]></sample_output>"
        "%s%s</item>" % (title, tests, extra_xml)
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
