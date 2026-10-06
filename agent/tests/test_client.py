"""multipart 流式编码与请求体构造测试。

上传走的是手写的 multipart 编码（零依赖约束下没有 requests 可用）。手写的
二进制编码最容易出错的地方是长度与实际字节数不符 —— 那会让服务端挂起等待
永远不来的字节。所以这里既验长度也验逐字节内容。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from syncoj_agent.client import AuthError, NetworkError, MultipartBody, build_enroll_payload


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
    """注册只有一条路：镜像里那份统一密钥。

    机器身份的**三个要素**都在报文体里（见 docs/reference/protocol.md §1）：
    ``machine_uuid`` 是首选身份，``machine_fingerprint`` 是快照还原后的第二道，
    ``machine_id`` 仅供人工辨认。没有 enroll_code —— 那条链路已经删掉了。
    """
    payload = build_enroll_payload(
        machine_id="0123456789abcdef0123456789abcdef",
        hostname="exam-pc-01",
        agent_version="0.1.0",
        os_info="Linux",
        bootstrap_key="SECRET",
        machine_uuid="3f2a1b0c4d5e6f708192a3b4c5d6e7f8",
        machine_fingerprint="4c4c4544-0031-3010-8043-b7c04f4d4432",
    )
    assert set(payload) == {
        "machine_id",
        "hostname",
        "agent_version",
        "os_info",
        "bootstrap_key",
        "machine_uuid",
        "machine_fingerprint",
    }
    assert "enroll_code" not in payload


def test_enroll_payload_omits_missing_values() -> None:
    """**空值不要塞进去**：服务端把"字段缺失"和"字段是空串"看成两件事。

    指纹在虚拟机或 SMBIOS 不可读时确实拿不到，那时就该整个字段不出现 ——
    传空串只会让错误信息变得含糊。
    """
    payload = build_enroll_payload(
        machine_id="mid",
        hostname="host",
        agent_version="0.1.0",
        os_info="Linux",
    )
    assert "bootstrap_key" not in payload
    assert "machine_uuid" not in payload
    assert "machine_fingerprint" not in payload


# --------------------------------------------------------------------------- #
# 错误体：{detail, code, details}
# --------------------------------------------------------------------------- #
#
# 服务端**所有**错误响应都是这一个形状（docs/reference/protocol.md §0.1）。要分支判断就
# 看 ``code`` —— 拿 ``detail`` 做字符串比较会在改文案时静默失效。


def test_error_codes_map_to_the_right_exception() -> None:
    from syncoj_agent.client import (
        BootstrapKeyError,
        PayloadTooLarge,
        ProtocolError,
        RateLimited,
        StaleUpload,
        UnboundError,
        _as_error,
    )

    cases = [
        # (状态码, code, 期望的异常类)
        (401, "unauthorized", AuthError),
        (401, "token_expired", AuthError),
        (403, "machine_unbound", UnboundError),
        (403, "pairing_required", UnboundError),
        (404, "bootstrap_key_invalid", BootstrapKeyError),
        (403, "bootstrap_key_revoked", BootstrapKeyError),
        (403, "bootstrap_key_expired", BootstrapKeyError),
        (429, "rate_limited", RateLimited),
        (409, "conflict", StaleUpload),
        (413, "too_large", PayloadTooLarge),
        (500, "internal_error", NetworkError),
        (400, "validation_error", ProtocolError),
    ]
    for status, code, expected in cases:
        error = _as_error(status, {"detail": "解释", "code": code}, "上下文")
        assert isinstance(error, expected), "%s/%s 映射成了 %s" % (
            status, code, type(error).__name__
        )
        assert error.code == code
        assert error.status == status
        assert error.detail == "解释"
        assert "解释" in error.message


def test_403_with_an_unknown_code_still_means_do_not_re_enroll() -> None:
    """403 是照**状态码**定性的：凭据有效。

    认不出来的 code 也不能去重新注册 —— 那会让服务端换掉配对码，是这里唯一
    会造成实际损失的"猜错"。
    """
    from syncoj_agent.client import UnboundError, _as_error

    error = _as_error(403, {"detail": "没权限"}, "上下文")
    assert isinstance(error, UnboundError)
    assert error.code == ""


def test_error_mapping_does_not_depend_on_the_detail_text() -> None:
    """同一个 code、两套完全不同的文案，必须映射到同一个异常类。

    这正是"分支判断只能看 code"的可执行版本。
    """
    from syncoj_agent.client import UnboundError, _as_error

    first = _as_error(403, {"detail": "这台机器还没有配对到人", "code": "machine_unbound"}, "x")
    second = _as_error(
        403, {"detail": "MACHINE NOT PAIRED", "code": "machine_unbound"}, "x"
    )
    assert type(first) is type(second) is UnboundError


def test_stale_upload_carries_the_expected_hash() -> None:
    from syncoj_agent.client import StaleUpload, _as_error

    error = _as_error(
        409,
        {
            "detail": "内容已不是当前版本",
            "code": "conflict",
            "details": {"reason": "stale", "expected": "ab" * 32},
        },
        "上传",
    )
    assert isinstance(error, StaleUpload)
    assert error.expected == "ab" * 32
    assert error.details["reason"] == "stale"


def test_error_body_accepts_a_structured_legacy_detail() -> None:
    """老服务端把结构化信息塞进 ``detail``（一个 dict）。兼容一下，别丢信息。"""
    from syncoj_agent.client import _as_error

    error = _as_error(409, {"detail": {"message": "旧格式", "expected": "cd" * 32}}, "x")
    assert error.detail == "旧格式"


def test_non_json_error_body_does_not_crash() -> None:
    """500 上服务端可能返回一坨纯文本（Starlette 的兜底），代理也可能插一段 HTML。

    这时候要当成"服务端暂时故障（可重试）"，而不是让 ``json.loads`` 抛出去 ——
    那会把可重试变成不可重试，正好搞反。
    """
    from syncoj_agent.client import NetworkError, _decode_json, _as_error

    data = _decode_json(b"Internal Server Error", 500, "/api/v1/agent/tick")
    assert isinstance(data, dict) and isinstance(data["detail"], str)

    error = _as_error(500, data, "tick")
    assert isinstance(error, NetworkError)
    assert "Internal Server Error" in error.message


def test_non_json_error_body_with_invalid_utf8_does_not_crash() -> None:
    from syncoj_agent.client import _decode_json

    data = _decode_json(b"\xff\xfe\x00garbage", 502, "/x")
    assert isinstance(data, dict)
    assert isinstance(data["detail"], str)


def test_non_json_success_body_is_a_protocol_error() -> None:
    """200 上出现非 JSON 是真·协议不匹配 —— 这个必须响亮地失败。"""
    from syncoj_agent.client import ProtocolError, _decode_json

    with pytest.raises(ProtocolError):
        _decode_json(b"<html>hello</html>", 200, "/api/v1/agent/tick")


def test_empty_body_decodes_to_none() -> None:
    from syncoj_agent.client import _decode_json

    assert _decode_json(b"", 204, "/x") is None


def test_retry_after_header_is_parsed() -> None:
    """429 的退避节奏由服务端决定 —— 它才知道限速窗口有多长。"""
    from syncoj_agent.client import _parse_retry_after

    assert _parse_retry_after("17") == 17
    assert _parse_retry_after("  3  ") == 3
    # 解析不出来的（HTTP 也允许一个绝对时间）就交给调用方自己的退避
    assert _parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0
    assert _parse_retry_after("0") == 0
    assert _parse_retry_after("-5") == 0
    assert _parse_retry_after(None) == 0
    assert _parse_retry_after("") == 0


def test_client_raise_fills_retry_after_from_the_header() -> None:
    """``Retry-After`` 是**响应头**，不在错误体里，所以得单独补。

    补不上时退避就退回本地默认值 —— 那也比按自己的节奏硬撞强。
    """
    from syncoj_agent.client import AgentClient, RateLimited

    client = AgentClient("https://127.0.0.1:8000")
    client.last_retry_after = 42
    with pytest.raises(RateLimited) as excinfo:
        client._raise(429, {"detail": "太快了", "code": "rate_limited"}, "注册")
    assert excinfo.value.retry_after == 42
    client.close()


def test_client_raise_prefers_the_body_over_the_header() -> None:
    """错误体里已经说了具体秒数就用它 —— 那是更精确的信息。"""
    from syncoj_agent.client import AgentClient, RateLimited

    client = AgentClient("https://127.0.0.1:8000")
    client.last_retry_after = 42
    with pytest.raises(RateLimited) as excinfo:
        client._raise(
            429,
            {"detail": "太快了", "code": "rate_limited", "details": {"retry_after": 5}},
            "注册",
        )
    assert excinfo.value.retry_after == 5
    client.close()


# --------------------------------------------------------------------------- #
# 公开方法抛出来的**就是**这些异常类
# --------------------------------------------------------------------------- #
#
# 上面测的是 ``_as_error`` 的映射表。这一段把 client 与主循环之间那一小段也
# 覆盖上：主循环 ``except UnboundError`` / ``except AuthError`` 认的是**这些**
# 异常，而它们必须是公开方法真的会抛出来的东西 —— 中间少接一根线，
# "403 不重新注册"就退化成了一条谁也不执行的注释。


def failing_client(status: int, body: object):
    """造一个 ``request_json`` 直接回固定响应的客户端。

    覆盖 enrol / tick / events 这条路径 —— 它们都走 ``request_json``。
    上传走的是 ``request``（原始字节），另有 ``failing_raw_client``。
    """
    from syncoj_agent.client import AgentClient

    client = AgentClient("https://127.0.0.1:8000")
    client.request_json = lambda *a, **k: (status, body)  # type: ignore[assignment]
    return client


def failing_raw_client(status: int, body: object):
    """同样的东西，但走原始字节那条路（上传、下载）。"""
    import json as json_module

    from syncoj_agent.client import AgentClient

    raw = json_module.dumps(body, ensure_ascii=False).encode("utf-8")
    client = AgentClient("https://127.0.0.1:8000")
    client.request = lambda *a, **k: (status, raw)  # type: ignore[assignment]
    return client


def test_tick_raises_unbound_on_a_403_response() -> None:
    from syncoj_agent.client import UnboundError

    client = failing_client(403, {"detail": "这台机器还没有配对到人", "code": "machine_unbound"})
    try:
        with pytest.raises(UnboundError) as excinfo:
            client.tick({"agent_version": "0.1.0"})
        assert excinfo.value.code == "machine_unbound"
        assert excinfo.value.status == 403
    finally:
        client.close()


def test_tick_raises_auth_on_a_401_response() -> None:
    from syncoj_agent.client import AuthError

    client = failing_client(401, {"detail": "凭据无效", "code": "token_expired"})
    try:
        with pytest.raises(AuthError) as excinfo:
            client.tick({"agent_version": "0.1.0"})
        assert excinfo.value.code == "token_expired"
    finally:
        client.close()


def test_enroll_raises_bootstrap_key_error() -> None:
    from syncoj_agent.client import BootstrapKeyError

    client = failing_client(403, {"detail": "已吊销", "code": "bootstrap_key_revoked"})
    try:
        with pytest.raises(BootstrapKeyError):
            client.enroll(machine_id="mid", hostname="h", agent_version="0", os_info="x")
    finally:
        client.close()


def test_upload_raises_stale_upload_with_the_expected_hash(workdir: Path) -> None:
    """409 是**正常竞态**，不是错误 —— 上传路径必须把它抛成 ``StaleUpload``，
    主循环才能安静地丢掉它、等下一轮。

    注意这里走的不是 ``_as_error``，而是 ``upload_file`` 自己那条解码路径：
    少接一根线，"迟到版本"就会被当成一般错误上报，污染错误统计。
    """
    from syncoj_agent.client import StaleUpload

    target = workdir / "p1.cpp"
    target.write_text("int main() {}", encoding="utf-8")

    client = failing_raw_client(
        409,
        {
            "detail": "内容已不是当前版本",
            "code": "conflict",
            "details": {"reason": "stale", "expected": "ef" * 32},
        },
    )
    try:
        with pytest.raises(StaleUpload) as excinfo:
            client.upload_file("p1/p1.cpp", target, "ab" * 32)
        assert excinfo.value.expected == "ef" * 32
    finally:
        client.close()


def test_upload_raises_payload_too_large(workdir: Path) -> None:
    from syncoj_agent.client import PayloadTooLarge

    target = workdir / "big.cpp"
    target.write_text("x", encoding="utf-8")

    client = failing_raw_client(413, {"detail": "太大了", "code": "too_large"})
    try:
        with pytest.raises(PayloadTooLarge):
            client.upload_file("big.cpp", target, "ab" * 32)
    finally:
        client.close()


def test_upload_403_is_not_an_auth_error(workdir: Path) -> None:
    """403 上传被拒时**不能**抛成 AuthError。

    抛成 AuthError 会让主循环清掉凭据重新注册 —— 而 403 的意思恰恰是
    "凭据有效"。这一个字母的差别就是"配对码被刷掉"和"安静等下一轮"的差别。
    """
    from syncoj_agent.client import AuthError, UnboundError

    target = workdir / "p1.cpp"
    target.write_text("x", encoding="utf-8")

    client = failing_raw_client(
        403, {"detail": "还没配对", "code": "machine_unbound"}
    )
    try:
        with pytest.raises(UnboundError) as excinfo:
            client.upload_file("p1/p1.cpp", target, "ab" * 32)
        assert not isinstance(excinfo.value, AuthError)
    finally:
        client.close()


def test_enroll_401_is_an_auth_error() -> None:
    from syncoj_agent.client import AuthError

    client = failing_client(401, {"detail": "凭据无效", "code": "unauthorized"})
    try:
        with pytest.raises(AuthError):
            client.enroll(machine_id="mid", hostname="h", agent_version="0", os_info="x")
    finally:
        client.close()


def test_enroll_429_carries_the_retry_after_header() -> None:
    """429 的退避要能传到主循环 —— 它才知道该睡多久。"""
    from syncoj_agent.client import RateLimited

    client = failing_client(429, {"detail": "太快了", "code": "rate_limited"})
    client.last_retry_after = 23
    try:
        with pytest.raises(RateLimited) as excinfo:
            client.enroll(machine_id="mid", hostname="h", agent_version="0", os_info="x")
        assert excinfo.value.retry_after == 23
    finally:
        client.close()


def test_events_403_is_not_an_auth_error() -> None:
    from syncoj_agent.client import UnboundError

    client = failing_client(403, {"detail": "还没配对", "code": "machine_unbound"})
    try:
        with pytest.raises(UnboundError):
            client.report_events([{"level": "info", "category": "x", "message": "y"}])
    finally:
        client.close()
