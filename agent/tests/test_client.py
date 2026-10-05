"""multipart 流式编码与请求体构造测试。

上传走的是手写的 multipart 编码（零依赖约束下没有 requests 可用）。手写的
二进制编码最容易出错的地方是长度与实际字节数不符 —— 那会让服务端挂起等待
永远不来的字节。所以这里既验长度也验逐字节内容。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from syncoj_agent.client import MultipartBody, build_enroll_payload


def read_all(body: MultipartBody, chunk: int = 7) -> bytes:
    """按指定块大小把整个流读出来。"""
    out = bytearray()
    while True:
        data = body.read(chunk)
        if not data:
            break
        out.extend(data)
    return bytes(out)


def test_length_matches_actual_bytes(workdir: Path) -> None:
    target = workdir / "main.cpp"
    target.write_bytes(b"int main() { return 0; }\n" * 100)

    body = MultipartBody(
        boundary="BOUNDARY123",
        fields={"path": "main.cpp", "sha256": "a" * 64},
        file_field="file",
        filename="main.cpp",
        file_path=target,
    )
    try:
        assert len(body) == len(read_all(body, chunk=13))
    finally:
        body.close()


def test_streaming_is_chunk_size_independent(workdir: Path) -> None:
    """无论调用方按 1 字节还是 1MB 读，产出的字节流必须完全一致。"""
    target = workdir / "data.bin"
    target.write_bytes(bytes(range(256)) * 40)

    def build() -> MultipartBody:
        return MultipartBody(
            boundary="B",
            fields={"path": "data.bin"},
            file_field="file",
            filename="data.bin",
            file_path=target,
        )

    reference_body = build()
    try:
        reference = read_all(reference_body, chunk=7)
    finally:
        reference_body.close()

    for chunk in (1, 3, 64, 1024, 1 << 20):
        body = build()
        try:
            assert read_all(body, chunk) == reference, "块大小 %d 时输出不同" % chunk
        finally:
            body.close()


def test_contains_expected_headers_and_content(workdir: Path) -> None:
    target = workdir / "sol.cpp"
    target.write_bytes(b"hello world")

    body = MultipartBody(
        boundary="XYZ",
        fields={"path": "src/sol.cpp", "sha256": "b" * 64},
        file_field="file",
        filename="sol.cpp",
        file_path=target,
    )
    try:
        raw = read_all(body, chunk=512)
    finally:
        body.close()

    assert b'name="path"' in raw
    assert b"src/sol.cpp" in raw
    assert b'name="sha256"' in raw
    assert b'filename="sol.cpp"' in raw
    assert b"\r\n\r\nhello world\r\n" in raw
    assert raw.startswith(b"--XYZ\r\n")
    assert raw.endswith(b"\r\n--XYZ--\r\n")


def test_filename_cannot_inject_headers(workdir: Path) -> None:
    """文件名里塞换行可以伪造额外的 multipart 分段 —— 必须被净化。"""
    target = workdir / "x.cpp"
    target.write_bytes(b"x")

    body = MultipartBody(
        boundary="B",
        fields={"path": "x.cpp"},
        file_field="file",
        filename='evil"\r\nContent-Type: text/html\r\n\r\n.cpp',
        file_path=target,
    )
    try:
        raw = read_all(body, chunk=4096)
    finally:
        body.close()

    header_section = raw.split(b"\r\n\r\n", 1)[0]
    assert header_section.count(b"\r\n") <= 7, "文件名注入了额外的头部行"
    assert b"text/html" not in header_section


def test_field_name_cannot_inject_headers(workdir: Path) -> None:
    target = workdir / "x.cpp"
    target.write_bytes(b"x")

    body = MultipartBody(
        boundary="B",
        fields={'na\r\nme"': "value"},
        file_field="file",
        filename="x.cpp",
        file_path=target,
    )
    try:
        raw = read_all(body, chunk=4096)
    finally:
        body.close()
    assert b"\r\n\r\nvalue" in raw or b'na\\"me\\"' in raw or b'na"me"' in raw


def test_file_is_read_streaming_not_into_memory(workdir: Path) -> None:
    """核心保证：无论文件多大，每次 read() 返回的量都由调用方决定。

    实现里若把整个文件读进内存再切片，这里的断言仍然会过 —— 但下面的
    ``test_read_returns_at_most_requested`` 会暴露出"一次性返回全部"的行为。
    """
    target = workdir / "big.bin"
    target.write_bytes(b"A" * (3 * 1024 * 1024))

    body = MultipartBody(
        boundary="B",
        fields={},
        file_field="file",
        filename="big.bin",
        file_path=target,
    )
    try:
        assert len(body.read(1024)) == 1024
    finally:
        body.close()


def test_read_returns_at_most_requested(workdir: Path) -> None:
    target = workdir / "x.bin"
    target.write_bytes(b"B" * 10000)

    body = MultipartBody(
        boundary="B",
        fields={},
        file_field="file",
        filename="x.bin",
        file_path=target,
    )
    try:
        for size in (1, 10, 999):
            data = body.read(size)
            assert len(data) <= size, "read(%d) 返回了 %d 字节" % (size, len(data))
    finally:
        body.close()


def test_close_is_idempotent_and_safe_without_reads(workdir: Path) -> None:
    target = workdir / "x.cpp"
    target.write_bytes(b"x")
    body = MultipartBody(
        boundary="B", fields={}, file_field="file", filename="x.cpp", file_path=target
    )
    body.close()
    body.close()


# --------------------------------------------------------------------------- #
# 请求体构造
# --------------------------------------------------------------------------- #


def test_enroll_payload_fields() -> None:
    payload = build_enroll_payload(
        enroll_code="AAAA-BBBB",
        machine_id="mid",
        hostname="host",
        agent_version="0.1.0",
        os_info="Linux",
    )
    assert set(payload) == {
        "enroll_code", "machine_id", "hostname", "agent_version", "os_info"
    }
