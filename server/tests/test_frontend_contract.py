"""前端接口路径契约测试。

**解决的问题**：`web/src/api/endpoints.ts` 里的接口路径是手写的字符串模板。
写错一个字母不会有任何编译错误、不会被任何后端测试发现 —— 要等到教师点了
那个按钮才炸，而那时现场正在考试。

这里把前端声明的每个路径与服务端 OpenAPI 里实际存在的路径做**双向**比对：

* 前端声明了、服务端没有 → 那个按钮点下去必然 404
* 服务端有、前端没声明 → 某个功能压根没有入口（"看着像是齐的"）

第二个方向同样重要。只查一个方向的话，"接口改名了但前端忘了改"会在一侧
报错，而"接口加了但没人接"会完全静默 —— 后者正是"少写一个入口"这类
问题的成因。

关于"接口必须被界面调用"
------------------------
那条不在这个文件里了：前端的 `npm run check:routes`
（`web/scripts/check-routes.mjs`）现在承担它，而且做得更准 ——
它读的是 `openapi.json` 与页面源码，跑在 `npm run build` 的最前面。
这里只留一条"那个守卫还在"的哨兵测试，防止它被悄悄删掉之后两边都以为
对方在管。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Set

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ENDPOINTS_SOURCE = REPO_ROOT / "web" / "src" / "api" / "endpoints.ts"
PACKAGE_JSON = REPO_ROOT / "web" / "package.json"
OPENAPI_JSON = REPO_ROOT / "web" / "openapi.json"

#: 前端声明的管理端前缀常量。它变了这里就得跟着变 —— 刻意的：
#: 宁可显式登记，也不要一个"猜出来"的前缀。
ADMIN_PREFIX = "/api/v1/admin"
ADMIN_CONST = "${ADMIN}"

#: 非 /api 开头、但确实存在的路径
EXTRA_SERVER_PATHS = {"/healthz"}

#: 路径里的占位符：``${contestId}``（前端）与 ``{contest_id}``（OpenAPI）
_TEMPLATE_PARAM = re.compile(r"\$\{[^}]*\}|\{[^}]*\}")


def normalize(text: str) -> str:
    """把两种占位符写法都归一成 ``{}``，这样两边才能逐字比对。"""
    return _TEMPLATE_PARAM.sub("{}", text)


def _endpoints_text() -> str:
    if not ENDPOINTS_SOURCE.is_file():
        pytest.skip("找不到 %s" % ENDPOINTS_SOURCE)
    return ENDPOINTS_SOURCE.read_text(encoding="utf-8")


def load_declared_paths() -> List[str]:
    """抽出 ``endpoints.ts`` 里声明的管理端路径模板。

    只认**模板字面量**（反引号包起来的那种）：`paths` 对象的每个值都是一个
    ``() => `${ADMIN}/...` ``。用"反引号 + ${ADMIN}"当锚点，比按调用切块稳得多 ——
    后者一旦换了写法（比如抽出 `paths` 对象）就会静默解析出零条，
    而"零条"看起来和"全都对"一模一样。
    """
    text = _endpoints_text()
    quote = chr(96)
    anchor = quote + ADMIN_CONST
    found: List[str] = []
    index = 0
    while True:
        start = text.find(anchor, index)
        if start < 0:
            break
        end = text.find(quote, start + len(anchor))
        if end < 0:  # pragma: no cover - 语法坏了，交给别的测试去报
            break
        found.append(normalize(text[start + len(anchor) : end]))
        index = end + 1
    return found


def load_server_paths() -> Dict[str, Set[str]]:
    """从 OpenAPI 里读出 ``{规范化路径: 支持的方法集合}``。"""
    if not OPENAPI_JSON.is_file():
        pytest.skip("找不到 %s，先运行 python server/tools/dump_openapi.py" % OPENAPI_JSON)

    schema = json.loads(OPENAPI_JSON.read_text(encoding="utf-8"))
    result: Dict[str, Set[str]] = {}
    for path, operations in schema.get("paths", {}).items():
        methods = {method.upper() for method in operations if method.islower()}
        result.setdefault(normalize(path), set()).update(methods)
    for extra in EXTRA_SERVER_PATHS:
        result.setdefault(extra, set()).add("GET")
    return result


def load_admin_paths() -> Dict[str, Set[str]]:
    """只看管理端 —— Agent 侧不由前端消费，比对进去只会全是噪音。"""
    return {
        path: methods
        for path, methods in load_server_paths().items()
        if (path == ADMIN_PREFIX or path.startswith(ADMIN_PREFIX + "/"))
    }


# --------------------------------------------------------------------------- #
# 自检：先确认测试本身没瞎
# --------------------------------------------------------------------------- #


def test_declared_paths_were_parsed() -> None:
    """防止解析失效导致这些测试"绿得毫无意义"。

    这是这份文件最容易犯的错：换个写法就解析出零条，而断言"没有任何不一致"
    在空集合上永远成立。
    """
    declared = load_declared_paths()
    assert len(declared) >= 40, "只解析出 %d 条路径，解析逻辑可能失效：%r" % (
        len(declared),
        declared,
    )
    for path in declared:
        assert path.startswith("/"), "路径必须以斜杠开头，多半是拼接写错了: %r" % path
        assert " " not in path, "路径里出现空格，多半是拼接写错了: %r" % path
        # 归一化之后只该剩下 `{}` 这一种花括号；还有别的说明漏了某个写法
        residue = path.replace("{}", "")
        assert "{" not in residue and "}" not in residue, (
            "占位符没被归一化，两边的写法会永远对不上: %r" % path
        )


def test_normalizer_treats_both_placeholder_styles_alike() -> None:
    assert normalize("/contests/${contestId}/players") == "/contests/{}/players"
    assert normalize("/contests/{contest_id}/players") == "/contests/{}/players"


# --------------------------------------------------------------------------- #
# 契约
# --------------------------------------------------------------------------- #


def test_every_declared_path_exists_on_server() -> None:
    """前端写了一个服务端不存在的路径 —— 那个按钮点下去就是 404。"""
    server = load_admin_paths()
    missing = sorted({ADMIN_PREFIX + p for p in load_declared_paths()} - set(server))
    assert not missing, (
        "web/src/api/endpoints.ts 里声明了服务端没有的路径（点下去会 404）：\n"
        + "\n".join("  %s" % p for p in missing)
    )


def test_every_server_path_is_declared_in_the_frontend() -> None:
    """服务端有的管理端接口，前端必须知道。

    这一条抓的是"接口加了但没人接"：它会**完全静默** —— 服务端测试全绿、
    前端构建也全绿，只有教师在界面上找不到那个功能。
    """
    undeclared = sorted(set(load_admin_paths()) - {ADMIN_PREFIX + p for p in load_declared_paths()})
    assert not undeclared, (
        "服务端有这些管理端接口，但 web/src/api/endpoints.ts 里没有声明"
        "（等于没有入口）：\n"
        + "\n".join("  %s" % p for p in undeclared)
    )


def test_healthz_is_reachable_from_the_frontend_contract() -> None:
    """``/healthz`` 不在 /api 下，但界面上的服务状态要用它。"""
    assert "/healthz" in load_server_paths()


def test_route_wiring_guard_still_exists() -> None:
    """"每个接口方法都要被界面调用"这条守卫现在归 npm 管。

    留一条哨兵：它要是被删掉，两边都会以为对方在管，于是没人管。
    """
    if not PACKAGE_JSON.is_file():  # pragma: no cover
        pytest.skip("找不到 %s" % PACKAGE_JSON)
    scripts = json.loads(PACKAGE_JSON.read_text(encoding="utf-8")).get("scripts", {})
    assert "check:routes" in scripts, (
        "web/package.json 里没有 check:routes —— "
        "「声明了接口但没人调用」这类问题就再也没人拦了"
    )
    assert "check:routes" in scripts.get("build", ""), (
        "check:routes 必须挂在 npm run build 的最前面，否则它只在有人手动跑时生效"
    )
