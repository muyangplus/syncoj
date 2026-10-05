#!/usr/bin/env python3
"""生成 Agent **真实**报文的样本，供服务端契约测试校验。

设计要点
--------
样本必须由 Agent 自己的报文体构造函数产出，而不是在测试里手写。手写的样本会
和真实代码**一起漂移** —— 开发者改了字段名，测试里的副本没改，两边都"通过"，
契约测试就变成了摆设。

本脚本刻意不联网、不需要 Agent 实例，因此可以安全地在 CI 里跑。

用法::

    python agent/tools/build_fixture.py            # 打印到 stdout
    python agent/tools/build_fixture.py -o out.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

# 允许从仓库根目录直接运行
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from syncoj_agent import __version__  # noqa: E402
from syncoj_agent.client import build_enroll_payload  # noqa: E402
from syncoj_agent.main import build_tick_payload  # noqa: E402
from syncoj_agent.policy import DEFAULT_POLICY  # noqa: E402
from syncoj_agent.scan import FileEntry, ScanOutcome  # noqa: E402


def _sample_results() -> List[Any]:
    """构造一份有代表性的扫描结果：正常文件 + 超限文件 + 扫描错误。"""
    code = ScanOutcome(root="/home/student/code")
    code.entries = [
        FileEntry(
            path="main.cpp",
            sha256="a" * 64,
            size=1234,
            mtime=1767225500,
        ),
        FileEntry(
            path="src/util.h",
            sha256="b" * 64,
            size=256,
            mtime=1767225510,
        ),
    ]
    code.oversize.append("big_input.txt")

    backup = ScanOutcome(root="/home/student/backup")
    backup.entries = [
        FileEntry(
            path="old.cpp",
            sha256="c" * 64,
            size=99,
            mtime=1767220000,
        )
    ]
    # 模拟子目录权限不足：该根目录扫描不完整
    backup.complete = False
    backup.errors.append("无法读取目录 /home/student/backup/private: Permission denied")

    return [("code", code), ("backup", backup)]


def build_fixture() -> Dict[str, Any]:
    tick_payload, oversize, errors = build_tick_payload(
        machine_id="0123456789abcdef0123456789abcdef",
        agent_version=__version__,
        scan_root="/home/student/code, /home/student/backup",
        results=_sample_results(),
        partials=[{"asset_id": 42, "bytes_done": 3145728}],
        stats={"disk_free": 10737418240, "queue": 0},
        completed_assets=[7],
    )

    return {
        "_comment": "由 agent/tools/build_fixture.py 自动生成，请勿手工编辑",
        "agent_version": __version__,
        "enroll": build_enroll_payload(
            enroll_code="ABCD-EFGH-JKLM-NPQR",
            machine_id="0123456789abcdef0123456789abcdef",
            hostname="exam-pc-01",
            agent_version=__version__,
            os_info="Linux 5.4.0 Ubuntu 20.04.6 LTS",
        ),
        "tick": tick_payload,
        "policy_defaults": DEFAULT_POLICY,
        # 便于人工核对：这两个不在请求体里，只用于审计事件
        "_oversize": oversize,
        "_errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 Agent 协议报文样本")
    parser.add_argument("-o", "--output", default=None, help="输出文件；省略则打印到 stdout")
    parser.add_argument("--compact", action="store_true", help="紧凑输出")
    args = parser.parse_args()

    fixture = build_fixture()
    text = json.dumps(
        fixture,
        ensure_ascii=False,
        indent=None if args.compact else 2,
        sort_keys=False,
    )
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
