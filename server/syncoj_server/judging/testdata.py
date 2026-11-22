"""数据包：一个 zip，里面是测试点与（可选的）配置、checker。

约定（与 LemonLime 的习惯一致，能直接拿现成的数据目录打包）::

    1.in  1.ans          # 答案也可以叫 1.out
    2.in  2.ans
    ...
    config.json          # 可选：每点分值、子任务
    checker.cpp          # 可选：special judge（testlib 风格），连同 testlib.h

测试点可以放在任意一层子目录里，**名字**（去掉扩展名的相对路径）唯一即可。按"自然序"
排（``2`` 在 ``10`` 前面）。

``config.json``（全部可选）::

    {"cases": [{"name": "1", "score": 10}, ...],
     "subtasks": [{"score": 40, "cases": ["1", "2"]}, ...]}

* 没写分值：总分 100 按测试点平均分（除不尽的余数给最后几个点）。
* 子任务：里面**全部** AC 才得这个子任务的分（捆绑测试）；子任务里的点不再单独计分。

校验在**上传时**做（服务端）：结构不对当场报出来，而不是等到评测时才发现。
"""

from __future__ import annotations

import json
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Dict, List, Optional, Sequence

__all__ = [
    "TestdataError",
    "Case",
    "Subtask",
    "Testdata",
    "parse_testdata",
    "safe_member",
    "MAX_CASES",
]

#: 测试点数上限（防一个错包把评测机拖死）
MAX_CASES = 500
#: 解压后总大小上限
MAX_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024
ANSWER_SUFFIXES = (".ans", ".out")
CHECKER_NAMES = ("checker.cpp", "spj.cpp", "chk.cpp")
CONFIG_NAME = "config.json"


class TestdataError(ValueError):
    """数据包结构不对。信息直接给教师看。"""


@dataclass
class Case:
    name: str
    input: str  # zip 内路径
    answer: str
    score: int = 0


@dataclass
class Subtask:
    score: int
    cases: List[str]


@dataclass
class Testdata:
    cases: List[Case]
    subtasks: List[Subtask] = field(default_factory=list)
    checker: Optional[str] = None
    #: checker 同目录下的其它源文件（testlib.h 等），编译时一起解出来
    checker_support: List[str] = field(default_factory=list)

    @property
    def total_score(self) -> int:
        in_subtask = {name for sub in self.subtasks for name in sub.cases}
        return sum(sub.score for sub in self.subtasks) + sum(
            case.score for case in self.cases if case.name not in in_subtask
        )

    def summary(self) -> Dict[str, object]:
        return {
            "cases": len(self.cases),
            "subtasks": len(self.subtasks),
            "has_checker": self.checker is not None,
            "total_score": self.total_score,
        }


def _natural_key(text: str):
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", text)]


def safe_member(name: str) -> bool:
    """zip 成员名是否安全：相对路径、不含 ``..``、不含反斜杠。导入器（problem_io）也用它。"""
    path = PurePosixPath(name)
    return not path.is_absolute() and ".." not in path.parts and "\\" not in name


#: 旧名，保留给已有调用方
_safe_member = safe_member


def parse_testdata(archive: zipfile.ZipFile) -> Testdata:
    """读出数据包结构；不对就抛 :class:`TestdataError`（信息给人看）。"""
    total = 0
    files: Dict[str, zipfile.ZipInfo] = {}
    for info in archive.infolist():
        if info.is_dir():
            continue
        if not safe_member(info.filename):
            raise TestdataError("数据包里有不安全的路径：%s" % info.filename)
        total += info.file_size
        if total > MAX_UNCOMPRESSED_BYTES:
            raise TestdataError("数据包解压后超过 2 GB")
        files[info.filename] = info

    inputs: Dict[str, str] = {}
    answers: Dict[str, str] = {}
    for name in files:
        lower = name.lower()
        stem = name.rsplit(".", 1)[0]
        if lower.endswith(".in"):
            inputs[stem] = name
        elif lower.endswith(ANSWER_SUFFIXES):
            # .ans 优先于 .out（有的数据目录两个都有，.out 是参考程序跑出来的）
            if stem not in answers or lower.endswith(".ans"):
                answers[stem] = name

    names = sorted(inputs, key=_natural_key)
    missing = [name for name in names if name not in answers]
    if missing:
        raise TestdataError("这些输入没有对应的答案（.ans / .out）：%s" % ", ".join(missing[:10]))
    if not names:
        raise TestdataError("数据包里没有测试点（要有成对的 X.in 与 X.ans）")
    if len(names) > MAX_CASES:
        raise TestdataError("测试点太多（%d > %d）" % (len(names), MAX_CASES))

    # 测试点名：去掉扩展名的相对路径；只有一层目录时去掉那层公共前缀，配置里好写
    prefixes = {n.rsplit("/", 1)[0] if "/" in n else "" for n in names}
    strip = prefixes.pop() + "/" if len(prefixes) == 1 and "" not in prefixes else ""
    cases = [
        Case(name=n[len(strip):] if strip else n, input=inputs[n], answer=answers[n]) for n in names
    ]

    config: Dict[str, object] = {}
    config_member = next((n for n in files if PurePosixPath(n).name == CONFIG_NAME), None)
    if config_member is not None:
        try:
            config = json.loads(archive.read(config_member).decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise TestdataError("config.json 不是合法 JSON：%s" % exc)
        if not isinstance(config, dict):
            raise TestdataError("config.json 必须是一个对象")

    _apply_scores(cases, config)
    subtasks = _parse_subtasks(cases, config)

    checker = next(
        (n for n in sorted(files) if PurePosixPath(n).name.lower() in CHECKER_NAMES), None
    )
    support: List[str] = []
    if checker is not None:
        folder = str(PurePosixPath(checker).parent)
        support = [
            n for n in files
            if n != checker and str(PurePosixPath(n).parent) == folder
            and n.lower().endswith((".h", ".hpp"))
        ]
    return Testdata(cases=cases, subtasks=subtasks, checker=checker, checker_support=support)


def _apply_scores(cases: Sequence[Case], config: Dict[str, object]) -> None:
    by_name = {case.name: case for case in cases}
    configured = config.get("cases")
    if configured is not None:
        if not isinstance(configured, list):
            raise TestdataError("config.json 的 cases 必须是数组")
        for item in configured:
            if not isinstance(item, dict) or str(item.get("name")) not in by_name:
                raise TestdataError("config.json 的 cases 里有不存在的测试点：%r" % (item,))
            score = item.get("score")
            if not isinstance(score, int) or score < 0:
                raise TestdataError("测试点 %s 的分值必须是非负整数" % item.get("name"))
            by_name[str(item["name"])].score = score
        return
    if config.get("subtasks"):
        return  # 只写了子任务：不在子任务里的点得 0 分，由子任务决定总分
    base, remainder = divmod(100, len(cases))
    for index, case in enumerate(cases):
        case.score = base + (1 if index >= len(cases) - remainder else 0)


def _parse_subtasks(cases: Sequence[Case], config: Dict[str, object]) -> List[Subtask]:
    raw = config.get("subtasks")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise TestdataError("config.json 的 subtasks 必须是数组")
    names = {case.name for case in cases}
    seen = set()
    subtasks = []
    for index, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise TestdataError("第 %d 个子任务不是对象" % index)
        score = item.get("score")
        members = item.get("cases")
        if not isinstance(score, int) or score < 0:
            raise TestdataError("第 %d 个子任务的分值必须是非负整数" % index)
        if not isinstance(members, list) or not members:
            raise TestdataError("第 %d 个子任务没有测试点" % index)
        members = [str(m) for m in members]
        unknown = [m for m in members if m not in names]
        if unknown:
            raise TestdataError("第 %d 个子任务里有不存在的测试点：%s" % (index, ", ".join(unknown)))
        dup = [m for m in members if m in seen]
        if dup:
            raise TestdataError("测试点 %s 出现在多个子任务里" % ", ".join(dup))
        seen.update(members)
        subtasks.append(Subtask(score=score, cases=members))
    return subtasks
