"""评测成绩解析与回写。

设计原则：**宁可漏解析，也不能错解析。**
------------------------------------------
我们无法预先覆盖所有评测器的输出格式（LemonLime / Arbiter 各自的格式还会随
版本变）。因此这里的解析器一律保守：

- 只在**明确认得**的结构里取分数。看不懂就返回 ``None``，绝不"猜一个最像的数字"。
- 解析不出来的文件会被记成 ``parse_status="unparsed"``，在界面上明示，等教师
  手工补录。静默丢弃会让人以为"评测还没跑"；静默猜分更糟 —— 教师会拿它当真。
- 新增一种评测器格式 = 往 ``PARSERS`` 里加一个函数，不需要动扫描逻辑。

成绩目录约定
------------
::

    judge_result/<场次 slug>/<选手编号>/<题目标识>/<评测器输出的任意文件>

评测器（LemonLime / Arbiter）只要把输出写到这个目录树下即可，
其余交给本模块。这也意味着**服务端不触发评测** —— 开考仍由教师在自己的
GUI 里点，SyncOJ 只负责把成绩汇总起来。
"""

from __future__ import annotations

import json
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import Contest, JudgeRun, Player, utcnow
from ..paths import PathValidationError, validate_relpath

__all__ = [
    "ParsedScore",
    "parse_result_file",
    "scan_contest_results",
    "ScanReport",
    "MAX_FILE_BYTES",
    "CANDIDATE_SUFFIXES",
]

log = logging.getLogger(__name__)

#: 单个结果文件解析上限。评测结果不该有多大，超过基本可以断定是别的文件
MAX_FILE_BYTES = 4 * 1024 * 1024
#: 每个 (选手, 题目) 目录最多看多少个文件
MAX_FILES_PER_PROBLEM = 40

#: 优先尝试的后缀。不在这个列表里的文件仍然会尝试解析，只是排序靠后 ——
#: 因为我们无法预知评测器用什么扩展名。
CANDIDATE_SUFFIXES = (
    ".json", ".xml", ".txt", ".result", ".log", ".csv", ".ini", ".out", ".res",
)


# --------------------------------------------------------------------------- #
# 解析结果
# --------------------------------------------------------------------------- #


@dataclass
class ParsedScore:
    score: Optional[int] = None
    max_score: Optional[int] = None
    status: Optional[str] = None
    detail: str = ""


# --------------------------------------------------------------------------- #
# 键名识别
# --------------------------------------------------------------------------- #


def _norm(key: Any) -> str:
    """把键名规范化后比较：``Total_Score`` / ``total score`` / ``totalscore`` 视为同一个。"""
    return re.sub(r"[\s_\-]+", "", str(key)).lower()


def _keyset(names: Sequence[str]) -> frozenset:
    return frozenset(_norm(n) for n in names)


#: 分数键。覆盖英文与中文的常见写法
SCORE_KEYS = _keyset([
    "score", "totalscore", "total", "points", "got", "sum", "scoresum",
    "scoretotal", "得分", "分数", "总分", "成绩",
])

MAX_SCORE_KEYS = _keyset([
    "maxscore", "fullscore", "full", "max", "totalmax", "maxpoints",
    "满分", "总分上限",
])

STATUS_KEYS = _keyset([
    "status", "result", "verdict", "state", "outcome", "结果", "状态", "评测结果",
])

#: 用于在嵌套结构里寻找"逐测试点结果"的容器键
CASE_CONTAINER_KEYS = _keyset([
    "cases", "case", "tasks", "task", "results", "result",
    "testcases", "testcase", "tests", "subtasks", "subtask",
    "details", "case_results", "caseresults", "测试点", "子任务",
])

_STATUS_SEVERITY = {
    "ac": 0, "ok": 0, "passed": 0, "pass": 0, "correct": 0, "accepted": 0,
    "skipped": 0, "judging": 0, "pending": 0, "": 0,
    "pe": 1, "presentationerror": 1, "format": 1,
    "wa": 2, "wrong": 2, "wronganswer": 2, "fail": 2, "failed": 2,
    "tle": 3, "timeout": 3, "timelimitexceeded": 3,
    "mle": 3, "memorylimitexceeded": 3,
    "ole": 3, "outputlimitexceeded": 3,
    "re": 4, "runtimeerror": 4, "crash": 4, "abort": 4,
    "ce": 5, "compileerror": 5, "compilefailed": 5,
    "se": 6, "systemerror": 6, "internalerror": 6,
}


def _as_int(value: Any) -> Optional[int]:
    """尽量把值转成整数，但**不做有歧义的猜测**。"""
    if isinstance(value, bool):
        return None  # True/False 不是分数
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        number = float(text)
    except ValueError:
        pass
    else:
        return int(number) if number.is_integer() else None

    # "100/100" -> 100
    if "/" in text:
        head = text.split("/", 1)[0].strip()
        try:
            return int(head)
        except ValueError:
            return None
    # "100分" / "100 分" -> 100（评测器导出中文报表时很常见）
    match = re.match(r"^(-?\d+)\s*[^\d\s]+$", text)
    if match:
        return int(match.group(1))
    return None


def _as_status(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text[:32] if text else None


def _pick(mapping: Dict[str, Any], keys: frozenset) -> Any:
    for key, value in mapping.items():
        if _norm(key) in keys:
            return value
    return None


def _score_from_mapping(mapping: Dict[str, Any]) -> Optional[int]:
    return _as_int(_pick(mapping, SCORE_KEYS))


def _status_from_mapping(mapping: Dict[str, Any]) -> Optional[str]:
    return _as_status(_pick(mapping, STATUS_KEYS))


def _worst_status(statuses: Iterable[str]) -> Optional[str]:
    """取最严重的状态。

    ``["AC","AC","WA"]`` 的整题状态应当是 WA 而不是 AC —— 否则一个错点会被
    后面的正确结果掩盖，教师在矩阵上看到全绿。
    """
    best: Optional[str] = None
    best_rank = -1
    for status in statuses:
        key = _norm(status)
        rank = _STATUS_SEVERITY.get(key, 7)  # 不认识的状态按"最严重"处理
        if rank > best_rank:
            best_rank = rank
            best = status
    return best


def _combine(parts: Sequence[ParsedScore]) -> Optional[ParsedScore]:
    """把逐测试点的结果合并成整题结果。"""
    if not parts:
        return None

    # 只要有一个测试点没解析出分数，就不敢求和 —— 那会得出一个偏低的假分数
    if any(part.score is None for part in parts):
        return None

    total = sum(part.score for part in parts if part.score is not None)
    maxes = [part.max_score for part in parts]
    max_total = sum(maxes) if all(m is not None for m in maxes) else None

    statuses = [part.status for part in parts if part.status]
    return ParsedScore(
        score=total,
        max_score=max_total,
        status=_worst_status(statuses) if statuses else None,
        detail=" %d 个测试点求和" % len(parts),
    )


# --------------------------------------------------------------------------- #
# 各格式解析器
# --------------------------------------------------------------------------- #


def parse_json_document(document: Any, depth: int = 0) -> Optional[ParsedScore]:
    """从已解析的 JSON 结构里提取成绩。

    递归只沿**已知的结果容器键**下降，不做全树盲搜 —— 盲搜会把评测信息里的
    ``time_limit`` 之类当成分数。
    """
    if depth > 6:
        return None

    if isinstance(document, dict):
        direct = _score_from_mapping(document)
        if direct is not None:
            return ParsedScore(
                score=direct,
                max_score=_as_int(_pick(document, MAX_SCORE_KEYS)),
                status=_status_from_mapping(document),
                detail="JSON 直接字段",
            )
        for key, value in document.items():
            if _norm(key) in CASE_CONTAINER_KEYS:
                nested = parse_json_document(value, depth + 1)
                if nested is not None:
                    return nested
        return None

    if isinstance(document, list):
        parts = []
        for item in document:
            parsed = parse_json_document(item, depth + 1)
            if parsed is None:
                return None  # 看不全就不合并，见 _combine 的说明
            parts.append(parsed)
        combined = _combine(parts)
        if combined is not None:
            combined.detail = "JSON 数组 %d 项求和" % len(parts)
        return combined

    return None


def parse_json_text(text: str) -> Optional[ParsedScore]:
    try:
        document = json.loads(text)
    except (ValueError, RecursionError):
        return None
    return parse_json_document(document)


def parse_xml_text(text: str) -> Optional[ParsedScore]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None

    # 收集所有"带分数语义"的属性与子元素文本
    parts: List[ParsedScore] = []
    candidates: List[Dict[str, Any]] = []

    for element in root.iter():
        mapping = dict(element.attrib)
        for child in element:
            if isinstance(child.text, str) and child.text.strip():
                mapping[child.tag] = child.text.strip()
        if _pick(mapping, SCORE_KEYS) is not None:
            candidates.append(mapping)

    if not candidates:
        return None

    if len(candidates) == 1:
        mapping = candidates[0]
        return ParsedScore(
            score=_as_int(_pick(mapping, SCORE_KEYS)),
            max_score=_as_int(_pick(mapping, MAX_SCORE_KEYS)),
            status=_status_from_mapping(mapping),
            detail="XML",
        )

    # 多个带分数的元素视为逐测试点结果
    for mapping in candidates:
        parts.append(
            ParsedScore(
                score=_as_int(_pick(mapping, SCORE_KEYS)),
                max_score=_as_int(_pick(mapping, MAX_SCORE_KEYS)),
                status=_status_from_mapping(mapping),
            )
        )
    combined = _combine(parts)
    if combined is not None:
        combined.detail = "XML %d 项求和" % len(parts)
    return combined


_KV_LINE = re.compile(r"^\s*([^:=]{1,64})\s*[:=]\s*(.*?)\s*$")


def parse_keyvalue_text(text: str) -> Optional[ParsedScore]:
    """解析 ``key = value`` / ``key: value`` 形式的文本。

    评测器导出的人读报表几乎都是这个形态（LemonLime 的 result、各种 .log）。
    """
    fields: Dict[str, str] = {}
    for line in text.splitlines()[:500]:
        match = _KV_LINE.match(line)
        if match:
            fields[match.group(1)] = match.group(2)

    if not fields:
        return None

    score = _as_int(_pick(fields, SCORE_KEYS))
    if score is None:
        return None
    return ParsedScore(
        score=score,
        max_score=_as_int(_pick(fields, MAX_SCORE_KEYS)),
        status=_as_status(_pick(fields, STATUS_KEYS)),
        detail="key=value 文本",
    )


#: 解析器链。新增评测器格式只需要往这里加一个函数。
PARSERS: Tuple[Tuple[str, Callable[[str], Optional[ParsedScore]]], ...] = (
    ("json", parse_json_text),
    ("xml", parse_xml_text),
    ("keyvalue", parse_keyvalue_text),
)


def _looks_binary(payload: bytes) -> bool:
    return b"\x00" in payload[:8192]


def parse_result_file(path: Path) -> Tuple[Optional[ParsedScore], str, str]:
    """解析一个候选结果文件。

    返回 ``(解析结果, 解析器名, 原因说明)``。解析失败时第一个元素为 ``None``，
    第二、三个元素说明为什么 —— 这个原因会原样出现在教师界面上。
    """
    try:
        size = path.stat().st_size
    except OSError as exc:
        return None, "", "无法读取文件信息: %s" % exc

    if size == 0:
        return None, "", "文件为空"
    if size > MAX_FILE_BYTES:
        return None, "", "文件过大（%d 字节），已跳过" % size

    try:
        payload = path.read_bytes()
    except OSError as exc:
        return None, "", "读取失败: %s" % exc

    if _looks_binary(payload):
        return None, "", "二进制文件，无法解析"

    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        for encoding in ("gb18030", "utf-16", "latin-1"):
            try:
                text = payload.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            return None, "", "编码无法识别"

    # 去掉 BOM：某些 Windows 工具导出的结果文件带 BOM，会让 json.loads 直接失败
    text = text.lstrip("\ufeff")

    for name, parser in PARSERS:
        try:
            parsed = parser(text)
        except Exception as exc:  # pragma: no cover - 解析器自身兜底
            log.debug("解析器 %s 处理 %s 时抛异常: %s", name, path, exc)
            continue
        if parsed is not None:
            return parsed, name, parsed.detail

    return None, "", "没有解析器认得这个格式"


# --------------------------------------------------------------------------- #
# 扫描
# --------------------------------------------------------------------------- #


@dataclass
class ScanReport:
    parsed: int = 0
    unparsed: int = 0
    unchanged: int = 0
    skipped: int = 0
    #: 因教师手工录入而被保留（未覆盖）的条目数
    manual: int = 0
    errors: List[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.parsed + self.unparsed


def _candidate_sort_key(path: Path) -> Tuple[int, float, str]:
    suffix = path.suffix.lower()
    try:
        rank = CANDIDATE_SUFFIXES.index(suffix)
    except ValueError:
        rank = len(CANDIDATE_SUFFIXES)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    # 先按"像结果文件"排序，再按新到旧
    return (rank, -mtime, path.name)


def scan_contest_results(
    session: Session,
    settings: Settings,
    contest: Contest,
    *,
    force: bool = False,
) -> ScanReport:
    """扫描一个场次的结果目录并回写成绩。

    ``force=False`` 时，mtime 未变化的 (选手, 题目) 会被跳过 —— 默认每 10 秒跑
    一次，不能每次都把几百个文件重新解析一遍。
    """
    report = ScanReport()
    contest_root = Path(settings.judge_result_root) / contest.slug
    if not contest_root.is_dir():
        return report

    players = {
        player.player_no: player
        for player in session.execute(
            select(Player).where(Player.contest_id == contest.id)
        ).scalars()
    }
    if not players:
        return report

    existing = {
        (row.player_id, row.problem): row
        for row in session.execute(
            select(JudgeRun).where(JudgeRun.contest_id == contest.id)
        ).scalars()
    }

    for result in _iter_problem_dirs(contest_root, report):
        player_no, problem, problem_dir = result

        player = players.get(player_no)
        if player is None:
            report.skipped += 1
            # 目录名对不上任何选手：可能是教师建错了目录名，也可能是导入了旧数据。
            # 明说出来，别让它静默消失。
            report.errors.append(
                "结果目录 %s 对应的选手 %r 不在本场次，已跳过"
                % (problem_dir.as_posix(), player_no)
            )
            continue

        try:
            problem = validate_relpath(problem, max_length=64)
        except PathValidationError:
            report.skipped += 1
            report.errors.append("题目标识不合法: %r" % problem)
            continue

        chosen, newest_mtime = _pick_result_file(problem_dir)
        if chosen is None:
            report.skipped += 1
            continue

        row = existing.get((player.id, problem))

        # 教师手工录入的成绩**永远优先**，连 force 重扫也不例外。
        # 否则教师点一下"重新扫描"，自己刚修正的分数就被静默改回去了 ——
        # 而"重扫"这个动作在教师看来只是想刷新一下，不该有破坏性。
        # 要清除手工值必须走显式的删除接口。
        if row is not None and row.parse_status == "manual":
            report.manual += 1
            continue

        if row is not None and not force and row.source_mtime == int(newest_mtime):
            report.unchanged += 1
            continue

        parsed, parser_name, reason = parse_result_file(chosen)
        try:
            relative = chosen.relative_to(Path(settings.judge_result_root)).as_posix()
        except ValueError:
            relative = chosen.as_posix()

        if row is None:
            row = JudgeRun(
                contest_id=contest.id,
                player_id=player.id,
                problem=problem,
                scanned_at=utcnow(),
            )
            session.add(row)
            existing[(player.id, problem)] = row

        row.source_path = relative[:1024]
        row.source_mtime = int(newest_mtime)
        row.updated_at = utcnow()

        if parsed is None:
            # 保留上一次成功解析的分数，只更新"没解析出来"的标记 ——
            # 评测器重新导出一个损坏文件不该把已有成绩抹掉
            row.parse_status = "unparsed"
            row.detail = (reason or "无法解析")[:512]
            report.unparsed += 1
        else:
            row.score = parsed.score
            row.max_score = parsed.max_score
            row.status = parsed.status
            row.parse_status = "ok"
            row.detail = ("%s：%s" % (parser_name, reason or ""))[:512]
            row.raw_json = None
            report.parsed += 1

    session.flush()
    return report


def _iter_problem_dirs(contest_root: Path, report: ScanReport):
    """遍历 ``<选手编号>/<题目标识>/`` 两级目录。"""
    try:
        player_dirs = sorted(p for p in contest_root.iterdir() if p.is_dir())
    except OSError as exc:
        report.errors.append("无法读取结果目录 %s: %s" % (contest_root, exc))
        return

    for player_dir in player_dirs:
        # 跳过隐藏目录（编辑器产生的 .cache 之类）
        if player_dir.name.startswith("."):
            continue
        try:
            problem_dirs = sorted(p for p in player_dir.iterdir() if p.is_dir())
        except OSError as exc:
            report.errors.append("无法读取 %s: %s" % (player_dir, exc))
            continue
        for problem_dir in problem_dirs:
            if problem_dir.name.startswith("."):
                continue
            yield player_dir.name, problem_dir.name, problem_dir


def _pick_result_file(problem_dir: Path) -> Tuple[Optional[Path], float]:
    """在题目目录里挑出最可能承载成绩的文件。"""
    files: List[Path] = []
    try:
        for entry in problem_dir.rglob("*"):
            if len(files) >= MAX_FILES_PER_PROBLEM:
                break
            try:
                if entry.is_file() and not entry.name.startswith("."):
                    files.append(entry)
            except OSError:
                continue
    except OSError:
        return None, 0.0

    if not files:
        return None, 0.0

    files.sort(key=_candidate_sort_key)
    chosen = files[0]
    try:
        return chosen, chosen.stat().st_mtime
    except OSError:
        return None, 0.0
