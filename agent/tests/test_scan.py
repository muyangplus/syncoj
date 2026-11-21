"""目录扫描测试。

扫描是"代码回收"的输入端，它漏掉什么、多收什么，直接决定教师看到的东西对不对。
"""

from __future__ import annotations

import json
import os
import time
import unicodedata
from pathlib import Path

import pytest

from conftest import make_tree
from syncoj_agent.policy import DEFAULT_POLICY
from syncoj_agent.scan import ScanPolicy, scan_directory
from syncoj_agent.state import HashCache


def policy(**overrides) -> ScanPolicy:
    merged = dict(DEFAULT_POLICY)
    merged.update(overrides)
    return ScanPolicy.from_mapping(merged)


def paths_of(outcome) -> list:
    return sorted(e.path for e in outcome.entries)


def test_collects_whitelisted_extensions_only(workdir: Path) -> None:
    make_tree(
        workdir,
        {
            "main.cpp": "int main(){}",
            "solution.py": "print(1)",
            "notes.txt": "不该被回收",
            "data.in": "1 2 3",
            "archive.zip": "PK",
            "Makefile": "all:",
        },
    )
    outcome = scan_directory(workdir, policy())

    assert paths_of(outcome) == ["main.cpp", "solution.py"]
    assert outcome.complete is True


def test_ignores_excluded_directories(workdir: Path) -> None:
    make_tree(
        workdir,
        {
            "main.cpp": "x",
            ".git/config": "x",
            "build/out.cpp": "x",
            "__pycache__/m.py": "x",
            "node_modules/pkg/index.js": "x",
            "src/helper.cpp": "x",
        },
    )
    outcome = scan_directory(workdir, policy())

    assert paths_of(outcome) == ["main.cpp", "src/helper.cpp"]


def test_ignores_hidden_files_and_editor_tempfiles(workdir: Path) -> None:
    make_tree(
        workdir,
        {
            "main.cpp": "x",
            ".hidden.cpp": "x",
            "main.cpp.swp": "x",
            "main.cpp~": "x",
            "backup.cpp.bak": "x",
            "main.cpp.orig": "x",
        },
    )
    outcome = scan_directory(workdir, policy())

    assert paths_of(outcome) == ["main.cpp"]


def test_ignores_unknown_suffix_even_if_extension_matches(workdir: Path) -> None:
    """``main.cpp.tmp`` 的扩展名是 .tmp 而不是 .cpp。"""
    make_tree(workdir, {"main.cpp.tmp": "x", "real.cpp": "x"})
    outcome = scan_directory(workdir, policy())

    assert paths_of(outcome) == ["real.cpp"]


def test_oversize_files_are_skipped_and_reported(workdir: Path) -> None:
    make_tree(workdir, {"small.cpp": "x" * 10, "huge.cpp": "x" * 5000})
    outcome = scan_directory(workdir, policy(max_file_size=1000))

    assert paths_of(outcome) == ["small.cpp"]
    assert outcome.oversize == ["huge.cpp"]
    # 超限是策略行为，不是扫描失败，不能标记成不完整
    assert outcome.complete is True


def test_extension_matching_is_case_insensitive(workdir: Path) -> None:
    make_tree(workdir, {"MAIN.CPP": "x", "Sol.Py": "x"})
    outcome = scan_directory(workdir, policy())
    assert paths_of(outcome) == ["MAIN.CPP", "Sol.Py"]


def test_symlinks_are_not_followed(workdir: Path) -> None:
    """选手放一个指向家目录的软链，绝不能让 Agent 把系统文件当成代码传上来。"""
    outside = workdir / "outside"
    outside.mkdir()
    (outside / "secret.cpp").write_text("secret", encoding="utf-8")

    scan_root = workdir / "code"
    scan_root.mkdir()
    (scan_root / "mine.cpp").write_text("mine", encoding="utf-8")

    try:
        os.symlink(outside, scan_root / "link", target_is_directory=True)
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("当前环境不支持创建符号链接")

    outcome = scan_directory(scan_root, policy())
    assert paths_of(outcome) == ["mine.cpp"]


def test_nested_paths_use_posix_separators(workdir: Path) -> None:
    """上报路径必须始终是 POSIX 风格 —— 服务端会拒绝含反斜杠的路径。"""
    make_tree(workdir, {"a/b/c/deep.cpp": "x"})
    outcome = scan_directory(workdir, policy())

    assert paths_of(outcome) == ["a/b/c/deep.cpp"]
    assert "\\" not in outcome.entries[0].path


def make_old(*paths: Path) -> None:
    """把 mtime 拨到一小时前：刚写的文件在 racy 窗口里，按设计不进缓存。"""
    past = time.time() - 3600
    for path in paths:
        os.utime(str(path), (past, past))


def test_hash_cache_avoids_recomputing(workdir: Path) -> None:
    make_tree(workdir, {"main.cpp": "same content"})
    make_old(workdir / "main.cpp")
    cache = HashCache()

    first = scan_directory(workdir, policy(), cache)
    assert cache.stats["misses"] == 1
    assert cache.stats["hits"] == 0
    digest = first.entries[0].sha256

    second = scan_directory(workdir, policy(), cache)
    assert cache.stats["hits"] == 1, "文件没变就不该重算哈希"
    assert second.entries[0].sha256 == digest


def test_hash_cache_invalidates_on_content_change(workdir: Path) -> None:
    target = workdir / "main.cpp"
    target.write_text("v1", encoding="utf-8")
    cache = HashCache()

    first = scan_directory(workdir, policy(), cache).entries[0].sha256
    # 长度也变，确保 mtime+size 两个维度都失效
    target.write_text("v2-longer-content", encoding="utf-8")
    second = scan_directory(workdir, policy(), cache).entries[0].sha256

    assert first != second


def test_hash_cache_prunes_vanished_files(workdir: Path) -> None:
    """prune 由调用方在整轮扫完之后做一次（逐根 prune 会互相清掉）。"""
    make_tree(workdir, {"a.cpp": "a", "b.cpp": "b"})
    make_old(workdir / "a.cpp", workdir / "b.cpp")
    cache = HashCache()
    outcome = scan_directory(workdir, policy(), cache)
    cache.prune_to(outcome.cache_keys)
    assert len(cache) == 2

    (workdir / "a.cpp").unlink()
    outcome = scan_directory(workdir, policy(), cache)
    cache.prune_to(outcome.cache_keys)
    assert len(cache) == 1, "消失的文件不该继续占着缓存"


def test_同一时刻的等长修改不会被缓存吃掉(workdir: Path) -> None:
    """曾经指纹是"秒级 mtime + size"：同一秒内把 n=100 改成 n=200，缓存返回旧哈希，
    直到下一次修改为止 —— 那若是最后一次保存，最终版就丢了。"""
    target = workdir / "main.cpp"
    target.write_text("n=100", encoding="utf-8")
    cache = HashCache()
    first = scan_directory(workdir, policy(), cache).entries[0].sha256

    stat = target.stat()
    target.write_text("n=200", encoding="utf-8")  # 等长
    os.utime(str(target), ns=(stat.st_atime_ns, stat.st_mtime_ns))  # mtime 也一模一样
    second = scan_directory(workdir, policy(), cache).entries[0].sha256

    assert first != second


def test_刚写的文件不进缓存_老文件才进(workdir: Path) -> None:
    make_tree(workdir, {"fresh.cpp": "x", "old.cpp": "y"})
    make_old(workdir / "old.cpp")
    cache = HashCache()
    scan_directory(workdir, policy(), cache)

    keys = list(cache.entries)
    assert len(keys) == 1 and keys[0].endswith("old.cpp")


def test_两个扫描根里同名同指纹的文件不串哈希(workdir: Path) -> None:
    """曾经所有根共用"相对路径"做键：两个根里同路径、同 mtime/size 的文件
    拿到彼此的哈希。现在键带上根的命名空间。"""
    one, two = workdir / "one", workdir / "two"
    make_tree(one, {"p/a.cpp": "AAAA"})
    make_tree(two, {"p/a.cpp": "BBBB"})
    stamp = time.time() - 3600
    for path in (one / "p" / "a.cpp", two / "p" / "a.cpp"):
        os.utime(str(path), (stamp, stamp))
    cache = HashCache()

    a = scan_directory(one, policy(), cache, cache_namespace=str(one)).entries[0].sha256
    b = scan_directory(two, policy(), cache, cache_namespace=str(two)).entries[0].sha256

    assert a != b


def test_文件名不是合法_utf8_时跳过且不影响其它文件(workdir: Path) -> None:
    """一个非 UTF-8 的文件名曾经让每一轮 tick 都在 JSON 编码时抛异常。"""
    if os.name != "posix":
        pytest.skip("只有 POSIX 文件系统允许任意字节的文件名")
    try:
        (workdir / os.fsdecode(b"bad\xff.cpp")).write_text("x", encoding="utf-8")
    except (OSError, UnicodeError):
        pytest.skip("这个文件系统不接受非 UTF-8 文件名")
    (workdir / "good.cpp").write_text("y", encoding="utf-8")

    outcome = scan_directory(workdir, policy())

    assert paths_of(outcome) == ["good.cpp"]
    assert any("UTF-8" in error for error in outcome.errors)
    for entry in outcome.entries:
        json.dumps(entry.to_dict(), ensure_ascii=False).encode("utf-8")


def test_nfd_文件名按_nfc_上报_本地仍按原名打开(workdir: Path) -> None:
    nfd = unicodedata.normalize("NFD", "é.cpp")
    try:
        (workdir / nfd).write_text("x", encoding="utf-8")
    except OSError:
        pytest.skip("这个文件系统不接受这个文件名")
    if not any(name == nfd for name in os.listdir(str(workdir))):
        pytest.skip("这个文件系统自己做了归一化（如 macOS），测不到")

    entry = scan_directory(workdir, policy()).entries[0]

    assert entry.path == unicodedata.normalize("NFC", "é.cpp")
    assert entry.local is not None and entry.local.name == nfd
    assert entry.local.read_text(encoding="utf-8") == "x"


def test_正好收满不算截断(workdir: Path) -> None:
    make_tree(workdir, {"a.cpp": "a", "b.cpp": "b"})

    full = scan_directory(workdir, policy(), max_files=2)
    over = scan_directory(workdir, policy(), max_files=1)

    assert full.complete is True and full.truncated is False and len(full.entries) == 2
    assert over.truncated is True and over.complete is False and len(over.entries) == 1


def test_missing_root_is_incomplete_not_crash(workdir: Path) -> None:
    missing = workdir / "nope"
    outcome = scan_directory(missing, policy())

    assert outcome.complete is False
    assert outcome.entries == []
    assert outcome.errors


def test_empty_directory_yields_empty_scan(workdir: Path) -> None:
    outcome = scan_directory(workdir, policy())
    assert outcome.entries == []
    assert outcome.complete is True


def test_max_files_limit_marks_incomplete(workdir: Path) -> None:
    """截断意味着"没扫完"，必须标记不完整 —— 否则漏掉的文件会被判成已删除。"""
    make_tree(workdir, {"f%d.cpp" % i: "x" for i in range(20)})
    outcome = scan_directory(workdir, policy(), max_files=5)

    assert len(outcome.entries) == 5
    assert outcome.truncated is True
    assert outcome.complete is False


def test_deterministic_results_for_same_tree(workdir: Path) -> None:
    make_tree(workdir, {"a.cpp": "a", "b/c.cpp": "c", "d/e/f.cpp": "f"})
    first = paths_of(scan_directory(workdir, policy()))
    second = paths_of(scan_directory(workdir, policy()))
    assert first == second
