"""``.key/`` 下的两把密钥：解析规则、init 的补齐行为、以及安全默认值。

这一批测试里有一条防的是**已经真实发生过**的缺陷：``Settings`` 曾经把
``release_signing_key`` 声明了两遍（一次读 ``SYNCOJ_RELEASE_KEY``、一次写死
``None``），后者静默覆盖前者 —— 于是"自更新为什么开不起来"在代码里怎么看都
像是配好了，而环境变量其实一个字节都没被读。见
:func:`test_release_env_var_is_not_shadowed_by_a_second_declaration`。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from syncoj_server import keys
from syncoj_server.cli import main as cli_main
from syncoj_server.config import Settings


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot(*paths: Path) -> dict:
    """文件名 → (内容摘要, mtime)。用来证明"没有被碰过"。

    只比内容不够：``write`` 写回同样的字节也会更新 mtime，而"重写了一遍密钥"
    在轮换密钥这件事上已经足够糟糕了。
    """
    return {
        path.name: (sha256_of(path), path.stat().st_mtime_ns)
        for path in paths
        if path.exists()
    }


def run_init(data_root: Path, extra=()) -> int:
    return cli_main(
        ["--data-root", str(data_root), "init", "--admin-password", "pw-123456", *extra]
    )


# --------------------------------------------------------------------------- #
# 密钥目录怎么解析
# --------------------------------------------------------------------------- #


def test_env_var_wins(isolated_key_dir: Path, tmp_path: Path) -> None:
    other = tmp_path / "elsewhere"
    other.mkdir()
    assert keys.key_dir() == isolated_key_dir.parent / "keys" or keys.key_dir() is not None

    os.environ[keys.KEY_DIR_ENV] = str(other)
    try:
        assert keys.key_dir() == other
        # 环境变量指向的位置**不要求存在**：操作员明确说了密钥在哪，
        # 由我们否决他只会让"为什么我的路径不生效"变成一个查不出来的问题
        assert keys.key_dir_for_write() == other
    finally:
        os.environ[keys.KEY_DIR_ENV] = str(isolated_key_dir)


def test_missing_key_dir_means_no_implicit_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``.key/`` 不存在 ⇒ 没有隐式密钥 ⇒ 行为与加入 ``.key/`` 之前完全一致。"""
    monkeypatch.delenv(keys.KEY_DIR_ENV, raising=False)
    monkeypatch.setattr(keys, "repo_root", lambda: tmp_path)

    assert keys.key_dir() is None
    assert keys.release_signing_key_path() is None
    assert Settings().release_signing_key is None


def test_unresolvable_repo_root_degrades_to_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pip 装进 site-packages 时"仓库根"没有意义。

    要求：退化成 ``None``，**不能抛异常**，也**不能**硬凑一个路径 —— 硬凑的结果
    是在某个意想不到的地方凭空多出一个密钥目录，而"看起来有密钥"比没有更危险。
    """
    monkeypatch.delenv(keys.KEY_DIR_ENV, raising=False)
    monkeypatch.setattr(keys, "repo_root", lambda: None)

    assert keys.key_dir() is None
    assert keys.key_dir_for_write() is None
    assert keys.release_signing_key_path() is None
    config = Settings()
    assert config.key_dir is None
    assert config.release_signing_key is None


def test_repo_root_is_only_reported_for_a_checkout() -> None:
    """真实的仓库布局下必须认得出来，否则上一条"退化成 None"就退得太早了。"""
    root = keys.repo_root()
    assert root is not None
    assert (root / "server" / "syncoj_server" / "keys.py").is_file()


def test_constructing_settings_creates_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """import 与构造 ``Settings`` 都不许有磁盘副作用。

    这条是硬要求：``create_app`` 早就有"import 时零副作用"的保证（测试全靠它），
    配置层不能把它破坏掉 —— 否则仅仅 import 一下就在磁盘上留下一个密钥目录。

    **两条分支都要走**：环境变量那条会提前 return，根本碰不到"仓库根 ``.key/``"
    的解析逻辑。第一版只测了环境变量分支，变异检查（把"存在才用"改成"顺手建出来"）
    照样是绿的 —— 也就是说那时的测试守不住它真正要守的那行。
    """
    # 分支一：环境变量给了路径。解析成功，但**不许**把目录建出来
    from_env = tmp_path / "keys-should-not-appear"
    monkeypatch.setenv(keys.KEY_DIR_ENV, str(from_env))
    Settings()
    Settings()
    assert keys.key_dir() == from_env          # 解析到它
    assert not from_env.exists(), "构造 Settings 把密钥目录建出来了"
    assert not (from_env / keys.RELEASE_SIGNING_KEY_NAME).exists()

    # 分支二：没有环境变量，走"仓库根/.key"。这个目录**不存在**时
    # key_dir() 必须返回 None，而且不许顺手把它 mkdir 出来
    monkeypatch.delenv(keys.KEY_DIR_ENV, raising=False)
    root = tmp_path / "checkout"
    (root / "server" / "syncoj_server").mkdir(parents=True)
    monkeypatch.setattr(keys, "repo_root", lambda: root)

    config = Settings()
    assert config.key_dir is None
    assert not (root / ".key").exists(), "解析密钥目录时把它建出来了"
    assert config.release_signing_key is None


# --------------------------------------------------------------------------- #
# 环境变量真的生效吗（回归测试）
# --------------------------------------------------------------------------- #


def test_release_env_var_is_not_shadowed_by_a_second_declaration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``SYNCOJ_RELEASE_KEY`` 必须真的被读到。

    这是本文件里最重要的一条。它守的是"同一个字段被声明两遍、后一个静默生效"
    这个具体缺陷 —— 断言的写法刻意是"设了环境变量就一定拿到那个路径"，
    而不是"拿到一个不是 None 的东西"：后者在字段被写死成别的默认值时照样绿。
    """
    explicit = tmp_path / "from-env.pem"
    explicit.write_text("占位，不解析内容\n", encoding="utf-8")
    monkeypatch.setenv("SYNCOJ_RELEASE_KEY", str(explicit))

    assert Settings().release_signing_key == explicit


def test_release_env_var_still_wins_over_the_key_dir(
    monkeypatch: pytest.MonkeyPatch, isolated_key_dir: Path
) -> None:
    """生产上密钥常在 ``/etc/syncoj/``，那是仓库布局之外的路径 —— 环境变量优先。"""
    where = isolated_key_dir
    where.mkdir(parents=True)
    (where / keys.RELEASE_SIGNING_KEY_NAME).write_text("仓库里的\n", encoding="utf-8")

    explicit = where.parent / "etc-release-key.pem"
    explicit.write_text("etc 里的\n", encoding="utf-8")
    monkeypatch.setenv("SYNCOJ_RELEASE_KEY", str(explicit))

    assert Settings().release_signing_key == explicit


def test_key_dir_fallback_requires_the_file_to_exist(
    monkeypatch: pytest.MonkeyPatch, isolated_key_dir: Path
) -> None:
    """``<key_dir>/release-key.pem`` 只在文件真的存在时才当私钥。

    "配了个路径但文件不在"如果也算启用，那"启用自更新"就成了猜测。
    """
    monkeypatch.delenv("SYNCOJ_RELEASE_KEY", raising=False)
    isolated_key_dir.mkdir(parents=True, exist_ok=True)
    assert Settings().release_signing_key is None

    private = isolated_key_dir / keys.RELEASE_SIGNING_KEY_NAME
    private.write_text("占位私钥\n", encoding="utf-8")
    assert Settings().release_signing_key == private


# --------------------------------------------------------------------------- #
# 没有密钥 ⇒ 不提供升级（安全默认值）
# --------------------------------------------------------------------------- #


def test_no_key_means_the_server_refuses_to_offer_upgrades(
    client: TestClient, admin_headers: dict
) -> None:
    """没有私钥时接口必须明说"升级不可用"，而不是给一个空列表让人以为没事。

    ``isolated_key_dir`` 在每个测试里都是空的，所以这里测的就是默认状态。
    """
    body = client.get("/api/v1/admin/releases", headers=admin_headers).json()

    assert body["signing_available"] is False
    assert body["key_id"] is None
    assert body["active_release"] is None


def test_a_key_in_the_key_dir_turns_upgrades_on(
    isolated_key_dir: Path, tmp_path: Path
) -> None:
    """上一条的反面。

    两条一起才有意义：只测"没密钥时是关的"，一个**从来不加载任何密钥**的实现
    也是绿的 —— 而那正是这个功能最可能的失效方式（路径解析对了、但没人用它）。
    """
    assert run_init(tmp_path / "data") == 0

    settings = Settings()
    settings.data_root = tmp_path / "data"
    from syncoj_server.main import create_app

    app = create_app(settings)
    with TestClient(app) as probe:
        login = probe.post(
            "/api/v1/admin/login", json={"username": "admin", "password": "pw-123456"}
        )
        headers = {"Authorization": "Bearer " + login.json()["token"]}
        body = probe.get("/api/v1/admin/releases", headers=headers).json()

    assert body["signing_available"] is True
    # key_id 要和磁盘上那份公钥对得上 —— 证明加载的确实是 ``.key/`` 里那对
    public = json.loads(
        (isolated_key_dir / keys.RELEASE_PUBLIC_KEY_NAME).read_text(encoding="utf-8")
    )
    assert body["key_id"] == public["key_id"]
    assert app.state.ctx.signing_key is not None


def test_upload_is_refused_without_a_key(
    client: TestClient, admin_headers: dict
) -> None:
    """没有私钥就签不出包。签不出就必须拒绝上传 —— 而不是存下来一个永远发不出去的包。"""
    response = client.post(
        "/api/v1/admin/releases",
        headers=admin_headers,
        files={"file": ("agent.tar.gz", b"not-a-real-bundle", "application/gzip")},
        data={"version": "9.9.9", "notes": "x", "channel": "stable"},
    )

    assert response.status_code >= 400
    assert response.json()["code"]


# --------------------------------------------------------------------------- #
# init 补齐密钥
# --------------------------------------------------------------------------- #


def test_init_creates_both_keys(isolated_key_dir: Path, tmp_path: Path) -> None:
    assert run_init(tmp_path / "data") == 0

    private = isolated_key_dir / keys.RELEASE_SIGNING_KEY_NAME
    public = isolated_key_dir / keys.RELEASE_PUBLIC_KEY_NAME
    bootstrap = isolated_key_dir / keys.BOOTSTRAP_KEY_NAME

    assert private.is_file()
    assert public.is_file()
    assert bootstrap.is_file()
    pem = private.read_text(encoding="utf-8")
    # 具体是 PKCS#1 还是 PKCS#8 取决于 openssl 版本（3.x 给 PKCS#8），
    # 两者我们都认 —— 这里只确认它真的是一段 PEM 私钥，不是别的什么东西
    assert pem.startswith("-----BEGIN") and pem.rstrip().endswith("PRIVATE KEY-----")
    # 公钥要能被解析成 JSON —— 它是给 Agent 当信任锚用的，不是随手写的文本
    assert json.loads(public.read_text(encoding="utf-8"))["key_id"]
    assert bootstrap.read_text(encoding="utf-8").strip()


def test_init_never_overwrites_existing_keys(
    isolated_key_dir: Path, tmp_path: Path
) -> None:
    """已有的密钥**一个字节、一个纳秒**都不能动。

    轮换签名私钥会让所有已发布的签名失效；重写统一密钥会让已经装好的整间机房
    的镜像作废。这两件事都必须有人明确按键，不能由 ``init`` 顺手做掉。
    """
    data_root = tmp_path / "data"
    assert run_init(data_root) == 0

    watched = [
        isolated_key_dir / keys.RELEASE_SIGNING_KEY_NAME,
        isolated_key_dir / keys.RELEASE_PUBLIC_KEY_NAME,
        isolated_key_dir / keys.BOOTSTRAP_KEY_NAME,
    ]
    before = snapshot(*watched)

    assert run_init(data_root) == 0
    assert run_init(data_root) == 0

    assert snapshot(*watched) == before


def test_init_restores_a_lost_public_key_from_the_private_key(
    isolated_key_dir: Path, tmp_path: Path
) -> None:
    """公钥丢了不必重签任何东西 —— 它是私钥的派生品。

    重跑 init 应当把它导回来，而私钥保持原样。
    """
    data_root = tmp_path / "data"
    assert run_init(data_root) == 0

    private = isolated_key_dir / keys.RELEASE_SIGNING_KEY_NAME
    public = isolated_key_dir / keys.RELEASE_PUBLIC_KEY_NAME
    private_before = snapshot(private)
    expected = json.loads(public.read_text(encoding="utf-8"))["key_id"]

    public.unlink()
    assert run_init(data_root) == 0

    assert snapshot(private) == private_before
    assert json.loads(public.read_text(encoding="utf-8"))["key_id"] == expected


def test_init_without_a_resolvable_key_dir_still_works(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """生产布局（没有仓库根、也没设环境变量）下 ``init`` 必须照常建库。

    密钥缺失只该让"升级"和"自动签发统一密钥"不可用，不该让初始化本身失败 ——
    那会让人以为整个服务装不起来。
    """
    monkeypatch.delenv(keys.KEY_DIR_ENV, raising=False)
    monkeypatch.setattr(keys, "repo_root", lambda: None)

    data_root = tmp_path / "data"
    assert run_init(data_root) == 0
    assert (data_root / "syncoj.db").is_file()


def test_bootstrap_key_created_by_init_actually_works(
    client: TestClient, admin_headers: dict, isolated_key_dir: Path, settings: Settings
) -> None:
    """光"文件出现了"不算数 —— 那把密钥必须真的能注册一台机器。

    这条守的是"文件写了、库里的哈希没写"这个交叉点：密钥的明文在 ``.key/`` 里，
    哈希在数据库里，两边都要在不才算可用。只断言文件存在的话，忘了写哈希的实现
    照样是绿的，而现场表现是"密钥无效"。

    刻意用 ``client`` 那个 app 的**同一个数据目录**跑 init：否则就是在两个库之间
    自说自话，测出来的是"我写进 A 库的密钥能被 A 库认可"。
    """
    assert run_init(settings.data_root) == 0

    raw = (isolated_key_dir / keys.BOOTSTRAP_KEY_NAME).read_text(encoding="utf-8").strip()
    assert raw

    response = client.post(
        "/api/v1/agent/enroll",
        json={
            "bootstrap_key": raw,
            "machine_id": "machine-from-init",
            "hostname": "pc-init",
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["claimed"] is False
    assert body["pair_code"]


def test_init_re_registers_a_bootstrap_key_whose_hash_is_gone(
    isolated_key_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``db reset`` 只删库、不碰 ``.key/`` —— 于是"文件在、哈希没了"必然出现。

    那时这把密钥在界面上看起来好好的，注册却会被判成"密钥无效"。所以 ``init``
    的判据不能是"文件在不在"，而必须是"文件里的这一把，库里登记了没有"。
    """
    data_root = tmp_path / "data"
    assert run_init(data_root) == 0
    bootstrap = isolated_key_dir / keys.BOOTSTRAP_KEY_NAME
    before = snapshot(bootstrap)

    assert cli_main(["--data-root", str(data_root), "db", "reset", "--yes"]) == 0
    # 密钥不是数据库里的数据，删库不该动它
    assert snapshot(bootstrap) == before

    assert run_init(data_root) == 0
    assert snapshot(bootstrap) == before      # 补登记，不是重新生成

    # 补登记之后这把密钥必须重新可用
    settings = Settings()
    settings.data_root = data_root
    from syncoj_server.main import create_app

    app = create_app(settings)
    with TestClient(app) as probe:
        response = probe.post(
            "/api/v1/agent/enroll",
            json={"bootstrap_key": bootstrap.read_text(encoding="utf-8").strip(),
                  "machine_id": "machine-after-reset"},
        )
    assert response.status_code == 200, response.text


def test_init_does_not_resurrect_a_revoked_key(
    isolated_key_dir: Path, tmp_path: Path
) -> None:
    """吊销是有意的动作，``init`` 不能把它悄悄撤销回来。

    这条与上一条是一对：上一条要求"哈希没了就补登记"，很容易顺手写成
    "任何时候都补登记" —— 那样一吊销再跑一次 init，密钥就自己活了。
    """
    data_root = tmp_path / "data"
    assert run_init(data_root) == 0

    settings = Settings()
    settings.data_root = data_root
    from syncoj_server.main import create_app

    app = create_app(settings)
    with TestClient(app) as probe:
        login = probe.post(
            "/api/v1/admin/login",
            json={"username": "admin", "password": "pw-123456"},
        )
        headers = {"Authorization": "Bearer " + login.json()["token"]}
        listed = probe.get("/api/v1/admin/bootstrap-keys", headers=headers).json()
        key_id = listed["items"][0]["id"]
        assert probe.post(
            "/api/v1/admin/bootstrap-keys/%d/revoke" % key_id, headers=headers
        ).status_code == 200

        assert run_init(data_root) == 0

        # 仍然被吊销：还能在列表里看到它，而且是吊销状态
        after = probe.get("/api/v1/admin/bootstrap-keys", headers=headers).json()
        assert after["items"][0]["revoked_at"], "init 把吊销状态撤销了"

        # 端到端再确认一次：这把密钥现在注册不了
        rejected = probe.post(
            "/api/v1/agent/enroll",
            json={"bootstrap_key": (isolated_key_dir / keys.BOOTSTRAP_KEY_NAME)
                  .read_text(encoding="utf-8").strip(),
                  "machine_id": "machine-revoked"},
        )
    assert rejected.status_code == 403, rejected.text


def test_bootstrap_key_file_is_root_only_on_posix(
    isolated_key_dir: Path, tmp_path: Path
) -> None:
    """明文密钥能注册整间机房，权限必须是 0600。"""
    if os.name != "posix":  # pragma: no cover - Windows 上没有 POSIX 权限位
        pytest.skip("Windows 上没有 POSIX 权限位")

    assert run_init(tmp_path / "data") == 0

    mode = (isolated_key_dir / keys.BOOTSTRAP_KEY_NAME).stat().st_mode & 0o777
    assert mode == 0o600, oct(mode)
    dir_mode = isolated_key_dir.stat().st_mode & 0o777
    assert dir_mode == 0o700, oct(dir_mode)
