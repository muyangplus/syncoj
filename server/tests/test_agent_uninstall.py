"""管理端「卸载这台机器上的 Agent」。

为什么这份测试盯的是**签名**而不是 HTTP 状态码
---------------------------------------------
这个功能的全部价值在于"教师点一下，那台机器上 root 的删除动作就会被执行"。
服务端这边能做错的、又能被 200 掩过去的事情只有一类：**签出来的令牌机器不认**。
它认不认由三件事决定 —— 签的是不是 ``b64url(payload)`` 那一段、用的钥匙是不是
信任锚里那把、字段对不对得上。所以这里**不复用服务端的签名实现去验自己**，
而是把令牌交给机器侧那份独立实现（``agent/syncoj_agent/rsa.py``，与发布包验签
用的是同一份代码）过一遍 —— 那份代码点头，才等于另一半真的能用。

另外两条守卫各自对应一个真实的失败方式：

* **没有私钥就不许发**：签不出来还回 200，教师会以为机器马上要卸载了。
* **审计事件不写令牌**：那枚令牌能在那台机器上换一次 root 删除，而事件表是
  会被到处导出、翻看的（"改 zip 密码不留密码"是同一条纪律）。
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator, List

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

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
from syncoj_server.models import Admin, EventLog
from syncoj_server.security import hash_password
from syncoj_server.services import uninstall
from syncoj_server.services.signing import (
    PKCS1_SHA256_DIGEST_INFO_PREFIX,
    generate_keypair,
    openssl_available,
    write_public_key_json,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "agent"))

from syncoj_agent.rsa import RSAPublicKey, verify_pkcs1v15_sha256  # noqa: E402

HAS_OPENSSL = openssl_available() is not None
requires_openssl = pytest.mark.skipif(not HAS_OPENSSL, reason="需要 openssl")

MACHINE_ID = "m-uninstall-test"
HOSTNAME = "exam-pc-uninstall"
FALLBACK_COMMAND_MARKER = "install/bootstrap.sh"

#: payload 里必须逐字存在的键。**写死一份**而不是从服务端常量里取：
#: 这份清单就是冻结的接口本身，从被测代码里推导它等于什么也没验。
EXPECTED_KEYS = {
    "v",
    "kind",
    "machine_uuid",
    "agent_id",
    "nonce",
    "issued_at",
    "expires_at",
    "key_id",
}

_NONCE_RE = re.compile(r"^[0-9a-f]{32}$")


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _decode_token(token: str) -> dict:
    """拆出令牌里的 payload。**不复用服务端代码**，见模块 docstring。"""
    signed, dot, _signature = token.partition(".")
    assert dot, "令牌必须是 b64url(payload).b64url(signature) 两段：%r" % token
    return json.loads(_b64url_decode(signed).decode("utf-8"))


# --------------------------------------------------------------------------- #
# 夹具：一台配好签名密钥的服务端 + 一台已配对、且报过公钥的机器
# --------------------------------------------------------------------------- #


@pytest.fixture()
def env(workdir: Path, isolated_key_dir: Path) -> Iterator[SimpleNamespace]:
    """配好**成对**密钥的服务端，以及一台已经配对到 S001 的机器。

    密钥按真实布局落进密钥目录（私钥 + 公钥两个文件），而不是直接塞一个
    ``settings.release_signing_key`` —— 令牌里的 ``key_id`` 必须与
    ``release-key.pub.json`` 里那个字段一致，只塞私钥就测不到那件事。
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

        contest = client.post(
            "/api/v1/admin/contests",
            json={"name": "卸载测试", "slug": "uninstall", "status": "running"},
            headers=headers,
        ).json()
        client.post(
            "/api/v1/admin/contests/%d/players" % contest["id"],
            json=[{"player_no": "S001", "name": "张三"}],
            headers=headers,
        )
        roster = make_roster(
            client, headers, "卸载测试班", entries=[{"player_no": "S001", "name": "张三"}]
        )

        raw_key = issue_bootstrap_key(client, headers)
        first = enroll_machine(
            client, raw_key, machine_id=MACHINE_ID, hostname=HOSTNAME
        )
        bind_by_code(client, headers, first["pair_code"], roster["entries"][0]["id"])
        enrolled = enroll_machine(
            client,
            raw_key,
            machine_id=MACHINE_ID,
            machine_uuid=first["machine_uuid"],
            hostname=HOSTNAME,
        )
        assert enrolled["bound"] is True, enrolled

        # 第一次心跳：机器报告"本机有发布公钥"。卸载的门禁之一就是它。
        do_tick(
            client,
            enrolled["token"],
            [],
            machine_id=MACHINE_ID,
            release_public_key=True,
        )

        yield SimpleNamespace(
            app=app,
            client=client,
            ctx=app.state.ctx,
            settings=settings,
            key=key,
            headers=headers,
            contest=contest,
            machine_uuid=first["machine_uuid"],
            token=enrolled["token"],
            agent_id=enrolled["agent_id"],
        )


def uninstall_url(agent_id: int) -> str:
    return "/api/v1/admin/agents/%d/uninstall" % agent_id


def request_uninstall(env: SimpleNamespace, confirm: str = HOSTNAME):
    return env.client.post(
        uninstall_url(env.agent_id),
        params={"confirm": confirm},
        headers=env.headers,
    )


def tick(env: SimpleNamespace, **kwargs) -> dict:
    """一次心跳。默认**不带** ``release_public_key``（= 老版本 Agent 的报文）。"""
    return do_tick(env.client, env.token, [], machine_id=MACHINE_ID, **kwargs)


def public_key(env: SimpleNamespace) -> RSAPublicKey:
    """从下发给机器的信任锚文件里读公钥 —— 与机器读取的是同一个文件。"""
    data = json.loads(
        (env.settings.release_signing_key.parent / keys.RELEASE_PUBLIC_KEY_NAME).read_text(
            encoding="utf-8"
        )
    )
    return RSAPublicKey.from_dict(data)


def event_rows(env: SimpleNamespace, category: str = "agent_uninstall") -> List[EventLog]:
    with env.ctx.db.session() as session:
        return list(
            session.execute(
                select(EventLog).where(EventLog.category == category)
            ).scalars()
        )


# --------------------------------------------------------------------------- #
# 点按 → 记审计 → 心跳拿到令牌
# --------------------------------------------------------------------------- #


@requires_openssl
def test_点一下按钮就记下请求并回执说清后果(env: SimpleNamespace) -> None:
    response = request_uninstall(env)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    # 回执要写清"下次心跳才执行"与有效期 —— 界面直接显示这句话
    assert "下次心跳" in body["detail"]
    assert "15 分钟" in body["detail"]

    rows = event_rows(env)
    assert len(rows) == 1, [r.message for r in rows]
    event = rows[0]
    assert event.level == "warning"
    # 审计要写明是哪台机器、哪个人
    assert HOSTNAME in event.message
    assert ADMIN_USER in event.message


@requires_openssl
def test_确认机器名打错就拒绝(env: SimpleNamespace) -> None:
    response = request_uninstall(env, confirm="不是这台机器")

    assert response.status_code == 400
    assert response.json()["code"] == "name_mismatch"
    assert not event_rows(env), "确认没过就不该留下任何卸载请求"


@requires_openssl
def test_机器不存在时_404(env: SimpleNamespace) -> None:
    response = env.client.post(
        uninstall_url(999999), params={"confirm": HOSTNAME}, headers=env.headers
    )

    assert response.status_code == 404
    assert response.json()["code"] == "agent_not_found"


@requires_openssl
def test_没请求卸载时心跳里是_null(env: SimpleNamespace) -> None:
    body = tick(env, release_public_key=True)

    assert body["uninstall_token"] is None


@requires_openssl
def test_请求之后下一次心跳拿得到令牌(env: SimpleNamespace) -> None:
    assert request_uninstall(env).status_code == 200

    body = tick(env, release_public_key=True)

    token = body["uninstall_token"]
    assert isinstance(token, str) and token


@requires_openssl
def test_再点一次会换一枚新令牌(env: SimpleNamespace) -> None:
    assert request_uninstall(env).status_code == 200
    first = tick(env, release_public_key=True)["uninstall_token"]

    assert request_uninstall(env).status_code == 200
    second = tick(env, release_public_key=True)["uninstall_token"]

    assert first and second
    assert first != second, "重新点一次必须换一枚新令牌"
    assert _decode_token(first)["nonce"] != _decode_token(second)["nonce"], (
        "旧的自然过期，靠的是 nonce 不同"
    )


@requires_openssl
def test_同一枚令牌会在多轮心跳里原样重发(env: SimpleNamespace) -> None:
    """机器可能一次没收到、也可能写盘失败 —— 重发的必须是**同一枚**。"""
    assert request_uninstall(env).status_code == 200

    first = tick(env, release_public_key=True)["uninstall_token"]
    second = tick(env, release_public_key=True)["uninstall_token"]

    assert first == second


@requires_openssl
def test_令牌过期后不再下发(env: SimpleNamespace) -> None:
    """15 分钟是硬边界：过了就不该再往那台机器上发任何东西。"""
    import datetime

    from syncoj_server.models import utcnow

    assert request_uninstall(env).status_code == 200

    # 直接把登记表里的时刻推回到过期之前 —— 不 sleep，也不去动服务端的时间源
    now = utcnow()
    stale = now - datetime.timedelta(seconds=uninstall.PENDING_TTL_SECONDS + 5)
    env.ctx.pending_uninstalls.request(
        env.machine_uuid, agent_id=env.agent_id, admin="测试", now=stale
    )

    body = tick(env, release_public_key=True)
    assert body["uninstall_token"] is None


# --------------------------------------------------------------------------- #
# 令牌本身（这一节才是"另一半能不能用"的判据）
# --------------------------------------------------------------------------- #


@requires_openssl
def test_令牌的字段与有效期都对(env: SimpleNamespace) -> None:
    assert request_uninstall(env).status_code == 200
    token = tick(env, release_public_key=True)["uninstall_token"]
    payload = _decode_token(token)

    assert set(payload) == EXPECTED_KEYS, payload
    assert payload["v"] == 1
    assert payload["kind"] == "agent_uninstall"
    assert payload["machine_uuid"] == env.machine_uuid
    assert payload["agent_id"] == env.agent_id
    assert payload["expires_at"] - payload["issued_at"] == 900
    assert _NONCE_RE.match(payload["nonce"]), payload["nonce"]


@requires_openssl
def test_令牌用机器侧那份独立实现验得通(env: SimpleNamespace) -> None:
    """**这份测试是整个功能的判据。**

    用的是 ``agent/syncoj_agent/rsa.py`` —— 与机器验发布包签名的是同一份代码，
    公钥取的是下发给机器的那个信任锚文件。它点头，另一半才真的能用。
    """
    assert request_uninstall(env).status_code == 200
    token = tick(env, release_public_key=True)["uninstall_token"]

    signed, _dot, _signature = token.partition(".")
    signature = _b64url_decode(_signature)

    assert (
        len(signature) * 8 == env.key.bits
    ), "签名长度必须恰好等于模数字节数，否则机器会直接拒收"
    assert verify_pkcs1v15_sha256(public_key(env), signed.encode("ascii"), signature), (
        "机器侧的验签实现拒绝了这枚令牌 —— 它到了机器上会被静默丢弃"
    )


@requires_openssl
def test_签名覆盖的是_b64_payload_那一段(env: SimpleNamespace) -> None:
    """签原始 JSON 也能"验得过"是一种错觉：这里钉死签的是哪串字节。

    用服务端之外的实现重建整段 ``EM`` 再比对，确认被哈希的是
    ``b64url(payload)`` 的 ASCII 字节 —— 换任何一个字节都该验不通。
    """
    assert request_uninstall(env).status_code == 200
    token = tick(env, release_public_key=True)["uninstall_token"]
    signed, _dot, _signature = token.partition(".")

    length = env.key.byte_length
    recovered = pow(
        int.from_bytes(_b64url_decode(_signature), "big"), env.key.e, env.key.n
    ).to_bytes(length, "big")
    digest_info = PKCS1_SHA256_DIGEST_INFO_PREFIX + hashlib.sha256(
        signed.encode("ascii")
    ).digest()
    expected = b"\x00\x01" + b"\xff" * (length - len(digest_info) - 3) + b"\x00" + digest_info

    assert recovered == expected

    # 反证：拿原始 payload 当签名对象时，验签必须失败
    raw_payload = _b64url_decode(signed)
    assert not verify_pkcs1v15_sha256(
        public_key(env), raw_payload, _b64url_decode(_signature)
    ), "签名对象写成了原始 JSON —— 换一台机器就验不过"


@requires_openssl
def test_换个密钥就验不通(env: SimpleNamespace, isolated_key_dir: Path) -> None:
    """令牌绑死在这把钥匙上：别的钥匙（另一台服务端的信任锚）认不了它。"""
    assert request_uninstall(env).status_code == 200
    token = tick(env, release_public_key=True)["uninstall_token"]

    other = generate_keypair(isolated_key_dir / "other-key.pem", bits=2048)
    other_public = RSAPublicKey(n=other.n, e=other.e, key_id=other.key_id)

    signed, _dot, _signature = token.partition(".")
    assert not verify_pkcs1v15_sha256(
        other_public, signed.encode("ascii"), _b64url_decode(_signature)
    )


@requires_openssl
def test_密钥标识与信任锚里那个一致(env: SimpleNamespace) -> None:
    """机器靠 ``key_id`` 认出"这是不是我信任锚里那把钥匙"。"""
    assert request_uninstall(env).status_code == 200
    token = tick(env, release_public_key=True)["uninstall_token"]

    anchored = json.loads(
        (env.settings.release_signing_key.parent / keys.RELEASE_PUBLIC_KEY_NAME).read_text(
            encoding="utf-8"
        )
    )
    assert _decode_token(token)["key_id"] == anchored["key_id"]


# --------------------------------------------------------------------------- #
# 两道门禁
# --------------------------------------------------------------------------- #


def test_没有发布私钥就不许发(workdir: Path, isolated_key_dir: Path) -> None:
    """服务端没配签名私钥时，这条路整体不可用 —— 而且要说清怎么绕过去。

    这里**不需要**密钥，所以不跳过：这正是默认状态，也是绝大多数现场的状态。
    """
    settings = Settings()
    settings.data_root = workdir / "data"
    assert settings.release_signing_key is None, "这个场景要求密钥目录里没有私钥"

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
        headers = {"Authorization": "Bearer " + login.json()["token"]}

        roster = make_roster(
            client, headers, "无密钥班", entries=[{"player_no": "S001", "name": "张三"}]
        )
        raw_key = issue_bootstrap_key(client, headers)
        first = enroll_machine(
            client, raw_key, machine_id=MACHINE_ID, hostname=HOSTNAME
        )
        bind_by_code(client, headers, first["pair_code"], roster["entries"][0]["id"])
        enrolled = enroll_machine(
            client,
            raw_key,
            machine_id=MACHINE_ID,
            machine_uuid=first["machine_uuid"],
            hostname=HOSTNAME,
        )
        # 即使机器报过公钥，缺私钥一样发不出去
        do_tick(
            client,
            enrolled["token"],
            [],
            machine_id=MACHINE_ID,
            release_public_key=True,
        )

        response = client.post(
            uninstall_url(enrolled["agent_id"]),
            params={"confirm": HOSTNAME},
            headers=headers,
        )

        assert response.status_code == 400, response.text
        body = response.json()
        assert body["code"] == "uninstall_unavailable"
        # 替代办法必须**可执行**：那句人话里得带着装机页那条命令
        assert FALLBACK_COMMAND_MARKER in body["detail"], body["detail"]
        assert "--uninstall --yes" in body["detail"], body["detail"]

        # 门禁没过就不该留下任何"教师点过"的痕迹
        with app.state.ctx.db.session() as session:
            rows = list(
                session.execute(
                    select(EventLog).where(EventLog.category == "agent_uninstall")
                ).scalars()
            )
        assert not rows, "签不出授权就不该记下这次请求"

        body = do_tick(
            client,
            enrolled["token"],
            [],
            machine_id=MACHINE_ID,
            release_public_key=True,
        )
        assert body["uninstall_token"] is None


@requires_openssl
def test_机器没报过公钥就不许发(env: SimpleNamespace) -> None:
    """没有信任锚的机器验不了签名，发过去只会让它拒收 —— 必须在这里挡住。

    这一条尤其重要：**服务端一切正常、教师看到"操作成功"，而机器毫无动静**，
    是这类守卫失效时的典型表现。
    """
    # 先把它打回"没报过"的状态：改一次心跳报文，明确说不带这个字段
    body = do_tick(
        env.client, env.token, [], machine_id=MACHINE_ID, release_public_key=False
    )
    assert body["uninstall_token"] is None

    response = request_uninstall(env)

    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert response.json()["code"] == "uninstall_unavailable"
    assert FALLBACK_COMMAND_MARKER in detail, detail
    assert "--uninstall --yes" in detail, detail
    assert not event_rows(env)

    # 而且这一刻之后心跳里也不会冒出令牌
    assert tick(env, release_public_key=False)["uninstall_token"] is None


# --------------------------------------------------------------------------- #
# 审计纪律
# --------------------------------------------------------------------------- #


@requires_openssl
def test_审计事件里没有令牌明文(env: SimpleNamespace) -> None:
    """令牌能在那台机器上换一次 root 删除 —— 事件表里不许有它的影子。

    断言的是**整行正文**（含 ``meta_json``），而不是某一列：抄进副字段里
    和抄进 message 一样糟。
    """
    assert request_uninstall(env).status_code == 200
    token = tick(env, release_public_key=True)["uninstall_token"]
    assert token

    rows = event_rows(env)
    assert rows, "连审计事件都没记"
    for row in rows:
        text = "%s\n%s" % (row.message, row.meta_json or "")
        assert token not in text, "审计事件里出现了令牌明文：%s" % text
        # 令牌的每一段也不许单独出现（有人可能只抄了 payload 或签名）
        for part in token.split("."):
            assert part not in text, "审计事件里出现了令牌的片段：%s" % text
