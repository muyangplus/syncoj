"""FPS 格式（Free Problem Set XML，格式 id ``fps``）。只导入，不导出。

单个 XML（直接上传，或 zip 里只有一个 .xml，HUSTOJ 勾选 zip 时叫 ``problem.xml``）。
根元素 ``<fps>``，每个 ``<item>`` 一道题，测试数据以文本内嵌（``FileRef(data=...)``）。

安全：用 expat 自己建树，遇到 DOCTYPE / ENTITY 声明立即中止（``xml_unsafe``），
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

import math
import re
from typing import Any, Dict, List, Optional, Tuple
from xml.etree import ElementTree
from xml.parsers import expat

from ...judging.testdata import MAX_CASES
from . import checkers
from .archive import ArchiveView
from .bundle import (
    DEFAULT_MEMORY_LIMIT_MB,
    DEFAULT_TIME_LIMIT_MS,
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


def _parse_xml(data: bytes) -> ElementTree.Element:
    """expat 建树；DOCTYPE / ENTITY / 外部实体一律拒绝。"""
    root_at = data.find(b"<fps")
    if _UNSAFE.search(data[: root_at if root_at >= 0 else len(data)]):
        raise ProblemIoError("xml_unsafe")
    builder = ElementTree.TreeBuilder()
    parser = expat.ParserCreate()
    parser.buffer_text = True
    parser.StartDoctypeDeclHandler = _refuse
    parser.EntityDeclHandler = _refuse
    parser.UnparsedEntityDeclHandler = _refuse
    parser.ExternalEntityRefHandler = _refuse
    parser.StartElementHandler = builder.start
    parser.EndElementHandler = builder.end
    parser.CharacterDataHandler = builder.data
    try:
        parser.Parse(data, True)
    except _Unsafe:
        raise ProblemIoError("xml_unsafe")
    except expat.ExpatError as exc:
        raise ProblemIoError("bad_manifest", path="XML", reason=str(exc)[:200])
    return builder.close()


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


def _parse_item(item: ElementTree.Element, index: int, result: ImportResult) -> ProblemBundle:
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
    count = sum(1 for t in tests if t[0] == "test_input")
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
    root = _parse_xml(view.read(_xml_name(view), MAX_XML_BYTES))
    if root.tag != "fps":
        raise ProblemIoError("unknown_format")
    result = ImportResult(format="fps")
    for index, item in enumerate(root.findall("item"), 1):
        result.problems.append(_parse_item(item, index, result))
    return result
