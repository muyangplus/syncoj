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
    [
        "../evil.in",
        "a/../../evil",
        "/etc/passwd",
        "a\\..\\b",
        "..\\evil.in",
        "\\etc\\passwd",
        "C:/win.ini",
        "C:\\win.ini",
    ],
)
def test_unsafe_member_names(tmp_path: Path, name: str) -> None:
    path = stage(tmp_path, _raw_zip(["ok.txt", name]))
    assert _code(SafeArchive, path) == "unsafe_path"
    assert _code(problem_io.parse_archive, path) == "unsafe_path"


def test_backslash_member_names_are_normalized(tmp_path: Path) -> None:
    """Windows PowerShell 5.1 的 Compress-Archive 写 ``a\\b``：规整成 ``a/b``，按原名读内容。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, data in (("pkg\\", b""), ("pkg\\data\\1.in", b"in"), ("pkg/top.txt", b"top")):
            info = zipfile.ZipInfo("placeholder")
            info.filename = name
            zf.writestr(info, data)
    with SafeArchive(stage(tmp_path, buffer.getvalue())) as archive:
        assert archive.names == ["pkg/data/1.in", "pkg/top.txt"]
        assert archive.is_dir("pkg/data")
        assert archive.read("pkg/data/1.in") == b"in"
        with archive.open("pkg/data/1.in") as handle:
            assert handle.read() == b"in"
        assert archive.size("pkg/data/1.in") == 2
        assert not archive.exists("pkg\\data\\1.in")


def test_names_colliding_after_normalization(tmp_path: Path) -> None:
    path = stage(tmp_path, _raw_zip(["a/b.in", "a\\b.in"]))
    assert _code(SafeArchive, path) == "duplicate_member"
    assert _code(problem_io.parse_archive, path) == "duplicate_member"


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


def _big_text(seed: int) -> str:
    return "".join("%d %d\n" % (i * seed, i) for i in range(4000))


def test_corrupt_case_member_fails_only_that_problem(tmp_path: Path) -> None:
    from problem_io_fixtures import corrupt_member, lemon_task, make_lemon_zip

    from syncoj_server.services.problem_io.build import try_build

    data = make_lemon_zip([lemon_task("game", 2), lemon_task("rect", 2)],
                          extra={"data/game/game1.in": _big_text(7)})
    path = stage(tmp_path, corrupt_member(data, "data/game/game1.in"))
    with problem_io.parse_archive(path) as parsed:
        game, rect = parsed.result.problems
        assert try_build(game, parsed.archive) is None
        assert [e.code for e in game.errors] == ["testdata_invalid"]
        assert try_build(rect, parsed.archive) is not None


def test_corrupt_descriptor_is_a_package_error(tmp_path: Path) -> None:
    from problem_io_fixtures import corrupt_member, make_lemon_zip

    data = make_lemon_zip(extra={"sample.cdf": '{"version": "1.0", "tasks": []%s}' % (" " * 20000)})
    path = stage(tmp_path, corrupt_member(data, "sample.cdf"))
    assert _code(problem_io.parse_archive, path) == "member_corrupt"


def test_corrupt_native_inner_zip(tmp_path: Path) -> None:
    from problem_io_fixtures import corrupt_member

    data = make_native_zip([native_problem("sum", testdata=make_zip({"1.in": _big_text(3), "1.ans": "x"}))])
    path = stage(tmp_path, corrupt_member(data, "sum/testdata.zip"))
    with problem_io.parse_archive(path) as parsed:
        (bundle,) = parsed.result.problems
        assert [e.code for e in bundle.errors] == ["testdata_invalid"]


# 深层嵌套：C 实现的 json 解码器抛 RecursionError（不是 ValueError），要转成 bad_manifest
DEEP_JSON = "[" * 200000 + "]" * 200000


def test_deeply_nested_native_manifest(tmp_path: Path) -> None:
    data = make_native_zip(extra={"syncoj-problems.json": DEEP_JSON})
    assert _code(problem_io.parse_archive, stage(tmp_path, data)) == "bad_manifest"


def test_deeply_nested_cdf(tmp_path: Path) -> None:
    from problem_io_fixtures import make_lemon_zip

    data = make_lemon_zip(cdf=DEEP_JSON.encode())
    assert _code(problem_io.parse_archive, stage(tmp_path, data)) == "bad_manifest"


def test_deeply_nested_hoj_json_fails_only_that_problem(tmp_path: Path) -> None:
    from problem_io_fixtures import hoj_problem_json, make_hoj_zip

    first = hoj_problem_json("P1000")
    first["judgeExtraFile"] = DEEP_JSON  # 顶层也允许是 JSON 字符串
    data = make_hoj_zip([first, hoj_problem_json("P1001")], extra={"problem_2.json": DEEP_JSON})
    with problem_io.parse_archive(stage(tmp_path, data)) as parsed:
        assert parsed.result.format == "hoj"
        ok, bad = parsed.result.problems
        assert not ok.errors
        assert [e.code for e in bad.errors] == ["bad_manifest"]


def test_deeply_nested_qduoj_json_fails_only_that_problem(tmp_path: Path) -> None:
    from problem_io_fixtures import make_qduoj_zip, qduoj_problem_json

    data = make_qduoj_zip([qduoj_problem_json("1001"), qduoj_problem_json("1002")],
                          extra={"2/problem.json": DEEP_JSON})
    with problem_io.parse_archive(stage(tmp_path, data)) as parsed:
        assert parsed.result.format == "qduoj"
        ok, bad = parsed.result.problems
        assert not ok.errors
        assert [e.code for e in bad.errors] == ["bad_manifest"]


def test_deeply_nested_json_is_a_400(client, contest: dict, admin_headers: dict) -> None:
    from problem_io_fixtures import make_lemon_zip

    from test_judge_import_api import preview

    for data in (make_native_zip(extra={"syncoj-problems.json": DEEP_JSON}), make_lemon_zip(cdf=DEEP_JSON.encode())):
        response = preview(client, contest, admin_headers, data)
        assert response.status_code == 400, response.text
        assert response.json()["details"]["code_detail"] == "bad_manifest"
