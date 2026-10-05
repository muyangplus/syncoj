"""HTTP 客户端。

只用 ``http.client`` + ``ssl`` + ``json``（全标准库）。不引入 ``requests``/
``httpx``：目标机 Python 3.8 已 EOL，第三方库的版本兼容是纯负担。

两个关键实现细节
----------------
1. **multipart 流式上传**。测试点动辄几十 MB，若先把整个请求体拼进内存，
   Agent 的内存占用会随文件大小线性增长 —— 而 systemd 给我们的预算只有
   200MB。这里的 ``MultipartBody`` 实现 ``read(n)`` 协议，``http.client``
   本来就会分块读请求体，所以整条链路上不存在"整个文件在内存里"的时刻。
2. **两条独立连接**。心跳/索引走短超时（30s），文件传输走长超时（600s）。
   共用一个连接的话，要么心跳被大文件传输阻塞，要么大文件被 30s 超时打断。
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import ssl
import time
import urllib.parse
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

__all__ = [
    "AgentClient",
    "AgentError",
    "NetworkError",
    "ProtocolError",
    "AuthError",
    "StaleUpload",
    "PayloadTooLarge",
    "MultipartBody",
]

log = logging.getLogger(__name__)

_READ_CHUNK = 256 * 1024


# --------------------------------------------------------------------------- #
# 异常
# --------------------------------------------------------------------------- #


class AgentError(Exception):
    """所有 Agent 侧错误的基类。"""


class NetworkError(AgentError):
    """连接失败/超时/中断 —— **可重试**，退避后下一轮再试。"""


class ProtocolError(AgentError):
    """服务端返回了不符合协议的内容 —— 通常是版本不匹配，不可重试。"""


class AuthError(AgentError):
    """凭据失效（401/403）—— 需要走重新注册。"""


class StaleUpload(AgentError):
    """409：本次要上传的内容已不是服务端当前认可的版本。

    这不是错误情况，是正常竞态（文件在 tick 与 upload 之间又变了）。
    Agent 应当**安静丢弃**，下一轮 tick 会拿到真正需要的版本。
    """

    def __init__(self, message: str, expected: str = "") -> None:
        super().__init__(message)
        self.expected = expected


class PayloadTooLarge(AgentError):
    """413：文件超过服务端策略上限。跳过并上报，不要反复重试。"""


# --------------------------------------------------------------------------- #
# multipart 流式编码
# --------------------------------------------------------------------------- #


class MultipartBody:
    """把 ``(普通字段们 + 一个大文件)`` 编码成 multipart/form-data 流。

    实现的是 ``http.client`` 需要的鸭子类型：``__len__`` 给出 Content-Length，
    ``read(n)`` 按需产出数据。整个过程文件只被顺序读一遍，且每次最多驻留
    ``n`` 字节。
    """

    def __init__(
        self,
        boundary: str,
        fields: Dict[str, str],
        file_field: str,
        filename: str,
        file_path: Path,
        content_type: str = "application/octet-stream",
    ) -> None:
        self.boundary = boundary
        segments: List[Tuple[str, object]] = []

        for name, value in fields.items():
            header = (
                "--%s\r\n"
                'Content-Disposition: form-data; name="%s"\r\n'
                "\r\n"
                "%s\r\n" % (boundary, _escape_field(name), value)
            )
            segments.append(("bytes", header.encode("utf-8")))

        safe_name = _escape_filename(filename)
        file_header = (
            "--%s\r\n"
            'Content-Disposition: form-data; name="%s"; filename="%s"\r\n'
            "Content-Type: %s\r\n"
            "\r\n" % (boundary, _escape_field(file_field), safe_name, content_type)
        )
        segments.append(("bytes", file_header.encode("utf-8")))
        segments.append(("file", file_path))
        segments.append(("bytes", ("\r\n--%s--\r\n" % boundary).encode("utf-8")))

        self._segments = segments
        self._index = 0
        self._offset = 0
        self._handle = None
        self._length = sum(
            len(seg[1]) if seg[0] == "bytes" else os.path.getsize(str(seg[1]))  # type: ignore[arg-type]
            for seg in segments
        )

    def __len__(self) -> int:
        return self._length

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = self._length

        chunks: List[bytes] = []
        remaining = size
        while remaining > 0 and self._index < len(self._segments):
            kind, payload = self._segments[self._index]

            if kind == "bytes":
                data = payload  # type: ignore[assignment]
                available = len(data) - self._offset
                take = min(available, remaining)
                chunks.append(data[self._offset : self._offset + take])
                self._offset += take
                remaining -= take
                if self._offset >= len(data):
                    self._index += 1
                    self._offset = 0
            else:
                if self._handle is None:
                    self._handle = open(str(payload), "rb")
                    self._handle.seek(self._offset)
                data = self._handle.read(min(remaining, _READ_CHUNK))
                if not data:
                    self._index += 1
                    self._offset = 0
                    self._handle.close()
                    self._handle = None
                    continue
                chunks.append(data)
                remaining -= len(data)

        return b"".join(chunks)

    def close(self) -> None:
        if self._handle is not None:
            try:
                self._handle.close()
            except OSError:
                pass
            self._handle = None


def _escape_field(value: str) -> str:
    """防 header 注入：换行和引号会破坏 multipart 边界。"""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\r", "").replace("\n", "")


def _escape_filename(value: str) -> str:
    cleaned = _escape_field(value)
    # 只保留基名，避免把客户端的目录结构泄进请求头
    return cleaned.replace("/", "_").replace("\\", "_")[:200] or "file"


# --------------------------------------------------------------------------- #
# 客户端
# --------------------------------------------------------------------------- #


class AgentClient:
    def __init__(
        self,
        base_url: str,
        token: Optional[str] = None,
        verify_tls: bool = True,
        ca_file: Optional[Path] = None,
        timeout: int = 30,
        download_timeout: int = 600,
    ) -> None:
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme not in ("http", "https"):
            raise ProtocolError("服务端地址必须是 http:// 或 https://")
        if not parsed.hostname:
            raise ProtocolError("服务端地址缺少主机名")

        self.scheme = parsed.scheme
        self.host = parsed.hostname
        self.port = parsed.port or (443 if parsed.scheme == "https" else 80)
        # 允许把服务端挂在子路径下（例如 https://host/syncoj）
        self.prefix = parsed.path.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.download_timeout = download_timeout
        self._ssl_context = _build_ssl_context(verify_tls, ca_file)

        self._control: Optional[http.client.HTTPConnection] = None
        self._transfer: Optional[http.client.HTTPConnection] = None

    # ---------------------------------------------------------------- #
    # 连接管理
    # ---------------------------------------------------------------- #

    def _new_connection(self, timeout: int) -> http.client.HTTPConnection:
        if self.scheme == "https":
            return http.client.HTTPSConnection(
                self.host, self.port, timeout=timeout, context=self._ssl_context
            )
        return http.client.HTTPConnection(self.host, self.port, timeout=timeout)

    def _connection(self, transfer: bool) -> http.client.HTTPConnection:
        slot = "_transfer" if transfer else "_control"
        connection = getattr(self, slot)
        if connection is None:
            connection = self._new_connection(
                self.download_timeout if transfer else self.timeout
            )
            setattr(self, slot, connection)
        return connection

    def _drop(self, transfer: bool) -> None:
        slot = "_transfer" if transfer else "_control"
        connection = getattr(self, slot)
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
            setattr(self, slot, None)

    def close(self) -> None:
        self._drop(False)
        self._drop(True)

    def drop_transfer_connection(self) -> None:
        """丢弃文件传输连接。

        流式响应被中途放弃（例如服务端没按 Range 返回）时必须调用 —— 连接上
        还有未读的响应体，直接复用会让下一个请求读到上一个响应的残留。
        """
        self._drop(True)

    def drop_control_connection(self) -> None:
        self._drop(False)

    def set_token(self, token: Optional[str]) -> None:
        self.token = token

    # ---------------------------------------------------------------- #
    # 底层请求
    # ---------------------------------------------------------------- #

    def request(
        self,
        method: str,
        path: str,
        body: object = None,
        headers: Optional[Dict[str, str]] = None,
        transfer: bool = False,
        retries: int = 2,
    ) -> Tuple[int, bytes]:
        """发一个请求，返回 ``(状态码, 响应体)``。

        连接层错误自动重建连接并重试（``retries`` 次）—— 长连接被中间的 NAT/
        防火墙静默掐断是常态，不重试的话 Agent 会周期性误报离线。
        """
        url = self.prefix + path
        sent_headers = {
            "Host": self.host if self.port in (80, 443) else "%s:%d" % (self.host, self.port),
            "Accept": "application/json",
            "User-Agent": "syncoj-agent",
            "Connection": "keep-alive",
        }
        if self.token:
            sent_headers["Authorization"] = "Bearer " + self.token
        if headers:
            sent_headers.update(headers)

        last_error: Optional[BaseException] = None
        for attempt in range(retries + 1):
            connection = self._connection(transfer)
            try:
                connection.request(method, url, body=body, headers=sent_headers)
                response = connection.getresponse()
                payload = response.read()
                status = response.status
                # 服务端要求关闭则让出连接，避免复用一个已死的 socket
                if response.will_close:
                    self._drop(transfer)
                return status, payload
            except (http.client.HTTPException, OSError, ssl.SSLError) as exc:
                last_error = exc
                self._drop(transfer)
                if attempt < retries:
                    # 退避一下再重试；连接被掐断往往是瞬时的
                    time.sleep(0.5 * (attempt + 1))
                    continue
        raise NetworkError("请求 %s %s 失败: %s" % (method, path, last_error))

    def request_json(
        self,
        method: str,
        path: str,
        payload: object = None,
        transfer: bool = False,
    ) -> Tuple[int, object]:
        body = None
        headers: Dict[str, str] = {}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
            headers["Content-Length"] = str(len(body))

        status, raw = self.request(method, path, body=body, headers=headers, transfer=transfer)
        return status, _decode_json(raw, status, path)

    # ---------------------------------------------------------------- #
    # 业务接口
    # ---------------------------------------------------------------- #

    def enroll(
        self,
        enroll_code: Optional[str],
        machine_id: str,
        hostname: str,
        agent_version: str,
        os_info: str,
        bootstrap_key: Optional[str] = None,
        machine_uuid: Optional[str] = None,
        machine_fingerprint: Optional[str] = None,
    ) -> dict:
        status, data = self.request_json(
            "POST",
            "/api/v1/agent/enroll",
            build_enroll_payload(
                enroll_code,
                machine_id,
                hostname,
                agent_version,
                os_info,
                bootstrap_key=bootstrap_key,
                machine_uuid=machine_uuid,
                machine_fingerprint=machine_fingerprint,
            ),
        )
        if status == 200 and isinstance(data, dict):
            return data
        raise _as_error(status, data, "注册失败")

    def tick(self, payload: dict) -> dict:
        status, data = self.request_json("POST", "/api/v1/agent/tick", payload)
        if status == 200 and isinstance(data, dict):
            return data
        raise _as_error(status, data, "tick 失败")

    def upload_file(
        self,
        rel_path: str,
        file_path: Path,
        sha256: str,
    ) -> dict:
        boundary = "----syncoj%s" % os.urandom(16).hex()
        body = MultipartBody(
            boundary=boundary,
            fields={"path": rel_path, "sha256": sha256},
            file_field="file",
            filename=Path(rel_path).name,
            file_path=file_path,
        )
        headers = {
            "Content-Type": "multipart/form-data; boundary=%s" % boundary,
            "Content-Length": str(len(body)),
        }
        try:
            status, raw = self.request(
                "POST", "/api/v1/agent/files", body=body, headers=headers, transfer=True
            )
        finally:
            body.close()

        data = _decode_json(raw, status, "/api/v1/agent/files")
        if status == 200 and isinstance(data, dict):
            return data
        raise _as_error(status, data, "上传 %s 失败" % rel_path)

    def report_events(self, events: Sequence[dict]) -> dict:
        status, data = self.request_json("POST", "/api/v1/agent/events", list(events))
        if status == 200:
            return data if isinstance(data, dict) else {}
        raise _as_error(status, data, "事件上报失败")

    def download_to_file(self, path: str, dest: Path, max_bytes: int) -> int:
        """把 ``path`` 的内容流式下载到 ``dest``，返回写入字节数。

        流式而非 ``request()`` 那样一次性读进内存：发布包可能上百 MB，而 Agent
        的内存预算只有 200MB。
        """
        dest.parent.mkdir(parents=True, exist_ok=True)
        connection = self._connection(True)
        url = self.prefix + path
        headers = {
            "Host": self.host if self.port in (80, 443) else "%s:%d" % (self.host, self.port),
            "Accept": "application/octet-stream",
            "User-Agent": "syncoj-agent",
        }
        if self.token:
            headers["Authorization"] = "Bearer " + self.token

        try:
            connection.request("GET", url, headers=headers)
            response = connection.getresponse()
        except (http.client.HTTPException, OSError, ssl.SSLError) as exc:
            self._drop(True)
            raise NetworkError("下载 %s 失败: %s" % (path, exc))

        if response.status != 200:
            payload = response.read()
            self._drop(True)
            data = _decode_json(payload, response.status, path)
            raise _as_error(response.status, data, "下载 %s 失败" % path)

        written = 0
        try:
            with open(str(dest), "wb") as handle:
                while True:
                    chunk = response.read(_READ_CHUNK)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > max_bytes:
                        raise PayloadTooLarge(
                            "下载内容超过上限 %d 字节" % max_bytes
                        )
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            try:
                dest.unlink()
            except OSError:
                pass
            self._drop(True)
            raise
        finally:
            _drain_quietly(response)

        return written

    def open_download(self, asset_id: int, offset: int = 0):
        """发起下载请求，返回 ``(status, headers, response)``。

        调用方负责消费 ``response`` 并关闭 —— 下载是流式的，不能在客户端里
        把内容读进内存。
        """
        headers = {"Accept": "application/octet-stream"}
        if offset > 0:
            headers["Range"] = "bytes=%d-" % offset

        connection = self._connection(True)
        url = self.prefix + "/api/v1/agent/assets/%d" % asset_id
        sent = {
            "Host": self.host,
            "Accept": headers["Accept"],
            "User-Agent": "syncoj-agent",
        }
        if self.token:
            sent["Authorization"] = "Bearer " + self.token
        if offset > 0:
            sent["Range"] = headers["Range"]

        try:
            connection.request("GET", url, headers=sent)
            response = connection.getresponse()
        except (http.client.HTTPException, OSError, ssl.SSLError) as exc:
            self._drop(True)
            raise NetworkError("发起下载失败: %s" % exc)

        if response.status not in (200, 206):
            payload = response.read()
            connection.close()
            self._drop(True)
            data = _decode_json(payload, response.status, "下载")
            raise _as_error(response.status, data, "下载 asset %d 失败" % asset_id)

        return response.status, dict(response.getheaders()), response


# --------------------------------------------------------------------------- #
# 请求体构造
# --------------------------------------------------------------------------- #
#
# 抽成独立函数是为了让 ``tools/build_fixture.py`` 能生成"Agent 真正会发出的"
# 报文样本，交给服务端的契约测试校验。若只是测试里手写一份 payload，协议漂移
# 就检不出来 —— 那份手写的副本会跟真实代码一起漂移。


def build_enroll_payload(
    enroll_code: Optional[str],
    machine_id: str,
    hostname: str,
    agent_version: str,
    os_info: str,
    bootstrap_key: Optional[str] = None,
    machine_uuid: Optional[str] = None,
    machine_fingerprint: Optional[str] = None,
) -> Dict[str, object]:
    """注册报文体。

    **两个凭据都为空时不要把它们塞进去**：服务端把"字段缺失"和"字段是空串"
    看成两件事，传空串只会让错误信息变得含糊。

    ``machine_uuid`` / ``machine_fingerprint`` 允许为空 —— 后者在虚拟机或
    SMBIOS 不可读时确实拿不到，服务端见到空值就当新机器处理（走人工配对），
    而不是拿空值去和别的机器"互相认回"。
    """
    payload: Dict[str, object] = {
        "machine_id": machine_id,
        "hostname": hostname,
        "agent_version": agent_version,
        "os_info": os_info,
    }
    if enroll_code:
        payload["enroll_code"] = enroll_code
    if bootstrap_key:
        payload["bootstrap_key"] = bootstrap_key
    if machine_uuid:
        payload["machine_uuid"] = machine_uuid
    if machine_fingerprint:
        payload["machine_fingerprint"] = machine_fingerprint
    return payload


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #


def _drain_quietly(response) -> None:
    """把响应体读完让连接可复用；读失败就静默放弃（连接会被丢弃）。"""
    if response is None:
        return
    try:
        while response.read(_READ_CHUNK):
            pass
    except Exception:
        pass


def _build_ssl_context(verify_tls: bool, ca_file: Optional[Path]) -> ssl.SSLContext:
    if not verify_tls:
        # 明确不校验：内网自签且暂时没配 CA 时的过渡选项。
        # 注意这让中间人可以直接读走 token —— 日志里会告警。
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context

    context = ssl.create_default_context(cafile=str(ca_file) if ca_file else None)
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context


def _decode_json(raw: bytes, status: int, path: str) -> object:
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        if status >= 400:
            return {"detail": raw[:300].decode("utf-8", "replace")}
        raise ProtocolError("%s 返回了非 JSON 内容（前 200 字节）: %r" % (path, raw[:200]))


def _as_error(status: int, data: object, context: str) -> AgentError:
    detail = ""
    if isinstance(data, dict):
        raw_detail = data.get("detail")
        if isinstance(raw_detail, dict):
            detail = str(raw_detail.get("message") or raw_detail)
        elif raw_detail is not None:
            detail = str(raw_detail)
    message = "%s（HTTP %d）%s" % (context, status, detail)

    if status in (401, 403):
        return AuthError(message)
    if status == 409:
        expected = ""
        if isinstance(data, dict) and isinstance(data.get("detail"), dict):
            expected = str(data["detail"].get("expected", ""))
        return StaleUpload(message, expected=expected)
    if status == 413:
        return PayloadTooLarge(message)
    if status >= 500:
        return NetworkError(message)  # 服务端临时故障，值得重试
    return ProtocolError(message)
