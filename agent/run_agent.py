#!/usr/bin/env python3
"""SyncOJ Agent 启动器。

存在的唯一理由
--------------
``python3 /opt/syncoj/current/syncoj_agent/main.py`` **跑不起来**：``main.py``
用的是包内相对导入（``from . import __version__``），直接当脚本执行会报
``ImportError: attempted relative import with no known parent package``。

而 systemd 又必须用 ``-E -s`` 启动（``-E`` 忽略所有 ``PYTHON*`` 环境变量，
``-s`` 忽略 user site-packages）—— 这是隔离选手 Python 环境污染的关键手段。
问题在于 ``-E`` 会**连 PYTHONPATH 一起忽略**，所以没法靠环境变量告诉解释器
包在哪。

于是这里显式把发布目录插进 ``sys.path``，再委托给真正的 ``main()``。启动器
本身只做这一件事，逻辑越少越不容易出错。

发布布局::

    /opt/syncoj/current/          <- 指向 releases/<版本>/ 的软链
    ├── run_agent.py              <- 本文件，systemd 的 ExecStart 指向它
    └── syncoj_agent/
        ├── __init__.py
        └── ...
"""

from __future__ import annotations

import os
import sys

_RELEASE_ROOT = os.path.dirname(os.path.abspath(__file__))
if _RELEASE_ROOT not in sys.path:
    # 插到最前面：确保加载的是这个版本目录里的包，而不是别处恰好同名的模块
    sys.path.insert(0, _RELEASE_ROOT)

from syncoj_agent.main import main  # noqa: E402  (必须在 sys.path 调整之后)

if __name__ == "__main__":
    raise SystemExit(main())
