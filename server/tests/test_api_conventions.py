"""接口**形状**的机械检查（`docs/api-conventions.md`）。

这个文件不做语义测试，它盯的是"整个接口面是不是一个形状"：

* 列表信封（`docs/api-conventions.md` §2）
* 结构性删除必须带确认（§5）
* 路径命名（§4）
* `web/openapi.json` 与当前代码一致（§7）

**为什么用机械检查**：这类约定的破口从来不是"某个人不同意"，而是"新加的
第 40 个接口忘了套信封"。人工 review 抓不住它，而它一旦漏掉，前端的通用
数据层就会在那个页面上悄悄失效 —— 表现是"这一页的表格转圈转到天荒地老"。

错误码与错误体由 ``test_errors.py`` 负责，这里不重复。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import pytest

from syncoj_server.config import Settings
from syncoj_server.main import create_app

REPO_ROOT = Path(__file__).resolve().parents[2]
OPENAPI_JSON = REPO_ROOT / "web" / "openapi.json"

#: 信封必须有的四个字段，一个都不能少
ENVELOPE_FIELDS = {"items", "total", "limit", "offset"}

#: 管理端 / Agent 侧前缀
#: 允许的命名空间。三个而不是两个：
#:   admin  —— 管理端（要登录）
#:   agent  —— 考试机（要凭据）
#:   player —— **免登录**的选手页（靠来源 IP 或「场次+考号」定位，见 DESIGN §5.6）
API_PREFIXES = ("/api/v1/admin", "/api/v1/agent", "/api/v1/player")

#: 目前应该有多少个 GET 集合走信封。少一个就说明有人新加列表时忘了套 ——
#: 下限而不是等号：加了新列表接口不该让这条测试变红，只要它是信封。
MIN_ENVELOPE_ENDPOINTS = 14

#: 结构性数据（名单/场次/选手/机器/题目）的**单对象**删除，必须带 `confirm`。
#: 显式列出而不是"凡是 DELETE 都查"：产品数据（代码文件、资产）是软删除，
#: 刻意不要求确认，见 §5.2。
SINGLE_DELETES_NEEDING_CONFIRM = {
    "/api/v1/admin/rosters/{roster_id}",
    "/api/v1/admin/roster-entries/{entry_id}",
    "/api/v1/admin/contests/{contest_id}",
    "/api/v1/admin/players/{player_id}",
    "/api/v1/admin/problems/{problem_id}",
    "/api/v1/admin/agents/{agent_id}",
    "/api/v1/admin/machines/pending/{agent_id}",
    "/api/v1/admin/contests/{contest_id}/judge/score",
}

#: 批量清空：确认走**请求体**（`{"confirm": "..."}`），因为清空删的是一批对象，
#: 没有单个名字可以打（§5.3）。
BULK_CLEARS = {
    "/api/v1/admin/rosters/{roster_id}/entries/clear",
    "/api/v1/admin/contests/{contest_id}/players/clear",
    "/api/v1/admin/contests/{contest_id}/files/clear",
    "/api/v1/admin/contests/{contest_id}/problems/clear",
    "/api/v1/admin/contests/{contest_id}/judge/runs/clear",
    "/api/v1/admin/events/clear",
    "/api/v1/admin/machines/pending/clear",
}

#: 单对象路径的**第一段**必须是复数集合名（§4）
SINGULAR_SEGMENTS = {
    "contest": "contests",
    "player": "players",
    "roster": "rosters",
    "agent": "agents",
    "asset": "assets",
    "problem": "problems",
    "event": "events",
    "deploy": "deploys",
    "file": "files",
    "release": "releases",
}

#: 路径参数必须是 snake_case（`{contest_id}`，不是 `{contestId}`）
_CAMEL_PARAM = re.compile(r"\{[a-z]+[A-Z][^}]*\}")


@pytest.fixture(scope="module")
def spec() -> Dict[str, Any]:
    return create_app(Settings()).openapi()


def _operations(spec: Dict[str, Any]) -> List[tuple]:
    out = []
    for path, item in spec["paths"].items():
        for method, operation in item.items():
            if method.islower():
                out.append((path, method.upper(), operation))
    return out


def _resolve(spec: Dict[str, Any], schema: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """把 ``$ref`` 展开成真正的 schema（只展开一层，够用了）。"""
    if not schema:
        return {}
    ref = schema.get("$ref")
    if not ref:
        return schema
    name = ref.rsplit("/", 1)[-1]
    return spec.get("components", {}).get("schemas", {}).get(name, {})


def _ok_schema(spec: Dict[str, Any], operation: Dict[str, Any]) -> Dict[str, Any]:
    responses = operation.get("responses", {})
    ok = responses.get("200") or {}
    content = ok.get("content", {}).get("application/json", {})
    return _resolve(spec, content.get("schema"))


def _ok_ref_name(operation: Dict[str, Any]) -> str:
    """200 响应直接引用的组件名（没有引用就返回空串）。"""
    content = (
        operation.get("responses", {})
        .get("200", {})
        .get("content", {})
        .get("application/json", {})
    )
    ref = (content.get("schema") or {}).get("$ref", "")
    return ref.rsplit("/", 1)[-1]


def _is_envelope(operation: Dict[str, Any], schema: Dict[str, Any]) -> bool:
    """判断一个响应是不是列表信封。

    **按组件名判断**（``Page_XxxOut_``）而不是"猜哪些字段像信封"：
    ``DeployTaskOut`` 里也有个 ``total``（那是目标总数，不是分页总数），
    按字段猜会把它误判成信封，然后报一个根本不存在的问题。
    """
    if _ok_ref_name(operation).startswith("Page_"):
        return True
    return ENVELOPE_FIELDS <= set(schema.get("properties", {}))


def _envelopes(spec: Dict[str, Any]) -> List[tuple]:
    out = []
    for path, method, operation in _operations(spec):
        if method != "GET":
            continue
        schema = _ok_schema(spec, operation)
        if _is_envelope(operation, schema):
            out.append((path, operation, schema))
    return out


# --------------------------------------------------------------------------- #
# 检查函数本身
#
# 每个检查都写成一个**返回问题清单**的函数，而不是把断言写在 for 里：
# 这样"自检"那一节可以喂合成数据进去，证明它真的会发现问题。
# 一条永远不会红的检查比没有检查更糟 —— 它会让人以为这里守着什么。
# --------------------------------------------------------------------------- #


def find_bare_array_gets(spec: Dict[str, Any]) -> List[str]:
    out = []
    for path, method, operation in _operations(spec):
        if method != "GET":
            continue
        if _ok_schema(spec, operation).get("type") == "array":
            out.append(path)
    return out


def find_incomplete_envelopes(spec: Dict[str, Any]) -> List[tuple]:
    out = []
    for path, _operation, schema in _envelopes(spec):
        missing = ENVELOPE_FIELDS - set(schema.get("properties", {}))
        if missing:
            out.append((path, sorted(missing)))
    return out


def find_envelopes_without_paging_params(spec: Dict[str, Any]) -> List[tuple]:
    out = []
    for path, operation, _schema in _envelopes(spec):
        names = {param["name"] for param in _params(operation, "query")}
        if not {"limit", "offset"} <= names:
            out.append((path, sorted(names)))
    return out


def find_single_deletes_without_confirm(
    spec: Dict[str, Any], paths: Optional[Set[str]] = None
) -> List[tuple]:
    out = []
    for path in sorted(paths if paths is not None else SINGLE_DELETES_NEEDING_CONFIRM):
        operation = spec["paths"].get(path, {}).get("delete")
        if operation is None:
            out.append((path, "路径或 DELETE 方法不存在"))
            continue
        confirm = {p["name"]: p for p in _params(operation, "query")}.get("confirm")
        if confirm is None:
            out.append((path, "缺少 confirm 参数"))
        elif not confirm.get("required"):
            out.append((path, "confirm 不是必填 —— 传空就会绕过确认"))
    return out


def find_clears_without_confirm(
    spec: Dict[str, Any], paths: Optional[Set[str]] = None
) -> List[tuple]:
    out = []
    for path in sorted(paths if paths is not None else BULK_CLEARS):
        operation = spec["paths"].get(path, {}).get("post")
        if operation is None:
            out.append((path, "路径或 POST 方法不存在"))
            continue
        request_body = operation.get("requestBody", {})
        schema = _resolve(
            spec,
            request_body.get("content", {}).get("application/json", {}).get("schema", {}),
        )
        properties = schema.get("properties", {})
        if "confirm" not in properties:
            out.append((path, "请求体里没有 confirm"))
        elif "confirm" not in schema.get("required", []):
            out.append((path, "confirm 不是必填 —— 传空就会绕过确认"))
    return out


def find_singular_collection_segments(spec: Dict[str, Any]) -> List[tuple]:
    out = []
    for path in spec["paths"]:
        segments = [s for s in path.split("/") if s]
        if len(segments) < 3 or segments[:2] != ["api", "v1"]:
            continue
        # 只看资源段，且只看**后面还有东西**的那种：
        #
        # * 第 3 段是领域名（`/api/v1/agent/...` 里的 `agent`），它天生是单数
        # * **最后一段可以是单数** —— 那是字段或动作（`/agents/{id}/contest`
        #   是给这台机器指定场次），不是集合名。集合名后面一定还跟着
        #   `{id}` 或者更深的一层
        for index, segment in enumerate(segments):
            if index < 3 or index + 1 >= len(segments):
                continue
            if segment in SINGULAR_SEGMENTS:
                out.append((path, segment))
    return out


def _params(operation: Dict[str, Any], location: str) -> List[Dict[str, Any]]:
    return [
        param
        for param in operation.get("parameters", [])
        if param.get("in") == location
    ]


# --------------------------------------------------------------------------- #
# §2 列表信封
# --------------------------------------------------------------------------- #


def test_no_get_endpoint_returns_a_bare_array(spec: Dict[str, Any]) -> None:
    """GET 集合一律走信封。

    裸数组是最容易漏的一处：它不会报错、类型也对得上（`T[]`），
    但前端的通用数据层拿到 `items` 时会是 `undefined`。
    """
    offenders = find_bare_array_gets(spec)
    assert not offenders, "这些 GET 接口返回裸数组，没有套列表信封：\n" + "\n".join(
        "  %s" % p for p in offenders
    )


def test_envelope_endpoints_have_all_four_fields(spec: Dict[str, Any]) -> None:
    """凡是被当成信封建模的响应，四个字段必须齐全。

    少一个 `total` 的话，分页器会拿 `items.length` 充数 —— 第二页就显示
    "共 50 条"。
    """
    offenders = find_incomplete_envelopes(spec)
    assert not offenders, "这些信封少了字段：\n" + "\n".join(
        "  %s -> %s" % item for item in offenders
    )

    envelopes = _envelopes(spec)
    assert len(envelopes) >= MIN_ENVELOPE_ENDPOINTS, (
        "只找到 %d 个信封接口，预期至少 %d 个 —— "
        "要么列表接口被改回了裸数组，要么它们不再走 Page 模型"
        % (len(envelopes), MIN_ENVELOPE_ENDPOINTS)
    )


def test_list_endpoints_accept_limit_and_offset(spec: Dict[str, Any]) -> None:
    """每个集合接口都要认 `limit` / `offset`，名字固定（§2）。

    名字一样，前端才能只写一套分页逻辑；某个接口把 `limit` 写成 `page_size`，
    它就退化成要单独适配的那一个。
    """
    offenders = find_envelopes_without_paging_params(spec)
    assert not offenders, "这些集合接口没有 limit/offset 参数：\n" + "\n".join(
        "  %s -> %s" % item for item in offenders
    )


# --------------------------------------------------------------------------- #
# §5 删除语义
# --------------------------------------------------------------------------- #


def test_structural_single_deletes_require_confirm(spec: Dict[str, Any]) -> None:
    """删结构性数据必须把名字打一遍，而且校验在**服务端**。

    前端弹窗是给人看的提示，服务端校验才是"没经过界面的调用也一样安全"。
    """
    offenders = find_single_deletes_without_confirm(spec)
    assert not offenders, "这些删除接口没有服务端确认：\n" + "\n".join(
        "  %s: %s" % item for item in offenders
    )


def test_bulk_clears_require_confirm_in_the_body(spec: Dict[str, Any]) -> None:
    """批量清空的确认走请求体 —— 清空没有单个名字可打，只有范围的名字。"""
    offenders = find_clears_without_confirm(spec)
    assert not offenders, "这些清空接口没有服务端确认：\n" + "\n".join(
        "  %s: %s" % item for item in offenders
    )


def test_clear_paths_are_post_not_delete() -> None:
    """这是对 `docs/api-conventions.md` §5.1 那个例子的守卫。

    清空是**动作**（`POST .../clear`）而不是"删掉这个集合"：DELETE 带请求体
    在浏览器与各种客户端上支持得七零八落，而确认值必须走请求体才不会被
    copy-paste 丢掉。`docs/api-conventions.md` 里给的例子就是 POST。
    """
    assert all(path.endswith("/clear") for path in BULK_CLEARS)
    assert len(BULK_CLEARS) >= 6, "清空入口少了一半以上，多半是漏掉了确认"


# --------------------------------------------------------------------------- #
# §4 命名
# --------------------------------------------------------------------------- #


def test_every_path_is_under_a_known_prefix(spec: Dict[str, Any]) -> None:
    extra = {"/healthz"}
    bad = [
        path
        for path in spec["paths"]
        if path not in extra and not path.startswith(API_PREFIXES)
    ]
    assert not bad, "这些路径不在 /api/v1/admin 或 /api/v1/agent 下：%r" % bad


def test_path_parameters_are_snake_case(spec: Dict[str, Any]) -> None:
    """`{contestId}` 这种驼峰占位符在 Python 侧会被忽略，参数直接对不上。"""
    bad = [path for path in spec["paths"] if _CAMEL_PARAM.search(path)]
    assert not bad, "路径参数应当是 snake_case：%r" % bad


def test_no_trailing_slashes(spec: Dict[str, Any]) -> None:
    assert not [path for path in spec["paths"] if path != "/" and path.endswith("/")]


def test_first_segment_is_plural_for_collections(spec: Dict[str, Any]) -> None:
    """集合用复数名词（§4）。

    单复数混着来最直接的后果是前端生成客户端时多出一层例外 ——
    而"多出一层例外"是这个约定当初被写下来要消灭的东西。
    """
    offenders = find_singular_collection_segments(spec)
    assert not offenders, "这些路径段用了单数名词：\n" + "\n".join(
        "  %s -> %s（应为 %s）" % (p, s, SINGULAR_SEGMENTS[s]) for p, s in offenders
    )


# --------------------------------------------------------------------------- #
# 自检：证明上面那些检查真的会红
#
# 这一节比看起来重要。一台"永远不会红的检查"比没有检查更糟：它会让人
# 以为这里守着什么。所以每个检查都喂一份**故意做坏**的规范进去，
# 断言它确实报出了问题。
# --------------------------------------------------------------------------- #


def _synthetic_spec(*, array_get: bool = False, thin_envelope: bool = False,
                    no_confirm: bool = False) -> Dict[str, Any]:
    """造一份最小可用的规范，按开关做坏某几处。

    ``no_confirm`` 那个开关作用在**真实的** ``SINGLE_DELETES_NEEDING_CONFIRM``
    里的一条路径上 —— 自检要验的正是"生产清单里的那条约束会不会红"，
    换成假路径就只能验到循环本身。
    """
    envelope = {
        "properties": {
            "items": {"type": "array"},
            "total": {"type": "integer"},
            "limit": {"type": "integer"},
            "offset": {"type": "integer"},
        }
    }
    if thin_envelope:
        envelope["properties"].pop("total")

    collection_response = {
        "content": {
            "application/json": {"schema": {"$ref": "#/components/schemas/Page_WidgetOut_"}}
        }
    }
    if array_get:
        collection_response = {
            "content": {"application/json": {"schema": {"type": "array"}}}
        }

    envelope_operation = {
        "parameters": [
            {"name": "limit", "in": "query", "required": False},
            {"name": "offset", "in": "query", "required": False},
        ],
        "responses": {"200": collection_response},
    }

    target = "/api/v1/admin/players/{player_id}"
    delete_operation: Dict[str, Any] = {"parameters": [], "responses": {"200": {}}}
    if not no_confirm:
        delete_operation["parameters"] = [
            {"name": "confirm", "in": "query", "required": True}
        ]

    return {
        "paths": {
            "/api/v1/admin/widgets": {"get": envelope_operation},
            target: {"delete": delete_operation},
            # 单数的集合名（后面还跟着更深的一层）—— 这一条应当被认出来
            "/api/v1/admin/player/{player_id}/parts": {"get": envelope_operation},
        },
        "components": {"schemas": {"Page_WidgetOut_": envelope}},
    }


def test_selfcheck_detects_a_bare_array() -> None:
    assert find_bare_array_gets(_synthetic_spec(array_get=True)) == [
        "/api/v1/admin/widgets",
        "/api/v1/admin/player/{player_id}/parts",
    ]
    assert find_bare_array_gets(_synthetic_spec()) == []


def test_selfcheck_detects_a_thin_envelope() -> None:
    offenders = find_incomplete_envelopes(_synthetic_spec(thin_envelope=True))
    assert offenders == [
        ("/api/v1/admin/widgets", ["total"]),
        ("/api/v1/admin/player/{player_id}/parts", ["total"]),
    ], offenders
    assert find_incomplete_envelopes(_synthetic_spec()) == []


def test_selfcheck_detects_a_delete_without_confirm() -> None:
    target = {"/api/v1/admin/players/{player_id}"}
    spec = _synthetic_spec(no_confirm=True)
    offenders = find_single_deletes_without_confirm(spec, target)
    assert offenders == [(sorted(target)[0], "缺少 confirm 参数")], offenders

    ok = find_single_deletes_without_confirm(_synthetic_spec(), target)
    assert ok == [], ok


def test_selfcheck_detects_a_singular_collection() -> None:
    offenders = find_singular_collection_segments(_synthetic_spec())
    assert ("/api/v1/admin/player/{player_id}/parts", "player") in offenders, offenders


def test_selfcheck_clears_without_confirm_is_detected() -> None:
    """清空接口少一个必填 confirm —— 必须被发现。"""
    path = "/api/v1/admin/machines/pending/clear"
    schema = {"properties": {"confirm": {"type": "string"}}, "required": ["confirm"]}
    good = {
        "paths": {
            path: {
                "post": {
                    "requestBody": {
                        "content": {"application/json": {"schema": schema}}
                    },
                    "responses": {"200": {}},
                }
            }
        }
    }
    bad = json.loads(json.dumps(good))
    bad["paths"][path]["post"]["requestBody"]["content"]["application/json"]["schema"][
        "required"
    ] = []

    target = {path}
    assert find_clears_without_confirm(good, target) == []
    offenders = find_clears_without_confirm(bad, target)
    assert (path, "confirm 不是必填 —— 传空就会绕过确认") in offenders, offenders


def test_selfcheck_placeholder_detection(spec: Dict[str, Any]) -> None:
    """`{contestId}` 这种驼峰占位符必须被发现。"""
    assert _CAMEL_PARAM.search("/api/v1/admin/contests/{contestId}/players")
    assert not _CAMEL_PARAM.search("/api/v1/admin/contests/{contest_id}/players")
    assert not [p for p in spec["paths"] if _CAMEL_PARAM.search(p)]


def test_placeholders_are_declared_once(spec: Dict[str, Any]) -> None:
    """同一个路径里同一个占位符出现两次，FastAPI 会静默取其中一个 —— 排查很费劲。"""
    bad = []
    for path in spec["paths"]:
        names = re.findall(r"\{([^}]*)\}", path)
        if len(names) != len(set(names)):
            bad.append(path)
    assert not bad, "路径里有重复占位符：%r" % bad


# --------------------------------------------------------------------------- #
# §7 生成物
# --------------------------------------------------------------------------- #


def test_openapi_json_matches_the_current_code(spec: Dict[str, Any]) -> None:
    """`web/openapi.json` 必须与当前代码一致。

    它是**生成物**：与代码不一致时，前端拿到的是旧类型 —— 少一个字段在
    TypeScript 里不会报错，只会让那个字段永远是 `undefined`。

    比的是**解析后的字典**而不是字节：格式化方式（缩进、键序、行尾）不属于
    契约，把它算进去只会让"重新生成一次就好了"这种噪声盖住真正的不一致。
    """
    if not OPENAPI_JSON.is_file():
        pytest.skip("找不到 %s，先运行 python server/tools/dump_openapi.py" % OPENAPI_JSON)
    committed = json.loads(OPENAPI_JSON.read_text(encoding="utf-8"))
    assert committed == spec, (
        "web/openapi.json 与当前代码不一致 —— 在仓库根目录跑一次：\n"
        "    python server/tools/dump_openapi.py\n"
        "然后（前端类型也依赖它）：\n"
        "    cd web && npm run gen:types"
    )
