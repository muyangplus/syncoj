"""导入包的格式识别（services/problem_io/detect.py）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from syncoj_server.services.problem_io import archive as archive_mod
from syncoj_server.services.problem_io.detect import CDF_MAGIC, detect
from syncoj_server.services.problem_io.notices import ProblemIoError

from problem_io_fixtures import (
    make_fps_xml,
    make_fps_zip,
    make_hoj_zip,
    make_hydro_zip,
    make_lemon_zip,
    make_native_zip,
    make_qduoj_zip,
    make_testdata_zip,
    make_zip,
    stage,
)


def _detect(tmp_path: Path, data: bytes, name: str = "upload.zip", forced: str = "auto"):
    path = stage(tmp_path, data, name)
    with archive_mod.open_upload(path, name) as archive:
        fmt, view = detect(archive, forced)
        return fmt, view.root


def _error(tmp_path: Path, data: bytes, name: str = "upload.zip") -> str:
    with pytest.raises(ProblemIoError) as info:
        _detect(tmp_path, data, name)
    return info.value.code


@pytest.mark.parametrize(
    "maker, expected",
    [
        (make_native_zip, "syncoj"),
        (make_hoj_zip, "hoj"),
        (make_lemon_zip, "lemonlime"),
        (make_hydro_zip, "hydro"),
        (make_qduoj_zip, "qduoj"),
        (make_fps_zip, "fps"),
    ],
)
def test_each_format_is_recognised(tmp_path: Path, maker, expected: str) -> None:
    assert _detect(tmp_path, maker()) == (expected, "")


@pytest.mark.parametrize(
    "maker, expected, root",
    [
        (make_native_zip, "syncoj", "外壳/"),
        (make_hoj_zip, "hoj", "外壳/"),
        # .cdf 深度 ≤ 2 即命中原视图；data/ 按 .cdf 所在目录解析，不需要剥壳
        (make_lemon_zip, "lemonlime", ""),
        (make_hydro_zip, "hydro", "外壳/"),
        (make_qduoj_zip, "qduoj", "外壳/"),
        # 只有一个 .xml，在哪一层都一样
        (make_fps_zip, "fps", ""),
    ],
)
def test_a_wrapping_folder_is_stripped(tmp_path: Path, maker, expected: str, root: str) -> None:
    assert _detect(tmp_path, maker(shell="外壳")) == (expected, root)


def test_bare_fps_xml(tmp_path: Path) -> None:
    assert _detect(tmp_path, make_fps_xml(), "problems.xml") == ("fps", "")
    bom = b"\xef\xbb\xbf" + make_fps_xml()
    assert _detect(tmp_path, bom, "problems.xml") == ("fps", "")


def test_hydro_variants(tmp_path: Path) -> None:
    assert _detect(tmp_path, make_hydro_zip(folder=""))[0] == "hydro"
    assert _detect(tmp_path, make_hydro_zip(config_name="config.ini", config="1\n"))[0] == "hydro"
    syzoj = make_zip({"data.yml": "subtasks: []\n", "1.in": "1", "1.out": "1"})
    assert _detect(tmp_path, syzoj)[0] == "hydro"


def test_macos_junk_is_ignored(tmp_path: Path) -> None:
    data = make_hoj_zip(extra={"__MACOSX/._problem_1.json": b"\x00\x05", ".DS_Store": b"\x00"})
    assert _detect(tmp_path, data) == ("hoj", "")
    # 外壳旁边多一个 __MACOSX 也照样剥壳
    data = make_native_zip(shell="pkg", extra={"__MACOSX/pkg/._x": b"\x00"})
    assert _detect(tmp_path, data) == ("syncoj", "pkg/")


def test_plain_testdata_is_redirected(tmp_path: Path) -> None:
    assert _error(tmp_path, make_testdata_zip()) == "plain_testdata"


def test_binary_cdf(tmp_path: Path) -> None:
    assert _error(tmp_path, CDF_MAGIC + b"\x00\x01rest", "sample.cdf") == "cdf_binary"


def test_cdf_without_data(tmp_path: Path) -> None:
    assert _error(tmp_path, b'{"version":"1.0","tasks":[]}', "sample.cdf") == "cdf_without_data"


def test_unknown(tmp_path: Path) -> None:
    assert _error(tmp_path, make_zip({"readme.txt": "hello"})) == "unknown_format"
    assert _error(tmp_path, b"plain text", "x.txt") == "unknown_format"
    assert _error(tmp_path, b"PK\x03\x04garbage") == "bad_zip"


def test_format_parameter_overrides_detection(tmp_path: Path) -> None:
    # 一个同时像 native 又像 hydro 的包：自动识别取 native，显式指定 hydro 时取 hydro
    data = make_native_zip(extra={"problem.yaml": "pid: X\n"})
    assert _detect(tmp_path, data)[0] == "syncoj"
    assert _detect(tmp_path, data, forced="hydro")[0] == "hydro"
    # 指定的格式一条都不命中：仍交给该格式（由解析器报具体错误），取最内层视图
    assert _detect(tmp_path, make_zip({"a.txt": "1"}, shell="s"), forced="hoj") == ("hoj", "s/")


def test_hoj_json_must_have_problem_and_samples(tmp_path: Path) -> None:
    data = make_zip({"x.json": '{"foo": 1}', "x/1.in": "1"})
    assert _error(tmp_path, data) == "unknown_format"


def test_qduoj_requires_its_keys(tmp_path: Path) -> None:
    data = make_zip({"1/problem.json": '{"title": "x"}', "1/testcase/1.in": "1"})
    assert _error(tmp_path, data) == "unknown_format"
