"""FPS 格式（Free Problem Set XML，格式 id ``fps``）。只导入，不导出。

单个 XML（直接上传，或 zip 里只有一个 .xml，HUSTOJ 勾选 zip 时叫 ``problem.xml``）。
根元素 ``<fps>``，每个 ``<item>`` 一道题，测试数据以文本内嵌（``FileRef(data=...)``）。

安全：用 expat 按块流式解析，只建要用的元素（见 :class:`_Builder`），遇到 DOCTYPE / ENTITY
声明立即中止（``xml_unsafe``），
不做实体展开、不取外部资源；另外先在根元素之前的原始字节里查一遍 ``<!DOCTYPE`` / ``<!ENTITY``
（只查序言：题面 CDATA 里出现 ``<!DOCTYPE html>`` 是正常内容）。

映射：

* ``title`` → 标题；ident 建议 ``p<序号>``
* ``time_limit@unit``：``s``（缺省）按浮点乘 1000，``ms`` 直接用；单位不分大小写
* ``memory_limit@unit``：``kb`` 除以 1024（向上取整），``gb`` 乘 1024，其余按 MB
* ``test_input`` / ``test_output``：全都带不重复的 ``name`` 时按 name 配对；否则按序号配对——
  先输入后输出的分组式按下标，交错式按"输入后面紧跟的输出"；缺输出报 ``missing_answer``
* 没有分值：由公共规整按 100 分平分（``scores_missing``）
* ``<spj language=C/C++>``（HUSTOJ 参数 in std user）→ 套外壳对调参数（``wrapped_hustoj_spj``）
* ``<tpj>`` → 按 testlib checker 导入（``tpj_order_uncertain``）
* ``<spj language=Text>`` / ``<interactor>`` → 错误
* ``sample_*`` 不导入；``prepend/template/append`` → ``template_dropped``；题面等 → ``statement_dropped``
"""

from __future__ import annotations

import io
import math
import re
from typing import IO, Any, Dict, List, Optional, Tuple, Union
from xml.etree import ElementTree
from xml.parsers import expat

from ...judging.testdata import MAX_CASES
from . import checkers
from .archive import ArchiveView, human_size
from .bundle import (
    DEFAULT_MEMORY_LIMIT_MB,
    DEFAULT_TIME_LIMIT_MS,
    MAX_PROBLEMS,
    BundleCase,
    FileRef,
    ImportResult,
    ProblemBundle,
)
from .notices import ProblemIoError, notice, register

__all__ = ["LABEL", "parse", "MAX_XML_BYTES"]

LABEL = "FPS"

#: FPS 把测试数据内嵌在 XML 里，单个文件比描述文件大得多
MAX_XML_BYTES = 256 * 1024 * 1024

register(
    {
        "template_dropped": "代码模板已丢弃，选手须提交完整程序",
        "text_judge_unsupported": "文本评测（人工判题）无法导入",
    }
)

_UNSAFE = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)", re.I)
_C_LANGUAGES = {"", "c", "c++", "cpp", "cc", "cxx", "g++", "gcc", "clang", "clang++"}
_TEMPLATE_TAGS = ("prepend", "template", "append")
_STATEMENT_TAGS = ("description", "input", "output", "hint", "source", "img", "solution", "sample_input")


class _Unsafe(Exception):
    pass


def _refuse(*_args: Any) -> None:
    raise _Unsafe()


#: ``<item>`` 下会被读的子元素；其余子元素连同文本一律不建
_TEST_TAGS = ("test_input", "test_output")
_KEPT_TAGS = frozenset(
    ("title", "time_limit", "memory_limit", "spj", "tpj", "interactor")
    + _TEST_TAGS + _TEMPLATE_TAGS + _STATEMENT_TAGS
)
_CHUNK = 1024 * 1024


class _Builder:
    """流式建一棵**只含要用的元素**的树：``fps`` → ``item`` → 关心的子元素。

    * 更深的元素不建，文本并进最近的保留祖先（等同 ``itertext``）
    * 非测试数据的子元素每种只留第一个（``find`` 也只取第一个）
    * 每题的 ``test_input`` / ``test_output`` 各只留前 ``MAX_CASES + 1`` 个，总数记在 :attr:`tests`
    * ``item`` 超过 :data:`MAX_PROBLEMS` 立刻报 ``too_many_problems``

    这样元素个数与 XML 里塞了多少空元素无关，内存只随保留下来的文本增长。
    """

    def __init__(self) -> None:
        self.root: Optional[ElementTree.Element] = None
        self.tests: Dict[int, int] = {}
        self.items = 0
        self._stack: List[Optional[ElementTree.Element]] = []
        self._text: Dict[int, List[str]] = {}

    def start(self, tag: str, attrs: Dict[str, str]) -> None:
        depth = len(self._stack)
        element: Optional[ElementTree.Element] = None
        if depth == 0:
            element = self.root = ElementTree.Element(tag, attrs)
        elif depth == 1 and tag == "item" and self._stack[0] is not None:
            self.items += 1
            if self.items > MAX_PROBLEMS:
                raise ProblemIoError("too_many_problems", limit=MAX_PROBLEMS)
            element = ElementTree.SubElement(self._stack[0], tag, attrs)
        elif depth == 2 and tag in _KEPT_TAGS and self._stack[1] is not None:
            item = self._stack[1]
            if tag in _TEST_TAGS:
                key = id(item) * 2 + (tag == "test_output")
                self.tests[key] = self.tests.get(key, 0) + 1
                if self.tests[key] <= MAX_CASES + 1:
                    element = ElementTree.SubElement(item, tag, attrs)
            elif item.find(tag) is None:
                element = ElementTree.SubElement(item, tag, attrs)
        if element is not None:
            self._text[id(element)] = []
        self._stack.append(element)

    def end(self, tag: str) -> None:
        element = self._stack.pop()
        if element is not None and len(self._stack) >= 2:
            element.text = "".join(self._text.pop(id(element), []))
        elif element is not None:
            self._text.pop(id(element), None)

    def data(self, text: str) -> None:
        # 文本归最近的、位于 item 子元素及以下（第 3 层起）的保留元素；fps / item 自身的空白不要
        for element in reversed(self._stack[2:]):
            if element is not None:
                self._text[id(element)].append(text)
                return

    def test_count(self, item: ElementTree.Element, tag: str) -> int:
        return self.tests.get(id(item) * 2 + (tag == "test_output"), 0)


def _parse_xml(source: Union[bytes, IO[bytes]], limit: int = MAX_XML_BYTES) -> "_Builder":
    """expat 流式建树；DOCTYPE / ENTITY / 外部实体一律拒绝。``source`` 是字节或可读流。"""
    stream = io.BytesIO(source) if isinstance(source, (bytes, bytearray)) else source
    builder = _Builder()
    parser = expat.ParserCreate()
    parser.buffer_text = True
    parser.StartDoctypeDeclHandler = _refuse
    parser.EntityDeclHandler = _refuse
    parser.UnparsedEntityDeclHandler = _refuse
    parser.ExternalEntityRefHandler = _refuse
    parser.StartElementHandler = builder.start
    parser.EndElementHandler = builder.end
    parser.CharacterDataHandler = builder.data
    prolog = b""
    seen_root = False
    total = 0
    try:
        while True:
            chunk = stream.read(_CHUNK)
            total += len(chunk)
            if total > limit:
                raise ProblemIoError("member_too_large", path="XML", limit=human_size(limit))
            if not seen_root:
                # 根元素之前的原始字节里查一遍（只查序言：题面 CDATA 里的 <!DOCTYPE html> 是正常内容）
                prolog += chunk
                root_at = prolog.find(b"<fps")
                if _UNSAFE.search(prolog[: root_at if root_at >= 0 else len(prolog)]):
                    raise ProblemIoError("xml_unsafe")
                # 序言超过 1 MB 还没见到根元素就不再攒（声明照样会被 expat 回调拒掉）
                seen_root = root_at >= 0 or len(prolog) > _CHUNK
                if seen_root:
                    prolog = b""
            parser.Parse(chunk, not chunk)
            if not chunk:
                break
    except _Unsafe:
        raise ProblemIoError("xml_unsafe")
    except expat.ExpatError as exc:
        raise ProblemIoError("bad_manifest", path="XML", reason=str(exc)[:200])
    if builder.root is None:
        raise ProblemIoError("bad_manifest", path="XML", reason="没有根元素")
    return builder


def _text(element: Optional[ElementTree.Element]) -> str:
    if element is None:
        return ""
    return "".join(element.itertext())


def _number(text: str) -> Optional[float]:
    try:
        value = float(text.strip())
    except ValueError:
        return None
    return value if math.isfinite(value) and value >= 0 else None


def _limits(item: ElementTree.Element, bundle: ProblemBundle) -> None:
    time_el, memory_el = item.find("time_limit"), item.find("memory_limit")
    time_value = _number(_text(time_el))
    memory_value = _number(_text(memory_el))
    if time_value is None or memory_value is None:
        bundle.warn(notice("limits_defaulted"))
    if time_value is None:
        bundle.time_limit_ms = DEFAULT_TIME_LIMIT_MS
    else:
        unit = (time_el.get("unit") or "s").strip().lower() if time_el is not None else "s"
        bundle.time_limit_ms = int(round(time_value if unit == "ms" else time_value * 1000))
    if memory_value is None:
        bundle.memory_limit_mb = DEFAULT_MEMORY_LIMIT_MB
    else:
        unit = (memory_el.get("unit") or "mb").strip().lower() if memory_el is not None else "mb"
        if unit in ("kb", "k"):
            memory_value /= 1024
        elif unit in ("gb", "g"):
            memory_value *= 1024
        bundle.memory_limit_mb = int(math.ceil(memory_value - 1e-9))


_Test = Tuple[str, Optional[str], bytes]  # (kind, name, data)


def _tests(item: ElementTree.Element) -> List[_Test]:
    found: List[_Test] = []
    for child in item:
        if child.tag in ("test_input", "test_output"):
            name = (child.get("name") or "").strip() or None
            found.append((child.tag, name, _text(child).encode("utf-8")))
    return found


def _unique_names(tests: List[_Test]) -> bool:
    names = [name for _, name, _ in tests]
    return all(names) and len(set(names)) == len(names)


def _pair(tests: List[_Test], bundle: ProblemBundle) -> List[Tuple[str, bytes, bytes]]:
    """[(测试点名, 输入, 答案)]；缺答案时记 ``missing_answer`` 并返回空表。"""
    inputs = [t for t in tests if t[0] == "test_input"]
    outputs = [t for t in tests if t[0] == "test_output"]
    if inputs and _unique_names(inputs) and _unique_names(outputs):
        by_name = {name: data for _, name, data in outputs}
        pairs = []
        for _, name, data in inputs:
            if name not in by_name:
                bundle.fail(notice("missing_answer", name=name))
                return []
            pairs.append((str(name), data, by_name[name]))
        return pairs

    named = _unique_names(inputs)
    kinds = [t[0] for t in tests]
    grouped = "test_output" not in kinds or kinds.index("test_output") >= len(inputs)
    raw: List[Tuple[_Test, Optional[bytes]]] = []
    if grouped:
        for index, test in enumerate(inputs):
            raw.append((test, outputs[index][2] if index < len(outputs) else None))
    else:
        pending: Optional[_Test] = None
        for test in tests:
            if test[0] == "test_input":
                if pending is not None:
                    raw.append((pending, None))
                pending = test
            elif pending is not None:
                raw.append((pending, test[2]))
                pending = None
        if pending is not None:
            raw.append((pending, None))
    pairs = []
    for index, (test, answer) in enumerate(raw, 1):
        name = str(test[1]) if named else str(index)
        if answer is None:
            bundle.fail(notice("missing_answer", name=name))
            return []
        pairs.append((name, test[2], answer))
    return pairs


def _language_ok(element: ElementTree.Element, bundle: ProblemBundle) -> bool:
    language = (element.get("language") or "").strip()
    if language.lower() in _C_LANGUAGES:
        return True
    if language.lower() == "text":
        bundle.fail(notice("text_judge_unsupported"))
    else:
        bundle.fail(notice("checker_language", language=language))
    return False


def _checker(item: ElementTree.Element, bundle: ProblemBundle) -> None:
    if item.find("interactor") is not None and _text(item.find("interactor")).strip():
        bundle.fail(notice("interactive_unsupported"))
        return
    spj, tpj = item.find("spj"), item.find("tpj")
    if spj is not None and (spj.get("language") or "").strip().lower() == "text":
        bundle.fail(notice("text_judge_unsupported"))
        return
    if tpj is not None and _text(tpj).strip():
        if not _language_ok(tpj, bundle):
            return
        source = _text(tpj).encode("utf-8")
        bundle.checker = FileRef(data=source)
        bundle.warn(notice("tpj_order_uncertain"))
        if not checkers.uses_testlib(source):
            bundle.warn(notice("checker_not_testlib"))
        return
    if spj is not None and _text(spj).strip():
        if not _language_ok(spj, bundle):
            return
        source = _text(spj).encode("utf-8")
        wrapper, support = checkers.hustoj_wrapper(source)
        bundle.checker = FileRef(data=wrapper)
        bundle.checker_support.update(support)
        # 外壳本身不 include testlib.h，原 spj 用了的话这里补上（build 只看外壳）
        injected = checkers.inject_testlib(source, bundle.checker_support)
        if injected is not None:
            bundle.warn(injected)
        bundle.warn(notice("wrapped_hustoj_spj"))


def _parse_item(item: ElementTree.Element, index: int, result: ImportResult, tree: _Builder) -> ProblemBundle:
    title = _text(item.find("title")).strip()
    bundle = ProblemBundle(key=str(index), source_name=title or "p%d" % index, ident="p%d" % index)
    bundle.title = title or None
    _limits(item, bundle)
    if any(_text(item.find(tag)).strip() for tag in _TEMPLATE_TAGS):
        bundle.warn(notice("template_dropped"))
    if any(_text(item.find(tag)).strip() for tag in _STATEMENT_TAGS):
        result.warn(notice("statement_dropped"))
    _checker(item, bundle)
    if bundle.errors:
        return bundle
    tests = _tests(item)
    count = tree.test_count(item, "test_input")
    if count > MAX_CASES:
        bundle.fail(notice("too_many_cases", count=count, limit=MAX_CASES))
        return bundle
    for name, data, answer in _pair(tests, bundle):
        bundle.cases.append(BundleCase(name=name, input=FileRef(data=data), answer=FileRef(data=answer)))
    return bundle


def _xml_name(view: ArchiveView) -> str:
    if len(view.names) == 1:
        return view.names[0]
    candidates = [name for name in view.names if name.lower().endswith(".xml")]
    if len(candidates) != 1:
        raise ProblemIoError("unknown_format")
    return candidates[0]


def parse(view: ArchiveView) -> ImportResult:
    """把 ``view``（已剥外壳的视图）解析成 ``ImportResult(format="fps")``。"""
    with view.open(_xml_name(view)) as stream:
        tree = _parse_xml(stream)
    root = tree.root
    if root is None or root.tag != "fps":
        raise ProblemIoError("unknown_format")
    result = ImportResult(format="fps")
    for index, item in enumerate(root.findall("item"), 1):
        result.problems.append(_parse_item(item, index, result, tree))
    return result
