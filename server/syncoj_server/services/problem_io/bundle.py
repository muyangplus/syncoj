"""题目导入导出的中间模型。

每种外来格式先被解析成 :class:`ProblemBundle`，再由 ``build.try_build`` 规整成
现有的数据包（``X.in`` / ``X.ans`` + ``config.json`` + ``checker.cpp``）。导出方向用
:class:`ExportProblem`（题目行的快照，脱离 ORM 会话）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

__all__ = [
    "FileRef",
    "BundleCase",
    "BundleSubtask",
    "Notice",
    "ProblemBundle",
    "ImportResult",
    "ExportProblem",
    "DEFAULT_TIME_LIMIT_MS",
    "DEFAULT_MEMORY_LIMIT_MB",
    "MAX_PROBLEMS",
]

DEFAULT_TIME_LIMIT_MS = 1000
DEFAULT_MEMORY_LIMIT_MB = 256
#: 一次导入最多多少道题（与 ``POST /contests/{id}/problems`` 一致）。格式模块拿到题目目录列表
#: 后就要查，别等全部解析完
MAX_PROBLEMS = 200


@dataclass
class FileRef:
    """一个文件的来源，二选一：归档内路径（**完整路径**，不是视图相对路径）或内存字节。"""

    member: Optional[str] = None
    data: Optional[bytes] = None

    def __post_init__(self) -> None:
        if (self.member is None) == (self.data is None):
            raise ValueError("FileRef 必须且只能给 member 或 data 之一")


@dataclass
class BundleCase:
    #: 来源里的测试点名（规整前）；子任务用它引用测试点
    name: str
    input: FileRef
    #: 答案；交互题、有 checker 的提交答案题可以没有（None）
    answer: Optional[FileRef]
    score: int = 0


@dataclass
class BundleSubtask:
    """捆绑子任务：全部 AC 才得 ``score``。``cases`` 是 :attr:`BundleCase.name` 列表。"""

    score: int
    cases: List[str]


@dataclass
class Notice:
    """警告或错误。``message`` 是给教师看的中文后果陈述；用 ``notices.notice()`` 构造。"""

    code: str
    message: str


@dataclass
class ProblemBundle:
    #: 来源中的稳定标识（如 "problem_3"、"tasks[1]"、"1001"），预览与应用之间靠它对应
    key: str
    #: 来源里显示的名字（problemId / sourceFileName / pid）
    source_name: str = ""
    #: 建议的 ident。格式模块填**原始**候选值即可，框架统一规整、去重并给 ident_renamed
    ident: str = ""
    title: Optional[str] = None
    #: None = 评测时按 1000 ms / 256 MB（与题目列的 NULL 同义；外来格式一律填具体值）
    time_limit_ms: Optional[int] = None
    memory_limit_mb: Optional[int] = None
    #: "stdio" | "file" | "auto"
    io_mode: str = "stdio"
    #: 文件 IO 的文件名；None = <ident>.in / <ident>.out。框架会把等于默认名的值规整成 None
    input_file: Optional[str] = None
    output_file: Optional[str] = None
    compile_flags: Optional[str] = None
    cases: List[BundleCase] = field(default_factory=list)
    subtasks: List[BundleSubtask] = field(default_factory=list)
    #: checker 源码（写成 checker.cpp）；None = 默认比较
    checker: Optional[FileRef] = None
    #: checker 的支持文件：写出时的文件名（只取 basename，须是 .h/.hpp）→ 来源
    checker_support: Dict[str, FileRef] = field(default_factory=dict)
    # ---- 题型（§12.13）与判定（§12.14）----
    #: traditional | grader | answer_only | interactive
    problem_type: str = "traditional"
    #: 默认比较器的空白处理；None = both
    compare_mode: Optional[str] = None
    #: checker 协议与 points 口径；None = 默认（testlib / ratio）
    checker_protocol: Optional[str] = None
    checker_points: Optional[str] = None
    #: 交互题的 interactor 源码（写成 interactor.cpp）
    interactor: Optional[FileRef] = None
    #: 函数交互题的 grader 源码（写成 grader.cpp / grader.c）与附带文件（文件名 → 来源）
    grader: Optional[FileRef] = None
    grader_name: str = "grader.cpp"
    grader_files: Dict[str, FileRef] = field(default_factory=dict)
    #: 提交答案题：测试点名（:attr:`BundleCase.name`）→ 答案文件名
    answer_files: Dict[str, str] = field(default_factory=dict)
    # ---- native 专用：原样带回的列 ----
    order_index: Optional[int] = None
    note: Optional[str] = None
    file_patterns: Optional[List[str]] = None
    judge_mode: str = "builtin"
    #: native：内层 testdata.zip 原样保留（sha256 不变）
    raw_testdata: Optional[FileRef] = None
    #: native：解析时已核对过的内层包 sha256 与摘要。都有时构建直接流式取归档成员，不再落临时文件
    raw_sha256: Optional[str] = None
    raw_summary: Optional[Dict[str, object]] = None
    #: native：数据包原名
    testdata_name: Optional[str] = None
    warnings: List[Notice] = field(default_factory=list)
    errors: List[Notice] = field(default_factory=list)

    def warn(self, notice: Notice) -> None:
        """加一条警告；同一代码只留第一条。"""
        if all(item.code != notice.code for item in self.warnings):
            self.warnings.append(notice)

    def fail(self, notice: Notice) -> None:
        """加一条错误（这道题只能跳过）。同一代码只留第一条。"""
        if all(item.code != notice.code for item in self.errors):
            self.errors.append(notice)

    @property
    def has_testdata(self) -> bool:
        return self.raw_testdata is not None or bool(self.cases)


@dataclass
class ImportResult:
    #: 格式 id（见 ``problem_io.FORMATS``）
    format: str
    problems: List[ProblemBundle] = field(default_factory=list)
    #: 整包级提示（如 statement_dropped），不属于某一道题
    warnings: List[Notice] = field(default_factory=list)

    def warn(self, notice: Notice) -> None:
        if all(item.code != notice.code for item in self.warnings):
            self.warnings.append(notice)


@dataclass
class ExportProblem:
    """导出用的题目快照（与 ``models.Problem`` 列一一对应，``file_patterns`` 已解析成列表）。"""

    id: int
    ident: str
    title: Optional[str] = None
    order_index: int = 0
    note: Optional[str] = None
    #: 库里存的原值：None = 用服务端默认模式
    file_patterns: Optional[List[str]] = None
    #: 收代码时实际生效的模式模板（``problem_rules.patterns_for``：没配时是服务端配置的
    #: ``default_file_pattern``）。None = 调用方没给，按 ``file_patterns`` 或出厂默认模式
    effective_patterns: Optional[List[str]] = None
    judge_mode: str = "external"
    time_limit_ms: Optional[int] = None
    memory_limit_mb: Optional[int] = None
    io_mode: str = "stdio"
    input_file: Optional[str] = None
    output_file: Optional[str] = None
    compile_flags: Optional[str] = None
    problem_type: str = "traditional"
    compare_mode: str = "both"
    testdata_sha256: Optional[str] = None
    testdata_name: Optional[str] = None
    testdata_size: Optional[int] = None

    @property
    def effective_time_limit_ms(self) -> int:
        return self.time_limit_ms or DEFAULT_TIME_LIMIT_MS

    @property
    def effective_memory_limit_mb(self) -> int:
        return self.memory_limit_mb or DEFAULT_MEMORY_LIMIT_MB

    @property
    def effective_input_file(self) -> str:
        return self.input_file or "%s.in" % self.ident

    @property
    def effective_output_file(self) -> str:
        return self.output_file or "%s.out" % self.ident
