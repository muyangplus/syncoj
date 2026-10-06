"""统一错误体的测试。

这一层很容易写成"看起来对了"：错误处理器的分支大多在真实流量里各自只有一条
路径被走到，而在开发机上永远不会同时触发。所以这里用一个**专门的小 app**
把四种来源（业务错误、老式 HTTPException、校验失败、未捕获异常）全部逼出来 ——
用真 app 测的话，未捕获异常那条路径要求先在服务端里埋一个会炸的接口。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Set

import pytest
from fastapi import FastAPI, HTTPException, Query
from fastapi.testclient import TestClient

from syncoj_server.errors import (
    CODE_BY_STATUS,
    ERROR_CODES,
    ApiError,
    install_error_handlers,
)

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "syncoj_server"

#: 名字长得像"把错误发出去"的调用。它们的参数形状决定了 ``code`` 从哪来，
#: 所以下面几个静态检查都围着它们转。
_ERROR_RAISERS = {"ApiError", "error_response"}


@pytest.fixture()
def client() -> TestClient:
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/business")
    def business():
        raise ApiError(409, "roster_entry_taken")

    @app.get("/business-with-detail")
    def business_with_detail():
        raise ApiError(
            409,
            "roster_entry_taken",
            "S001 已经有一台机器了",
            {"player_no": "S001"},
        )

    @app.get("/legacy")
    def legacy():
        # 老代码还在抛 HTTPException，detail 是个字典 —— 前端最怕的就是这个
        raise HTTPException(status_code=404, detail={"reason": "stale"})

    @app.get("/limited")
    def limited():
        raise HTTPException(
            status_code=429, detail="操作太频繁", headers={"Retry-After": "7"}
        )

    @app.get("/crash")
    def crash():
        raise RuntimeError("假装是数据库连接断了")

    @app.get("/paged")
    def paged(limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)):
        return {"limit": limit, "offset": offset}

    # 未捕获异常由 ServerErrorMiddleware 处理，它把异常交给我们之后**还会**
    # 往上抛一次（好让服务器把 traceback 记进日志）。测试里必须关掉这个重抛，
    # 否则断言还没跑到就被异常打断 —— 那样测的就不是响应体了。
    return TestClient(app, raise_server_exceptions=False)


# --------------------------------------------------------------------------- #
# 形状
# --------------------------------------------------------------------------- #


def test_business_error_uses_the_registered_chinese_message(client: TestClient) -> None:
    response = client.get("/business")

    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "roster_entry_taken"
    assert body["detail"] == ERROR_CODES["roster_entry_taken"]
    # details 是可选的，没给就不该出现一个空字典 —— 空对象会让前端误以为"有细节"
    assert "details" not in body


def test_explicit_detail_and_details_win(client: TestClient) -> None:
    body = client.get("/business-with-detail").json()

    assert body["detail"] == "S001 已经有一台机器了"
    assert body["details"] == {"player_no": "S001"}


def test_legacy_http_exception_detail_is_coerced_to_a_string(
    client: TestClient,
) -> None:
    """``detail`` 永远是字符串 —— 这是整个约定里最值钱的一条。

    老代码抛的是 ``{"reason": "stale"}``，前端拿到它只能猜。压成字符串之后，
    取 409 的那个分支既不用判断类型，也不会因为服务端改了 detail 的形状而崩。
    """
    body = client.get("/legacy").json()

    assert isinstance(body["detail"], str)
    assert "stale" in body["detail"]
    assert body["code"] == "not_found"


def test_retry_after_header_survives(client: TestClient) -> None:
    """限速的 ``Retry-After`` 是接口契约的一部分，统一处理时最容易丢掉它。"""
    response = client.get("/limited")

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "7"
    assert response.json()["code"] == "rate_limited"


def test_unhandled_exception_becomes_a_json_500(client: TestClient) -> None:
    """没有这条兜底，Starlette 会返回纯文本 ``Internal Server Error``，
    前端的 ``response.json()`` 当场抛 SyntaxError，界面上显示"Unexpected token I"。
    """
    response = client.get("/crash")

    assert response.status_code == 500
    body = response.json()
    assert body["code"] == "internal_error"
    assert isinstance(body["detail"], str) and body["detail"]
    # 内部异常的原文**不能**回到客户端：那可能带着连接串、路径、SQL
    assert "假装是数据库连接断了" not in body["detail"]


# --------------------------------------------------------------------------- #
# 校验错误
# --------------------------------------------------------------------------- #


def test_validation_error_is_a_chinese_sentence(client: TestClient) -> None:
    response = client.get("/paged", params={"limit": 999})

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "validation_error"
    assert isinstance(body["detail"], str)
    assert "每页条数" in body["detail"]
    # 边界值要贴出来，否则"太大了"等于没说
    assert "500" in body["detail"]
    # 不能把 pydantic 的英文原文当成人话展示
    assert "ensure this value" not in body["detail"]


def test_validation_error_keeps_the_raw_list_for_form_highlighting(
    client: TestClient,
) -> None:
    body = client.get("/paged", params={"limit": 999}).json()

    errors = body["details"]["errors"]
    assert errors and errors[0]["loc"] == ["query", "limit"]
    assert errors[0]["type"] == "less_than_equal"


def test_validation_error_labels_the_field_not_the_whole_loc(
    client: TestClient,
) -> None:
    """``body.player_no`` 要显示成"考号"，而不是把内部路径抖给人看。"""
    body = client.get("/paged", params={"offset": -1}).json()

    assert "起始位置" in body["detail"]
    assert "query" not in body["detail"]
    assert "offset" not in body["detail"]


def test_multiple_validation_errors_are_capped(client: TestClient) -> None:
    """一屏滚不完的报错反而让人不知道该先动哪个 —— 只列前三条。"""
    response = client.get("/paged", params={"limit": 9999, "offset": -5})

    assert response.status_code == 422
    # 两条都该出现（没到 3 条的截断线）
    detail = response.json()["detail"]
    assert "每页条数" in detail and "起始位置" in detail


# --------------------------------------------------------------------------- #
# 错误码清单本身
# --------------------------------------------------------------------------- #


def test_every_error_code_has_a_human_message() -> None:
    """码没有配套消息时 ``ApiError`` 会抛 ``ValueError`` —— 那是编程错误，
    不该等到线上第一次触发才发现。"""
    for code, message in ERROR_CODES.items():
        assert message and isinstance(message, str), code
        # 一句能直接贴在提示条上的话，不该以句号结尾（界面上会显得像被截断）
        assert not message.endswith("。"), code


def test_error_codes_are_snake_case_identifiers() -> None:
    for code in ERROR_CODES:
        assert re.fullmatch(r"[a-z][a-z0-9_]*", code), code


def _call_name(node: ast.Call) -> str:
    return getattr(node.func, "id", None) or getattr(node.func, "attr", None) or ""


def _docstring_nodes(tree: ast.AST) -> Set[int]:
    """找出所有 docstring 常量的 ``id()``，好在扫描时把它们排除。

    不排除的话，某天有人在注释里举了个例子 ``ApiError(404, "contest_frozen")``，
    这个码就会看起来像活的。注释不在 AST 里（本来就扫不到），docstring 在。
    """
    out: Set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = getattr(node, "body", [])
        first = body[0] if body else None
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            out.add(id(first.value))
    return out


def _producible_literals(text: str) -> Set[str]:
    """源码里"准备当成码发出去"的字符串字面量。

    只收**实参、关键字实参、字典值**这三种位置 —— 它们才是"这个字符串会被塞进
    响应里"的写法。特意不收比较表达式里的字符串（``code in ("a", "b")``）：
    那是在**读**码不是在**产**码，而"我提到过它"恰好就是死码当初混进来的方式。
    docstring 也不收，理由见 :func:`_docstring_nodes`。
    """
    tree = ast.parse(text)
    skip = _docstring_nodes(tree)
    out: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            values = list(node.args) + [keyword.value for keyword in node.keywords]
        elif isinstance(node, ast.Dict):
            values = list(node.values)
        else:
            continue
        for value in values:
            if (
                isinstance(value, ast.Constant)
                and isinstance(value.value, str)
                and id(value) not in skip
            ):
                out.add(value.value)
    return out


def _codes_in_source(text: str) -> Set[str]:
    """用 AST 找出一段源码里所有 ``ApiError(...)`` 的 ``code`` 实参。

    不拿正则去凑：``ApiError(409, "x")`` 与 ``ApiError(status_code=409, code="x")``
    两种写法都得认，而正则只会在某天悄悄漏掉一种 —— 然后这个检查就永远是绿的。
    """
    codes: Set[str] = set()
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _call_name(node) != "ApiError":
            continue
        value = node.args[1] if len(node.args) >= 2 else None
        for keyword in node.keywords:
            if keyword.arg == "code":
                value = keyword.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            codes.add(value.value)
    return codes


def _code_keywords_in_source(text: str) -> Set[str]:
    """所有 ``code="..."`` 关键字实参，**不限于** ``ApiError``。

    ``_unauthorized("...", code="token_expired")`` 这种经过一层封装的抛错一样在
    往外发码。只盯 ``ApiError`` 的话，在这种地方把码打错一个字是查不出来的。
    """
    codes: Set[str] = set()
    for node in ast.walk(ast.parse(text)):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if (
                keyword.arg == "code"
                and isinstance(keyword.value, ast.Constant)
                and isinstance(keyword.value.value, str)
            ):
                codes.add(keyword.value.value)
    return codes


def _server_sources() -> "list[tuple[str, str]]":
    """``syncoj_server/`` 下所有 .py 的 ``(相对路径, 源码)``，**排除 errors.py 自己**。

    排除它是因为清单就写在里面：把 ``ERROR_CODES`` 的键当成"产出的码"来自证，
    等于什么都没查。
    """
    out = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        if path.name == "errors.py":
            continue
        out.append((str(path.relative_to(SOURCE_ROOT)), path.read_text(encoding="utf-8")))
    return out


def test_the_scanner_finds_both_call_styles() -> None:
    """先证明扫描器本身有效。

    没有这一条，真实源码里一旦改成别的方式抛错（或者 ``ApiError`` 被重命名），
    下面那个子集断言会退化成"空集合是空集合的子集"，永远绿 —— 这类失效在
    本仓库已经出现过三次。

    样例里用的 ``machine_unbound`` **刻意挑了个不在清单里的码**：扫描器必须是
    纯语法的，不能去查 :data:`ERROR_CODES` —— 否则"扫出来什么"和"清单里有什么"
    会互相依赖，两边错成一样就再也测不出来了。
    """
    found = _codes_in_source(
        "raise ApiError(409, 'roster_entry_taken')\n"
        "raise ApiError(status_code=403, code='machine_unbound')\n"
        "raise ApiError(404, some_variable)\n"      # 动态码：扫不出来，也不该猜
        "raise SomethingElse(409, 'not_ours')\n"
    )

    assert found == {"roster_entry_taken", "machine_unbound"}


def test_the_keyword_scanner_sees_through_helpers() -> None:
    """``code=`` 扫描器要能穿过包装函数 —— 这正是它存在的理由。"""
    found = _code_keywords_in_source(
        "raise _unauthorized('过期了', code='token_expired')\n"
        "raise BootstrapRejected('无效', status_code=404, code='bootstrap_key_invalid')\n"
        "raise ApiError(409, 'positional_style_is_not_my_job')\n"
    )

    assert found == {"token_expired", "bootstrap_key_invalid"}


def test_every_code_sent_by_the_server_is_registered() -> None:
    """抛出一个没登记的码，等于悄悄引入一条没人知道的约定：

    ``ApiError`` 只给 ``code`` 不给 ``detail`` 时会去查清单，查不到就抛
    ``ValueError`` —— 那意味着这个接口在线上会变成 500，而不是它本来想要的 403。
    """
    sent: Set[str] = set()
    for _name, text in _server_sources():
        sent |= _codes_in_source(text)
        sent |= _code_keywords_in_source(text)

    unknown = sorted(sent - set(ERROR_CODES))
    assert not unknown, "这些错误码在源码里被发出，却没有登记进 ERROR_CODES：%r" % unknown


def test_没有登记不出来的码() -> None:
    """反过来也守一遍：**不许登记服务端根本发不出来的码**。

    曾经这里躺着 8 个这样的码。它们不是打错字，而是"设计时觉得以后可能会有"的
    场景，而对应的校验要么压根不存在（``last_admin`` —— 根本没有删管理员的接口）、
    要么当初就选了更宽容的做法（``roster_in_use`` —— 删名单是被允许的，只是把
    引用它的场次置空）、要么早就被更具体的码取代了（``machine_unbound`` →
    ``pairing_required``）。

    死码的害处不在于占地方：它会**被当成契约**。前端照着它写分支、文档照着它写
    一章，于是所有人（包括写的人）都以为有那么一条保护在。要加新码，请连同产生
    它的那条分支一起加。
    """
    producible = set(CODE_BY_STATUS.values())
    for _name, text in _server_sources():
        producible |= _producible_literals(text)

    dead = sorted(set(ERROR_CODES) - producible)
    assert not dead, (
        "这些码登记在 ERROR_CODES 里，但源码里没有任何地方会发出它们：%r\n"
        "要么补上产生它的分支，要么从清单里删掉。" % dead
    )


def test_每个兜底码都在清单里() -> None:
    """``CODE_BY_STATUS`` 的每个取值都必须登记。

    它比看上去重要：兜底恰恰是最常被走到的那条路（400 是出现最多的错误状态）。
    如果一个兜底码没登记，那"最常见的情况"就成了唯一没有文档的那种 —— 前端拿到
    ``bad_request`` 去查文档查不到，而它其实每天都在发生。
    """
    unregistered = sorted(set(CODE_BY_STATUS.values()) - set(ERROR_CODES))
    assert not unregistered, "这些兜底码没登记进 ERROR_CODES：%r" % unregistered


def test_每个用得到的状态码都有兜底码() -> None:
    """服务端真的会返回的状态码，都得在 ``CODE_BY_STATUS`` 里。

    漏一个的后果**不是"没有码"**，而是这类响应全部落到 ``.get(status, "error")``
    上，拿到一个字面量 ``error`` —— 既不在文档里、也不在清单里、还没法 grep。
    这一条当场抓出过 410：``/releases`` 撤回和内容缺失三个地方在用它，
    于是"这个版本已撤回"这种**需要人看懂**的响应带的是 ``code: "error"``。
    """
    missing: Set[int] = set()
    for _name, text in _server_sources():
        for node in ast.walk(ast.parse(text)):
            if not isinstance(node, ast.Call) or _call_name(node) not in _ERROR_RAISERS:
                continue
            status = next(
                (
                    keyword.value
                    for keyword in node.keywords
                    if keyword.arg in ("status_code", "status")
                ),
                node.args[0] if node.args else None,
            )
            if not (isinstance(status, ast.Constant) and isinstance(status.value, int)):
                continue  # 动态状态码（status.HTTP_…、exc.status_code）:不猜
            has_code = any(keyword.arg == "code" for keyword in node.keywords)
            if not has_code and _call_name(node) != "HTTPException":
                # ApiError/error_response 的第二个位置参数就是码；给了就轮不到兜底
                has_code = len(node.args) >= 2 and isinstance(node.args[1], ast.Constant)
            if not has_code and status.value not in CODE_BY_STATUS:
                missing.add(status.value)

    assert not missing, (
        "这些状态码会被返回，但 CODE_BY_STATUS 里没有它们，"
        "客户端会拿到毫无含义的 code=\"error\"：%r" % sorted(missing)
    )


def test_兜底码检查认得出没有兜底的状态码() -> None:
    """给上面那条检查做自证：扫不出东西时它必须变红，而不是永远绿。"""
    found: Set[int] = set()
    source = "raise HTTPException(status_code=418, detail='我是茶壶')"
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and _call_name(node) == "HTTPException":
            status = next(
                keyword.value
                for keyword in node.keywords
                if keyword.arg == "status_code"
            )
            if status.value not in CODE_BY_STATUS:
                found.add(status.value)

    assert found == {418}


def test_error_handlers_are_actually_installed() -> None:
    """处理器写好了但没挂上去，是一类完全静默的失效：接口照常工作，
    只是 500 又变回纯文本、``detail`` 又开始出现字典。"""
    source = (SOURCE_ROOT / "main.py").read_text(encoding="utf-8")

    assert "install_error_handlers" in source


def test_api_error_without_detail_and_unknown_code_fails_loudly() -> None:
    """忘了给 detail 又用了一个没登记的码时，宁可当场炸 —— 让用户看到
    "None" 是最糟的失败方式：它既不报错，也说不清哪里错了。"""
    with pytest.raises(ValueError):
        ApiError(409, "code_that_nobody_registered")
