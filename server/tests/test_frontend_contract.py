"""前端接口路径契约测试。

**解决的问题**：`web/src/api/index.ts` 里的接口路径是手写的字符串模板。
写错一个字母不会有任何编译错误、不会被任何后端测试发现 —— 要等到教师点了
那个按钮才炸，而那时现场正在考试。

这里把前端声明的每个路径与服务端 OpenAPI 里实际存在的路径做比对，
把"点下去才发现"提前到 CI。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Set, Tuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
API_SOURCE = REPO_ROOT / "web" / "src" / "api" / "index.ts"
OPENAPI_JSON = REPO_ROOT / "web" / "openapi.json"

#: 前端 api 层里声明的基础路径常量。新增常量时这里要一起加 ——
#: 否则对应的路径会被当成未知而报错，这是刻意的：宁可显式登记。
BASE_CONSTANTS: Dict[str, str] = {
    "ADMIN": "/api/v1/admin",
}

#: 非 /api 开头、但确实存在的路径
EXTRA_SERVER_PATHS = {"/healthz"}

#: 路径里出现这些前缀才算接口路径
PATH_PREFIXES = ("/api/", "/healthz")


def _read_balanced(text: str, start: int) -> Tuple[str, int]:
    """从 ``text[start]``（一个 ``{``）开始读配平的大括号块。

    返回 ``(大括号内的内容, 闭合括号之后的索引)``。

    不能只匹配到第一个 ``}`` —— 前端里有 ``${query({ ... })}`` 这种嵌套写法，
    简单正则会把它截断成 ``{})}``，产生一个看起来像真实路径的假象。
    """
    depth = 0
    index = start
    while index < len(text):
        char = text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1 : index], index + 1
        index += 1
    return text[start + 1 :], len(text)


def normalize_template(expr: str) -> str:
    """把 TS 模板字面量规范化成 OpenAPI 风格的路径模板。

    - 路径参数 ``${contestId}`` -> ``{}``
    - 查询串 ``${query({...})}`` -> 整段丢掉（它不属于路径）
    """
    parts: List[str] = []
    index = 0
    while index < len(expr):
        if expr.startswith("${", index):
            # index+1 指向 `${` 的那个 `{`
            inner, next_index = _read_balanced(expr, index + 1)
            if not inner.strip().startswith("query("):
                parts.append("{}")
            index = next_index
        else:
            parts.append(expr[index])
            index += 1

    normalized = "".join(parts)
    # 兜底：万一还有没识别的可选后缀
    return normalized.split("?", 1)[0]


def _source_text() -> str:
    if not API_SOURCE.is_file():
        pytest.skip("找不到 %s" % API_SOURCE)
    text = API_SOURCE.read_text(encoding="utf-8")
    for name, value in BASE_CONSTANTS.items():
        text = text.replace("${%s}" % name, value)
    return text


def load_frontend_calls() -> List[Tuple[str, str]]:
    """抽出前端声明的 ``(方法, 路径模板)``。

    **按 `request` 调用切块**，每个块只看自己的那一小段。

    早先的实现是"取路径字面量之后的 400 个字符找 method:"，结果越界读到了下一个
    函数，把 `GET /contests` 误判成 `POST /contests` —— 契约测试自己造出了一堆
    不存在的"不一致"，比不做还糟。
    """
    text = _source_text()
    calls: List[Tuple[str, str]] = []

    chunks = text.split("request")
    for chunk in chunks[1:]:
        match = re.search(r"`(/[^`]*)`", chunk)
        if not match:
            continue
        raw = match.group(1)
        if not raw.startswith(PATH_PREFIXES):
            continue

        # 方法只可能出现在紧随其后的对象字面量里；遇到下一个 request 就停，
        # 避免越界到下一个函数
        boundary = chunk.find("request", match.end())
        tail = chunk[match.end() : boundary if boundary > 0 else None]
        method_match = re.search(r"method:\s*'([A-Z]+)'", tail)
        method = method_match.group(1) if method_match else "GET"

        calls.append((method, normalize_template(raw)))

    return calls


def load_server_paths() -> Dict[str, Set[str]]:
    """从 OpenAPI 里读出 ``{规范化路径: 支持的方法集合}``。"""
    if not OPENAPI_JSON.is_file():
        pytest.skip("找不到 %s，先运行 python server/tools/dump_openapi.py" % OPENAPI_JSON)

    schema = json.loads(OPENAPI_JSON.read_text(encoding="utf-8"))
    result: Dict[str, Set[str]] = {}
    for path, operations in schema.get("paths", {}).items():
        key = re.sub(r"\{[^}]*\}", "{}", path)
        methods = {method.upper() for method in operations if method.islower()}
        result.setdefault(key, set()).update(methods)
    for extra in EXTRA_SERVER_PATHS:
        result.setdefault(extra, set()).add("GET")
    return result


# --------------------------------------------------------------------------- #
# 自检：先确认测试本身没瞎
# --------------------------------------------------------------------------- #


def test_normalizer_handles_nested_braces() -> None:
    """这是上一版正则翻车的地方：`${query({...})}` 里有嵌套大括号。"""
    assert normalize_template("/a/${query({ player_id: 1 })}") == "/a/"
    assert normalize_template("/a/${contestId}/b") == "/a/{}/b"
    assert (
        normalize_template("/c/${contestId}/judge/score${query({ a: 1, b: 2 })}")
        == "/c/{}/judge/score"
    )
    # 嵌套更深的也要能吃掉
    assert normalize_template("/x${query({ a: { b: 1 } })}") == "/x"


def test_some_calls_were_parsed() -> None:
    """防止正则失效导致这些测试"绿得毫无意义"。"""
    calls = load_frontend_calls()
    assert len(calls) >= 15, "只解析出 %d 个接口调用，解析逻辑可能失效：%r" % (
        len(calls),
        calls,
    )
    for method, path in calls:
        assert method.isupper(), "方法解析异常: %r" % method
        assert " " not in path, "路径里出现空格，多半是拼接写错了: %r" % path


# --------------------------------------------------------------------------- #
# 契约
# --------------------------------------------------------------------------- #


def test_every_frontend_path_exists_on_server() -> None:
    server = load_server_paths()
    unknown = sorted({path for _method, path in load_frontend_calls() if path not in server})

    assert not unknown, (
        "前端声明了服务端不存在的接口路径（这类错误此前只能等教师点按钮才发现）：\n"
        + "\n".join("  %s" % path for path in unknown)
        + "\n\n服务端现有路径：\n"
        + "\n".join("  %s" % p for p in sorted(server))
    )


def test_http_methods_used_by_frontend_are_supported() -> None:
    """路径存在但方法不对（服务端是 PUT、前端写成 POST）同样只会在运行期炸。"""
    server = load_server_paths()
    problems: List[str] = []

    for method, path in load_frontend_calls():
        allowed = server.get(path)
        if allowed is None:
            problems.append("%s %s —— 服务端没有这个路径" % (method, path))
        elif method not in allowed:
            problems.append(
                "%s %s —— 服务端只支持 %s" % (method, path, "/".join(sorted(allowed)))
            )

    assert not problems, "前后端接口不一致：\n" + "\n".join("  " + p for p in problems)
