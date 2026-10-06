"""把一份**真实**的服务端状态造到固定目录里，供浏览器验收/截图使用。

这不是测试：文件名不以 ``test_`` 开头，常规收集不会碰它。要跑它得显式指名：

    .venv\\Scripts\\python.exe -m pytest server/tests/_shot_seed.py::test_shot_seed -q

为什么复用 conftest 与 test_player_page 里那套夹具，而不手写 SQL：造出"一台已经
配对、并且从某个 IP 说过话的机器"要同时把 machine_uuid / token_hash / 绑定关系
都对上，手写 INSERT 漏一个字段，页面点一下就是 500 —— 而那时我会以为是页面坏了。
夹具已经把这条路走对过很多遍，这里只是把 data_root 换成固定目录，让数据留下来。

跑完之后用同一份数据起服务端：

    .venv\\Scripts\\python.exe -m syncoj_server.cli --data-root .tmp-state/shot-env/data serve --port 8016
"""

from __future__ import annotations

import os
import pathlib
from datetime import datetime, timedelta, timezone

import pytest

from syncoj_server.config import Settings

ROOT = pathlib.Path(__file__).resolve().parents[2]
#: **按仓库根算**，不要按 cwd 算：pytest 一般是在 ``server/`` 下跑的，而
#: `.tmp-state/shot-env` 是相对仓库根的路径 —— 按 cwd 算会让"造数据的"和"读数据的"
#: 各自指向一个目录，表现是截图里空空的，而 pytest 说它成功了。
SHOT_ROOT = pathlib.Path(
    os.environ.get("SYNCOJ_SHOT_ROOT") or (ROOT / ".tmp-state" / "shot-env")
).resolve()


@pytest.fixture()
def settings() -> Settings:
    """与 conftest 的同名夹具一样，只是 data_root 指向**固定**目录（跑完不删）。"""
    config = Settings()
    config.data_root = SHOT_ROOT / "data"
    config.discovery_enabled = False
    config.web_dist = ROOT / "web" / "dist"
    return config


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_shot_seed(app, client, admin_headers, contest, player, roster_entry, bootstrap_key):
    # 这两个帮手与"选手页"的测试共用同一套动作，别在这里重写一遍
    from test_player_page import (
        client_from,
        deploy_to,
        enroll_bound_machine,
        text_asset,
        upload_asset,
    )

    # 1. 考试时间窗：开考在半小时前、结束在一个半小时后 —— 截图里能看到真实的时间
    now = datetime.now(timezone.utc)
    patched = client.patch(
        "/api/v1/admin/contests/%d" % contest["id"],
        json={
            "starts_at": _iso(now - timedelta(minutes=30)),
            "ends_at": _iso(now + timedelta(minutes=90)),
        },
        headers=admin_headers,
    )
    assert patched.status_code == 200, patched.text

    # 2. 考场公告（一份下发的 NOTICE.md）+ 一份题面：选手页上两块内容都出现
    notice = text_asset(
        client,
        contest,
        admin_headers,
        "NOTICE.md",
        "# 考场须知\n"
        "\n"
        "**开考 30 分钟内不准离场**，交卷前先存盘 —— 考场只保证收到的最后一份。\n"
        "\n"
        "## 交卷前检查\n"
        "\n"
        "- 文件名按题面要求，不要自己改名\n"
        "- 关掉多余的编辑器窗口\n"
        "- 有疑问举手，**不要互相询问**\n"
        "\n"
        "代码放在 `桌面/<考号>/` 下面，具体路径见题面里的说明。\n"
        "\n"
        "### 几条口诀（没写 - 也要各自成行）\n"
        "\n"
        "先编译再交\n"
        "先存盘再举手\n"
        "\n"
        "### 时间与科目\n"
        "\n"
        "| 科目 | 时间 |\n"
        "| --- | --- |\n"
        "| 一试 | 08:30-12:00 |\n"
        "| 二试 | 14:00-18:00 |\n"
        "\n"
        "> 提前交卷的同学请安静离场。\n",
    )
    deploy_to(client, contest, admin_headers, notice["id"], [player["id"]])

    statement = upload_asset(client, contest, admin_headers, "题面.zip", b"statement-bytes")
    deploy_to(client, contest, admin_headers, statement["id"], [player["id"]])

    # 3. 一台"已经配对、并且从 127.0.0.1 说过话"的机器 —— 浏览器也从这里打开，
    #    所以选手页会自动认出这台机器（这正是主路径）
    ip_client = client_from(app, "127.0.0.1")
    enroll_bound_machine(client, ip_client, admin_headers, bootstrap_key, roster_entry["id"])

    print("\n截图数据就绪：%s" % SHOT_ROOT)
    print("起服务端：--data-root %s serve --port 8016" % (SHOT_ROOT / "data"))
