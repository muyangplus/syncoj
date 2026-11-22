"""服务端实例 ID（``server_uuid``）：落盘、跨重启不变、随 tick 下发。

Agent 靠它在失联后只认"原来那台"服务端（§7.3、§11），所以它一旦在重启之间变了，
已经装好的机器就再也不会自动跟过来 —— 这里盯的就是"不变"。
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from conftest import do_tick
from syncoj_server.context import AppContext
from syncoj_server.services import server_id


def test_首次生成_之后原样读回(tmp_path: Path) -> None:
    first = server_id.load_or_create_server_uuid(tmp_path)
    second = server_id.load_or_create_server_uuid(tmp_path)

    assert len(first) == 32 and all(c in "0123456789abcdef" for c in first)
    assert first == second
    path = tmp_path / server_id.SERVER_ID_FILE
    assert path.read_text(encoding="ascii").strip() == first
    assert not list(tmp_path.glob("*.tmp")), "临时文件要清掉"


@pytest.mark.skipif(sys.platform == "win32", reason="Windows 没有 POSIX 权限位")
def test_文件只有属主能读(tmp_path: Path) -> None:
    server_id.load_or_create_server_uuid(tmp_path)
    mode = stat.S_IMODE(os.stat(str(tmp_path / server_id.SERVER_ID_FILE)).st_mode)
    assert mode == 0o600


def test_内容坏了就重新生成(tmp_path: Path) -> None:
    (tmp_path / server_id.SERVER_ID_FILE).write_text("not-a-uuid\n", encoding="ascii")
    value = server_id.load_or_create_server_uuid(tmp_path)
    assert len(value) == 32
    assert server_id.load_or_create_server_uuid(tmp_path) == value


def test_两个数据目录是两台服务端(tmp_path: Path) -> None:
    a = server_id.load_or_create_server_uuid(tmp_path / "a")
    b = server_id.load_or_create_server_uuid(tmp_path / "b")
    assert a != b


def test_重启_重新建上下文_实例ID不变(settings) -> None:
    first = AppContext.create(settings).server_uuid
    second = AppContext.create(settings).server_uuid

    assert first and first == second
    assert (settings.data_root / server_id.SERVER_ID_FILE).is_file()


def test_tick_响应带实例ID(client, app, enrolled: dict) -> None:
    body = do_tick(client, enrolled["token"], [], machine_id=enrolled["machine_id"])
    assert body["server_uuid"] == app.state.ctx.server_uuid
    assert len(body["server_uuid"]) == 32


def test_未配对机器的_tick_也带实例ID(client, app, machine: dict) -> None:
    body = do_tick(client, machine["token"], [], machine_id=machine["machine_id"])
    assert body["claimed"] is False
    assert body["server_uuid"] == app.state.ctx.server_uuid
