"""服务端实例 ID（``server_uuid``）：这台服务端"是谁"，跨重启、换 IP 都不变。

Agent 第一次 tick 成功后记住它；之后局域网发现（§7.3）与主动通告（§11）找到的
服务端，只有 ``server_uuid`` 相同才切过去 —— 发布私钥可能被多台服务端共用（同一份
``.key/`` 拷到隔壁考场），光验签分不清"原来那台"和"隔壁那台"。

存在 ``<data_root>/server-id``（一行 32 位十六进制，``0600``）。放数据目录而不是
``.key/``：它不是密钥，泄露了也冒充不了谁（应答照样要私钥签）；而它该跟着
**数据**走 —— 数据目录整体搬到新机器上，机器们认的仍是同一台服务端；重新
``init`` 一个空数据目录，就是一台新服务端。不放库里：库结构演进走迁移，这一个值
不值得占一个迁移号，而且备份 / 恢复库时它也不该被单独回滚。
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from pathlib import Path

__all__ = ["SERVER_ID_FILE", "load_or_create_server_uuid"]

log = logging.getLogger("syncoj")

SERVER_ID_FILE = "server-id"

_VALID = re.compile(r"^[0-9a-f]{32}$")


def _read(path: Path) -> str:
    return path.read_text(encoding="ascii").strip()


def load_or_create_server_uuid(data_root: Path) -> str:
    """读 ``<data_root>/server-id``；没有就生成一个落盘。读写失败返回空串（不阻止启动）。

    空串的后果只是 tick 响应里不带 ``server_uuid``、发现与挑战应答按旧格式签 ——
    机器端退回"只验签"的老行为，不会失联。
    """
    path = Path(data_root) / SERVER_ID_FILE
    try:
        existing = _read(path)
    except FileNotFoundError:
        existing = None
    except (OSError, UnicodeDecodeError) as exc:
        log.error("读不出服务端实例 ID %s（%s），本次运行不带 server_uuid", path, exc)
        return ""
    if existing is not None:
        if _VALID.match(existing):
            return existing
        log.warning("服务端实例 ID %s 内容不合法，重新生成（已记住旧 ID 的机器不会再自动跟过来）", path)

    value = uuid.uuid4().hex
    tmp = path.with_name("%s.%d.tmp" % (SERVER_ID_FILE, os.getpid()))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(value + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if existing is None:
            # 两个进程（serve 与一条 CLI）同时首次启动时只能有一个赢：link 遇到已存在
            # 就失败，输的一方读赢家写下的那个
            try:
                os.link(str(tmp), str(path))
            except FileExistsError:
                return _read(path)
            except OSError:
                os.replace(str(tmp), str(path))
        else:
            os.replace(str(tmp), str(path))
    except OSError as exc:
        log.error("写不下服务端实例 ID %s（%s），本次运行不带 server_uuid", path, exc)
        return ""
    finally:
        try:
            os.unlink(str(tmp))
        except OSError:
            pass
    log.info("已生成服务端实例 ID %s（%s）", value, path)
    return value
