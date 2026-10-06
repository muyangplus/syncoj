"""注册单元的**单元名**只许有一个写法：``syncoj-agent-enroll.service``。

漂移过一次，而且漂在最要命的地方：写进**每台机器的 agent.ini 注释**、和印给现场
照着敲的提示里。教师照着敲会得到 ``Unit not found``，而看起来"就是那个名字"。

所以这里不只是改回来，而是钉住：**全仓库（agent/ 与 server/）里不许再出现那个
错名**。守卫用"把名字拼出来"的方式拿到它，而不是把错名原样抄进本文件 ——
否则这条守卫自己就会成为命中，还得给自己开例外。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

#: 正确名（全仓库唯一写法）
CORRECT = "syncoj-agent-enroll.service"
#: 错名：**拼**出来的，不是抄的（抄进来这条守卫自己就命中）
WRONG = "syncoj-" + "enroll" + ".service"

#: 扫描范围与跳过项。``.git`` 不必说；``.pytest-tmp`` 是测试自己的临时目录，
#: 里面可能躺着历史副本（扫它会把"旧代码里有过这个名字"当成现在的错）。
SCAN_ROOTS = ("agent", "server")
SKIP_DIRS = {".git", ".pytest-tmp", ".venv", "node_modules", "__pycache__", ".pytest_cache"}


def iter_files():
    for root_name in SCAN_ROOTS:
        root = REPO_ROOT / root_name
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            # 跳过项按**相对扫描根**判断：整个仓库可能被拷到别处（变异测试就是
            # 在 .pytest-tmp/mut 下跑这一份），按绝对路径判断会把整棵树都跳过，
            # 于是守卫永远绿、变异永远抓不到。
            relative = path.relative_to(root)
            if any(part in SKIP_DIRS for part in relative.parts):
                continue
            if path.suffix in (".pyc", ".pyo", ".gz", ".zip", ".png", ".jpg", ".ico"):
                continue
            yield path


def test_错名在全仓库里一个都不剩() -> None:
    assert WRONG != CORRECT and WRONG.replace("syncoj-", "syncoj-agent-") == CORRECT, (
        "守卫自己的模式串写错了（它必须是「正确名去掉 -agent」）"
    )

    hits = []
    for path in iter_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if WRONG in line:
                hits.append("%s:%d: %s" % (path.relative_to(REPO_ROOT), number, line.strip()))

    assert hits == [], (
        "单元名又漂了 —— 正确写法是 %s，下面这些地方写着错名：\n%s"
        % (CORRECT, "\n".join(hits))
    )


def test_正确名出现在每个必须说它的地方() -> None:
    """错名没了还不够：这些地方**必须**真的提到它，否则说明有人把整句删了。"""
    required = {
        REPO_ROOT / "agent" / "packaging" / "install.py",
        REPO_ROOT / "agent" / "syncoj_agent" / "main.py",
        REPO_ROOT / "agent" / "config.example.ini",
        REPO_ROOT / "agent" / "packaging" / "README.md",
    }
    for path in sorted(required):
        text = path.read_text(encoding="utf-8")
        assert CORRECT in text, "%s 里没有 %s" % (path.relative_to(REPO_ROOT), CORRECT)


def test_机器侧的名字来自常量而不是手打() -> None:
    """写进配置/提示的那两处必须走常量 —— 手打字符串就是这次漂移的来源。"""
    import syncoj_agent.main as main_module

    assert main_module.ENROLL_UNIT_NAME == CORRECT

    install_source = (REPO_ROOT / "agent" / "packaging" / "install.py").read_text(
        encoding="utf-8"
    )
    assert 'ENROLL_UNIT_FILENAME = SERVICE_NAME + "-enroll.service"' in install_source
    # 模板注释里用的是占位符，值从常量来
    assert "enroll_unit=ENROLL_UNIT_FILENAME" in install_source

    main_source = (REPO_ROOT / "agent" / "syncoj_agent" / "main.py").read_text(
        encoding="utf-8"
    )
    assert "ENROLL_UNIT_NAME" in main_source
