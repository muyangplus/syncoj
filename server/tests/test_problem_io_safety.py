"""导入包的安全限制（services/problem_io/archive.py 与公共规整）。"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from syncoj_server.services import problem_io
from syncoj_server.services.problem_io import archive as archive_mod
from syncoj_server.services.problem_io.archive import SafeArchive
from syncoj_server.services.problem_io.notices import ProblemIoError

from problem_io_fixtures import make_native_zip, make_zip, native_problem, stage


def _raw_zip(names) -> bytes:
    """绕过 ZipFile 对名字的规整，直接写出指定成员名。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name in names:
            info = zipfile.ZipInfo("placeholder")
            info.filename = name
            zf.writestr(info, b"x")
    return buffer.getvalue()


def _code(callable_, *args, **kwargs) -> str:
    with pytest.raises(ProblemIoError) as info:
        callable_(*args, **kwargs)
    return info.value.code


@pytest.mark.parametrize(
    "name",
    # NUL 不在这里测：zipfile 读名字时已在 NUL 处截断，到不了我们这一层
    ["../evil.in", "a/../../evil", "/etc/passwd", "a\\..\\b", "C:/win.ini"],
)
def test_unsafe_member_names(tmp_path: Path, name: str) -> None:
    path = stage(tmp_path, _raw_zip(["ok.txt", name]))
    assert _code(SafeArchive, path) == "unsafe_path"
    assert _code(problem_io.parse_archive, path) == "unsafe_path"


def test_member_count_limit(tmp_path: Path) -> None:
    path = stage(tmp_path, make_zip({"f%d" % i: "x" for i in range(11)}))
    assert _code(SafeArchive, path, max_members=10) == "too_many_members"
    SafeArchive(path, max_members=11).close()


def test_total_uncompressed_limit(tmp_path: Path) -> None:
    path = stage(tmp_path, make_zip({"a": "x" * 60, "b": "y" * 60}))
    assert _code(SafeArchive, path, max_total=100) == "archive_too_large"


def test_compression_bomb(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_BZIP2) as zf:
        zf.writestr("zeros.in", b"\x00" * (4 * 1024 * 1024))
    path = stage(tmp_path, buffer.getvalue())
    monkeypatch.setattr(archive_mod, "BOMB_MIN_BYTES", 1024 * 1024)
    assert _code(SafeArchive, path) == "zip_bomb"
    # 解压后不大时不算炸弹，即使压缩比很高
    monkeypatch.setattr(archive_mod, "BOMB_MIN_BYTES", 64 * 1024 * 1024)
    SafeArchive(path).close()


def test_read_is_capped_by_bytes_actually_read(tmp_path: Path) -> None:
    path = stage(tmp_path, make_zip({"big.json": "x" * 100}))
    with SafeArchive(path) as archive:
        assert archive.read("big.json", limit=100) == b"x" * 100
        assert _code(archive.read, "big.json", limit=99) == "member_too_large"


def test_too_many_problems(tmp_path: Path) -> None:
    problems = [native_problem("p%d" % i, testdata=None) for i in range(201)]
    path = stage(tmp_path, make_native_zip(problems))
    assert _code(problem_io.parse_archive, path) == "too_many_problems"
    path = stage(tmp_path, make_native_zip(problems[:200]))
    with problem_io.parse_archive(path) as parsed:
        assert len(parsed.result.problems) == 200


def test_inner_zip_sha_mismatch(tmp_path: Path) -> None:
    path = stage(tmp_path, make_native_zip(tamper_sha=True))
    with problem_io.parse_archive(path) as parsed:
        (bundle,) = parsed.result.problems
        assert [e.code for e in bundle.errors] == ["sha_mismatch"]
        assert bundle.raw_testdata is None


def test_inner_zip_must_be_valid_testdata(tmp_path: Path) -> None:
    bad = make_zip({"1.in": "1"})  # 缺答案
    path = stage(tmp_path, make_native_zip([native_problem(testdata=bad)]))
    with problem_io.parse_archive(path) as parsed:
        assert [e.code for e in parsed.result.problems[0].errors] == ["testdata_invalid"]


def test_ignored_junk_still_has_to_be_safe(tmp_path: Path) -> None:
    path = stage(tmp_path, _raw_zip(["syncoj-problems.json", "__MACOSX/../../evil"]))
    assert _code(SafeArchive, path) == "unsafe_path"


def test_overlong_name_segment(tmp_path: Path) -> None:
    path = stage(tmp_path, _raw_zip(["ok.txt", "d/" + "1" * 256 + ".in"]))
    assert _code(SafeArchive, path) == "name_too_long"
    SafeArchive(stage(tmp_path, _raw_zip(["d/" + "1" * 255]), "ok.zip")).close()
