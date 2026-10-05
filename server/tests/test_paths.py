"""路径安全测试。

这是整个服务端的信任边界：Agent 跑在选手机器上，随时可能被替换成恶意程序。
这里的每一条断言都对应一个真实攻击面。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from syncoj_server.paths import (
    PathValidationError,
    is_within,
    safe_join,
    slugify,
    validate_relpath,
)


# --------------------------------------------------------------------------- #
# 应该被接受
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("main.cpp", "main.cpp"),
        ("src/main.cpp", "src/main.cpp"),
        ("a/b/c/d.pas", "a/b/c/d.pas"),
        ("题目一/solution.cpp", "题目一/solution.cpp"),
        ("a-b_c.d+e.cpp", "a-b_c.d+e.cpp"),
        ("nested/deep/x", "nested/deep/x"),
        ("2025/mock-1/S001/main.cpp", "2025/mock-1/S001/main.cpp"),
    ],
)
def test_accepts_normal_relative_paths(raw: str, expected: str) -> None:
    assert validate_relpath(raw) == expected


# --------------------------------------------------------------------------- #
# 应该被拒绝 —— 穿越与绝对路径
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw",
    [
        "../etc/passwd",
        "a/../../etc/passwd",
        "a/b/../../../root/.ssh/id_rsa",
        "..",
        "a/..",
        "/etc/passwd",
        "/",
        "//server/share/x",
        "C:/Windows/system32",
        "c:foo.txt",
        "\\\\server\\share\\x",
        "a\\b\\c.cpp",
        "~/secret.cpp",
        "a/~/b.cpp",
    ],
)
def test_rejects_traversal_and_absolute(raw: str) -> None:
    with pytest.raises(PathValidationError):
        validate_relpath(raw)


# --------------------------------------------------------------------------- #
# 应该被拒绝 —— 歧义与畸形写法
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw",
    [
        "",
        " ",
        " main.cpp",
        "main.cpp ",
        "a//b.cpp",
        "a/\x00b.cpp",
        "a/\x01b.cpp",
        "a/\x7fb.cpp",
        "a/.",  # 末段是 .
        "a/b./c.cpp",  # 段以点结尾：Windows 会静默去掉
        "a/con/b.cpp",
        "a/NUL.txt",
        "a/com1",
        "a/lpt9",
    ],
)
def test_rejects_malformed(raw: str) -> None:
    with pytest.raises(PathValidationError):
        validate_relpath(raw)


def test_rejects_overlong_segment() -> None:
    with pytest.raises(PathValidationError):
        validate_relpath("a" * 300 + ".cpp")


def test_rejects_overlong_path() -> None:
    # 200 段 × 8 字符 ≈ 1799 字符，超过默认 1024 上限
    with pytest.raises(PathValidationError):
        validate_relpath("/".join(["abcdefgh"] * 200))


def test_rejects_non_string() -> None:
    for value in (None, 123, b"bytes", ["list"]):
        with pytest.raises(PathValidationError):
            validate_relpath(value)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# safe_join：符号链接穿越
# --------------------------------------------------------------------------- #


def test_safe_join_normal(workdir: Path) -> None:
    root = workdir / "root"
    root.mkdir()
    target = safe_join(root, "a/b/c.cpp")
    assert target == (root / "a" / "b" / "c.cpp").resolve()
    assert is_within(target, root)


def test_safe_join_blocks_symlink_escape(workdir: Path) -> None:
    """根目录内放一个指向外部的软链，落盘必须被拒绝。"""
    root = workdir / "root"
    outside = workdir / "outside"
    root.mkdir()
    outside.mkdir()
    link = root / "escape"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("当前环境不支持创建符号链接")

    with pytest.raises(PathValidationError):
        safe_join(root, "escape/pwned.cpp")


def test_safe_join_rejects_dotdot_even_inside_root(workdir: Path) -> None:
    root = workdir / "root"
    root.mkdir()
    with pytest.raises(PathValidationError):
        safe_join(root, "a/../../root/x.cpp")


def test_is_within(workdir: Path) -> None:
    inside = workdir / "a" / "b"
    inside.mkdir(parents=True)
    assert is_within(inside, workdir)
    assert is_within(workdir, workdir)
    assert not is_within(workdir, inside)
    assert not is_within(workdir / "sibling", workdir / "a")


# --------------------------------------------------------------------------- #
# slugify
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("mock-1", "mock-1"),
        ("Mock Contest 2025", "Mock-Contest-2025"),
        ("校内模拟赛", "x"),
        ("../../etc", "etc"),
        ("...", "x"),
        ("", "x"),
        ("a" * 200, "a" * 64),
    ],
)
def test_slugify(raw: str, expected: str) -> None:
    assert slugify(raw) == expected


def test_slugify_never_produces_traversal_or_dotfile() -> None:
    for raw in ["..", "../..", ".hidden", "....//", "///", "a/b/../c"]:
        result = slugify(raw)
        assert ".." not in result
        assert "/" not in result and "\\" not in result
        assert not result.startswith(".")
