#!/usr/bin/env python3
"""从服务端导出 OpenAPI 描述，供前端生成 TS 类型。

为什么把 openapi.json 提交进仓库
--------------------------------
前端类型必须是**服务端模型**的派生品，而不是手写副本。把导出的 JSON 提交进
仓库，CI 就能在"服务端 schema 改了但前端类型没重新生成"时失败 —— 靠的是对比
生成结果，而不是靠人记得跑哪条命令。

用法::

    python server/tools/dump_openapi.py            # 写到 web/openapi.json
    python server/tools/dump_openapi.py --check    # 只校验是否已最新（CI 用）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "server"))

DEFAULT_OUT = REPO_ROOT / "web" / "openapi.json"


def build_schema() -> Dict[str, Any]:
    from syncoj_server.main import create_app

    # 用不落盘的配置构造应用。create_app 会建目录，所以指向一个临时位置再清掉
    import shutil
    import tempfile

    from syncoj_server.config import Settings

    tmp = Path(tempfile.mkdtemp(prefix="syncoj-openapi-"))
    try:
        settings = Settings()
        settings.data_root = tmp
        app = create_app(settings)
        return app.openapi()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def serialized(schema: Dict[str, Any]) -> str:
    # sort_keys 让输出稳定：否则字典顺序一变，"是否最新"的判断就会误报
    return json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def read_raw(path: Path) -> str:
    """读文件，**不做换行翻译**。

    ``Path.read_text()`` 默认会把 ``\\r\\n`` 统一成 ``\\n``，于是"文件里其实是
    CRLF"这件事被它悄悄盖住了：``--check`` 照样报"最新"，而 git 里存的是另一个
    东西。这里的目的是比较字节，所以必须关掉翻译。
    """
    with path.open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


def main(argv: list) -> int:
    parser = argparse.ArgumentParser(description="导出服务端 OpenAPI 描述")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--check", action="store_true",
                        help="只校验文件是否已最新，不写入（CI 用）")
    args = parser.parse_args(argv[1:])

    out = Path(args.out)
    text = serialized(build_schema())

    if args.check:
        if not out.is_file():
            print("错误：%s 不存在。请运行 python server/tools/dump_openapi.py" % out, file=sys.stderr)
            return 1
        if read_raw(out) != text:
            print(
                "错误：%s 已过期 —— 服务端 schema 改了但前端类型没重新生成。\n"
                "      请运行：python server/tools/dump_openapi.py && "
                "(cd web && npm run gen:types)" % out,
                file=sys.stderr,
            )
            return 1
        print("OpenAPI 描述已是最新")
        return 0

    out.parent.mkdir(parents=True, exist_ok=True)
    # 必须显式指明换行符：Windows 上 Path.write_text 默认会把 \n 翻成 \r\n，
    # 于是同一个生成物在开发机上带 CR、在 CI 上不带。首当其冲的是 diff ——
    # 整个文件看起来"全变了"，而真正改动的两行淹在里面没人看得见。
    out.write_text(text, encoding="utf-8", newline="\n")
    print("已写入 %s（%d 字节）" % (out, len(text)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
