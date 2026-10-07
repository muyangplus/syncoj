"""zip 打密码 / 改密码 / 生成随机密码 + 自动写 password.txt。

这一层的东西只靠"看起来对"是验不出来的，每条判据背后都有一个具体的现场后果：

1. **写出来的加密包必须是标准能读的**。标准库写不了加密 zip，所以这一段是自己
   拼的字节；拼错一位的表现是"密码明明对，学生却打不开"。这里的往返测试用
   ``zipfile``（**独立实现**）把包读回来，并在环境里有 InfoZIP 的 ``zip``/
   ``unzip`` 时再做一次交叉验证（没有就 skip，不假装验过）。
2. **说清楚了 ZipCrypto 是弱加密**。选它是因为学生的 Archive Manager 只认传统
   加密；它是"挡得住随手翻看，挡不住有心人"。页面与文案的口径都钉在这条上。
3. **改内容的语义与「在线改正文」完全一致**：同一个 asset id 换 sha256/size、
   把 ``done`` 的目标重排回 ``pending`` 并**清零续传偏移**、写审计事件。
4. **旧密码错时资产一个字节都不能动**。这一步在重新打包之前就拦住，而不是
   "先改了再说"。
5. **密码不另存一份**：它只活在 ``password.txt`` 这份文本资产里，所以
   "丢了也能找回"—— 这条也顺便把"改密码 = 更新同一份 password.txt"钉住。
6. **随机密码的字符集**：去掉易混字符（0 O 1 l I），两次生成必须不同。
7. **"打包"与"改密码"是两件事**：非 zip 资产走打包（同一个 asset id、文件名换成
   ``<原基名>.zip``、zip 里的成员名**仍是原文件名**），zip 资产走改密码；回执里的
   ``packaged`` 是区分它们的字段。两条路共用同一套重排与 ``password.txt`` 机制，
   所以"非 zip"不再是 400 —— 那是老口径（见
   ``test_POST_非_zip_会被打包成_zip_而不是_400`` 的注释）。

关于 ``bytes_done``：服务端没有任何写入路径会碰它（它是留给界面看的镜像），
所以"重排时清零"这条断言必须从一个**我们亲手设的**非零值出发，否则 0 → 0
永远测不出问题。
"""

from __future__ import annotations

import hashlib
import io
import re
import shutil
import subprocess
import zipfile
import zlib
from pathlib import Path
from typing import Dict, List, Tuple

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from syncoj_server.api import admin as admin_module
from syncoj_server.models import Asset, DeployStatus, DeployTarget, DeployTask, EventLog
from syncoj_server.services import zipcrypto

from conftest import do_tick

ASSETS_URL = "/api/v1/admin/contests/%d/assets"
TEXT_URL = "/api/v1/admin/contests/%d/assets/%d/text"
ZIP_URL = "/api/v1/admin/contests/%d/assets/%d/zip-password"
DEPLOYS_URL = "/api/v1/admin/contests/%d/deploys"
PLAYER_PATH = "/api/v1/player/context"

ZIP_NAME = "题面.zip"
PASSWORD_FILE = "password.txt"
MEMBER_TEXT = "第一版题面\n"
MEMBER_BYTES = bytes(range(256)) * 8

#: 抽查"重排时清零"的续传偏移：服务端不写这个字段，只能由测试钉进去。
PINNED_OFFSET = 64 * 1024


# --------------------------------------------------------------------------- #
# 造包与读包
# --------------------------------------------------------------------------- #


def make_zip() -> bytes:
    """一个包含"多个成员 / 目录项 / store 与 deflate 混用 / 注释"的包。

    刻意把每一种情况都放进来：只测一个 deflate 成员的往返，看不出"压缩方式被
    统一改成 store"这类错 —— 而那种错在真实题面（store 的图片 + deflate 的文本）
    上会让一半成员解出来是垃圾。
    """
    buf = io.BytesIO()
    when = (2026, 10, 6, 13, 20, 0)
    with zipfile.ZipFile(buf, "w") as archive:
        text = zipfile.ZipInfo("题面.txt", date_time=when)
        text.compress_type = zipfile.ZIP_DEFLATED
        archive.writestr(text, MEMBER_TEXT.encode("utf-8"))

        raw = zipfile.ZipInfo("raw.bin", date_time=when)
        raw.compress_type = zipfile.ZIP_STORED
        archive.writestr(raw, MEMBER_BYTES)

        folder = zipfile.ZipInfo("data/", date_time=when)
        folder.compress_type = zipfile.ZIP_STORED
        archive.writestr(folder, b"")

        child = zipfile.ZipInfo("data/c.txt", date_time=when)
        child.compress_type = zipfile.ZIP_DEFLATED
        child.comment = b"member-comment"
        archive.writestr(child, b"hello")

        archive.comment = b"archive-comment"
    return buf.getvalue()


EXPECTED: Dict[str, bytes] = {
    "题面.txt": MEMBER_TEXT.encode("utf-8"),
    "raw.bin": MEMBER_BYTES,
    "data/": b"",
    "data/c.txt": b"hello",
}


def make_plain_zip_by_infozip(tmp_path: Path, password: str) -> Path:
    """用 InfoZIP 的 ``zip -P`` 打一个包（作为读那一半的参照实现）。"""
    (tmp_path / "hello.txt").write_bytes(b"hi there\n")
    result = subprocess.run(
        [ZIP_BIN, "-q", "-P", password, "packed.zip", "hello.txt"],
        cwd=str(tmp_path),
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    return tmp_path / "packed.zip"


ZIP_BIN = shutil.which("zip") or shutil.which("zip.exe")


def _unzip_candidates() -> List[str]:
    """可能支持 ``-P`` 的 unzip。

    不能只认 ``shutil.which("unzip")``：Windows 上 w64devkit 的 BusyBox unzip
    不认 ``-P``，而它可能排在 InfoZIP 前面。所以连同 ``zip`` 同目录的
    ``unzip`` 一起列出来，下面再用**行为**（拿一个真的加密包试一次）判定。
    """
    found: List[str] = []
    for name in ("unzip", "unzip.exe"):
        path = shutil.which(name)
        if path and path not in found:
            found.append(path)
    if ZIP_BIN:
        for sibling in ("unzip.exe", "unzip"):
            path = Path(ZIP_BIN).with_name(sibling)
            if path.is_file() and str(path) not in found:
                found.append(str(path))
    return found


@pytest.fixture(scope="module")
def unzip_with_password(tmp_path_factory) -> str:
    """环境里能解 ZipCrypto 的 unzip；没有就 skip（不假装验过）。"""
    if not ZIP_BIN:
        pytest.skip("环境里没有 InfoZIP 的 zip，做不了交叉验证")
    work = tmp_path_factory.mktemp("zipcross")
    (work / "probe.txt").write_bytes(b"probe\n")
    created = subprocess.run(
        [ZIP_BIN, "-q", "-P", "probe-pw", "probe.zip", "probe.txt"],
        cwd=str(work),
        capture_output=True,
    )
    if created.returncode != 0:  # pragma: no cover - 取决于环境里的 zip
        pytest.skip("环境里的 zip 不支持 -P")
    for candidate in _unzip_candidates():
        try:
            result = subprocess.run(
                [candidate, "-P", "probe-pw", "-p", "probe.zip"],
                cwd=str(work),
                capture_output=True,
            )
        except OSError:  # pragma: no cover
            continue
        if result.returncode == 0 and result.stdout == b"probe\n":
            return candidate
    pytest.skip("环境里的 unzip 不支持 -P（传统加密解压）")


# --------------------------------------------------------------------------- #
# 接口辅助
# --------------------------------------------------------------------------- #


def upload(
    client: TestClient,
    headers: dict,
    contest_id: int,
    filename: str,
    data: bytes,
    kind: str = "题面",
) -> dict:
    response = client.post(
        ASSETS_URL % contest_id,
        files={"file": (filename, data, "application/zip")},
        data={"kind": kind},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def upload_packaged(
    client: TestClient,
    headers: dict,
    contest_id: int,
    filename: str,
    data: bytes,
    *,
    zip_password: str = "",
    kind: str = "题面",
) -> dict:
    """上传时就要求服务端打包成 zip（``package_zip=true``）。

    密码留空 = 让服务端生成：这一条与接口的默认值是同一个口径，所以这里不传
    ``zip_password`` 时**不写这个字段**，走服务端自己的默认。
    """
    form = {"kind": kind, "package_zip": "true"}
    if zip_password:
        form["zip_password"] = zip_password
    response = client.post(
        ASSETS_URL % contest_id,
        files={"file": (filename, data, "application/octet-stream")},
        data=form,
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def get_status(client: TestClient, headers: dict, contest_id: int, asset_id: int):
    return client.get(ZIP_URL % (contest_id, asset_id), headers=headers)


def set_password(client: TestClient, headers: dict, contest_id: int, asset_id: int, **body):
    return client.post(ZIP_URL % (contest_id, asset_id), json=body, headers=headers)


def stored(client: TestClient, asset_id: int) -> Tuple[str, int, str]:
    """库上那一行：``(sha256, size, filename)``。

    "一个字节都没动"这类断言必须打在**库**上 —— 接口回显只能证明服务端**说**它
    存了什么。
    """
    ctx = client.app.state.ctx
    with ctx.db.session() as session:
        row = session.get(Asset, asset_id)
        assert row is not None
        return row.sha256, int(row.size), row.filename


def blob_bytes(client: TestClient, sha256: str) -> bytes:
    ctx = client.app.state.ctx
    with ctx.blobs.open(sha256) as handle:
        return handle.read()


def asset_text(client: TestClient, headers: dict, contest_id: int, asset_id: int) -> str:
    response = client.get(TEXT_URL % (contest_id, asset_id), headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["content"]


def listed_asset(client: TestClient, headers: dict, contest_id: int, asset_id: int) -> dict:
    page = client.get(ASSETS_URL % contest_id, headers=headers)
    assert page.status_code == 200, page.text
    return next(item for item in page.json()["items"] if item["id"] == asset_id)


def count_assets_named(client: TestClient, contest_id: int, filename: str) -> int:
    ctx = client.app.state.ctx
    with ctx.db.session() as session:
        rows = (
            session.execute(
                select(Asset).where(
                    Asset.contest_id == contest_id, Asset.filename == filename
                )
            )
            .scalars()
            .all()
        )
        return len(rows)


def password_asset_id_of(client: TestClient, headers: dict, contest_id: int) -> int:
    """场上那份 ``password.txt`` 的 id。"""
    page = client.get(ASSETS_URL % contest_id, headers=headers)
    assert page.status_code == 200, page.text
    return next(
        item["id"] for item in page.json()["items"] if item["filename"] == PASSWORD_FILE
    )


def password_lines(content: str) -> Dict[str, str]:
    """把 password.txt 的正文拆成 ``{文件: …, 密码: …, 生成时间: …}``。"""
    return dict(
        line.split("：", 1) for line in content.strip().splitlines() if "：" in line
    )


def blob_exists(client: TestClient, sha256: str) -> bool:
    return client.app.state.ctx.blobs.has(sha256)


def add_player(client: TestClient, headers: dict, contest_id: int, player_no: str) -> dict:
    response = client.post(
        "/api/v1/admin/contests/%d/players" % contest_id,
        json=[{"player_no": player_no}],
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()["players"][0]


def deploy_to(
    client: TestClient, headers: dict, contest_id: int, asset_id: int, player_ids: List[int]
) -> dict:
    response = client.post(
        DEPLOYS_URL % contest_id,
        json={
            "asset_id": asset_id,
            "target_kind": "player",
            "player_ids": player_ids,
            "dest_dir": "",
            "mode": "overwrite",
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def _target_rows(session, asset_id: int) -> List[DeployTarget]:
    return list(
        session.execute(
            select(DeployTarget)
            .join(DeployTask, DeployTarget.task_id == DeployTask.id)
            .where(DeployTask.asset_id == asset_id)
            .order_by(DeployTarget.id)
        ).scalars()
    )


def target_states(client: TestClient, asset_id: int) -> List[Tuple[str, int]]:
    ctx = client.app.state.ctx
    with ctx.db.session() as session:
        return [(t.status, int(t.bytes_done)) for t in _target_rows(session, asset_id)]


def pin_bytes_done(client: TestClient, asset_id: int, value: int) -> None:
    ctx = client.app.state.ctx
    with ctx.db.session() as session:
        for target in _target_rows(session, asset_id):
            target.bytes_done = value


def audit_events(client: TestClient) -> List[dict]:
    ctx = client.app.state.ctx
    with ctx.db.session() as session:
        rows = (
            session.execute(
                select(EventLog)
                .where(EventLog.category == "asset_zip_password")
                .order_by(EventLog.id)
            )
            .scalars()
            .all()
        )
        return [
            {
                "level": row.level,
                "category": row.category,
                "contest_id": row.contest_id,
                "message": row.message,
            }
            for row in rows
        ]


def read_encrypted(blob: bytes, password: str, name: str = "题面.txt") -> bytes:
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        return archive.read(name, pwd=password.encode("utf-8"))


#: 标准库用**错**密码读加密成员时可能抛的三种东西。
#:
#: 不是"标准库不稳定"，而是 ZipCrypto 的密码校验只有一个字节（CRC 的高字节）：
#: 错密码有 1/256 的概率撞对那个校验字节，于是它继续往下解密 —— deflate 成员会在
#: zlib 里当场炸开（``zlib.error``），store 成员要到末尾比对 CRC 才失败
#: （``BadZipFile``）；没撞对时新版 Python 在加密头那一关就抛 ``RuntimeError``。
#: 三种都算"这份密码打不开这个包"。
#:
#: 服务端**不需要**在这里区分：``zipcrypto.rewrite_zip`` 把三种都归一成
#: ``BadPasswordError``（见 ``test_老版本_Python_用_zlib_报错也要当成密码错``）。
WRONG_PASSWORD_ERRORS = (RuntimeError, zipfile.BadZipFile, zlib.error)


def assert_password_rejected(
    archive: zipfile.ZipFile, name: str, password: str
) -> None:
    with pytest.raises(WRONG_PASSWORD_ERRORS):
        archive.read(name, pwd=password.encode("utf-8"))


# --------------------------------------------------------------------------- #
# 1. 纯服务层：写出来的加密包，独立实现能读回来
# --------------------------------------------------------------------------- #


def test_往返_改写成加密包后标准库能带密码读回原文() -> None:
    out = io.BytesIO()
    zipcrypto.rewrite_zip(io.BytesIO(make_zip()), out, new_password=b"Pw123456")

    with zipfile.ZipFile(io.BytesIO(out.getvalue())) as archive:
        assert set(archive.namelist()) == set(EXPECTED)
        for info in archive.infolist():
            assert info.flag_bits & 0x1, "每个成员都必须是加密的：%s" % info.filename
            got = archive.read(info, pwd=b"Pw123456")
            assert got == EXPECTED[info.filename]


def test_不带密码读会失败() -> None:
    out = io.BytesIO()
    zipcrypto.rewrite_zip(io.BytesIO(make_zip()), out, new_password=b"Pw123456")

    with zipfile.ZipFile(io.BytesIO(out.getvalue())) as archive:
        with pytest.raises(RuntimeError):
            archive.read("题面.txt")


def test_目录项_时间戳_压缩方式_注释都保留() -> None:
    original = zipfile.ZipFile(io.BytesIO(make_zip()))
    before = {info.filename: info for info in original.infolist()}

    out = io.BytesIO()
    zipcrypto.rewrite_zip(io.BytesIO(make_zip()), out, new_password=b"Pw123456")

    with zipfile.ZipFile(io.BytesIO(out.getvalue())) as archive:
        after = {info.filename: info for info in archive.infolist()}
        assert set(after) == set(before)
        for name, info in after.items():
            source = before[name]
            assert info.compress_type == source.compress_type, (
                "压缩方式必须原样保留（store 不能变成 deflate，反之亦然）：%s" % name
            )
            assert info.date_time == source.date_time, "时间戳不该漂移：%s" % name
            assert info.is_dir() == source.is_dir()
            assert info.comment == source.comment
            assert info.external_attr == source.external_attr
        assert archive.comment == original.comment


def test_改密码后旧密码打不开新密码打得开() -> None:
    first = io.BytesIO()
    zipcrypto.rewrite_zip(io.BytesIO(make_zip()), first, new_password=b"OldPw123")

    second = io.BytesIO()
    zipcrypto.rewrite_zip(
        io.BytesIO(first.getvalue()), second, new_password=b"NewPw456", old_password=b"OldPw123"
    )

    with zipfile.ZipFile(io.BytesIO(second.getvalue())) as archive:
        assert archive.read("题面.txt", pwd=b"NewPw456") == EXPECTED["题面.txt"]
        assert_password_rejected(archive, "题面.txt", "OldPw123")


def test_源是_InfoZIP_打出来的包也能改密码() -> None:
    if not ZIP_BIN:
        pytest.skip("环境里没有 InfoZIP 的 zip")

    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        work = Path(raw)
        source = make_plain_zip_by_infozip(work, "oldpw123")
        out = io.BytesIO()
        zipcrypto.rewrite_zip(
            io.BytesIO(source.read_bytes()),
            out,
            new_password=b"newpw456",
            old_password=b"oldpw123",
        )

        with zipfile.ZipFile(io.BytesIO(out.getvalue())) as archive:
            assert archive.read("hello.txt", pwd=b"newpw456") == b"hi there\n"
            assert_password_rejected(archive, "hello.txt", "oldpw123")


def test_unzip_能用新密码解出我们写的包(tmp_path, unzip_with_password: str) -> None:
    """有 InfoZIP 的 unzip 时做一次真正的外部交叉验证。

    学生机器上是 Archive Manager（同样是 InfoZIP 传统加密这一套），所以
    "InfoZIP 的 unzip 能不能解开"比"Python 能不能解开"更接近那个使用现场。
    """
    source = make_plain_zip_by_infozip(tmp_path, "oldpw123")
    out = io.BytesIO()
    zipcrypto.rewrite_zip(
        io.BytesIO(source.read_bytes()),
        out,
        new_password=b"newpw456",
        old_password=b"oldpw123",
    )
    packed = tmp_path / "rewritten.zip"
    packed.write_bytes(out.getvalue())

    good = subprocess.run(
        [unzip_with_password, "-P", "newpw456", "-p", str(packed)],
        capture_output=True,
    )
    assert good.returncode == 0, good.stderr
    assert good.stdout == b"hi there\n"

    bad = subprocess.run(
        [unzip_with_password, "-P", "oldpw123", "-p", str(packed)],
        capture_output=True,
    )
    assert bad.returncode != 0, "旧密码不该还能解开"


def test_已加密的源不给旧密码会被拦住() -> None:
    encrypted = io.BytesIO()
    zipcrypto.rewrite_zip(io.BytesIO(make_zip()), encrypted, new_password=b"Pw123456")

    with pytest.raises(zipcrypto.PasswordRequiredError):
        zipcrypto.rewrite_zip(
            io.BytesIO(encrypted.getvalue()), io.BytesIO(), new_password=b"Another1"
        )


def test_旧密码不对会被拦住() -> None:
    encrypted = io.BytesIO()
    zipcrypto.rewrite_zip(io.BytesIO(make_zip()), encrypted, new_password=b"Pw123456")

    with pytest.raises(zipcrypto.BadPasswordError):
        zipcrypto.rewrite_zip(
            io.BytesIO(encrypted.getvalue()),
            io.BytesIO(),
            new_password=b"Another1",
            old_password=b"WrongPw1",
        )


def test_老版本_Python_用_zlib_报错也要当成密码错(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Python 3.8 的 zipfile **不校验加密头**：密码不对时它会先把解密出来的垃圾
    喂给 zlib，于是最先炸的是 ``zlib.error`` 而不是 ``RuntimeError``。目标机上跑的
    就是 3.8，不接住它的话"旧密码错"会变成 500 而不是 400。

    本机（3.14）走不到那条路，所以这里直接把 ``ZipFile.open`` 换成抛
    ``zlib.error`` —— 验的是我们的**错误映射**，而不是标准库当时的脾气。
    """
    encrypted = io.BytesIO()
    zipcrypto.rewrite_zip(io.BytesIO(make_zip()), encrypted, new_password=b"Pw123456")

    def fake_open(self, name, mode="r", pwd=None, **kwargs):
        raise zlib.error("Error -3 while decompressing data: invalid distance too far back")

    monkeypatch.setattr(zipfile.ZipFile, "open", fake_open)
    with pytest.raises(zipcrypto.BadPasswordError):
        zipcrypto.rewrite_zip(
            io.BytesIO(encrypted.getvalue()),
            io.BytesIO(),
            new_password=b"Another1",
            old_password=b"WrongPw1",
        )


def test_真的撞对了加密头校验字节的错密码也要归一成_BadPasswordError() -> None:
    """上一条是模拟；这一条找**真的**会越过加密头检查的错密码。

    ZipCrypto 的校验字节只有一个字节，任取一个错密码有 1/256 的概率撞对它；
    撞对之后zipfile 会继续解密，于是失败点变成 zlib.error（deflate 成员）或
    末尾的 CRC（store 成员）。这两条路也必须被翻译成 ``BadPasswordError``，
    否则"旧密码错"在服务端会变成 500 —— 而它是教师每天都会犯的错。
    """
    encrypted = io.BytesIO()
    zipcrypto.rewrite_zip(io.BytesIO(make_zip()), encrypted, new_password=b"RightPw1")
    data = encrypted.getvalue()

    slipped = None
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for index in range(4096):
            candidate = ("wrong-%d" % index).encode("utf-8")
            try:
                with archive.open("题面.txt", pwd=candidate) as handle:
                    handle.read()
            except RuntimeError:
                continue  # 没撞对校验字节 —— 标准库在加密头那一关就拒绝了
            except (zipfile.BadZipFile, zlib.error):
                slipped = candidate
                break
            else:  # pragma: no cover - 撞对校验字节又 CRC 全对，概率约 2^-32
                pytest.fail("一个错密码居然完整读出了成员")

    if slipped is None:  # pragma: no cover - 4096 次都撞不上的概率约 1e-7
        pytest.skip("4096 个错密码都没撞上 1 字节的校验字节")

    with pytest.raises(zipcrypto.BadPasswordError):
        zipcrypto.rewrite_zip(
            io.BytesIO(data),
            io.BytesIO(),
            new_password=b"Another1",
            old_password=slipped,
        )


def test_不是_zip_的字节会被认出() -> None:
    with pytest.raises(zipcrypto.NotAZipError):
        zipcrypto.probe_encrypted(io.BytesIO(b"this is not a zip"))

    with pytest.raises(zipcrypto.NotAZipError):
        zipcrypto.rewrite_zip(
            io.BytesIO(b"this is not a zip"), io.BytesIO(), new_password=b"Pw123456"
        )


def test_随机密码_长度字符集与两次不同() -> None:
    assert zipcrypto.PASSWORD_LENGTH == 12
    ambiguous = set("0O1lI")
    seen = set()
    for _ in range(200):
        password = zipcrypto.generate_password()
        assert len(password) == zipcrypto.PASSWORD_LENGTH
        assert set(password) <= set(zipcrypto.PASSWORD_ALPHABET)
        assert not (set(password) & ambiguous), "不许出现易混字符：%r" % password
        seen.add(password)
    assert len(seen) > 1, "两次生成不该是同一个密码"


def test_打包_非ASCII成员名与内容能带密码读回() -> None:
    """成员名走 UTF-8 + bit 11：教师上传的 `题面.pdf` 用默认 cp437 装不下。"""
    payload = bytes(range(256)) * 4
    out = io.BytesIO()
    zipcrypto.pack_member(
        io.BytesIO(payload), out, member_name="题面.pdf", password=b"Pw123456"
    )

    with zipfile.ZipFile(io.BytesIO(out.getvalue())) as archive:
        assert archive.namelist() == ["题面.pdf"]
        info = archive.infolist()[0]
        assert info.flag_bits & 0x1, "给了密码，成员就必须是加密的"
        assert info.flag_bits & 0x800, "非 ASCII 名字必须置上 UTF-8 标志位"
        assert info.file_size == len(payload)
        assert archive.read("题面.pdf", pwd=b"Pw123456") == payload
        with pytest.raises(RuntimeError):
            archive.read("题面.pdf")


def test_打包_0字节文件与不给密码写明文包() -> None:
    """0 字节也要是一个能读回来的空成员，不能是一个坏包。"""
    out = io.BytesIO()
    zipcrypto.pack_member(io.BytesIO(b""), out, member_name="空.bin")

    with zipfile.ZipFile(io.BytesIO(out.getvalue())) as archive:
        info = archive.infolist()[0]
        assert info.file_size == 0
        assert not (info.flag_bits & 0x1), "没给密码就该写明文包"
        assert archive.read("空.bin") == b""


def test_打包_压缩方式可以选_store() -> None:
    payload = bytes(range(256))
    out = io.BytesIO()
    zipcrypto.pack_member(
        io.BytesIO(payload),
        out,
        member_name="raw.bin",
        password=b"Pw123456",
        method=zipfile.ZIP_STORED,
    )

    with zipfile.ZipFile(io.BytesIO(out.getvalue())) as archive:
        assert archive.infolist()[0].compress_type == zipfile.ZIP_STORED
        assert archive.read("raw.bin", pwd=b"Pw123456") == payload


def test_unzip_能解开打包出来的包(tmp_path, unzip_with_password: str) -> None:
    """打包那条路也做一次外部交叉验证（成员名用 ASCII，避免环境差异）。"""
    payload = b"statement bytes\n"
    out = io.BytesIO()
    zipcrypto.pack_member(
        io.BytesIO(payload), out, member_name="statement.pdf", password=b"Pw123456"
    )
    packed = tmp_path / "packed-by-us.zip"
    packed.write_bytes(out.getvalue())

    good = subprocess.run(
        [unzip_with_password, "-P", "Pw123456", "-p", str(packed)],
        capture_output=True,
    )
    assert good.returncode == 0, good.stderr
    assert good.stdout == payload

    bad = subprocess.run(
        [unzip_with_password, "-P", "WrongPw1", "-p", str(packed)],
        capture_output=True,
    )
    assert bad.returncode != 0, "错密码不该打得开"


# --------------------------------------------------------------------------- #
# 2. 只读探测
# --------------------------------------------------------------------------- #


def test_GET_明文包是未加密(client: TestClient, admin_headers: dict, contest: dict) -> None:
    asset = upload(client, admin_headers, contest["id"], ZIP_NAME, make_zip())

    response = get_status(client, admin_headers, contest["id"], asset["id"])

    assert response.status_code == 200, response.text
    assert response.json() == {"encrypted": False, "filename": ZIP_NAME}


def test_GET_打完密码之后是已加密(
    client: TestClient, admin_headers: dict, contest: dict
) -> None:
    asset = upload(client, admin_headers, contest["id"], ZIP_NAME, make_zip())
    set_password(client, admin_headers, contest["id"], asset["id"], password="Pw123456")

    response = get_status(client, admin_headers, contest["id"], asset["id"])

    assert response.status_code == 200, response.text
    assert response.json()["encrypted"] is True


def test_GET_不是_zip_就是_400(client: TestClient, admin_headers: dict, contest: dict) -> None:
    """GET 仍然拒绝非 zip —— 但理由变了。

    老理由："不能让界面给非 zip 画上「密码」按钮"。新口径下非 zip **有**动作
    （打包成 zip），所以界面靠这个错误码把动作从「密码」切成「打包成 zip」；
    GET 继续回 400 是因为它回答的问题是"包里有没有密码"，对非 zip 没有答案。
    """
    asset = upload(
        client, admin_headers, contest["id"], ZIP_NAME, "明显不是 zip 的字节".encode("utf-8")
    )

    response = get_status(client, admin_headers, contest["id"], asset["id"])

    assert response.status_code == 400, response.text
    assert response.json()["code"] == "asset_not_zip"
    assert response.json()["detail"]


# --------------------------------------------------------------------------- #
# 3. 打密码：同一个 asset id 换内容，并自动写好 password.txt
# --------------------------------------------------------------------------- #


def test_打密码_同一个_asset_id_换_sha_并写好_password_txt(
    client: TestClient, admin_headers: dict, contest: dict
) -> None:
    asset = upload(client, admin_headers, contest["id"], ZIP_NAME, make_zip())
    before = stored(client, asset["id"])

    response = set_password(
        client, admin_headers, contest["id"], asset["id"], password="Pw123456"
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["asset"]["id"] == asset["id"], "打密码不是新建一条资产"
    assert body["asset"]["filename"] == ZIP_NAME, "内容变了，文件名不该跟着变"
    assert body["asset"]["sha256"] != before[0], "加密之后的字节就是新的那一份"
    assert body["password"] == "Pw123456"
    assert body["requeued"] == 0, "一个目标都没下发过，没有机器需要重排"
    assert body["packaged"] is False, "本来就是 zip，走的是改密码那条路"

    # 落盘的是真加密包：标准库带密码读得到原文、不带密码读不出来
    blob = blob_bytes(client, body["asset"]["sha256"])
    assert read_encrypted(blob, "Pw123456") == EXPECTED["题面.txt"]
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        with pytest.raises(RuntimeError):
            archive.read("题面.txt")

    # password.txt：就是一份普通文本资产，内容自解释
    password_asset = body["password_asset"]
    assert password_asset["filename"] == PASSWORD_FILE
    content = asset_text(client, admin_headers, contest["id"], password_asset["id"])
    assert "文件：%s\n" % ZIP_NAME in content, "要说清这是哪个包的密码"
    assert "密码：Pw123456\n" in content, "正文里写的就是这次生效的那个密码"
    assert "生成时间" in content
    assert listed_asset(
        client, admin_headers, contest["id"], password_asset["id"]
    )["editable"] is True

    # 它走的是正常下发流程：教师可以直接把它发给机器
    deploy = deploy_to(
        client,
        admin_headers,
        contest["id"],
        password_asset["id"],
        [add_player(client, admin_headers, contest["id"], "S100")["id"]],
    )
    assert deploy["total"] == 1


def test_打密码_不改动别的资产(
    client: TestClient, admin_headers: dict, contest: dict
) -> None:
    other = upload(client, admin_headers, contest["id"], "样例.zip", make_zip())
    other_before = stored(client, other["id"])
    other_blob = blob_bytes(client, other_before[0])
    asset = upload(client, admin_headers, contest["id"], ZIP_NAME, make_zip())

    set_password(client, admin_headers, contest["id"], asset["id"], password="Pw123456")

    assert stored(client, other["id"]) == other_before
    assert blob_bytes(client, other_before[0]) == other_blob


def test_既没给密码也没让生成_是_400(
    client: TestClient, admin_headers: dict, contest: dict
) -> None:
    asset = upload(client, admin_headers, contest["id"], ZIP_NAME, make_zip())
    before = stored(client, asset["id"])

    response = set_password(client, admin_headers, contest["id"], asset["id"])

    assert response.status_code == 400, response.text
    assert response.json()["code"] == "zip_password_missing"
    assert stored(client, asset["id"]) == before


def test_POST_非_zip_会被打包成_zip_而不是_400(
    client: TestClient, admin_headers: dict, contest: dict
) -> None:
    """**口径变了的那一条**：非 zip 原来是 ``400 asset_not_zip``，现在就地打包。

    老口径把"不是 zip"当成"这个动作只对 zip 有意义"；新需求要它对**任意**资产都
    有意义 —— 非 zip 就先包成 zip，再套密码。所以这里不再拒绝，而是同一个 asset
    id 换内容、文件名换成 ``<原基名>.zip``，zip 里的成员名**仍是原来的文件名**。
    """
    raw = "明显不是 zip 的字节".encode("utf-8")
    asset = upload(client, admin_headers, contest["id"], "题面.pdf", raw)
    before = stored(client, asset["id"])

    response = set_password(
        client, admin_headers, contest["id"], asset["id"], password="Pw123456"
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["packaged"] is True, "回执必须能区分这次是打包还是改密码"
    assert body["asset"]["id"] == asset["id"], "就地替换，不是新建一条"
    assert body["asset"]["filename"] == "题面.zip"
    assert body["asset"]["sha256"] != before[0]
    assert body["asset"]["size"] != before[1]

    blob = blob_bytes(client, body["asset"]["sha256"])
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        assert archive.namelist() == ["题面.pdf"], "zip 里那个成员仍用原来的文件名"
        assert archive.read("题面.pdf", pwd=b"Pw123456") == raw

    content = asset_text(
        client, admin_headers, contest["id"], body["password_asset"]["id"]
    )
    lines = password_lines(content)
    assert lines["文件"] == "题面.zip", "password.txt 说的是改名之后的那一份"
    assert lines["密码"] == "Pw123456"


def test_打包_不改动别的资产(
    client: TestClient, admin_headers: dict, contest: dict
) -> None:
    other = upload(client, admin_headers, contest["id"], "样例.pdf", b"other pdf")
    other_before = stored(client, other["id"])
    other_blob = blob_bytes(client, other_before[0])
    asset = upload(client, admin_headers, contest["id"], "题面.pdf", b"pdf body")

    set_password(client, admin_headers, contest["id"], asset["id"], password="Pw123456")

    assert stored(client, other["id"]) == other_before
    assert blob_bytes(client, other_before[0]) == other_blob


def test_打包_没有扩展名的文件名补上_zip(
    client: TestClient, admin_headers: dict, contest: dict
) -> None:
    asset = upload(client, admin_headers, contest["id"], "题面", b"pdf body")

    body = set_password(
        client, admin_headers, contest["id"], asset["id"], password="Pw123456"
    ).json()

    assert body["packaged"] is True
    assert body["asset"]["filename"] == "题面.zip"
    with zipfile.ZipFile(io.BytesIO(blob_bytes(client, body["asset"]["sha256"]))) as archive:
        assert archive.namelist() == ["题面"]


def test_打包后目标被重排且偏移清零(
    client: TestClient,
    admin_headers: dict,
    contest: dict,
    player: dict,
    enrolled: dict,
) -> None:
    """打包与「在线改正文」是同一套语义：done → pending、偏移清零、下一轮带新 sha。"""
    asset = upload(client, admin_headers, contest["id"], "题面.pdf", b"pdf body")
    deploy_to(client, admin_headers, contest["id"], asset["id"], [player["id"]])
    do_tick(
        client,
        enrolled["token"],
        [],
        machine_id=enrolled["machine_id"],
        completed_assets=[asset["id"]],
    )
    assert target_states(client, asset["id"]) == [(DeployStatus.DONE, 0)]
    pin_bytes_done(client, asset["id"], PINNED_OFFSET)

    body = set_password(
        client, admin_headers, contest["id"], asset["id"], password="Pw123456"
    ).json()

    assert body["requeued"] == 1
    assert target_states(client, asset["id"]) == [(DeployStatus.PENDING, 0)]

    jobs = do_tick(client, enrolled["token"], [], machine_id=enrolled["machine_id"])[
        "deploy_jobs"
    ]
    job = next(item for item in jobs if item["asset_id"] == asset["id"])
    assert job["sha256"] == body["asset"]["sha256"]
    assert job["size"] == body["asset"]["size"]
    assert job["offset"] == 0


def test_打包的审计写清了动作与改名且不含密码(
    client: TestClient, admin_headers: dict, contest: dict
) -> None:
    asset = upload(client, admin_headers, contest["id"], "题面.pdf", b"pdf body")

    set_password(client, admin_headers, contest["id"], asset["id"], password="Secret1Pw")

    events = audit_events(client)
    assert len(events) == 1
    message = events[0]["message"]
    assert "打包" in message, "审计要能看出这次是打包，不是改密码"
    assert "题面.pdf" in message
    assert "题面.zip" in message
    assert "Secret1Pw" not in message
    assert PASSWORD_FILE in message


def test_已经是加密_zip_的资产走改密码那条路(
    client: TestClient, admin_headers: dict, contest: dict
) -> None:
    """扩展名不是 .zip、内容却是加密 zip：判据是**内容**，所以走改密码，不打包。"""
    encrypted = io.BytesIO()
    zipcrypto.rewrite_zip(io.BytesIO(make_zip()), encrypted, new_password=b"OldPw123")
    asset = upload(client, admin_headers, contest["id"], "题面.pdf", encrypted.getvalue())

    status = get_status(client, admin_headers, contest["id"], asset["id"])
    assert status.json()["encrypted"] is True

    before = stored(client, asset["id"])
    refused = set_password(
        client, admin_headers, contest["id"], asset["id"], password="NewPw456"
    )
    assert refused.status_code == 400, refused.text
    assert refused.json()["code"] == "zip_password_required"
    assert stored(client, asset["id"]) == before

    body = set_password(
        client,
        admin_headers,
        contest["id"],
        asset["id"],
        password="NewPw456",
        old_password="OldPw123",
    ).json()
    assert body["packaged"] is False, "本来就是 zip，走的是改密码"
    assert body["asset"]["filename"] == "题面.pdf", "改密码不改名"
    blob = blob_bytes(client, body["asset"]["sha256"])
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        assert archive.read("题面.txt", pwd=b"NewPw456") == EXPECTED["题面.txt"]
        assert_password_rejected(archive, "题面.txt", "OldPw123")


# --------------------------------------------------------------------------- #
# 3b. 上传时就打包：磁盘上直接只有 zip，没有原始字节
# --------------------------------------------------------------------------- #


def test_上传时打包_密码写进_password_txt且磁盘上没有原始字节(
    client: TestClient, admin_headers: dict, contest: dict
) -> None:
    raw = "第一版题面\n".encode("utf-8")
    asset = upload_packaged(
        client, admin_headers, contest["id"], "题面.pdf", raw, zip_password="UploadPw1"
    )

    assert asset["filename"] == "题面.zip", "落库的文件名就是打包后的那个"
    assert asset["sha256"] != hashlib.sha256(raw).hexdigest()
    # "不是先把原文件存下来再改"：原始字节那份 blob 根本不存在
    assert not blob_exists(client, hashlib.sha256(raw).hexdigest())

    blob = blob_bytes(client, asset["sha256"])
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        assert archive.namelist() == ["题面.pdf"]
        assert archive.read("题面.pdf", pwd=b"UploadPw1") == raw

    password_id = password_asset_id_of(client, admin_headers, contest["id"])
    lines = password_lines(asset_text(client, admin_headers, contest["id"], password_id))
    assert lines["文件"] == "题面.zip"
    assert lines["密码"] == "UploadPw1"

    events = audit_events(client)
    assert len(events) == 1
    assert "打包" in events[0]["message"]
    assert "UploadPw1" not in events[0]["message"]


def test_上传时打包_不填密码就用服务端生成的随机密码(
    client: TestClient, admin_headers: dict, contest: dict
) -> None:
    raw = b"pdf bytes"
    asset = upload_packaged(client, admin_headers, contest["id"], "题面.pdf", raw)

    assert asset["filename"] == "题面.zip"
    password_id = password_asset_id_of(client, admin_headers, contest["id"])
    password = password_lines(
        asset_text(client, admin_headers, contest["id"], password_id)
    )["密码"]
    assert len(password) == zipcrypto.PASSWORD_LENGTH
    assert set(password) <= set(zipcrypto.PASSWORD_ALPHABET)
    assert not (set(password) & set("0O1lI"))

    blob = blob_bytes(client, asset["sha256"])
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        assert archive.read("题面.pdf", pwd=password.encode("utf-8")) == raw


def test_上传时不打包就还是原样落盘(
    client: TestClient, admin_headers: dict, contest: dict
) -> None:
    """``package_zip`` 默认 false：老行为一个字节都不变（原样落盘、不写密码文件）。"""
    raw = b"pdf bytes"
    asset = upload(client, admin_headers, contest["id"], "题面.pdf", raw)

    assert asset["filename"] == "题面.pdf"
    assert asset["sha256"] == hashlib.sha256(raw).hexdigest()
    assert blob_bytes(client, asset["sha256"]) == raw
    assert count_assets_named(client, contest["id"], PASSWORD_FILE) == 0


# --------------------------------------------------------------------------- #
# 4. 改密码：必须给旧密码，错了就一个字节都不能动
# --------------------------------------------------------------------------- #


def test_已加密但不给旧密码_400_且没有任何改动(
    client: TestClient, admin_headers: dict, contest: dict
) -> None:
    asset = upload(client, admin_headers, contest["id"], ZIP_NAME, make_zip())
    first = set_password(
        client, admin_headers, contest["id"], asset["id"], password="GoodPw123"
    ).json()
    before_blob = blob_bytes(client, first["asset"]["sha256"])
    before_row = stored(client, asset["id"])
    before_file = asset_text(client, admin_headers, contest["id"], first["password_asset"]["id"])

    response = set_password(
        client, admin_headers, contest["id"], asset["id"], password="Another123"
    )

    assert response.status_code == 400, response.text
    assert response.json()["code"] == "zip_password_required"
    assert "已经有密码" in response.json()["detail"]
    assert stored(client, asset["id"]) == before_row
    assert blob_bytes(client, first["asset"]["sha256"]) == before_blob
    assert (
        asset_text(client, admin_headers, contest["id"], first["password_asset"]["id"])
        == before_file
    )


def test_旧密码错_400_且资产一个字节都没动(
    client: TestClient, admin_headers: dict, contest: dict
) -> None:
    asset = upload(client, admin_headers, contest["id"], ZIP_NAME, make_zip())
    first = set_password(
        client, admin_headers, contest["id"], asset["id"], password="GoodPw123"
    ).json()
    sha = first["asset"]["sha256"]
    blob = blob_bytes(client, sha)
    before_row = stored(client, asset["id"])
    before_file = asset_text(client, admin_headers, contest["id"], first["password_asset"]["id"])

    response = set_password(
        client,
        admin_headers,
        contest["id"],
        asset["id"],
        password="Attacker123",
        old_password="WrongPw1",
    )

    assert response.status_code == 400, response.text
    assert response.json()["code"] == "zip_password_wrong"
    # 库上的那一行、blob、以及 password.txt 全都没动
    assert stored(client, asset["id"]) == before_row
    assert blob_bytes(client, sha) == blob
    assert (
        asset_text(client, admin_headers, contest["id"], first["password_asset"]["id"])
        == before_file
    )
    # 原来的密码照样打得开 —— 这才是"没动"的最终证据
    assert read_encrypted(blob, "GoodPw123") == EXPECTED["题面.txt"]


def test_改密码_旧密码打不开新密码打得开(
    client: TestClient, admin_headers: dict, contest: dict
) -> None:
    asset = upload(client, admin_headers, contest["id"], ZIP_NAME, make_zip())
    set_password(client, admin_headers, contest["id"], asset["id"], password="FirstPw1")

    response = set_password(
        client,
        admin_headers,
        contest["id"],
        asset["id"],
        password="SecondPw2",
        old_password="FirstPw1",
    )

    assert response.status_code == 200, response.text
    blob = blob_bytes(client, response.json()["asset"]["sha256"])
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        assert archive.read("题面.txt", pwd=b"SecondPw2") == EXPECTED["题面.txt"]
        assert_password_rejected(archive, "题面.txt", "FirstPw1")


def test_明文包上传的旧密码被忽略(
    client: TestClient, admin_headers: dict, contest: dict
) -> None:
    """明文包不需要旧密码。教师顺手填了一个不该让请求变成 400。"""
    asset = upload(client, admin_headers, contest["id"], ZIP_NAME, make_zip())

    response = set_password(
        client,
        admin_headers,
        contest["id"],
        asset["id"],
        password="Pw123456",
        old_password="随手填的",
    )

    assert response.status_code == 200, response.text
    assert read_encrypted(
        blob_bytes(client, response.json()["asset"]["sha256"]), "Pw123456"
    ) == EXPECTED["题面.txt"]


# --------------------------------------------------------------------------- #
# 5. 重排语义：与「在线改正文」完全一致
# --------------------------------------------------------------------------- #


def test_已经下发的目标被重排且偏移清零(
    client: TestClient,
    admin_headers: dict,
    contest: dict,
    player: dict,
    enrolled: dict,
) -> None:
    asset = upload(client, admin_headers, contest["id"], ZIP_NAME, make_zip())
    deploy_to(client, admin_headers, contest["id"], asset["id"], [player["id"]])

    # 走**真实的** Agent 上报路径把这一条目标标成 done，而不是往库里塞状态
    do_tick(
        client,
        enrolled["token"],
        [],
        machine_id=enrolled["machine_id"],
        completed_assets=[asset["id"]],
    )
    assert target_states(client, asset["id"]) == [(DeployStatus.DONE, 0)]
    pin_bytes_done(client, asset["id"], PINNED_OFFSET)

    body = set_password(
        client, admin_headers, contest["id"], asset["id"], password="Pw123456"
    ).json()

    assert body["requeued"] == 1
    assert target_states(client, asset["id"]) == [(DeployStatus.PENDING, 0)]

    # 下一轮 tick 必须真的把这个作业发出去，而且带着**新**的 sha256
    jobs = do_tick(client, enrolled["token"], [], machine_id=enrolled["machine_id"])[
        "deploy_jobs"
    ]
    job = next(item for item in jobs if item["asset_id"] == asset["id"])
    assert job["sha256"] == body["asset"]["sha256"]
    assert job["size"] == body["asset"]["size"]
    assert job["offset"] == 0


def test_还没落地的目标不被重排也不被清零(
    client: TestClient,
    admin_headers: dict,
    contest: dict,
    player: dict,
    enrolled: dict,
) -> None:
    other = add_player(client, admin_headers, contest["id"], "S002")
    asset = upload(client, admin_headers, contest["id"], ZIP_NAME, make_zip())
    deploy_to(
        client, admin_headers, contest["id"], asset["id"], [player["id"], other["id"]]
    )
    do_tick(
        client,
        enrolled["token"],
        [],
        machine_id=enrolled["machine_id"],
        completed_assets=[asset["id"]],
    )
    pin_bytes_done(client, asset["id"], PINNED_OFFSET)
    before = target_states(client, asset["id"])
    assert sorted(status for status, _ in before) == [DeployStatus.DONE, DeployStatus.PENDING]

    body = set_password(
        client, admin_headers, contest["id"], asset["id"], password="Pw123456"
    ).json()

    assert body["requeued"] == 1, "只有一条 done，重排数就该是 1"
    after = target_states(client, asset["id"])
    assert sorted(status for status, _ in after) == [DeployStatus.PENDING, DeployStatus.PENDING]
    assert sorted(offset for _, offset in after) == [0, PINNED_OFFSET], (
        "还没落地那条的偏移不该被动过"
    )


def test_没有下发过的资产_requeued_是_0(
    client: TestClient, admin_headers: dict, contest: dict
) -> None:
    asset = upload(client, admin_headers, contest["id"], ZIP_NAME, make_zip())

    body = set_password(
        client, admin_headers, contest["id"], asset["id"], password="Pw123456"
    ).json()

    assert body["requeued"] == 0


def test_password_txt_自己被更新时也会重排(
    client: TestClient,
    admin_headers: dict,
    contest: dict,
    player: dict,
    enrolled: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """以前收过旧密码的机器，必须能拿到新的那一份 password.txt。

    这条如果不成立，改完密码的现场就是"包换了新密码，机器上躺着的还是旧密码" ——
    学生照着旧密码打不开，而界面上一切正常。
    """
    monkeypatch.setattr(admin_module, "_password_created_at", lambda: "2026-10-06 13:20")
    asset = upload(client, admin_headers, contest["id"], ZIP_NAME, make_zip())
    first = set_password(
        client, admin_headers, contest["id"], asset["id"], password="FirstPw1"
    ).json()
    password_id = first["password_asset"]["id"]
    deploy_to(client, admin_headers, contest["id"], password_id, [player["id"]])
    do_tick(
        client,
        enrolled["token"],
        [],
        machine_id=enrolled["machine_id"],
        completed_assets=[password_id],
    )
    assert target_states(client, password_id) == [(DeployStatus.DONE, 0)]

    set_password(
        client,
        admin_headers,
        contest["id"],
        asset["id"],
        password="SecondPw2",
        old_password="FirstPw1",
    )

    assert target_states(client, password_id) == [(DeployStatus.PENDING, 0)]


# --------------------------------------------------------------------------- #
# 6. password.txt：同名同内容复用 / 同名不同内容更新同一条
# --------------------------------------------------------------------------- #


def test_password_txt_同名同内容复用同一条(
    client: TestClient,
    admin_headers: dict,
    contest: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """生成时间被钉住，两次调用的正文才是逐字节一致的（否则这条测试是在赌没跨分钟）。"""
    monkeypatch.setattr(admin_module, "_password_created_at", lambda: "2026-10-06 13:20")
    asset = upload(client, admin_headers, contest["id"], ZIP_NAME, make_zip())

    first = set_password(
        client, admin_headers, contest["id"], asset["id"], password="SamePw12"
    ).json()
    second = set_password(
        client,
        admin_headers,
        contest["id"],
        asset["id"],
        password="SamePw12",
        old_password="SamePw12",
    ).json()

    assert second["password_asset"]["id"] == first["password_asset"]["id"]
    assert second["password_asset"]["sha256"] == first["password_asset"]["sha256"]
    assert count_assets_named(client, contest["id"], PASSWORD_FILE) == 1


def test_password_txt_同名不同内容更新同一条(
    client: TestClient,
    admin_headers: dict,
    contest: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """改密码不新增一条同名资产：列表上出现多条 password.txt 会让人发错那一份。"""
    monkeypatch.setattr(admin_module, "_password_created_at", lambda: "2026-10-06 13:20")
    asset = upload(client, admin_headers, contest["id"], ZIP_NAME, make_zip())

    first = set_password(
        client, admin_headers, contest["id"], asset["id"], password="FirstPw1"
    ).json()
    password_id = first["password_asset"]["id"]
    old_sha = first["password_asset"]["sha256"]

    second = set_password(
        client,
        admin_headers,
        contest["id"],
        asset["id"],
        password="SecondPw2",
        old_password="FirstPw1",
    ).json()

    assert second["password_asset"]["id"] == password_id, "原地更新，不是新建"
    assert second["password_asset"]["sha256"] != old_sha
    assert count_assets_named(client, contest["id"], PASSWORD_FILE) == 1
    content = asset_text(client, admin_headers, contest["id"], password_id)
    assert "密码：SecondPw2\n" in content
    assert "FirstPw1" not in content


# --------------------------------------------------------------------------- #
# 7. 随机密码（走接口）
# --------------------------------------------------------------------------- #


def test_服务端生成的随机密码符合字符集且与上一次不同(
    client: TestClient, admin_headers: dict, contest: dict
) -> None:
    asset = upload(client, admin_headers, contest["id"], ZIP_NAME, make_zip())

    first = set_password(client, admin_headers, contest["id"], asset["id"], generate=True).json()
    password = first["password"]
    assert len(password) == zipcrypto.PASSWORD_LENGTH
    assert set(password) <= set(zipcrypto.PASSWORD_ALPHABET)
    assert not (set(password) & set("0O1lI"))
    assert read_encrypted(blob_bytes(client, first["asset"]["sha256"]), password) == (
        EXPECTED["题面.txt"]
    )

    second = set_password(
        client,
        admin_headers,
        contest["id"],
        asset["id"],
        generate=True,
        old_password=password,
    ).json()
    assert second["password"] != password
    assert "密码：%s\n" % second["password"] in asset_text(
        client, admin_headers, contest["id"], second["password_asset"]["id"]
    )


# --------------------------------------------------------------------------- #
# 8. 越权、审计与"密码不另外存"
# --------------------------------------------------------------------------- #


def test_别的场次的资产是_404(client: TestClient, admin_headers: dict, contest: dict) -> None:
    asset = upload(client, admin_headers, contest["id"], ZIP_NAME, make_zip())
    before = stored(client, asset["id"])
    other = client.post(
        "/api/v1/admin/contests",
        json={"name": "另一场", "slug": "other-zip", "status": "running"},
        headers=admin_headers,
    ).json()

    assert get_status(client, admin_headers, other["id"], asset["id"]).status_code == 404
    assert (
        set_password(
            client, admin_headers, other["id"], asset["id"], password="Pw123456"
        ).status_code
        == 404
    )
    assert stored(client, asset["id"]) == before

    # 压根不存在的 id 也是 404
    assert get_status(client, admin_headers, contest["id"], 999999).status_code == 404
    assert (
        set_password(
            client, admin_headers, contest["id"], 999999, password="Pw123456"
        ).status_code
        == 404
    )


def test_审计事件写清了场次文件与前后版本且不含密码(
    client: TestClient, admin_headers: dict, contest: dict
) -> None:
    asset = upload(client, admin_headers, contest["id"], ZIP_NAME, make_zip())
    old_sha = stored(client, asset["id"])[0]

    body = set_password(
        client, admin_headers, contest["id"], asset["id"], password="Secret1Pw"
    ).json()

    events = audit_events(client)
    assert len(events) == 1
    event = events[0]
    assert event["level"] == "info"
    assert event["category"] == "asset_zip_password"
    assert event["contest_id"] == contest["id"]
    assert ZIP_NAME in event["message"]
    assert old_sha[:8] in event["message"]
    assert body["asset"]["sha256"][:8] in event["message"]
    assert "重新排队 0 台机器" in event["message"]
    # 审计里出现密码 = 服务端又存了一份明文，这正是设计上不允许的
    assert "Secret1Pw" not in event["message"]
    assert PASSWORD_FILE in event["message"]


def test_密码只活在_password_txt_里_能读回来(
    client: TestClient, admin_headers: dict, contest: dict
) -> None:
    """「丢了也能找回」的依据：password.txt 是普通文本资产，读它就行。"""
    asset = upload(client, admin_headers, contest["id"], ZIP_NAME, make_zip())
    body = set_password(
        client, admin_headers, contest["id"], asset["id"], password="Recover1"
    ).json()

    content = asset_text(
        client, admin_headers, contest["id"], body["password_asset"]["id"]
    )
    lines = dict(
        line.split("：", 1) for line in content.strip().splitlines() if "：" in line
    )
    assert lines["文件"] == ZIP_NAME
    assert lines["密码"] == "Recover1"


# --------------------------------------------------------------------------- #
# 9. password.txt 白名单：服务端拒绝 + 界面不给入口
#
# `password.txt` 是服务端生成的单件（正文由 `_upsert_password_asset` 维护）。把它
# 打成 `password.zip` 会顺手再生成一份新的 `password.txt` —— 这个文件能自己滚下去
# （password.zip → 新的 password.txt → 再打包 → …）。
#
# **判据在服务端**：界面那道守卫只管"从列表点"这条常规路径，而接口对任何人开放
# （curl、上传时勾了打包）。所以主体是接口级断言；界面那一条保留为纵深防御。
# --------------------------------------------------------------------------- #

DEPLOY_VIEW = (
    Path(__file__).resolve().parents[2] / "web" / "src" / "views" / "DeploysView.vue"
)


def _deploy_view_source() -> str:
    if not DEPLOY_VIEW.is_file():  # pragma: no cover - 前端被挪走时不该静默通过
        pytest.skip("找不到 %s" % DEPLOY_VIEW)
    return DEPLOY_VIEW.read_text(encoding="utf-8")


def test_给_password_txt_打密码直接被拒(
    client: TestClient, admin_headers: dict, contest: dict
) -> None:
    """打接口拿 400，而且**资产一字节没动**、场上仍然只有一份 ``password.txt``。

    这条以前是"读 .vue 源码对账"—— 那只能证明界面上没有那个按钮，证明不了绕过
    界面调接口会怎样。现在服务端自己拒了（码是 ``asset_is_password_file``）。
    """
    # 先造一份正常的 zip，好让场上有一份 password.txt
    asset = upload(client, admin_headers, contest["id"], ZIP_NAME, make_zip())
    set_password(client, admin_headers, contest["id"], asset["id"], password="First123")

    password_id = password_asset_id_of(client, admin_headers, contest["id"])
    before = listed_asset(client, admin_headers, contest["id"], password_id)
    before_sha = before["sha256"]
    before_text = asset_text(client, admin_headers, contest["id"], password_id)
    assert count_assets_named(client, contest["id"], PASSWORD_FILE) == 1

    response = set_password(
        client, admin_headers, contest["id"], password_id, password="Another1"
    )

    assert response.status_code == 400, response.text
    assert response.json()["code"] == "asset_is_password_file"

    after = listed_asset(client, admin_headers, contest["id"], password_id)
    assert after["sha256"] == before_sha, "被拒的请求不该改动资产内容"
    assert after["filename"] == PASSWORD_FILE, "被拒的请求不该改文件名"
    assert asset_text(client, admin_headers, contest["id"], password_id) == before_text
    assert count_assets_named(client, contest["id"], PASSWORD_FILE) == 1, "多出了一份"
    assert count_assets_named(client, contest["id"], "password.zip") == 0


def test_上传一个叫_password_txt_的文件并要求打包也被拒(
    client: TestClient, admin_headers: dict, contest: dict
) -> None:
    """"上传时勾了打包"这条路界面管不到 —— 它也必须被服务端拒，且**不落库**。

    上传一个 ``password.txt``（哪怕内容不是密码文件）并要求 ``package_zip``：
    若放行，服务端会打出一个 ``password.zip``，并**再生成一份新的** ``password.txt``
    —— 正是那个能自己滚下去的循环。
    """
    response = client.post(
        ASSETS_URL % contest["id"],
        files={"file": (PASSWORD_FILE, b"not really a password file", "text/plain")},
        data={"kind": "须知", "package_zip": "true"},
        headers=admin_headers,
    )

    assert response.status_code == 400, response.text
    assert response.json()["code"] == "asset_is_password_file"

    # 一字节都没落：没有 password.zip、也没有新的 password.txt
    assert count_assets_named(client, contest["id"], "password.zip") == 0
    assert count_assets_named(client, contest["id"], PASSWORD_FILE) == 0


def test_界面仍然不给_password_txt_打包入口() -> None:
    """**纵深防御**：服务端拒了，界面也不该摆一个点了必然报错的按钮。

    两条一起才有意义：界面那条让教师不会撞上 400，服务端那条让绕过界面的人也
    打不出这个包。
    """
    source = _deploy_view_source()

    # 1) 名字就是服务端那个常量名（同一个字符串，不另起别名）
    assert re.search(r"const PASSWORD_FILENAME = ['\"]password\.txt['\"]", source), (
        "DeploysView 里必须有 password.txt 这个常量（服务端 PASSWORD_FILENAME）"
    )

    # 2) 守卫函数真的拿它做排除，不是恒真的壳
    matched = re.search(r"function canZipAction\b[^{]*\{(.*?)\n\}", source, re.S)
    assert matched, "找不到 canZipAction()：打包动作的入口守卫被删了"
    assert "PASSWORD_FILENAME" in matched.group(1), (
        "canZipAction 必须按 PASSWORD_FILENAME 排除"
    )

    # 3) 打开对话框的入口只有一处，而且被这个守卫包着
    assert source.count("openZipPasswordDialog(row)") == 1, (
        "openZipPasswordDialog 的调用点不止一处：绕过 canZipAction 的那一个会漏出去"
    )
    assert 'v-if="canZipAction(row)"' in source, (
        "开放那个动作的按钮必须由 canZipAction 把关"
    )
