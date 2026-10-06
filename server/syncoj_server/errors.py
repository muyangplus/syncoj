"""统一的错误体。

为什么要有这一层
----------------
FastAPI 默认的 ``HTTPException`` 会把 ``detail`` 原样塞进响应体，于是同一个字段
在不同接口里可能是字符串、字符串数组，甚至一个字典（``{"error": ...}``）。
前端拿到它只能猜，最后长成一堆 ``typeof detail === 'string' ? ... : ...``。
校验失败更糟：pydantic 的 422 里是一条条 ``loc``/``msg``/``type``，全是英文，
教师看到的是 ``{"loc":["body","limit"],"msg":"ensure this value is >= 0"}``。

所以这里的规矩只有三条：

1. ``detail`` **永远是字符串**，而且是一个能直接显示给人看的中文句子
2. ``code`` 永远是稳定的短标识符，前端要分支判断时用它，不要拿 ``detail`` 做
   字符串比较（那句话随时会被改得更通顺）
3. ``details`` 放结构化补充信息（哪个字段、哪个 id、冲突对象是谁），可缺省

三者之外的字段一律不加 —— 响应体不是放"调试信息"的地方。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

__all__ = [
    "ApiError",
    "ERROR_CODES",
    "error_response",
    "install_error_handlers",
]

log = logging.getLogger("syncoj")


# --------------------------------------------------------------------------- #
# 错误码
# --------------------------------------------------------------------------- #

#: 状态码 → 兜底错误码。只在调用方**没有**给 ``code`` 时用。
#:
#: 这种兜底码本身没有信息量，它的作用是让 ``code`` 字段**永远存在**：前端可以
#: 无条件地写 ``error.code``，而不用先判断字段在不在。真正需要分支的地方请在
#: 抛错时显式给出下面那些具名码。
#:
#: 两条要守住的规矩（都有测试）：
#:
#: 1. **这里列到的每个码都必须在** :data:`ERROR_CODES` **里**。否则前端拿到的
#:    是一个它在文档里永远查不到的码 —— 而"兜底"恰恰是最常被走到的那条路
#:    （400 是最常见的错误状态，如果它没登记，那最常见的情况就是没文档的）
#: 2. **服务端真的会返回的状态码都得在这里**。漏一个（曾经漏了 410）的后果
#:    不是"没有码"，而是所有这类响应都拿到下面那个 ``"error"`` 兜底 ——
#:    一个既没写进文档、也没有任何含义的码，前端的分支和日志的 grep 全都白做。
CODE_BY_STATUS = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    410: "gone",
    413: "payload_too_large",
    422: "validation_error",
    429: "rate_limited",
    500: "internal_error",
    503: "unavailable",
}

#: 具名错误码清单。**加新码时同时加到这里**，否则等于悄悄引入一个没人知道的约定。
#:
#: 前端目前只对少数几个做分支（见 web/src/api/client.ts），其余一律直接展示
#: ``detail``。所以这里的原则是"够用就好"：不为了对称给每个接口都编一个码。
#:
#: 还有一条同样重要的反面规矩：**不许登记服务端根本拋不出来的码**。曾经这里躺着
#: 8 个这样的码（``contest_frozen``、``roster_in_use``、``last_admin`` 之类），
#: 它们描述的是"设计时觉得以后可能会有"的场景 —— 而实际对应的校验要么不存在、
#: 要么当初就选了另一种更宽容的做法。死码的害处不在于占地方，而在于它会被当成
#: 契约：前端照着它写分支、文档照着它写章节，于是所有人都以为有那么一条保护在。
#: 现在由 ``tests/test_errors.py::test_没有登记不出来的码`` 守着这件事。
ERROR_CODES = {
    # 认证 / 鉴权
    "unauthorized": "未登录或会话已过期",
    "forbidden": "没有权限执行这个操作",
    "token_expired": "登录已过期，请重新登录",
    "bad_credentials": "用户名或密码不对",
    # 机器注册与配对
    "pairing_required": "这台机器还没有配对到名单里的任何人",
    "machine_revoked": "这台机器已被作废",
    "bootstrap_key_invalid": "统一密钥无效",
    "bootstrap_key_revoked": "统一密钥已被吊销",
    "bootstrap_key_expired": "统一密钥已过期",
    "pair_code_invalid": "配对码不对，或者已经过期了",
    "machine_already_bound": "这台机器已经配对给别人了",
    "machine_mismatch": "凭据与这台机器对不上（凭据可能被复制到了别的机器）",
    "stale_upload": "这份内容已经过期，服务端手上是更新的版本",
    "roster_entry_taken": "这个人已经有一台机器了",
    "no_active_contest": "还没有包含你的场次",
    "ambiguous_contest": "你同时在多个进行中的场次里，需要先指定一个",
    "contest_missing": "给这台机器指定的场次已经被删掉了",
    "contest_player_missing": "指定场次的选手名单里没有这个人，需要先把名单应用到场次",
    # 通用资源
    "bad_request": "提交的内容不合法",
    "not_found": "找不到这个对象",
    "gone": "这个对象已经没了（可能已被撤回，或者内容已被清理）",
    "conflict": "与当前状态冲突",
    "validation_error": "请求内容不合法",
    "rate_limited": "操作太频繁，请稍后再试",
    "payload_too_large": "内容太大了",
    "internal_error": "服务端出错了，请看服务端日志",
    "unavailable": "服务暂时不可用",
    # 业务
    "name_mismatch": "两次输入的确认名称不一致",
    "path_invalid": "路径不合法",
    "release_not_signed": "服务端没有配置发布签名私钥，无法提供升级",
    "release_trust_anchor_missing": "本机没有要内嵌进包里的发布公钥，打出的包机器验不了签名",
    "release_source_missing": "本机没有可用于构建的 Agent 源码",
    "release_build_failed": "构建 Agent 升级包失败",
    "version_mismatch": "提交的版本号和源码里的对不上",
}


# --------------------------------------------------------------------------- #
# 抛错用的异常
# --------------------------------------------------------------------------- #


class ApiError(Exception):
    """业务错误。**优先用它，而不是** ``HTTPException``。

    用法::

        raise ApiError(409, "roster_entry_taken", "%s 已经有一台机器了" % player_no)

    ``detail`` 请写成一句完整的中文，因为前端会把它直接贴在提示条上。
    想省事可以只给 ``code``，此时 ``detail`` 从 :data:`ERROR_CODES` 里取。
    """

    def __init__(
        self,
        status_code: int,
        code: str,
        detail: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> None:
        resolved = detail or ERROR_CODES.get(code)
        if not resolved:
            # 忘了给 detail 又用了一个没登记过的码 —— 直接暴露在开发期，
            # 而不是让用户看到一句 "None"
            raise ValueError("ApiError(%d, %r) 既没给 detail，%r 也不在 ERROR_CODES 里" % (status_code, code, code))
        super().__init__(resolved)
        self.status_code = status_code
        self.code = code
        self.detail = resolved
        self.details = details
        #: 额外的响应头。401 要带 ``WWW-Authenticate``、429 要带 ``Retry-After`` ——
        #: 它们是 HTTP 契约的一部分，不能因为统一了响应体就丢掉
        self.headers = headers


# --------------------------------------------------------------------------- #
# 校验错误的翻译
# --------------------------------------------------------------------------- #

#: 常见查询参数/字段名 → 中文。**不必求全**：没登记的字段直接显示原名，
#: 显示 ``default_roster_id`` 也比显示 ``body.default_roster_id`` 强。
FIELD_LABELS = {
    "username": "用户名",
    "password": "口令",
    "name": "名称",
    "slug": "标识",
    "status": "状态",
    "note": "备注",
    "limit": "每页条数",
    "offset": "起始位置",
    "player_no": "考号",
    "real_name": "姓名",
    "seat": "座位",
    "group_name": "分组",
    "contest_id": "场次",
    "roster_id": "名单",
    "roster_entry_id": "名单条目",
    "problem_id": "题目",
    "agent_id": "机器",
    "path": "路径",
    "target_path": "下发路径",
    "asset_id": "资产",
    "file_id": "代码文件",
    "pair_code": "配对码",
    "bootstrap_key": "统一密钥",
    "machine_uuid": "机器 UUID",
    "machine_fingerprint": "机器指纹",
    "machine_id": "机器编号",
    "hostname": "主机名",
    "os_info": "系统信息",
    "agent_version": "Agent 版本",
    "confirm": "确认名称",
    "confirm_name": "确认名称",
    "kind": "种类",
    # 题目
    "ident": "题目标识",
    "title": "标题",
    "order_index": "顺序",
    "file_patterns": "归题模式",
    # 资产与下发
    "filename": "文件名",
    "dest_dir": "下发路径",
    "target_kind": "下发对象",
    "target_group": "分组",
    "player_ids": "选手",
    "mode": "冲突处理方式",
    "purge": "连未消失的一起清",
    # 名单应用与清场
    "prune": "清理不在名单里的选手",
    "keep_with_submissions": "保留有提交的选手",
    "keep_manual": "保留手工分",
    "older_than_days": "只清几天前的",
    "level": "级别",
    "category": "类别",
    # 成绩
    "score": "分数",
    "max_score": "满分",
    "problem": "题目",
    # 发布
    "version": "版本号",
    "notes": "发布说明",
    "expires_days": "有效天数",
    "label": "用途备注",
    "id": "编号",
}

#: pydantic 的错误类型 → 中文模板。``{label}`` 会被字段中文名替换。
VALIDATION_TEMPLATES = {
    "missing": "{label}不能为空",
    "string_too_short": "{label}太短了",
    "string_too_long": "{label}太长了",
    "string_type": "{label}必须是文本",
    "int_type": "{label}必须是整数",
    "int_parsing": "{label}必须是整数",
    "float_parsing": "{label}必须是数字",
    "bool_type": "{label}必须是真/假",
    "list_type": "{label}必须是列表",
    "dict_type": "{label}必须是对象",
    "greater_than": "{label}太小了",
    "greater_than_equal": "{label}太小了",
    "less_than": "{label}太大了",
    "less_than_equal": "{label}太大了",
    "extra_forbidden": "{label}不是这个接口认得的字段",
    "enum": "{label}只能是限定的几个取值之一",
    "value_error": "{label}不合法",
    "json_invalid": "请求体不是合法的 JSON",
}


def _field_label(loc) -> str:
    """``("body", "player_no")`` → ``考号``。

    只取最后一段：中间那些 ``body``/``query``/``path`` 是 FastAPI 的内部结构，
    对使用者没有意义。
    """
    parts = [str(item) for item in loc if item not in ("body", "query", "path", "header")]
    if not parts:
        return "请求内容"
    # 数组下标（``items.0.name``）用方括号写出来更像人话
    out = ""
    for part in parts:
        if part.isdigit():
            out += "[%s]" % part
        else:
            out += ("." if out else "") + FIELD_LABELS.get(part, part)
    return out


def describe_validation_error(exc: RequestValidationError) -> str:
    """把 pydantic 的 422 揉成一句话。

    多条错误只显示前三条 —— 教师改完第一处再提交，剩下的通常也跟着消失了，
    而一屏滚不完的报错反而让人不知道该先动哪个。
    """
    messages = []
    for error in exc.errors():
        label = _field_label(error.get("loc", ()))
        error_type = str(error.get("type", ""))
        template = VALIDATION_TEMPLATES.get(error_type, "{label}不合法")
        message = template.format(label=label)
        # 范围类错误把边界值贴出来，否则"太小了"等于没说
        ctx = error.get("ctx") or {}
        for key, suffix in (("ge", "（最小 %s）"), ("le", "（最大 %s）"), ("gt", "（最小 %s）"), ("lt", "（最大 %s）")):
            if key in ctx:
                message += suffix % ctx[key]
                break
        messages.append(message)

    if not messages:
        return "请求内容不合法"
    shown = messages[:3]
    if len(messages) > 3:
        shown.append("等 %d 处问题" % len(messages))
    return "；".join(shown)


# --------------------------------------------------------------------------- #
# 安装
# --------------------------------------------------------------------------- #


def _body(
    status_code: int,
    code: str,
    detail: str,
    details: Optional[Dict[str, Any]] = None,
) -> JSONResponse:
    payload: Dict[str, Any] = {"detail": detail, "code": code}
    if details:
        payload["details"] = details
    return JSONResponse(status_code=status_code, content=payload)


def error_response(
    status_code: int,
    code: str,
    detail: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
) -> JSONResponse:
    """按统一错误体造一个响应。

    给**路由之外**的地方用（例如 ``main.py`` 里为 ``PathValidationError``
    注册的处理器、静态兜底）：那些地方拿不到异常处理器的自动包装，
    手写 ``JSONResponse`` 很容易漏掉 ``code`` 或忘了中文句子。
    """
    resolved = detail or ERROR_CODES.get(code)
    if not resolved:
        raise ValueError("error_response(%d, %r) 缺 detail，且该 code 不在 ERROR_CODES 里" % (status_code, code))
    response = _body(status_code, code, resolved, details)
    for key, value in (headers or {}).items():
        response.headers[key] = value
    return response


def install_error_handlers(app: FastAPI) -> None:
    """把四类异常统一成同一种响应体。

    特意**包含** ``Exception`` 兜底：没有它时，未捕获的异常由 Starlette 处理，
    返回一坨纯文本 ``Internal Server Error``，前端的 ``response.json()`` 当场
    抛 ``SyntaxError``，最后界面上显示的是"Unexpected token I"而不是任何有用的
    东西。宁可多写十行，也不要让用户看见解析错误。
    """

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        response = _body(exc.status_code, exc.code, exc.detail, exc.details)
        for key, value in (exc.headers or {}).items():
            response.headers[key] = value
        return response

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        # 兼容仍然在抛 HTTPException 的老代码：detail 可能是任何东西，压成字符串
        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        code = CODE_BY_STATUS.get(exc.status_code, "error")
        response = _body(exc.status_code, code, detail)
        # 429 的 Retry-After 是接口契约的一部分，别在统一过程中丢掉
        if exc.headers:
            for key, value in exc.headers.items():
                response.headers[key] = value
        return response

    @app.exception_handler(RequestValidationError)
    async def _validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # 回显原始错误列表：前端表单要据此把红框标到具体某个输入框上，
        # 而那句汇总的中文只够放在提示条的标题里
        return _body(
            422,
            "validation_error",
            describe_validation_error(exc),
            {"errors": _jsonable_errors(exc)},
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("未处理的异常：%s %s", request.method, request.url.path)
        return _body(500, "internal_error", ERROR_CODES["internal_error"])


def _jsonable_errors(exc: RequestValidationError):
    """把 pydantic 的错误列表压成能 JSON 序列化的形状。

    原始 ``errors()`` 里可能带 ``ValueError`` 之类的对象（``ctx`` 里），
    ``json.dumps`` 会当场炸掉 —— 而抛在异常处理器里的异常没人再兜底。
    """
    out = []
    for error in exc.errors():
        out.append(
            {
                "loc": [str(item) for item in error.get("loc", ())],
                "type": str(error.get("type", "")),
                "message": str(error.get("msg", "")),
            }
        )
    return out
