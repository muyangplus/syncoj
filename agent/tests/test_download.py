"""下发/断点续传测试。

断续续传的价值全在边界情况上：网络中途断掉、服务端不支持 Range、分片其实是
上一次崩溃留下的坏数据、目标路径想穿越到 /etc。这些路径平常跑不到，一旦跑错
就是"评测器读到半截测试数据"这种极难排查的故障。
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path
from typing import List, Optional

from conftest import make_tree
import time

from syncoj_agent.download import (
    PART_SUFFIX,
    PARTS_DIRNAME,
    download_asset,
    part_path_for,
    part_size,
)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FakeResponse:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            chunk = self._data[self._pos :]
            self._pos = len(self._data)
            return chunk
        chunk = self._data[self._pos : self._pos + size]
        self._pos += len(chunk)
        return chunk


class FakeClient:
    """模拟服务端的分片下载接口。"""

    def __init__(self, content: bytes, ignore_range: bool = False, fail_after: Optional[int] = None):
        self.content = content
        self.ignore_range = ignore_range
        self.fail_after = fail_after
        self.requests: List[int] = []
        self.dropped = 0

    def open_download(self, asset_id: int, offset: int = 0):
        self.requests.append(offset)

        if self.ignore_range:
            # 服务端忽略 Range，总是返回完整内容
            return 200, {"Content-Length": str(len(self.content))}, FakeResponse(self.content)

        data = self.content[offset:]
        if self.fail_after is not None and len(data) > self.fail_after:
            # 模拟传到一半连接断了
            data = data[: self.fail_after]

        if offset == 0:
            return 200, {"Content-Length": str(len(data))}, FakeResponse(data)
        return 206, {"Content-Range": "bytes %d-/%d" % (offset, len(self.content))}, FakeResponse(data)

    def drop_transfer_connection(self) -> None:
        self.dropped += 1

    def close(self) -> None:
        pass


def seed_part(deploy_root: Path, digest: str, data: bytes) -> Path:
    """在本机分片目录里放一份"上次下到一半"的分片。"""
    part = part_path_for(deploy_root, digest)
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(data)
    return part


def make_job(content: bytes, dest: str = "exam/data.zip", **overrides) -> dict:
    job = {
        "asset_id": 7,
        "url": "/api/v1/agent/assets/7",
        "sha256": sha(content),
        "size": len(content),
        "dest": dest,
        "target_id": 70,
        "mode": "overwrite",
    }
    job.update(overrides)
    return job


# --------------------------------------------------------------------------- #
# 正常路径
# --------------------------------------------------------------------------- #


def test_fresh_download_places_file(workdir: Path) -> None:
    content = b"test data " * 1000
    deploy_root = workdir / "deploy"
    client = FakeClient(content)

    outcome = download_asset(client, make_job(content), deploy_root)

    assert outcome.status == "ok", outcome.error
    assert outcome.bytes_written == len(content)
    assert (deploy_root / "exam" / "data.zip").read_bytes() == content


def test_no_part_file_left_behind(workdir: Path) -> None:
    content = b"abc" * 100
    deploy_root = workdir / "deploy"
    outcome = download_asset(FakeClient(content), make_job(content), deploy_root)

    assert outcome.status == "ok"
    leftovers = list(deploy_root.rglob("*" + PART_SUFFIX))
    assert leftovers == [], "成功后不该留下分片文件"


def test_identical_existing_file_is_skipped(workdir: Path) -> None:
    content = b"already here" * 100
    deploy_root = workdir / "deploy"
    make_tree(deploy_root, {"exam/data.zip": content})
    client = FakeClient(content)

    outcome = download_asset(client, make_job(content), deploy_root)

    assert outcome.status == "skipped"
    assert client.requests == [], "内容已一致就不该发起下载"


def test_skip_exist_mode_keeps_different_local_file(workdir: Path) -> None:
    """skip_exist 的语义是"存在就不动"，即使内容不同。"""
    remote = b"remote version"
    local = b"local edited version"
    deploy_root = workdir / "deploy"
    make_tree(deploy_root, {"exam/data.zip": local})

    outcome = download_asset(
        FakeClient(remote), make_job(remote, mode="skip_exist"), deploy_root
    )

    assert outcome.status == "skipped"
    assert (deploy_root / "exam" / "data.zip").read_bytes() == local


def test_overwrite_mode_replaces_different_local_file(workdir: Path) -> None:
    remote = b"remote version"
    local = b"local edited version"
    deploy_root = workdir / "deploy"
    make_tree(deploy_root, {"exam/data.zip": local})

    outcome = download_asset(FakeClient(remote), make_job(remote), deploy_root)

    assert outcome.status == "ok"
    assert (deploy_root / "exam" / "data.zip").read_bytes() == remote


# --------------------------------------------------------------------------- #
# 断点续传
# --------------------------------------------------------------------------- #


def test_resumes_from_local_partial(workdir: Path) -> None:
    content = b"0123456789" * 5000
    deploy_root = workdir / "deploy"
    dest = deploy_root / "exam" / "data.zip"
    dest.parent.mkdir(parents=True)

    already = 20000
    seed_part(deploy_root, sha(content), content[:already])

    client = FakeClient(content)
    outcome = download_asset(client, make_job(content), deploy_root)

    assert outcome.status == "ok", outcome.error
    assert client.requests == [already], "应当从本地已有字节数继续，而不是从头下载"
    assert dest.read_bytes() == content


def test_resumes_from_local_part_regardless_of_job_fields(workdir: Path) -> None:
    """本地磁盘才是事实：作业里即使带着别的字段（老服务端的 offset），也只看本地分片。"""
    content = b"X" * 50000
    deploy_root = workdir / "deploy"
    dest = deploy_root / "exam" / "data.zip"
    dest.parent.mkdir(parents=True)
    local_have = 30000
    seed_part(deploy_root, sha(content), content[:local_have])

    client = FakeClient(content)
    # 服务端以为我们只有 1000 字节
    outcome = download_asset(client, make_job(content, offset=1000), deploy_root)

    assert outcome.status == "ok"
    assert client.requests == [local_have]


def test_restarts_when_server_ignores_range(workdir: Path) -> None:
    """代理剥掉 Range 头是常见现象，必须能退回整包下载而不是写坏文件。"""
    content = b"Y" * 20000
    deploy_root = workdir / "deploy"
    dest = deploy_root / "exam" / "data.zip"
    dest.parent.mkdir(parents=True)
    seed_part(deploy_root, sha(content), content[:5000])

    client = FakeClient(content, ignore_range=True)
    outcome = download_asset(client, make_job(content), deploy_root)

    assert outcome.status == "ok", outcome.error
    assert client.requests[0] == 5000, "第一次应当尝试续传"
    assert 0 in client.requests[1:], "发现服务端不支持 Range 后应从头重下"
    assert dest.read_bytes() == content


def test_incomplete_transfer_keeps_part_for_next_round(workdir: Path) -> None:
    """传输中断时**必须保留分片**，否则每轮都从头下，弱网下大文件永远传不完。"""
    content = b"Z" * 100000
    deploy_root = workdir / "deploy"
    dest = deploy_root / "exam" / "data.zip"

    client = FakeClient(content)
    client.fail_after = 10000  # 只给 10000 字节就断

    outcome = download_asset(client, make_job(content), deploy_root)

    assert outcome.status == "failed"
    assert "未完成" in (outcome.error or "")
    part = part_path_for(deploy_root, sha(content))
    assert part.is_file(), "中断后应当保留分片以便续传"
    assert part.stat().st_size == 10000
    assert not dest.exists(), "没下完绝不能出现在最终位置"


def test_progress_accumulates_across_rounds(workdir: Path) -> None:
    """连续几轮都在断，但每轮都应有进展 —— 这是断点续传存在的全部意义。"""
    content = b"P" * 100000
    deploy_root = workdir / "deploy"
    dest = deploy_root / "exam" / "data.zip"

    client = FakeClient(content)
    sizes = []
    for limit in (10000, 20000, 30000):
        client.fail_after = limit
        outcome = download_asset(client, make_job(content), deploy_root)
        sizes.append(part_path_for(deploy_root, sha(content)).stat().st_size)

    assert sizes == sorted(sizes) and sizes[-1] > sizes[0], (
        "每轮的已下载量必须单调递增，实际为 %r" % sizes
    )

    # 最后一轮放开限制，应当一次补齐
    client.fail_after = None
    outcome = download_asset(client, make_job(content), deploy_root)
    assert outcome.status == "ok", outcome.error
    assert dest.read_bytes() == content


def test_stale_part_larger_than_expected_is_discarded(workdir: Path) -> None:
    content = b"Q" * 1000
    deploy_root = workdir / "deploy"
    dest = deploy_root / "exam" / "data.zip"
    dest.parent.mkdir(parents=True)
    # 分片比声称的总大小还大 = 属于另一个版本或已损坏
    seed_part(deploy_root, sha(content), b"W" * 5000)

    outcome = download_asset(FakeClient(content), make_job(content), deploy_root)

    assert outcome.status == "ok", outcome.error
    assert dest.read_bytes() == content


# --------------------------------------------------------------------------- #
# 校验失败
# --------------------------------------------------------------------------- #


def test_hash_mismatch_discards_part(workdir: Path) -> None:
    content = b"good content"
    corrupted = b"bad content!"
    deploy_root = workdir / "deploy"

    job = make_job(content)
    outcome = download_asset(FakeClient(corrupted), job, deploy_root)

    assert outcome.status == "failed"
    assert "sha256 不符" in (outcome.error or "")
    assert not (deploy_root / "exam" / "data.zip").exists()
    assert list(deploy_root.rglob("*" + PART_SUFFIX)) == [], (
        "校验失败必须丢弃分片，否则下一轮会接着坏数据续传"
    )


def test_shorter_than_expected_keeps_part(workdir: Path) -> None:
    """收到内容比预期短 = 没传完，保留分片等下一轮续传。"""
    content = b"12345"
    deploy_root = workdir / "deploy"
    dest = deploy_root / "exam" / "data.zip"
    job = make_job(content, size=999)

    outcome = download_asset(FakeClient(content), job, deploy_root)

    assert outcome.status == "failed"
    assert "未完成" in (outcome.error or "")
    assert part_path_for(deploy_root, sha(content)).stat().st_size == 5


def test_longer_than_expected_discards_part(workdir: Path) -> None:
    """收到内容比预期长 = 分片损坏或属于另一版本，不可续传，必须丢弃。"""
    content = b"12345" * 100
    deploy_root = workdir / "deploy"
    dest = deploy_root / "exam" / "data.zip"
    job = make_job(content, size=10)  # 声称只有 10 字节，实际给了 500

    outcome = download_asset(FakeClient(content), job, deploy_root)

    assert outcome.status == "failed"
    assert "超出预期" in (outcome.error or "")
    assert not part_path_for(deploy_root, sha(content)).exists()


def test_invalid_sha_length_rejected_before_transfer(workdir: Path) -> None:
    content = b"x"
    deploy_root = workdir / "deploy"
    client = FakeClient(content)
    job = make_job(content, sha256="tooshort")

    outcome = download_asset(client, job, deploy_root)

    assert outcome.status == "failed"
    assert client.requests == [], "参数不合法时不该发起任何请求"


# --------------------------------------------------------------------------- #
# 路径安全
# --------------------------------------------------------------------------- #


def test_path_traversal_dest_is_rejected(workdir: Path) -> None:
    """服务端（或中间人）下发 ../../etc/passwd 时必须拒绝。"""
    content = b"pwned"
    deploy_root = workdir / "deploy"
    client = FakeClient(content)

    outcome = download_asset(client, make_job(content, dest="../../etc/pwned.conf"), deploy_root)

    assert outcome.status == "failed"
    assert "路径" in (outcome.error or "")
    assert client.requests == [], "路径不合法时不该发起请求"


def test_absolute_dest_is_rejected(workdir: Path) -> None:
    content = b"x"
    deploy_root = workdir / "deploy"
    outcome = download_asset(
        FakeClient(content), make_job(content, dest="/etc/pwned"), deploy_root
    )
    assert outcome.status == "failed"


def test_backslash_dest_is_rejected(workdir: Path) -> None:
    content = b"x"
    deploy_root = workdir / "deploy"
    outcome = download_asset(
        FakeClient(content), make_job(content, dest="exam\\..\\..\\pwned"), deploy_root
    )
    assert outcome.status == "failed"


def test_nested_dest_directories_are_created(workdir: Path) -> None:
    content = b"nested"
    deploy_root = workdir / "deploy"
    outcome = download_asset(
        FakeClient(content), make_job(content, dest="a/b/c/deep.zip"), deploy_root
    )
    assert outcome.status == "ok"
    assert (deploy_root / "a" / "b" / "c" / "deep.zip").read_bytes() == content


def test_missing_fields_fail_gracefully(workdir: Path) -> None:
    deploy_root = workdir / "deploy"
    outcome = download_asset(FakeClient(b"x"), {"asset_id": 1}, deploy_root)
    assert outcome.status == "failed"
    assert outcome.error


# --------------------------------------------------------------------------- #
# 分片按内容命名、集中存放
# --------------------------------------------------------------------------- #


def test_part_size_reports_local_progress_by_content(workdir: Path) -> None:
    deploy_root = workdir / "deploy"
    content = b"abc" * 100
    part = part_path_for(deploy_root, sha(content))
    part.parent.mkdir(parents=True)
    part.write_bytes(content[:120])

    assert part.parent.name == PARTS_DIRNAME
    assert part_size(deploy_root, sha(content)) == 120
    assert part_size(deploy_root, "0" * 64) == 0


def test_content_change_never_resumes_from_the_old_part(workdir: Path) -> None:
    """教师在线改了正文：同一个 asset、新内容。曾经分片按 asset_id 命名，会拿旧分片
    去续新内容。按 sha256 命名之后，新内容有自己的分片，从头下。"""
    old = b"O" * 5000
    new = b"N" * 5000
    deploy_root = workdir / "deploy"
    old_part = part_path_for(deploy_root, sha(old))
    old_part.parent.mkdir(parents=True)
    old_part.write_bytes(old[:3000])

    client = FakeClient(new)
    outcome = download_asset(client, make_job(new), deploy_root)

    assert outcome.status == "ok", outcome.error
    assert client.requests == [0], "新内容必须从头下，不能续旧内容的分片"
    assert (deploy_root / "exam" / "data.zip").read_bytes() == new


def test_outcome_carries_target_and_content_for_the_receipt(workdir: Path) -> None:
    content = b"receipt"
    outcome = download_asset(FakeClient(content), make_job(content), workdir / "deploy")

    assert outcome.status == "ok"
    assert outcome.target_id == 70
    assert outcome.sha256 == sha(content)


class ExplodingResponse(FakeResponse):
    """读到一半抛异常（连接被重置），并记下被读了多少次。"""

    def __init__(self, data: bytes, explode_after_reads: int) -> None:
        super().__init__(data)
        self.reads = 0
        self.explode_after = explode_after_reads

    def read(self, size: int = -1) -> bytes:
        self.reads += 1
        if self.reads > self.explode_after:
            raise ConnectionResetError(104, "Connection reset by peer")
        return super().read(size)


def test_error_mid_transfer_drops_the_connection_instead_of_draining(workdir: Path) -> None:
    """曾经 finally 里 _drain(response)：出错之后还要把剩下的几百 MB 读完再扔掉。"""
    content = b"D" * (256 * 1024 * 8)
    response = ExplodingResponse(content, explode_after_reads=2)

    class Client(FakeClient):
        def open_download(self, asset_id, offset=0):
            self.requests.append(offset)
            return 200, {}, response

    client = Client(content)
    outcome = download_asset(client, make_job(content), workdir / "deploy")

    assert outcome.status == "failed"
    assert client.dropped == 1, "出错必须丢连接"
    assert response.reads == 3, "出错之后不该再读剩下的响应体"
    assert part_size(workdir / "deploy", sha(content)) == 2 * 256 * 1024, "已写下的保留续传"


def test_deadline_stops_with_partial_and_keeps_the_part(workdir: Path) -> None:
    content = b"T" * (256 * 1024 * 4)
    deploy_root = workdir / "deploy"

    outcome = download_asset(
        FakeClient(content), make_job(content), deploy_root, deadline=time.monotonic() - 1
    )

    assert outcome.status == "partial"
    assert not (deploy_root / "exam" / "data.zip").exists()


def test_disk_full_is_flagged(workdir: Path, monkeypatch) -> None:
    import errno as errno_module

    content = b"F" * 1000
    deploy_root = workdir / "deploy"
    real_open = open

    def full_open(path, mode="r", *args, **kwargs):
        if str(path).endswith(PART_SUFFIX) and "b" in mode and ("w" in mode or "a" in mode):
            raise OSError(errno_module.ENOSPC, "No space left on device")
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", full_open)
    outcome = download_asset(FakeClient(content), make_job(content), deploy_root)

    assert outcome.status == "failed"
    assert outcome.disk_full is True


# --------------------------------------------------------------------------- #
# 下发资产是**文件**，不是"要解开的压缩包"
# --------------------------------------------------------------------------- #
#
# 题面与样例本身就是 zip，服务端只负责搬运字节；解压是选手/评测机自己的事。
# 一旦 Agent"顺手解开"，桌面上就会多出一堆目录，而真正要下发的那个 .zip 反而
# 不见了 —— 更糟的是解压会把 `../../` 这类成员名写到 deploy_root 外面，
# 而服务端下发的字节并不都被信任。


def make_zip_bytes() -> bytes:
    """造一个"真的能被解开"的 zip，用来证明我们**没有**去解它。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("题面/statement.md", "# 题目\n")
        archive.writestr("../../escape.txt", "逃出去的文件\n")
    return buffer.getvalue()


def test_zip_asset_is_stored_byte_for_byte(workdir: Path) -> None:
    content = make_zip_bytes()
    deploy_root = workdir / "deploy"

    outcome = download_asset(FakeClient(content), make_job(content, dest="题面.zip"), deploy_root)

    assert outcome.status == "ok", outcome.error
    target = deploy_root / "题面.zip"
    assert target.is_file()
    assert target.read_bytes() == content, "字节被改动了 —— 下发必须原样搬运"
    # 解开它就会多出这些目录；出现任何一个都说明有人在解压
    assert not (deploy_root / "题面").exists()
    assert list(deploy_root.rglob("*")) == [target]


def test_dest_without_suffix_is_a_plain_file(workdir: Path) -> None:
    """``dest`` 没有扩展名是完全正常的 —— 服务端说它是文件路径，那就是文件。"""
    content = b"no extension here"
    deploy_root = workdir / "deploy"

    outcome = download_asset(
        FakeClient(content), make_job(content, dest="exam/readme"), deploy_root
    )

    assert outcome.status == "ok", outcome.error
    assert (deploy_root / "exam" / "readme").read_bytes() == content


def test_dest_with_trailing_slash_is_rejected_loudly(workdir: Path) -> None:
    """``dest`` 带结尾斜杠 = 服务端把它当目录了，而协议里它**永远是文件路径**。

    这种情况要**响亮地失败**，不能悄悄当成同名文件写下去 —— 服务端以为在往
    某个目录里放东西，客户端却建了个同名文件，双方对"下到哪儿了"的理解就岔开了，
    而下一个下到同一目录的资产会因为这个同名文件而失败。
    """
    content = b"ambiguous"
    deploy_root = workdir / "deploy"

    outcome = download_asset(
        FakeClient(content), make_job(content, dest="exam/题面.zip/"), deploy_root
    )

    assert outcome.status == "failed"
    assert "不合法" in (outcome.error or "")
    assert not (deploy_root / "exam" / "题面.zip").exists()
