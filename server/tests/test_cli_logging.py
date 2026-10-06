"""给 cli.py 的 `_configure_logging` 加一条守卫。

这条守卫的写法有点绕，但绕得有理由：pytest 自己会给 root 挂处理器，于是
"日志能不能出来"在测试进程里**永远是 True**，直接断言等于什么都没测。
所以先把 root 上的处理器摘掉、确认 INFO 确实被挡住，再看修复是否把它打开。
"""

from __future__ import annotations

import logging

import pytest

from syncoj_server import cli


@pytest.fixture()
def bare_root(monkeypatch: pytest.MonkeyPatch):
    """把 root 恢复成"没有任何处理器"的状态。

    不要动 `disable_existing_loggers` 之类的外部开关，只把 handlers 摘掉 ——
    Python 在"没有处理器"时的兜底行为就是把 INFO 丢掉。
    """
    root = logging.getLogger()
    original = list(root.handlers)
    original_level = root.level
    root.handlers = []
    root.setLevel(logging.WARNING)
    try:
        yield root
    finally:
        root.handlers = original
        root.setLevel(original_level)


def test_不配日志时_INFO_是出不来的(bare_root) -> None:
    """先把"问题真的存在"钉住 —— 否则下面那条断言可能只是在测 pytest。"""
    assert not logging.getLogger("syncoj").isEnabledFor(logging.INFO)


def test_serve_会把_syncoj_的_INFO_打开(bare_root) -> None:
    """服务端自己的 INFO 一条都不显示 —— 而这个坑很隐蔽。

    uvicorn 的 ``log_config`` 只配置它自己的 logger，所以"已载入发布签名私钥"、
    "局域网发现已启用"、"SyncOJ 启动"这些 INFO 全被丢掉。发现这个问题的场合是
    端到端验证局域网发现：机器那边收得到应答，服务端日志里连一句"发现已启用"
    都没有 —— 而排查"机器找不到服务端"时，最该看的就是那句。
    """
    cli._configure_logging("info")

    assert logging.getLogger("syncoj").isEnabledFor(logging.INFO)
    assert bare_root.handlers, "应当给根挂上处理器，否则日志没有出口"


def test_日志级别跟着_log_level_走(bare_root) -> None:
    """``serve --log-level warning`` 时就不该再刷 INFO。"""
    cli._configure_logging("warning")

    assert not logging.getLogger("syncoj").isEnabledFor(logging.INFO)
    assert logging.getLogger("syncoj").isEnabledFor(logging.WARNING)
