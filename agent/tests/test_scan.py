"""目录扫描测试。

扫描是"代码回收"的输入端，它漏掉什么、多收什么，直接决定教师看到的东西对不对。
"""

from __future__ import annotations

import os
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


def test_hash_cache_avoids_recomputing(workdir: Path) -> None:
    make_tree(workdir, {"main.cpp": "same content"})
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
    make_tree(workdir, {"a.cpp": "a", "b.cpp": "b"})
    cache = HashCache()
    scan_directory(workdir, policy(), cache)
    assert len(cache) == 2

    (workdir / "a.cpp").unlink()
    scan_directory(workdir, policy(), cache)
    assert len(cache) == 1, "消失的文件不该继续占着缓存"


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
