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
    ERROR_CODES,
    ApiError,
    install_error_handlers,
)

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "syncoj_server"


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
        if getattr(node.func, "id", None) != "ApiError":
            continue
        value = node.args[1] if len(node.args) >= 2 else None
        for keyword in node.keywords:
            if keyword.arg == "code":
                value = keyword.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            codes.add(value.value)
    return codes


def test_the_scanner_finds_both_call_styles() -> None:
    """先证明扫描器本身有效。

    没有这一条，真实源码里一旦改成别的方式抛错（或者 ``ApiError`` 被重命名），
    下面那个子集断言会退化成"空集合是空集合的子集"，永远绿 —— 这类失效在
    本仓库已经出现过三次。
    """
    found = _codes_in_source(
        "raise ApiError(409, 'roster_entry_taken')\n"
        "raise ApiError(status_code=403, code='machine_unbound')\n"
        "raise ApiError(404, some_variable)\n"      # 动态码：扫不出来，也不该猜
        "raise SomethingElse(409, 'not_ours')\n"
    )

    assert found == {"roster_entry_taken", "machine_unbound"}


def test_every_code_raised_in_the_source_is_registered() -> None:
    """抛出一个没登记的码，等于悄悄引入一条没人知道的约定：

    ``ApiError`` 只给 ``code`` 不给 ``detail`` 时会去查清单，查不到就抛
    ``ValueError`` —— 那意味着这个接口在线上会变成 500，而不是它本来想要的 403。
    """
    raised: Set[str] = set()
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        raised |= _codes_in_source(path.read_text(encoding="utf-8"))

    unknown = sorted(raised - set(ERROR_CODES))
    assert not unknown, "这些错误码在源码里被抛出，却没有登记进 ERROR_CODES：%r" % unknown


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
