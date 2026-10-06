"""发布版本时的「高级选项」：随包带下去的三条安装策略。

这份测试盯的是一件事：**同一份策略能不能在三个地方读到同样的东西**。

策略本身很简单（三个字段），出事的地方全在"三处各写一遍"上：

* 装机台账说 ``replace``、包内 ``install_policy.json`` 说 ``keep`` —— 机器离线装机
  时按包里那份走、联网时按台账走，同一个包在两种装机方式下行为不同；
* 升级清单里少一个字段 —— 机器升级时只能拿默认值，教师配的那条静默失效。

三条策略的字段名与取值是**冻结的接口**（机器侧 ``agent/packaging/install.py``
按同样的名字读），所以这里也逐字断言名字，而不是只断言"字段存在"。
"""

from __future__ import annotations

import io
import json
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
    bind_by_code,
    do_tick,
    enroll_machine,
    issue_bootstrap_key,
    make_roster,
)
from syncoj_server import keys
from syncoj_server.config import Settings
from syncoj_server.main import create_app
from syncoj_server.models import Admin, AgentRelease, utcnow
from syncoj_server.security import hash_password
from syncoj_server.services import install_policy, packaging
from syncoj_server.services.signing import (
    generate_keypair,
    openssl_available,
    write_public_key_json,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "agent"))

HAS_OPENSSL = openssl_available() is not None
requires_openssl = pytest.mark.skipif(not HAS_OPENSSL, reason="需要 openssl")


def _source_available() -> bool:
    """本机仓库里有 ``agent/`` 源码可以构建吗。

    没有的话（CI 上只 checkout 了 server/）这批用例只能跳过 —— 但没有源码不
    影响"策略存进发布记录"这条路径，那些用例照旧跑。
    """
    return packaging.probe_source().available


requires_source = pytest.mark.skipif(
    not _source_available(), reason="本机仓库里没有 agent/ 源码，无法构建"
)

BUILD_URL = "/api/v1/admin/releases/build"
SOURCE_URL = "/api/v1/admin/releases/source"
LEDGER_URL = "/api/v1/agent/install.json"

#: 三条策略的字段名。**写死**：这份清单就是冻结的接口本身，
#: 从被测代码里取常量再比对，等于什么也没验。
POLICY_FIELDS = {"bootstrap_key_policy", "config_policy", "upgrade_mode"}


@pytest.fixture()
def env(workdir: Path, isolated_key_dir: Path) -> Iterator[SimpleNamespace]:
    """配好签名密钥的服务端 + 一台已配对、可收升级的机器。"""
    if not HAS_OPENSSL:
        pytest.skip("需要 openssl")

    private = isolated_key_dir / keys.RELEASE_SIGNING_KEY_NAME
    key = generate_keypair(private, bits=2048)
    write_public_key_json(isolated_key_dir / keys.RELEASE_PUBLIC_KEY_NAME, key)

    settings = Settings()
    settings.data_root = workdir / "data"
    assert settings.release_signing_key == private

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
            json={"name": "策略测试", "slug": "policy", "status": "running"},
            headers=headers,
        ).json()
        client.post(
            "/api/v1/admin/contests/%d/players" % contest["id"],
            json=[{"player_no": "S001", "name": "张三"}],
            headers=headers,
        )
        roster = make_roster(
            client, headers, "策略测试班", entries=[{"player_no": "S001", "name": "张三"}]
        )
        raw_key = issue_bootstrap_key(client, headers)
        first = enroll_machine(client, raw_key, machine_id="m-policy-test")
        bind_by_code(client, headers, first["pair_code"], roster["entries"][0]["id"])
        enrolled = enroll_machine(
            client,
            raw_key,
            machine_id="m-policy-test",
            machine_uuid=first["machine_uuid"],
        )
        assert enrolled["bound"] is True, enrolled

        yield SimpleNamespace(
            app=app,
            client=client,
            ctx=app.state.ctx,
            settings=settings,
            key=key,
            headers=headers,
            contest=contest,
            token=enrolled["token"],
        )


def build(env: SimpleNamespace, *, version: str = None, **policy) -> dict:
    """构建一个版本并返回发布记录。``policy`` 里只放想改的那几项。"""
    body = {"version": version or packaging.source_version()}
    body.update(policy)
    response = env.client.post(BUILD_URL, json=body, headers=env.headers)
    assert response.status_code == 200, response.text
    return response.json()


def stored_release(env: SimpleNamespace, version: str) -> AgentRelease:
    with env.ctx.db.session() as session:
        from sqlalchemy import select

        return session.execute(
            select(AgentRelease).where(AgentRelease.version == version)
        ).scalar_one()


def ledger(env: SimpleNamespace) -> dict:
    """摊开这个版本之后的装机台账。"""
    response = env.client.get(LEDGER_URL)
    assert response.status_code == 200, response.text
    return response.json()


def upgrade_manifest(env: SimpleNamespace) -> dict:
    """机器心跳里拿到的那份升级清单。"""
    body = do_tick(env.client, env.token, [], machine_id="m-policy-test")
    assert body["upgrade"] is not None, body
    return body["upgrade"]


def bundle_policy(env: SimpleNamespace, release: dict) -> dict:
    """从打出来的离线包里读那份 ``install_policy.json``。"""
    with env.ctx.blobs.open(release["sha256"]) as handle:
        data = handle.read()
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        raw = archive.extractfile(install_policy.POLICY_JSON_NAME)
        assert raw is not None, "离线包里没有 %s" % install_policy.POLICY_JSON_NAME
        return json.loads(raw.read().decode("utf-8"))


def rollout(env: SimpleNamespace, release: dict) -> None:
    response = env.client.post(
        "/api/v1/admin/releases/%d/rollout" % release["id"], headers=env.headers
    )
    assert response.status_code == 200, response.text


# --------------------------------------------------------------------------- #
# 规范清单：与 agent/config.example.ini 逐键对账
# --------------------------------------------------------------------------- #


def agent_config_keys() -> list:
    """``agent/config.example.ini`` 里真正被赋值的键，形如 ``<段>.<键>``。"""
    import configparser

    path = REPO_ROOT / "agent" / "config.example.ini"
    parser = configparser.ConfigParser()
    parser.read(path, encoding="utf-8")
    return sorted(
        "%s.%s" % (section, key)
        for section in parser.sections()
        for key in parser.options(section)
    )


def test_逐键策略清单与配置示例逐键对账() -> None:
    """``CONFIG_POLICY_KEYS`` 必须与 ``agent/config.example.ini`` 一模一样。

    多一个：教师能配一条**安装器不会写**的键 —— 界面显示配好了，机器上什么都没
    发生。少一个：某个键永远没法被逐键覆盖，而没有任何地方会提示这件事。

    这条对账是"逐键策略"这个功能能不能用的前提，所以它比别的用例更值得存在。
    """
    declared = sorted(install_policy.CONFIG_POLICY_KEYS)
    actual = agent_config_keys()

    assert declared == actual, (
        "CONFIG_POLICY_KEYS 与 agent/config.example.ini 对不上：\n"
        "  清单里多出来的：%s\n"
        "  示例里有、清单里没有的：%s"
        % (sorted(set(declared) - set(actual)), sorted(set(actual) - set(declared)))
    )


def test_键名写法统一是_段点键() -> None:
    """规范写法只有一种：``<段>.<键>``。混一个简写进来，机器侧就找不到那个键。"""
    assert all("." in key and key.count(".") == 1 for key in install_policy.CONFIG_POLICY_KEYS)


def test_包内策略文件名与机器侧读的那个一致() -> None:
    """名字猜错的表现是"策略静默不生效" —— 而两台机器上看起来一切正常。

    机器侧的常量在 ``agent/packaging/install.py``（那个文件属另一半，这里只读它、
    不改它）。两边共用一个字面量是刻意的：契约要有第二个人看着。
    """
    source = (REPO_ROOT / "agent" / "packaging" / "install.py").read_text(encoding="utf-8")

    assert 'POLICY_JSON_FILENAME = "%s"' % install_policy.POLICY_JSON_NAME in source


def test_三条策略的字段名与机器侧声明的一致() -> None:
    """机器侧把这三个名字写在常量里。**逐字**对一遍 —— 字段名漂了，
    下发过去的策略没人读，而两边都不会报错。"""
    source = (REPO_ROOT / "agent" / "packaging" / "install.py").read_text(encoding="utf-8")

    for field in sorted(POLICY_FIELDS):
        assert '"%s"' % field in source, "机器侧没有声明 %s" % field


# --------------------------------------------------------------------------- #
# 三条策略进发布记录
# --------------------------------------------------------------------------- #


@requires_source
def test_三条策略都存进发布记录(env: SimpleNamespace) -> None:
    release = build(
        env,
        include_bootstrap_key=True,
        bootstrap_key_policy="replace",
        config_policy={"scan.roots": "force", "log.level": "default"},
        upgrade_mode="stage",
    )

    row = stored_release(env, release["version"])
    assert row.bootstrap_key_policy == "replace"
    assert row.upgrade_mode == "stage"
    assert json.loads(row.config_policy_json) == {
        "scan.roots": "force",
        "log.level": "default",
    }


@requires_source
def test_勾了附带密钥时默认覆盖(env: SimpleNamespace) -> None:
    """默认值跟着"附带密钥"走 —— 那正是真机事故的修复。"""
    release = build(env, include_bootstrap_key=True)

    assert release["bootstrap_key_policy"] == "replace"
    assert stored_release(env, release["version"]).bootstrap_key_policy == "replace"


@requires_source
def test_没勾附带密钥时不带策略也不报错(env: SimpleNamespace) -> None:
    """没勾附带时这一项无从谈起，按 keep 记；其余两项仍是各自默认值。"""
    release = build(env)

    assert release["bootstrap_key_policy"] == "keep"
    assert release["upgrade_mode"] == "apply"
    assert stored_release(env, release["version"]).config_policy_json is None


@requires_source
def test_可以显式让包内的密钥不覆盖机器上那把(env: SimpleNamespace) -> None:
    """显式 ``keep`` 与"没给"必须分得开：否则教师没法在附带密钥的同时保住旧钥。"""
    release = build(env, include_bootstrap_key=True, bootstrap_key_policy="keep")

    assert release["bootstrap_key_policy"] == "keep"


# --------------------------------------------------------------------------- #
# 三处读到同一份
# --------------------------------------------------------------------------- #


@requires_source
def test_台账_升级清单_包内清单三处逐字相同(env: SimpleNamespace) -> None:
    """这就是整个功能的判据：同一份真相出现在三个地方，而且**由同一个 helper 产生**。

    三处只要有一处不同，同一个包在联网装机与离线装机下就会按不同的策略行动 ——
    而那种不一致在服务端这边看不出来（三处都是 200）。
    """
    release = build(
        env,
        include_bootstrap_key=True,
        bootstrap_key_policy="replace",
        config_policy={"scan.roots": "force"},
        upgrade_mode="stage",
    )
    rollout(env, release)

    from_bundle = bundle_policy(env, release)
    from_ledger = {key: ledger(env)[key] for key in POLICY_FIELDS}
    from_manifest = {key: upgrade_manifest(env)[key] for key in POLICY_FIELDS}

    assert set(from_bundle) == POLICY_FIELDS
    assert from_ledger == from_bundle, "台档里的策略与包内那份不一致"
    assert from_manifest == from_bundle, "升级清单里的策略与包内那份不一致"

    # 具体值也钉一遍，免得"三处都很一致地什么都不对"
    assert from_bundle["bootstrap_key_policy"] == "replace"
    assert from_bundle["upgrade_mode"] == "stage"
    assert from_bundle["config_policy"]["scan.roots"] == "force"


@requires_source
def test_台账里的逐键策略是补全过的完整映射(env: SimpleNamespace) -> None:
    """没提到的键按"不改"处理。**补全放在服务端**：让界面自己推默认值，
    等于把"默认是不改"这条规矩抄进第二个地方。"""
    release = build(env, config_policy={"scan.roots": "force"})
    rollout(env, release)

    policy = ledger(env)["config_policy"]

    assert set(policy) == set(install_policy.CONFIG_POLICY_KEYS)
    assert policy["scan.roots"] == "force"
    assert all(
        value == install_policy.CONFIG_POLICY_KEEP
        for key, value in policy.items()
        if key != "scan.roots"
    )


@requires_source
def test_source_接口给出逐键清单与默认值(env: SimpleNamespace) -> None:
    """界面靠它列出"可以逐键覆盖哪些键"，而不是自己抄一份清单。"""
    body = env.client.get(SOURCE_URL, headers=env.headers).json()

    assert body["config_policy_keys"] == list(install_policy.CONFIG_POLICY_KEYS)
    assert set(body["config_policy_defaults"]) == set(install_policy.CONFIG_POLICY_KEYS)
    assert set(body["config_policy_defaults"].values()) == {install_policy.CONFIG_POLICY_KEEP}


# --------------------------------------------------------------------------- #
# 取值校验：非法一律 400，不静默丢
# --------------------------------------------------------------------------- #


@requires_source
def test_未知的逐键策略键会被拒(env: SimpleNamespace) -> None:
    """静默丢弃是最坏的失败方式：教师以为配好了，机器上什么都没发生。"""
    response = env.client.post(
        BUILD_URL,
        json={
            "version": packaging.source_version(),
            "config_policy": {"scan.不存在": "force"},
        },
        headers=env.headers,
    )

    assert response.status_code == 400, response.text
    body = response.json()
    assert body["code"] == "bad_request"
    assert "scan.不存在" in body["detail"]


@requires_source
def test_逐键策略的取值非法会被拒(env: SimpleNamespace) -> None:
    response = env.client.post(
        BUILD_URL,
        json={
            "version": packaging.source_version(),
            "config_policy": {"scan.roots": "覆盖"},
        },
        headers=env.headers,
    )

    assert response.status_code == 400, response.text
    assert "scan.roots" in response.json()["detail"]


@requires_source
def test_升级模式取值非法会被拒(env: SimpleNamespace) -> None:
    response = env.client.post(
        BUILD_URL,
        json={"version": packaging.source_version(), "upgrade_mode": "升级"},
        headers=env.headers,
    )

    assert response.status_code == 400, response.text
    assert "upgrade_mode" in response.json()["detail"]


@requires_source
def test_没勾附带密钥时不许写_replace(env: SimpleNamespace) -> None:
    """包里压根没有那把钥匙，"覆盖"无从谈起 —— 静默当成 keep 会让教师以为生效了。"""
    response = env.client.post(
        BUILD_URL,
        json={
            "version": packaging.source_version(),
            "include_bootstrap_key": False,
            "bootstrap_key_policy": "replace",
        },
        headers=env.headers,
    )

    assert response.status_code == 400, response.text
    assert "bootstrap_key_policy" in response.json()["detail"]


@requires_source
def test_策略非法时不会留下半个发布记录(env: SimpleNamespace) -> None:
    """校验必须发生在动手之前：否则磁盘上会多一个没人认领的包。"""
    response = env.client.post(
        BUILD_URL,
        json={
            "version": packaging.source_version(),
            "upgrade_mode": "升级",
        },
        headers=env.headers,
    )
    assert response.status_code == 400

    with env.ctx.db.session() as session:
        from sqlalchemy import select

        rows = list(session.execute(select(AgentRelease)).scalars())
    assert rows == []


@requires_source
def test_上传那条路不带策略也不猜(workdir: Path, isolated_key_dir: Path) -> None:
    """服务端不知道上传进来的包裹里写了什么策略，所以只留默认值 —— 不猜。

    猜错的后果是机器按一套没人指定过的策略去改 ``agent.ini``。
    """
    if not HAS_OPENSSL:
        pytest.skip("需要 openssl")
    generate_keypair(isolated_key_dir / keys.RELEASE_SIGNING_KEY_NAME, bits=2048)

    settings = Settings()
    settings.data_root = workdir / "data"
    app = create_app(settings)
    with TestClient(app) as client:
        with app.state.ctx.db.session() as session:
            session.add(
                Admin(username=ADMIN_USER, password_hash=hash_password(ADMIN_PASSWORD))
            )
        headers = {
            "Authorization": "Bearer "
            + client.post(
                "/api/v1/admin/login",
                json={"username": ADMIN_USER, "password": ADMIN_PASSWORD},
            ).json()["token"]
        }

        response = client.post(
            "/api/v1/admin/releases",
            files={"file": ("agent-1.2.3.tar.gz", b"fake-bundle", "application/gzip")},
            data={"version": "1.2.3"},
            headers=headers,
        )
        assert response.status_code == 200, response.text

        row = stored_release(SimpleNamespace(ctx=app.state.ctx), "1.2.3")

    assert row.bootstrap_key_policy is None
    assert row.config_policy_json is None
    assert row.upgrade_mode is None

    body = response.json()
    assert body["bootstrap_key_policy"] == "keep"
    assert body["upgrade_mode"] == "apply"


# --------------------------------------------------------------------------- #
# 老发布记录：策略列是 NULL，但"夹带过密钥"这件事记得住
# --------------------------------------------------------------------------- #


def _legacy_bundle_bytes(policy: dict) -> bytes:
    """造一个最小 tar.gz，里面只有那份策略文件。"""
    buffer = io.BytesIO()
    payload = json.dumps(policy, ensure_ascii=False).encode("utf-8")
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo(install_policy.POLICY_JSON_NAME)
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


def _plant_legacy(
    env: SimpleNamespace, version: str, *, bundled_key_id: int = None
) -> None:
    """把一份"老包"直接放进 blob 存储，并记一条**策略列全是 NULL**的发布记录。

    为什么不走构建接口：构建接口一定会把策略写进库里，那样就测不到"老记录"
    这个场景了 —— 而这条默认值的由来正是老记录。
    """
    sha256, size = env.ctx.blobs.put_stream(
        io.BytesIO(_legacy_bundle_bytes({"bootstrap_key_policy": None})),
        max_bytes=1024 * 1024,
    )
    with env.ctx.db.session() as session:
        session.add(
            AgentRelease(
                version=version,
                channel="stable",
                sha256=sha256,
                signature="legacy",
                size=size,
                bootstrap_key_id=bundled_key_id,
                bootstrap_key_policy=None,
                config_policy_json=None,
                upgrade_mode=None,
                published_at=utcnow(),
            )
        )


@requires_source
def test_老记录夹带了密钥时默认覆盖(env: SimpleNamespace) -> None:
    """**这条就是这个默认值的由来。**

    迁移之前建的版本没有策略列，可它确实夹带过密钥。若拿一个固定的 ``keep`` 去补，
    就等于把那批记录当成"教师选了不覆盖" —— 机器上那把已吊销的旧钥继续挡着包内
    新钥，机器永远注册不上，而服务端这边一切正常。
    """
    _plant_legacy(env, "legacy-1.0.0", bundled_key_id=1)

    # 台账
    assert ledger(env)["bootstrap_key_policy"] == "replace"
    # 升级清单
    assert upgrade_manifest(env)["bootstrap_key_policy"] == "replace"


@requires_source
def test_老记录没夹带密钥时默认不改(env: SimpleNamespace) -> None:
    """没有包内密钥时"覆盖"无从谈起 —— 补 ``keep``，不是 ``replace``。"""
    _plant_legacy(env, "legacy-1.0.0", bundled_key_id=None)

    assert ledger(env)["bootstrap_key_policy"] == "keep"
    assert upgrade_manifest(env)["bootstrap_key_policy"] == "keep"
