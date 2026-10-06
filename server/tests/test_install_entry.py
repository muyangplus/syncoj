"""装机入口：一台什么都没有的机器从哪儿拿到 Agent。

这四个端点**故意不鉴权** —— 初次安装时机器手上没有 Agent、没有凭据、什么都没有，
要求鉴权就没有入口。所以这里最要紧的三条不是"能不能下"，而是：

* **它只发已经铺开的版本**。上传和铺开的风险分界在别处都是硬的，这里也得硬：
  一个教师刚构建出来、还没敢铺开的包，不该能被装机入口拿走。
* **台账里报的路径必须真的存在**。台账与路由各写一遍路径的话，某天改了路由就会
  指到一个 404 上，而现象是"装机装不上" —— 所以有一条测试拿台账里的路径去真的
  请求一遍。
* **自举脚本必须真的是通的**。它从前 curl 的是 `/dist/install.py` —— 那是照着一台
  静态文件服务器写的，而 SyncOJ 服务端从来没有 `/dist/` 这条路。也就是说"空机器
  一条命令装好"这条链一直是断的，只是没人从空机器上试过。
"""

from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from conftest import ADMIN_PASSWORD, ADMIN_USER
from syncoj_server.api import agent as agent_api
from syncoj_server.config import Settings
from syncoj_server.main import create_app
from syncoj_server.models import Admin
from syncoj_server.security import hash_password
from syncoj_server.services import packaging
from syncoj_server.services.signing import generate_keypair, openssl_available

HAS_OPENSSL = openssl_available() is not None
requires_openssl = pytest.mark.skipif(not HAS_OPENSSL, reason="需要 openssl")

LEDGER = "/api/v1/agent/install.json"


def make_bundle(version: str = "3.1.4") -> bytes:
    """一个最小的、结构合法的 Agent 包（装机入口只搬字节，不解析它）。"""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, content in (
            ("syncoj_agent/__init__.py", '__version__ = "%s"\n' % version),
            ("run_agent.py", "# launcher\n"),
        ):
            payload = content.encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


@pytest.fixture()
def env(workdir: Path) -> Iterator[SimpleNamespace]:
    """一个配好签名私钥的服务端，带管理员。"""
    if not HAS_OPENSSL:
        pytest.skip("需要 openssl")

    settings = Settings()
    settings.data_root = workdir / "data"
    key_path = workdir / "release.pem"
    generate_keypair(key_path, bits=2048)
    settings.release_signing_key = key_path

    app = create_app(settings)
    with TestClient(app) as client:
        with app.state.ctx.db.session() as session:
            session.add(
                Admin(username=ADMIN_USER, password_hash=hash_password(ADMIN_PASSWORD))
            )
        login = client.post(
            "/api/v1/admin/login",
            json={"username": ADMIN_USER, "password": ADMIN_PASSWORD},
        )
        assert login.status_code == 200, login.text
        headers = {"Authorization": "Bearer " + login.json()["token"]}
        yield SimpleNamespace(
            app=app,
            client=client,
            ctx=app.state.ctx,
            headers=headers,
            settings=settings,
        )


def publish(client, headers, version: str = "3.1.4", *, rollout: bool = True) -> dict:
    """上传一个包，可选地铺开它。返回发布记录。"""
    response = client.post(
        "/api/v1/admin/releases",
        files={"file": ("agent-%s.tar.gz" % version, make_bundle(version), "application/gzip")},
        data={"version": version},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    release = response.json()
    if rollout:
        ok = client.post(
            "/api/v1/admin/releases/%d/rollout" % release["id"], headers=headers
        )
        assert ok.status_code == 200, ok.text
        release = ok.json()
    return release


# --------------------------------------------------------------------------- #
# 谁来管边界：只发已铺开的版本
# --------------------------------------------------------------------------- #


def test_没有铺开任何版本时给一句人话(env) -> None:
    """必须能分清"还没铺开"和"我地址打错了" —— 这两件事的处理完全不同。"""
    response = env.client.get(LEDGER)

    assert response.status_code == 404
    body = response.json()
    assert body["code"] == "install_unavailable"
    assert "铺开" in body["detail"]


def test_上传但没铺开的版本拿不到(env) -> None:
    """上传和铺开的风险分界在别处都是硬的，装机入口也得硬。"""
    publish(env.client, env.headers, "3.1.4", rollout=False)

    assert env.client.get(LEDGER).status_code == 404


def test_铺开之后能拿到台账(env) -> None:
    release = publish(env.client, env.headers, "3.1.4")

    body = env.client.get(LEDGER).json()

    assert body["version"] == release["version"]
    assert body["sha256"] == release["sha256"]
    assert body["size"] == release["size"]
    assert body["signature"], "台账要带上签名，机器上恰好有公钥时就能验"
    assert body["key_id"]


def test_更新但未铺开的新版本不会顶掉老版本(env) -> None:
    """台账报的是**最新那个已铺开的**，不是最新那个存在的。

    少了这条判据，教师一构建就会被装机入口发出去 —— 而构建是"还没铺开"的。
    """
    publish(env.client, env.headers, "3.1.4")
    publish(env.client, env.headers, "9.9.9", rollout=False)

    assert env.client.get(LEDGER).json()["version"] == "3.1.4"


def test_撤回之后就拿不到了(env) -> None:
    release = publish(env.client, env.headers, "3.1.4")
    assert (
        env.client.post(
            "/api/v1/admin/releases/%d/yank" % release["id"], headers=env.headers
        ).status_code
        == 200
    )

    assert env.client.get(LEDGER).status_code == 404


# --------------------------------------------------------------------------- #
# 不鉴权（这是这个入口存在的前提）
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/agent/install.json",
        "/api/v1/agent/install/bundle",
        "/api/v1/agent/install/installer",
        "/api/v1/agent/install/bootstrap.sh",
    ],
)
def test_四个端点都不需要凭据(env, path: str) -> None:
    """带凭据反而不该是**必需**的 —— 空机器上什么都没有。"""
    publish(env.client, env.headers, "3.1.4")

    response = env.client.get(path)  # 不带 Authorization

    assert response.status_code != 401 and response.status_code != 403, response.text


# --------------------------------------------------------------------------- #
# 包体
# --------------------------------------------------------------------------- #


def test_包体的字节与台账一致(env) -> None:
    release = publish(env.client, env.headers, "3.1.4")

    body = env.client.get("/api/v1/agent/install/bundle")

    assert body.status_code == 200
    assert hashlib.sha256(body.content).hexdigest() == release["sha256"]
    assert len(body.content) == release["size"]


def test_包体支持断点续传(env) -> None:
    """装机包可能几十 MB，而考场网线不稳 —— 与升级下载走同一套 Range 逻辑。"""
    publish(env.client, env.headers, "3.1.4")
    full = env.client.get("/api/v1/agent/install/bundle").content

    part = env.client.get(
        "/api/v1/agent/install/bundle", headers={"Range": "bytes=0-99"}
    )

    assert part.status_code == 206
    assert part.content == full[:100]


# --------------------------------------------------------------------------- #
# 安装器与自举脚本
# --------------------------------------------------------------------------- #


def test_安装器就是仓库里那个文件(env) -> None:
    """不发这个文件的话，空机器上只有一个 curl，第一步就断了。"""
    response = env.client.get("/api/v1/agent/install/installer")

    assert response.status_code == 200
    assert response.content == packaging.installer_path().read_bytes()
    assert b"class Installer" in response.content


def test_自举脚本就是仓库里那个文件(env) -> None:
    """给教师的"一条 curl"直接指向它，所以它必须真的发得出去。"""
    response = env.client.get("/api/v1/agent/install/bootstrap.sh")

    assert response.status_code == 200
    assert response.content == packaging.bootstrap_path().read_bytes()


def test_自举脚本指着真的端点(env) -> None:
    """它从前 curl 的是 `/dist/install.py`，而服务端没有 `/dist/` 这条路。

    也就是说"空机器一条命令装好"这条链**一直是断的** —— 只是没人从空机器上试过。
    所以这里直接盯脚本内容：不许再出现那个不存在的目录，而且必须去真的端点拿。
    """
    body = env.client.get("/api/v1/agent/install/bootstrap.sh").content.decode("utf-8")
    # 只看**真会执行**的行：脚本里保留了"从前指着 /dist/、而那条路根本不存在"
    # 这段注释，它正是这件事的历史，不该被这条断言逼着删掉。
    code = "\n".join(
        line for line in body.splitlines() if not line.lstrip().startswith("#")
    )

    assert "/api/v1/agent/install/installer" in code
    assert "/dist/" not in code


def test_自举脚本把参数原样传下去(env) -> None:
    """它只做"拿安装器、跑起来"两件事，所以命令行透传不能丢。"""
    body = env.client.get("/api/v1/agent/install/bootstrap.sh").content.decode("utf-8")

    assert "--from-server" in body, "包体交给 install.py 自己取（它才会对着台账校验）"
    assert "--bootstrap-key" in body
    assert "--public-key" in body, "有公钥就会验签，这条路得能传进去"
    assert "--sha256" in body


def test_不在仓库里运行时给一句人话(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """生产上服务端可能是 pip 装出来的、根本不在仓库里。

    这时要老实说清"为什么没有"，而不是一个裸 404 让人以为是路径写错了。
    """
    def boom() -> Path:
        raise packaging.BuildError("服务端不是从源码仓库运行的，找不到 agent/ 源码")

    monkeypatch.setattr(packaging, "agent_root", boom)

    for path in ("/api/v1/agent/install/installer", "/api/v1/agent/install/bootstrap.sh"):
        response = env.client.get(path)
        assert response.status_code == 404, path
        body = response.json()
        assert body["code"] == "installer_unavailable"
        assert "仓库" in body["detail"]


# --------------------------------------------------------------------------- #
# 台账里的路径与真实路由必须一致
# --------------------------------------------------------------------------- #


def test_台账里报的路径真的存在(env) -> None:
    """台账与路由各写一遍路径的话，某天改了路由就会指到 404 上 ——
    而现象是"装机装不上"，排查时谁也不会先怀疑台账里那个字符串。
    """
    publish(env.client, env.headers, "3.1.4")

    ledger = env.client.get(LEDGER).json()

    for key in ("bundle", "installer", "bootstrap"):
        response = env.client.get(ledger[key])
        assert response.status_code == 200, "%s → %s" % (key, ledger[key])


def test_路径常量与真实路由一致(env) -> None:
    """同样的意思，从常量那一侧再钉一遍（台账用的是常量，不是字面量）。"""
    publish(env.client, env.headers, "3.1.4")

    ledger = env.client.get(LEDGER).json()

    assert ledger["bundle"] == agent_api.INSTALL_BUNDLE_PATH
    assert ledger["installer"] == agent_api.INSTALL_INSTALLER_PATH
    assert ledger["bootstrap"] == agent_api.INSTALL_BOOTSTRAP_PATH
