"""Agent 发布与自更新接口测试。

核心是**风险边界**：上传一个包和把它推给 50 台考试机，是风险等级完全不同的
两件事。服务端必须让这两步之间的界线是硬的。
"""

from __future__ import annotations

import io
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from conftest import (
    ADMIN_PASSWORD,
    ADMIN_USER,
    agent_headers,
    bind_by_code,
    do_tick,
    enroll_machine,
    issue_bootstrap_key,
    make_roster,
)
from syncoj_server.config import Settings
from syncoj_server.main import create_app
from syncoj_server.models import Admin
from syncoj_server.security import hash_password
from syncoj_server.services.signing import generate_keypair, openssl_available

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = REPO_ROOT / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from syncoj_agent.upgrade import (  # noqa: E402
    ReleaseManifest,
    safe_extract_tar,
    verify_bundle,
)
from syncoj_agent.rsa import RSAPublicKey  # noqa: E402

#: 这台发布测试机器的 machine_id。夹具与 ``tick_upgrade`` 都要用它 ——
#: 服务端会校验 tick 里声明的 machine_id 与凭据是否一致
MACHINE_ID = "m-release-test"

HAS_OPENSSL = openssl_available() is not None
requires_openssl = pytest.mark.skipif(not HAS_OPENSSL, reason="需要 openssl")


def make_agent_bundle(version: str = "9.9.9") -> bytes:
    """构造一个最小的、结构合法的 Agent 发布包。"""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, content in (
            ("syncoj_agent/__init__.py", '__version__ = "%s"\n' % version),
            ("syncoj_agent/main.py", "print('agent')\n"),
        ):
            payload = content.encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


@pytest.fixture()
def release_env(workdir: Path) -> Iterator[SimpleNamespace]:
    """一个配好签名私钥的独立服务端实例。"""
    if not HAS_OPENSSL:
        pytest.skip("需要 openssl")

    settings = Settings()
    settings.data_root = workdir / "data"
    key_path = workdir / "release.pem"
    key = generate_keypair(key_path, bits=2048)
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

        contest = client.post(
            "/api/v1/admin/contests",
            json={"name": "发布测试", "slug": "rel", "status": "running"},
            headers=headers,
        ).json()
        player = client.post(
            "/api/v1/admin/contests/%d/players" % contest["id"],
            json=[{"player_no": "S001"}],
            headers=headers,
        ).json()["players"][0]

        # 机器绑的是**人**：建一份含 S001 的名单并把机器配上去，
        # 否则它拿不到场次，tick 只会一直回 403
        roster = make_roster(
            client, headers, "发布测试班", entries=[{"player_no": "S001"}]
        )
        raw_key = issue_bootstrap_key(client, headers)
        first = enroll_machine(client, raw_key, machine_id=MACHINE_ID)
        bind_by_code(client, headers, first["pair_code"], roster["entries"][0]["id"])
        # 再注册一次：配对之后机器才知道自己该扫哪、准考证号是多少
        enrolled = enroll_machine(
            client,
            raw_key,
            machine_id=MACHINE_ID,
            machine_uuid=first["machine_uuid"],
        )
        assert enrolled["bound"] is True, enrolled

        yield SimpleNamespace(
            app=app,
            client=client,
            settings=settings,
            key=key,
            key_path=key_path,
            headers=headers,
            contest=contest,
            player=player,
            enrolled=enrolled,
        )


def upload(client: TestClient, headers: dict, version: str, bundle: bytes = None):
    return client.post(
        "/api/v1/admin/releases",
        files={
            "file": (
                "agent-%s.tar.gz" % version,
                bundle if bundle is not None else make_agent_bundle(version),
                "application/gzip",
            )
        },
        data={"version": version, "notes": "测试版本"},
        headers=headers,
    )


def tick_upgrade(client: TestClient, enrolled: dict):
    body = do_tick(client, enrolled["token"], [], machine_id=MACHINE_ID)
    return body.get("upgrade")


# --------------------------------------------------------------------------- #
# 签名可用性
# --------------------------------------------------------------------------- #


def test_release_listing_requires_admin(release_env) -> None:
    assert release_env.client.get("/api/v1/admin/releases").status_code == 401


def test_status_reports_signing_available(release_env) -> None:
    body = release_env.client.get("/api/v1/admin/releases", headers=release_env.headers).json()
    assert body["signing_available"] is True
    assert body["key_id"] == release_env.key.key_id
    assert body["active_release"] is None
    assert body["releases"] == []


def test_upload_without_signing_key_is_refused(workdir: Path) -> None:
    """没有私钥就签不出包，上传必须直接拒绝 —— 而不是存下一个无法验证的包。"""
    settings = Settings()
    settings.data_root = workdir / "no-key"
    settings.release_signing_key = None
    app = create_app(settings)
    with TestClient(app) as client:
        with app.state.ctx.db.session() as session:
            session.add(Admin(username=ADMIN_USER, password_hash=hash_password(ADMIN_PASSWORD)))
        token = client.post(
            "/api/v1/admin/login",
            json={"username": ADMIN_USER, "password": ADMIN_PASSWORD},
        ).json()["token"]

        response = client.post(
            "/api/v1/admin/releases",
            files={"file": ("a.tar.gz", make_agent_bundle(), "application/gzip")},
            data={"version": "1.0.0"},
            headers={"Authorization": "Bearer " + token},
        )
        assert response.status_code == 503
        assert "签名" in response.json()["detail"]


def test_broken_signing_key_does_not_block_startup(workdir: Path) -> None:
    """签名只影响自更新一个功能，不该让整个考试系统起不来。"""
    settings = Settings()
    settings.data_root = workdir / "bad-key"
    bad = workdir / "bad.pem"
    bad.write_bytes(b"-----BEGIN RSA PRIVATE KEY-----\nnot base64!!!\n-----END RSA PRIVATE KEY-----\n")
    settings.release_signing_key = bad

    app = create_app(settings)  # 不应抛异常
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
    assert app.state.ctx.signing_key is None
    assert app.state.ctx.signing_key_error


# --------------------------------------------------------------------------- #
# 上传与铺开
# --------------------------------------------------------------------------- #


@requires_openssl
def test_upload_creates_unpublished_release(release_env) -> None:
    response = upload(release_env.client, release_env.headers, "1.0.0")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["version"] == "1.0.0"
    assert body["rolled_out"] is False, "上传不等于铺开"
    assert body["yanked"] is False
    assert len(body["sha256"]) == 64
    assert body["size"] > 0


@requires_openssl
def test_unpublished_release_is_invisible_to_agents(release_env) -> None:
    """上传后还没铺开时，Agent 的 tick 里不该出现 upgrade。"""
    upload(release_env.client, release_env.headers, "1.0.0")
    assert tick_upgrade(release_env.client, release_env.enrolled) is None


@requires_openssl
def test_rollout_makes_release_visible(release_env) -> None:
    release = upload(release_env.client, release_env.headers, "1.0.0").json()

    rollout = release_env.client.post(
        "/api/v1/admin/releases/%d/rollout" % release["id"], headers=release_env.headers
    )
    assert rollout.status_code == 200
    assert rollout.json()["rolled_out"] is True

    upgrade = tick_upgrade(release_env.client, release_env.enrolled)
    assert upgrade is not None
    assert upgrade["version"] == "1.0.0"
    assert upgrade["sha256"] == release["sha256"]
    assert upgrade["signature"]
    assert upgrade["size"] == release["size"]
    assert upgrade["url"].endswith("/releases/%d" % release["id"])


@requires_openssl
def test_yank_hides_release_from_agents(release_env) -> None:
    release = upload(release_env.client, release_env.headers, "1.0.0").json()
    release_env.client.post(
        "/api/v1/admin/releases/%d/rollout" % release["id"], headers=release_env.headers
    )
    assert tick_upgrade(release_env.client, release_env.enrolled) is not None

    yank = release_env.client.post(
        "/api/v1/admin/releases/%d/yank" % release["id"], headers=release_env.headers
    )
    assert yank.status_code == 200
    assert tick_upgrade(release_env.client, release_env.enrolled) is None


@requires_openssl
def test_rollout_auto_yanks_previous_version(release_env) -> None:
    """同一时刻只能有一个版本在铺开。

    否则不同机器可能拿到不同版本，出问题时无法判断"这台机器到底跑的是哪个"。
    """
    first = upload(release_env.client, release_env.headers, "1.0.0").json()
    second = upload(release_env.client, release_env.headers, "2.0.0").json()

    release_env.client.post(
        "/api/v1/admin/releases/%d/rollout" % first["id"], headers=release_env.headers
    )
    release_env.client.post(
        "/api/v1/admin/releases/%d/rollout" % second["id"], headers=release_env.headers
    )

    upgrade = tick_upgrade(release_env.client, release_env.enrolled)
    assert upgrade["version"] == "2.0.0"

    listing = release_env.client.get(
        "/api/v1/admin/releases", headers=release_env.headers
    ).json()
    active = [r for r in listing["releases"] if r["rolled_out"]]
    assert len(active) == 1
    assert active[0]["version"] == "2.0.0"


@requires_openssl
def test_upload_rejects_invalid_version(release_env) -> None:
    for bad in ("", "   ", "abc"):
        response = upload(release_env.client, release_env.headers, bad)
        assert response.status_code == 400, "版本号 %r 应当被拒绝" % bad


@requires_openssl
def test_upload_can_replace_unpublished_version(release_env) -> None:
    upload(release_env.client, release_env.headers, "1.0.0")
    again = upload(release_env.client, release_env.headers, "1.0.0")
    assert again.status_code == 200

    listing = release_env.client.get(
        "/api/v1/admin/releases", headers=release_env.headers
    ).json()
    assert len(listing["releases"]) == 1


@requires_openssl
def test_upload_refuses_to_replace_active_version(release_env) -> None:
    """正在铺开的版本不能被静默替换 —— 那会让已下载的机器和清单对不上。"""
    release = upload(release_env.client, release_env.headers, "1.0.0").json()
    release_env.client.post(
        "/api/v1/admin/releases/%d/rollout" % release["id"], headers=release_env.headers
    )

    again = upload(release_env.client, release_env.headers, "1.0.0")
    assert again.status_code == 409


# --------------------------------------------------------------------------- #
# 下载
# --------------------------------------------------------------------------- #


@requires_openssl
def test_download_requires_auth(release_env) -> None:
    release = upload(release_env.client, release_env.headers, "1.0.0").json()
    assert release_env.client.get("/api/v1/agent/releases/%d" % release["id"]).status_code == 401


@requires_openssl
def test_download_unpublished_release_is_not_found(release_env) -> None:
    release = upload(release_env.client, release_env.headers, "1.0.0").json()
    response = release_env.client.get(
        "/api/v1/agent/releases/%d" % release["id"],
        headers=agent_headers(release_env.enrolled["token"]),
    )
    assert response.status_code == 404


@requires_openssl
def test_download_yanked_release_is_gone(release_env) -> None:
    release = upload(release_env.client, release_env.headers, "1.0.0").json()
    release_env.client.post(
        "/api/v1/admin/releases/%d/rollout" % release["id"], headers=release_env.headers
    )
    release_env.client.post(
        "/api/v1/admin/releases/%d/yank" % release["id"], headers=release_env.headers
    )

    response = release_env.client.get(
        "/api/v1/agent/releases/%d" % release["id"],
        headers=agent_headers(release_env.enrolled["token"]),
    )
    assert response.status_code == 410


@requires_openssl
def test_download_returns_exact_bundle(release_env) -> None:
    bundle = make_agent_bundle("3.1.4")
    release = upload(release_env.client, release_env.headers, "3.1.4", bundle).json()
    release_env.client.post(
        "/api/v1/admin/releases/%d/rollout" % release["id"], headers=release_env.headers
    )

    response = release_env.client.get(
        "/api/v1/agent/releases/%d" % release["id"],
        headers=agent_headers(release_env.enrolled["token"]),
    )
    assert response.status_code == 200
    assert response.content == bundle


@requires_openssl
def test_download_supports_range(release_env) -> None:
    bundle = make_agent_bundle("3.1.5") * 3
    release = upload(release_env.client, release_env.headers, "3.1.5", bundle).json()
    release_env.client.post(
        "/api/v1/admin/releases/%d/rollout" % release["id"], headers=release_env.headers
    )

    response = release_env.client.get(
        "/api/v1/agent/releases/%d" % release["id"],
        headers=dict(agent_headers(release_env.enrolled["token"]), Range="bytes=100-"),
    )
    assert response.status_code == 206
    assert response.content == bundle[100:]


# --------------------------------------------------------------------------- #
# 端到端：Agent 真能验证并解包这个包
# --------------------------------------------------------------------------- #


@requires_openssl
def test_agent_can_verify_and_extract_server_release(release_env, workdir: Path) -> None:
    """把服务端签出来的东西交给 Agent 的验证 + 解包代码跑一遍。

    这是整条自更新链路上最关键的交接点：签名算法、签名对象（摘要还是原始字节）、
    tar 结构，任何一处两侧理解不一致，都会在真实升级时才暴露。
    """
    bundle = make_agent_bundle("4.2.0")
    release = upload(release_env.client, release_env.headers, "4.2.0", bundle).json()
    release_env.client.post(
        "/api/v1/admin/releases/%d/rollout" % release["id"], headers=release_env.headers
    )

    upgrade = tick_upgrade(release_env.client, release_env.enrolled)
    assert upgrade is not None

    downloaded = release_env.client.get(
        upgrade["url"], headers=agent_headers(release_env.enrolled["token"])
    )
    assert downloaded.status_code == 200

    local = workdir / "downloaded.tar.gz"
    local.write_bytes(downloaded.content)

    public = RSAPublicKey.from_dict(release_env.key.public_key_dict())
    manifest = ReleaseManifest.from_dict(upgrade)

    # 验签 + 摘要
    verify_bundle(public, local, manifest)

    # 安全解包
    dest = workdir / "extracted"
    count = safe_extract_tar(local, dest)
    assert count == 2
    assert (dest / "syncoj_agent" / "__init__.py").read_text(encoding="utf-8").strip() == (
        '__version__ = "4.2.0"'
    )


@requires_openssl
def test_tampered_release_is_rejected_by_agent(release_env, workdir: Path) -> None:
    """服务端被攻破、往包里塞了东西时，Agent 这一侧的签名校验是最后一道防线。"""
    bundle = make_agent_bundle("4.2.1")
    release = upload(release_env.client, release_env.headers, "4.2.1", bundle).json()
    release_env.client.post(
        "/api/v1/admin/releases/%d/rollout" % release["id"], headers=release_env.headers
    )
    upgrade = tick_upgrade(release_env.client, release_env.enrolled)

    local = workdir / "tampered.tar.gz"
    local.write_bytes(bundle + b"\n# injected malicious payload\n")

    public = RSAPublicKey.from_dict(release_env.key.public_key_dict())
    manifest = ReleaseManifest.from_dict(upgrade)

    from syncoj_agent.upgrade import UpgradeError

    with pytest.raises(UpgradeError):
        verify_bundle(public, local, manifest)


@requires_openssl
def test_agent_rejects_release_signed_by_another_key(release_env, workdir: Path) -> None:
    """换一把密钥签出来的包必须被拒绝 —— 这就是"信任锚"的意义。"""
    bundle = make_agent_bundle("5.0.0")
    release = upload(release_env.client, release_env.headers, "5.0.0", bundle).json()
    release_env.client.post(
        "/api/v1/admin/releases/%d/rollout" % release["id"], headers=release_env.headers
    )
    upgrade = tick_upgrade(release_env.client, release_env.enrolled)

    other = generate_keypair(workdir / "other.pem", bits=2048)
    wrong_public = RSAPublicKey.from_dict(other.public_key_dict())

    local = workdir / "bundle.tar.gz"
    local.write_bytes(bundle)

    from syncoj_agent.upgrade import UpgradeError

    with pytest.raises(UpgradeError, match="签名"):
        verify_bundle(wrong_public, local, ReleaseManifest.from_dict(upgrade))
