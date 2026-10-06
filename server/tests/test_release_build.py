"""「发布当前版本」——从仓库源码构建 Agent 升级包并签发。

这个功能的风险不在"能不能打包"，而在**它会不会悄悄发出去一个没法用的包**。
三处具体的坑，各有一条测试盯着：

* 包里带错公钥 → 每台机器都拒绝升级，而服务端一切正常（最难查的那种）
* 版本号与源码不一致 → 包里那份代码和发布记录上那个号对不上
* 构建成功就顺手铺开 → 一次点击推给 50 台机器

所以这里最要紧的一条断言是：**拿包里那把公钥，用 Agent 自己的验签代码，能验通
服务端签出来的签名。** 它一次性串起了 `.key/` 那对文件是否配套、包的成员是否齐全、
签名对象是不是 sha256 的十六进制串 —— 这三件事任何一件错，现场的表现都是
"所有机器静默不升级"。
"""

from __future__ import annotations

import hashlib
import io
import json
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

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
from syncoj_server import keys
from syncoj_server.config import Settings
from syncoj_server.main import create_app
from syncoj_server.models import Admin, AgentRelease, BootstrapKey, EventLog
from syncoj_server.security import hash_bootstrap_key, hash_password
from syncoj_server.services import packaging
from syncoj_server.services.signing import (
    generate_keypair,
    openssl_available,
    write_public_key_json,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "agent"))

from syncoj_agent.rsa import RSAPublicKey  # noqa: E402
from syncoj_agent.upgrade import (  # noqa: E402
    ReleaseManifest,
    safe_extract_tar,
    verify_bundle,
)

HAS_OPENSSL = openssl_available() is not None
requires_openssl = pytest.mark.skipif(not HAS_OPENSSL, reason="需要 openssl")

BUILD_URL = "/api/v1/admin/releases/build"
SOURCE_URL = "/api/v1/admin/releases/source"

#: 发布测试机器的 machine_id。必须与 tick 里声明的一致 —— 服务端会校验它和凭据
#: 是否对得上（防止一份凭据被复制到别的机器上）。
MACHINE_ID = "m-build-test"


def _keys_present() -> bool:
    """本机仓库里真的有 agent/ 源码可以构建吗。

    没有的话（例如 CI 上只 checkout 了 server/）这批用例只能跳过 —— 但跳过
    之前要说清是哪一半缺了，否则"跳过"和"通过"在现场是一回事。
    """
    return packaging.probe_source().available


requires_source = pytest.mark.skipif(
    not _keys_present(), reason="本机仓库里没有 agent/ 源码，无法构建"
)


@pytest.fixture()
def build_env(workdir: Path, isolated_key_dir: Path) -> Iterator[SimpleNamespace]:
    """一个配好**成对**签名密钥的独立服务端实例。

    密钥按真实布局写进 ``<密钥目录>/``（私钥 + 公钥两个文件），而不是像别的
    发布测试那样直接塞一个 ``settings.release_signing_key`` —— 因为"构建"这条路
    同时要用到**私钥**（签名）和**公钥**（内嵌进包），只塞私钥就测不到"公钥缺失"
    与"公钥不配套"这两个真实故障。
    """
    if not HAS_OPENSSL:
        pytest.skip("需要 openssl")

    private = isolated_key_dir / keys.RELEASE_SIGNING_KEY_NAME
    key = generate_keypair(private, bits=2048)
    write_public_key_json(isolated_key_dir / keys.RELEASE_PUBLIC_KEY_NAME, key)

    settings = Settings()
    settings.data_root = workdir / "data"
    assert settings.release_signing_key == private, "私钥应当由密钥目录解析出来"

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

        # 配一台能收升级的机器。自更新只推给**已配对**的机器（它得先知道自己是谁、
        # 在哪场次里），所以夹具得走完"建名单 → 注册 → 配对 → 再注册"这一串。
        contest = client.post(
            "/api/v1/admin/contests",
            json={"name": "构建测试", "slug": "build", "status": "running"},
            headers=headers,
        ).json()
        client.post(
            "/api/v1/admin/contests/%d/players" % contest["id"],
            json=[{"player_no": "S001"}],
            headers=headers,
        )
        roster = make_roster(
            client, headers, "构建测试班", entries=[{"player_no": "S001"}]
        )
        raw_key = issue_bootstrap_key(client, headers)
        first = enroll_machine(client, raw_key, machine_id=MACHINE_ID)
        bind_by_code(client, headers, first["pair_code"], roster["entries"][0]["id"])
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
            ctx=app.state.ctx,
            settings=settings,
            key=key,
            private=private,
            headers=headers,
            contest=contest,
            enrolled=enrolled,
        )


@pytest.fixture()
def built(build_env: SimpleNamespace) -> dict:
    """构建一次并返回发布记录（本文件大部分用例都从这一步开始）。"""
    response = build_env.client.post(
        BUILD_URL,
        json={"version": packaging.source_version(), "notes": "第一次构建"},
        headers=build_env.headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def blob_bytes(ctx, sha256: str) -> bytes:
    with ctx.blobs.open(sha256) as handle:
        return handle.read()


def members_of(data: bytes):
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        return archive.getnames()


# --------------------------------------------------------------------------- #
# 打出来的到底是什么
# --------------------------------------------------------------------------- #


@requires_source
def test_构建出的包里有源码_启动器和公钥(build_env: SimpleNamespace, built: dict) -> None:
    """包必须能被装在目标机上并按升级流程解开。

    三样缺一样都不行：``syncoj_agent/``（代码本身）、``run_agent.py``（systemd
    的入口，缺了它 Agent 起不来）、``release-key.pub.json``（信任锚，缺了它机器
    拒绝升级）。它们分别由打包脚本的三段逻辑负责，所以三样都断言。
    """
    names = members_of(blob_bytes(build_env.ctx, built["sha256"]))

    assert any(name.startswith("syncoj_agent/") for name in names), names
    assert "syncoj_agent/__init__.py" in names
    assert "run_agent.py" in names
    assert keys.RELEASE_PUBLIC_KEY_NAME in names


@requires_source
def test_包里的版本就是源码里的版本(build_env: SimpleNamespace, built: dict) -> None:
    """包里那份 ``__init__.py`` 里的 ``__version__`` 必须和发布记录一致。

    不一致时，机器上报的版本和教师看到的版本是两个数 —— 排查"这台到底升没升"
    时，这两个数会互相矛盾。
    """
    data = blob_bytes(build_env.ctx, built["sha256"])
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        raw = archive.extractfile("syncoj_agent/__init__.py").read().decode("utf-8")

    version = next(
        line.split("=", 1)[1].strip().strip("'\"")
        for line in raw.splitlines()
        if line.strip().startswith("__version__")
    )
    assert version == built["version"] == packaging.source_version()


@requires_source
def test_字节数与_sha256_和库里那份内容对得上(
    build_env: SimpleNamespace, built: dict
) -> None:
    """发布记录里的 ``size``/``sha256`` 指的是**库里那份字节**。

    它们对不上就意味着：记在案上的校验和与实际存着的东西不是一份 —— 而 Agent
    是拿这个 sha256 验包的，于是"所有机器都校验失败"。
    """
    data = blob_bytes(build_env.ctx, built["sha256"])

    assert len(data) == built["size"]
    assert hashlib.sha256(data).hexdigest() == built["sha256"]


@requires_source
def test_构建出的包能被真_Agent_验签并解开(
    build_env: SimpleNamespace, built: dict, workdir: Path
) -> None:
    """整条链一次串起来 —— 这是本文件最有价值的一条。

    构建 → 签发 → 铺开 → 从**面向 Agent 的那个接口**下载 → 交给 Agent 自己的
    ``verify_bundle`` 验签验摘要 → 用 ``safe_extract_tar`` 解开。全程没有一步
    是"我们自己再实现一遍"，用的就是目标机上会跑的那份代码。

    于是下面任何一件事错，它都会红：

    * ``.key/`` 里的私钥和公钥不是一对（比如只换了其中一个）
    * 构建时公钥没内嵌进去、或者内嵌的是另一把
    * 签名对象不是 sha256 的十六进制串（Agent 就是这么验的）
    * 打出来的包结构不合 ``safe_extract_tar`` 的规矩（比如漏了启动器）
    * 库里存的那份字节和记录里的 sha256 不是一份东西

    这些在现场的表现全都是"界面上发布成功、所有机器静默不升级"，而服务端日志
    干干净净 —— 没有这条测试，只能靠人去机房一台一台看。
    """
    rollout = build_env.client.post(
        "/api/v1/admin/releases/%d/rollout" % built["id"], headers=build_env.headers
    )
    assert rollout.status_code == 200, rollout.text

    # 机器下一次心跳会拿到升级信息 —— 走的就是真实下发路径
    tick = do_tick(build_env.client, build_env.enrolled["token"], [], machine_id=MACHINE_ID)
    upgrade = tick.get("upgrade")
    assert upgrade is not None, "铺开之后 Agent 应当收到升级信息：%r" % tick
    assert upgrade["version"] == built["version"]

    downloaded = build_env.client.get(
        upgrade["url"], headers=agent_headers(build_env.enrolled["token"])
    )
    assert downloaded.status_code == 200, downloaded.text

    local = workdir / "downloaded.tar.gz"
    local.write_bytes(downloaded.content)

    # 信任锚来自**包里内嵌的那份**，不是从密钥目录再读一次 —— 这正是要证的
    # "服务端打出的包，机器用包里带的公钥就能验"
    with tarfile.open(fileobj=io.BytesIO(downloaded.content)) as archive:
        embedded = json.loads(
            archive.extractfile(keys.RELEASE_PUBLIC_KEY_NAME).read().decode("utf-8")
        )
    public = RSAPublicKey.from_dict(embedded)

    verify_bundle(public, local, ReleaseManifest.from_dict(upgrade))

    dest = workdir / "extracted"
    safe_extract_tar(local, dest)
    assert (dest / "syncoj_agent" / "__init__.py").is_file()
    assert (dest / "run_agent.py").is_file()


@requires_source
def test_同一份源码两次构建字节完全相同(build_env: SimpleNamespace) -> None:
    """可复现打包不是洁癖：签名签的是 sha256，而"同一版两次打包得到不同字节"
    会让"这个包是不是那个包"这件事无法判断 —— 现场想核对就只能重发一次。
    """
    first = build_env.settings.data_root.parent / "a.tar.gz"
    second = build_env.settings.data_root.parent / "b.tar.gz"

    _, size_a, sha_a = packaging.build_agent_bundle(first)
    _, size_b, sha_b = packaging.build_agent_bundle(second)
    assert sha_a == sha_b, "同样内容产出不同字节，说明时间戳/文件名漏进了压缩流"
    assert size_a == size_b


# --------------------------------------------------------------------------- #
# 可选附带统一注册密钥
#
# 服务端只有密钥的哈希、拿不到明文，所以"挑一把已签发的密钥塞进包"这条路
# 从一开始就不存在 —— 只能在构建那一刻**现场签一把新的**。这一节盯的就是：
# 不勾选时什么都没多签；勾选时包里真的有一把能注册的密钥、而库里仍然只有哈希。
# --------------------------------------------------------------------------- #


def _bootstrap_key_count(ctx) -> int:
    with ctx.db.session() as session:
        return int(
            session.execute(select(func.count()).select_from(BootstrapKey)).scalar() or 0
        )


def _bundle_bootstrap_key(data: bytes) -> bytes:
    """取包内的 ``bootstrap.key``；没有就断言失败（消息里带上全部成员名）。"""
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        names = archive.getnames()
        assert packaging.BOOTSTRAP_KEY_NAME in names, (
            "包里的根目录下没有 %s：%r" % (packaging.BOOTSTRAP_KEY_NAME, names)
        )
        return archive.extractfile(packaging.BOOTSTRAP_KEY_NAME).read()


@requires_source
def test_不勾选附带密钥时包里没有密钥也不多签一把(
    build_env: SimpleNamespace, built: dict
) -> None:
    """默认必须是关的，而且"关"要关得彻底：**库里一把密钥都不该多出来**。

    这条不只是"字段默认值是 False"——它守的是"没勾选的那条路上一次
    ``new_bootstrap_key()`` 都没调过"。多签一把没人用的密钥会污染密钥列表，
    而列表本身是教师判断"哪把钥匙还在外面"的唯一依据。
    """
    before = _bootstrap_key_count(build_env.ctx)
    response = build_env.client.post(
        BUILD_URL,
        json={"version": packaging.source_version(), "include_bootstrap_key": False},
        headers=build_env.headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["bootstrap_key_id"] is None, body
    assert body["bootstrap_key_label"] is None, body
    assert body["bootstrap_key_revoked"] is False, body

    data = blob_bytes(build_env.ctx, body["sha256"])
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        assert packaging.BOOTSTRAP_KEY_NAME not in archive.getnames()

    assert _bootstrap_key_count(build_env.ctx) == before, "没勾选却多签了一把密钥"


@requires_source
def test_勾选附带密钥时包里有一把能注册的密钥(build_env: SimpleNamespace) -> None:
    """勾选之后，包里的 ``bootstrap.key`` 必须**真的能注册**。

    "包里有一个叫 bootstrap.key 的文件"这种断言太弱了：权限错了、内容少了换行、
    或者塞进去的是哈希而不是明文，都会让那条断言照样绿、而装机时注册不上。
    所以这里拿它的内容直接走一次真实的 ``/api/v1/agent/enroll``。
    """
    before = _bootstrap_key_count(build_env.ctx)
    response = build_env.client.post(
        BUILD_URL,
        json={"version": packaging.source_version(), "include_bootstrap_key": True},
        headers=build_env.headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()

    # 1) 回执：明确写出"这个包附带了一把统一注册密钥（#id，标签）"
    assert body["bootstrap_key_id"] is not None, body
    assert body["bootstrap_key_label"] == "随版本 %s 附带" % body["version"], body
    assert body["bootstrap_key_revoked"] is False, body

    # 2) 包内确实有它，而且权限是 0600（这把钥匙不该躺在别人读得到的地方）
    data = blob_bytes(build_env.ctx, body["sha256"])
    plain = _bundle_bootstrap_key(data)
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        mode = archive.getmember(packaging.BOOTSTRAP_KEY_NAME).mode
    assert mode == packaging.BOOTSTRAP_KEY_MODE, "密钥在包内的权限应当是 0600"

    # 3) 明文能注册 —— 用真接口、真明文，不是"看着像"
    raw = plain.decode("utf-8").strip()
    enrolled = enroll_machine(build_env.client, raw, machine_id="m-bundled-key")
    assert enrolled["token"]

    # 4) 库里**只有哈希**：拿明文去查哈希列，一行都不该有
    with build_env.ctx.db.session() as session:
        keys = list(session.execute(select(BootstrapKey)).scalars())
        matched = [
            row for row in keys
            if row.id == body["bootstrap_key_id"] and row.key_hash == raw
        ]
    assert not matched, "库里存下了明文或明文长度不够的哈希"
    row = next(k for k in keys if k.id == body["bootstrap_key_id"])
    assert row.key_hash == hash_bootstrap_key(raw)
    assert raw not in row.key_hash

    # 5) 库里只该多出这一把
    assert _bootstrap_key_count(build_env.ctx) == before + 1


@requires_source
def test_附带密钥的包不会把明文漏进任何响应(build_env: SimpleNamespace) -> None:
    """明文只该寄生在包里那一个字节串上。

    构造失败那次最容易漏：回执里带一句"密钥是 xxx"，或者审计事件把明文写进
    ``meta_json`` —— 而 events 表是教师界面能翻的，等于把密钥重新公开一次。
    """
    response = build_env.client.post(
        BUILD_URL,
        json={"version": packaging.source_version(), "include_bootstrap_key": True},
        headers=build_env.headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    raw = _bundle_bootstrap_key(blob_bytes(build_env.ctx, body["sha256"])).decode().strip()

    status = build_env.client.get("/api/v1/admin/releases", headers=build_env.headers)
    keys = build_env.client.get("/api/v1/admin/bootstrap-keys", headers=build_env.headers)
    events = build_env.client.get("/api/v1/admin/events", headers=build_env.headers)

    for name, payload in (("构建回执", body), ("版本列表", status.json()),
                          ("密钥列表", keys.json()), ("审计日志", events.json())):
        assert raw not in json.dumps(payload, ensure_ascii=False), (
            "%s 里出现了密钥明文" % name
        )


@requires_source
def test_吊销附带密钥后注册被拒而版本记录还在(build_env: SimpleNamespace) -> None:
    """带密钥的版本要能**按版本单独吊销**，而且吊销不能顺手删掉版本记录。

    "这个包是哪天发出去的、当时带的是哪把钥匙"是排查现场的第一手材料。
    同时这里守住 ``release.bootstrap_key_id`` 在吊销后**仍然指着那一行**：
    只有密钥记录被**删除**时才回落成 NULL（界面回落到 ``#id``）。
    """
    built = build_env.client.post(
        BUILD_URL,
        json={"version": packaging.source_version(), "include_bootstrap_key": True},
        headers=build_env.headers,
    ).json()
    key_id = built["bootstrap_key_id"]
    assert key_id is not None

    revoke = build_env.client.post(
        "/api/v1/admin/bootstrap-keys/%d/revoke" % key_id, headers=build_env.headers
    )
    assert revoke.status_code == 200, revoke.text
    assert revoke.json()["revoked_at"] is not None

    # 1) 机器再用它注册会被拒
    raw = _bundle_bootstrap_key(blob_bytes(build_env.ctx, built["sha256"])).decode().strip()
    denied = build_env.client.post(
        "/api/v1/agent/enroll",
        json={"bootstrap_key": raw, "machine_id": "m-after-revoke"},
    )
    assert denied.status_code == 403, denied.text
    assert denied.json()["code"] == "bootstrap_key_revoked"

    # 2) 版本记录还在，而且仍然指向那一把（只是标记为已吊销）
    with build_env.ctx.db.session() as session:
        row = session.get(AgentRelease, built["id"])
        assert row is not None, "吊销密钥把版本记录一起带走了"
        assert row.bootstrap_key_id == key_id, (
            "吊销不该动版本记录上的引用（只有**删除**密钥记录时才置 NULL）"
        )
        assert row.sha256 == built["sha256"]

    listing = build_env.client.get(
        "/api/v1/admin/releases", headers=build_env.headers
    ).json()["releases"]
    shown = next(r for r in listing if r["id"] == built["id"])
    assert shown["bootstrap_key_revoked"] is True
    assert shown["bootstrap_key_label"] == "随版本 %s 附带" % built["version"]


@requires_source
def test_删掉没有用过的附带密钥之后版本记录还在(
    build_env: SimpleNamespace,
) -> None:
    """密钥记录被**删除**（不是吊销）时版本记录必须活着，只是不再指着那一行。

    这一列的外键是 ``ON DELETE SET NULL`` 而不是 ``CASCADE`` —— 反过来写的话，
    随手清掉一行密钥记录会把"当时发的是哪个包"一起静默干掉。
    """
    built = build_env.client.post(
        BUILD_URL,
        json={"version": packaging.source_version(), "include_bootstrap_key": True},
        headers=build_env.headers,
    ).json()
    key_id = built["bootstrap_key_id"]

    removed = build_env.client.delete(
        "/api/v1/admin/bootstrap-keys/%d" % key_id, headers=build_env.headers
    )
    assert removed.status_code == 200, removed.text

    with build_env.ctx.db.session() as session:
        row = session.get(AgentRelease, built["id"])
        assert row is not None, "密钥被删之后版本记录不该跟着消失"
        assert row.bootstrap_key_id is None, (
            "外键应当是 ON DELETE SET NULL：版本记录活着，引用置空"
        )

    listing = build_env.client.get(
        "/api/v1/admin/releases", headers=build_env.headers
    ).json()["releases"]
    shown = next(r for r in listing if r["id"] == built["id"])
    assert shown["bootstrap_key_id"] is None
    assert shown["bootstrap_key_label"] is None


# --------------------------------------------------------------------------- #
# 风险边界：构建不等于铺开
# --------------------------------------------------------------------------- #


@requires_source
def test_构建只建草稿不会铺开(build_env: SimpleNamespace, built: dict) -> None:
    """构建和"推给 50 台机器"是风险等级完全不同的两件事。"""
    assert built["published_at"] is None
    assert built["rolled_out"] is False
    assert built["yanked"] is False

    status = build_env.client.get(
        "/api/v1/admin/releases", headers=build_env.headers
    ).json()
    assert status["active_release"] is None, "刚构建出来的版本不该是生效版本"


@requires_source
def test_正在铺开的同名版本不许被构建覆盖(
    build_env: SimpleNamespace, built: dict
) -> None:
    """覆盖一个正在铺开的版本，等于让一部分机器拿到 A、一部分拿到 B。"""
    rollout = build_env.client.post(
        "/api/v1/admin/releases/%d/rollout" % built["id"], headers=build_env.headers
    )
    assert rollout.status_code == 200, rollout.text

    again = build_env.client.post(
        BUILD_URL, json={"version": built["version"]}, headers=build_env.headers
    )

    assert again.status_code == 409
    assert "撤回" in again.json()["detail"]


@requires_source
def test_尚未铺开的同名版本可以重新构建(
    build_env: SimpleNamespace, built: dict
) -> None:
    """改了点东西重打一遍是日常动作，不该逼人换一个版本号。"""
    again = build_env.client.post(
        BUILD_URL, json={"version": built["version"]}, headers=build_env.headers
    )
    assert again.status_code == 200, again.text

    rows = build_env.client.get(
        "/api/v1/admin/releases", headers=build_env.headers
    ).json()["releases"]
    assert len([row for row in rows if row["version"] == built["version"]]) == 1


# --------------------------------------------------------------------------- #
# 四类拒绝：每一条都对应一种"看起来成功了"的现场故障
# --------------------------------------------------------------------------- #


@requires_source
def test_版本号和源码对不上会被拒(build_env: SimpleNamespace, built: dict) -> None:
    """挡住"改了代码忘了改 __version__，于是拿旧号发新包"。"""
    response = build_env.client.post(
        BUILD_URL, json={"version": "9.9.9"}, headers=build_env.headers
    )

    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "version_mismatch"
    # 报错要指出**两个**版本号，否则教师不知道改哪一边
    assert "9.9.9" in body["detail"]
    assert packaging.source_version() in body["detail"]
    assert body["details"]["source_version"] == packaging.source_version()


@requires_source
def test_不给版本号直接拒绝(build_env: SimpleNamespace) -> None:
    """版本号必须显式提交：界面预填是体贴，服务端替人猜是灾难。"""
    response = build_env.client.post(BUILD_URL, json={}, headers=build_env.headers)

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


@requires_source
def test_版本号本身不合法会被拒(build_env: SimpleNamespace) -> None:
    response = build_env.client.post(
        BUILD_URL, json={"version": "不是版本号"}, headers=build_env.headers
    )

    assert response.status_code == 400
    assert "版本号" in response.json()["detail"]


def test_没有签名私钥时拒绝构建(workdir: Path) -> None:
    """宁可在这一步拦住，也不要打出一个未签名的包：

    Agent 会拒收它，于是"界面上发布成功、所有机器都升不上去" —— 界面上一切正常
    的失败是最坏的失败。
    """
    settings = Settings()
    settings.data_root = workdir / "data"
    settings.release_signing_key = None
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

        response = client.post(BUILD_URL, json={"version": "1.0.0"}, headers=headers)

    assert response.status_code == 503
    assert response.json()["code"] == "release_not_signed"


@requires_openssl
def test_有私钥但缺公钥时拒绝构建(workdir: Path, isolated_key_dir: Path) -> None:
    """这一条挡的是最难查的现场：服务端能签，机器验不了。

    本机能签名（私钥在），但包里带不上公钥（``release-key.pub.json`` 不在）。
    打出来的包本身没坏 —— 它只是没有任何机器能验。界面上会显示"发布成功"，
    铺开也成功，然后所有机器一动不动。
    """
    generate_keypair(isolated_key_dir / keys.RELEASE_SIGNING_KEY_NAME, bits=2048)
    assert keys.release_public_key_path() is None, "这个场景要求公钥确实不在"

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

        response = client.post(BUILD_URL, json={"version": "1.0.0"}, headers=headers)

    assert response.status_code == 503
    body = response.json()
    assert body["code"] == "release_trust_anchor_missing"
    # 要说清"缺哪个文件"，否则这条拦路就成了纯粹的绊脚石
    assert keys.RELEASE_PUBLIC_KEY_NAME in body["detail"]


# --------------------------------------------------------------------------- #
# 源码缺失 / 构建失败：给一句话，而不是一个栈
# --------------------------------------------------------------------------- #


def test_没有源码时_source_接口如实说明而不是_500(
    build_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """"本机没有源码"是**正常状态**（生产上服务端可能根本没 checkout）。

    该显示成一句能照着做的解释，而不是让人点一下再吃一个 500。
    """
    monkeypatch.setattr(keys, "repo_root", lambda: None)

    response = build_env.client.get(SOURCE_URL, headers=build_env.headers)

    assert response.status_code == 200
    body = response.json()
    assert body["available"] is False
    assert body["version"] is None
    assert "上传" in body["reason"], body["reason"]


@requires_source
def test_source_接口会报出版本号和公钥路径(
    build_env: SimpleNamespace,
) -> None:
    """界面靠它预填版本号，也靠它提前警告"包里没有公钥"。"""
    body = build_env.client.get(SOURCE_URL, headers=build_env.headers).json()

    assert body["available"] is True
    assert body["version"] == packaging.source_version()
    assert body["public_key"] and body["public_key"].endswith(
        keys.RELEASE_PUBLIC_KEY_NAME
    )


def test_没有源码时构建也被拒且说清出路(
    build_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(keys, "repo_root", lambda: None)

    response = build_env.client.post(
        BUILD_URL, json={"version": "1.0.0"}, headers=build_env.headers
    )

    assert response.status_code == 503
    body = response.json()
    assert body["code"] == "release_source_missing"
    assert "上传" in body["detail"]


def test_构建失败时回一句人话而不是一个栈(
    build_env: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """打包脚本崩了（缺文件、权限、磁盘满）都要翻译成一句能照着做的中文。

    直接把 stderr 抖给教师是不行的：它可能带着一堆路径与栈，而界面上的位置
    只够放一句话。
    """
    fake = tmp_path / "agent"
    (fake / "syncoj_agent").mkdir(parents=True)
    (fake / "syncoj_agent" / "__init__.py").write_text(
        '__version__ = "1.0.0"\n', encoding="utf-8"
    )
    (fake / "run_agent.py").write_text("# launcher\n", encoding="utf-8")
    # 刻意不放 packaging/build_bundle.py：构建必然失败
    monkeypatch.setattr(packaging, "agent_root", lambda: fake)

    response = build_env.client.post(
        BUILD_URL, json={"version": "1.0.0"}, headers=build_env.headers
    )

    assert response.status_code == 500
    body = response.json()
    assert body["code"] == "release_build_failed"
    assert body["detail"], "总得有一句话说明失败了"
    assert "Traceback" not in body["detail"]
    assert "packaging" in body["detail"], "要指出缺的是打包脚本"


# --------------------------------------------------------------------------- #
# 审计：这条路径等价于"让服务端发布可执行代码"
# --------------------------------------------------------------------------- #


@requires_source
def test_构建会留下带操作人的审计(
    build_env: SimpleNamespace, built: dict
) -> None:
    """必须能回答"谁、什么时候、发了哪个版本"。

    ``EventLog`` 没有管理员外键（它记的是全场次的事件流），所以操作人写在
    ``meta_json`` 里 —— 只把版本号写进消息是不够的：没有任何办法回答"是谁"。
    """
    with build_env.ctx.db.session() as session:
        rows = list(
            session.execute(
                EventLog.__table__.select().where(EventLog.category == "release_built")
            )
        )

    assert len(rows) == 1, "一次构建留一条，不要按机器数刷屏"
    row = rows[0]
    assert built["version"] in row.message
    meta = json.loads(row.meta_json)
    assert meta["by"] == ADMIN_USER
    assert meta["source"] == "built"


@requires_source
def test_上传那条路的审计没有被顺手改掉(
    build_env: SimpleNamespace, built: dict
) -> None:
    """两条路共用 ``_store_release``，但审计里的来路必须能分辨 ——
    "这个包是打哪儿来的"在排查时是第一个要问的问题。"""
    upload = build_env.client.post(
        "/api/v1/admin/releases",
        files={"file": ("agent-9.9.9.tar.gz", b"fake-bundle", "application/gzip")},
        data={"version": "9.9.9"},
        headers=build_env.headers,
    )
    assert upload.status_code == 200, upload.text

    with build_env.ctx.db.session() as session:
        categories = sorted(
            row.category
            for row in session.execute(EventLog.__table__.select())
            if row.category.startswith("release_")
        )

    assert categories == ["release_built", "release_uploaded"]


def test_构建接口需要管理员登录(build_env: SimpleNamespace) -> None:
    """"让服务端发布可执行代码"不能是匿名可达的。"""
    anonymous = build_env.client.post(BUILD_URL, json={"version": "1.0.0"})

    assert anonymous.status_code == 401


def test_source_接口也需要管理员登录(build_env: SimpleNamespace) -> None:
    """它会报出本机的目录路径，不该给未登录的人看。"""
    assert build_env.client.get(SOURCE_URL).status_code == 401
