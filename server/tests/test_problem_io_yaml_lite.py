"""受限 YAML 子集（services/problem_io/yaml_lite.py）。"""

from __future__ import annotations

import pytest

from syncoj_server.services.problem_io import yaml_lite
from syncoj_server.services.problem_io.yaml_lite import YamlError

from problem_io_fixtures import HYDRO_CONFIG

HYDRO_FULL = """---
# Hydro 的 config.yaml
type: default
time: 1s
memory: 256m          # 行尾注释
filename: "aplusb"
checker_type: testlib
checker: checker.cpp
judge_extra_files:
  - testlib.h
subtasks:
  - id: 1
    score: 30
    type: min
    cases:
      - input: 1-1.in
        output: 1-1.out
      - {input: 1-2.in, output: '1-2.out'}
  - id: 2
    score: 70
    if: [1]
    time: 2s
    cases: [
      {input: 2-1.in, output: 2-1.out},
      {input: 2-2.in, output: 2-2.out}
    ]
langs:
- cc.cc17o2
- cc17
"""


def test_hydro_config_parses() -> None:
    data = yaml_lite.load(HYDRO_FULL)
    assert data["type"] == "default"
    assert data["time"] == "1s" and data["memory"] == "256m"
    assert data["filename"] == "aplusb"
    assert data["judge_extra_files"] == ["testlib.h"]
    first, second = data["subtasks"]
    assert first == {
        "id": 1, "score": 30, "type": "min",
        "cases": [{"input": "1-1.in", "output": "1-1.out"}, {"input": "1-2.in", "output": "1-2.out"}],
    }
    assert second["if"] == [1]
    assert second["cases"][1] == {"input": "2-2.in", "output": "2-2.out"}
    assert data["langs"] == ["cc.cc17o2", "cc17"]


def test_fixture_config_parses() -> None:
    assert len(yaml_lite.load(HYDRO_CONFIG)["subtasks"]) == 2


@pytest.mark.parametrize(
    "text, expected",
    [
        ("a: 1", {"a": 1}),
        ("a: -2", {"a": -2}),
        ("a: 1.5", {"a": 1.5}),
        ("a: 1e-6", {"a": 1e-6}),
        ("a: true\nb: False", {"a": True, "b": False}),
        ("a: null\nb: ~\nc:", {"a": None, "b": None, "c": None}),
        ("a: '1'", {"a": "1"}),
        ('a: "x\\ty"', {"a": "x\ty"}),
        ("a: 'it''s'", {"a": "it's"}),
        ("a: x # 注释", {"a": "x"}),
        ("a: 'x # 不是注释'", {"a": "x # 不是注释"}),
        ("a: http://x/y", {"a": "http://x/y"}),
        ("'a b': 1", {"a b": 1}),
        ("- 1\n- [2, 3]\n- {}", [1, [2, 3], {}]),
        ("a:\n- 1\n- 2\nb: 3", {"a": [1, 2], "b": 3}),
        ("cases: [1, 2]", {"cases": [1, 2]}),
        ("", None),
    ],
)
def test_scalars_and_collections(text: str, expected) -> None:
    assert yaml_lite.load(text) == expected


@pytest.mark.parametrize(
    "text, line",
    [
        ("a: &anchor 1", 1),
        ("a: 1\nb: *anchor", 2),
        ("a: !!str 1", 1),
        ("a: |\n  text", 1),
        ("a: >\n  text", 1),
        ("? complex\n: key", 1),
        ("%YAML 1.2\n---\na: 1", 1),
        ("a: 1\n---\nb: 2", 2),
        ("a: plain\n  continued", 2),
    ],
)
def test_unsupported_syntax_is_rejected_with_line(text: str, line: int) -> None:
    with pytest.raises(YamlError) as info:
        yaml_lite.load(text)
    assert info.value.code == "yaml_unsupported"
    assert info.value.line == line


@pytest.mark.parametrize(
    "text",
    [
        "a: [1, 2",
        "a: \"unterminated",
        "a: 1\na: 2",
        "a: 1\n  - b",
        "\ta: 1",
    ],
)
def test_invalid_yaml_is_rejected(text: str) -> None:
    with pytest.raises(YamlError):
        yaml_lite.load(text)


def test_depth_limit() -> None:
    text = "a: " + "[" * 40 + "]" * 40
    with pytest.raises(YamlError):
        yaml_lite.load(text)


def test_size_limit() -> None:
    with pytest.raises(YamlError):
        yaml_lite.load("a: " + "x" * (yaml_lite.MAX_INPUT_BYTES + 1))


def test_dump_round_trips() -> None:
    value = {
        "time": "1000ms",
        "memory": "256m",
        "filename": "a b:#c",
        "checker_type": "testlib",
        "subtasks": [
            {"id": 1, "score": 30, "type": "min", "cases": [{"input": "1.in", "output": "1.out"}]},
            {"id": 2, "score": 70, "type": "min", "cases": [], "if": [1]},
        ],
        "tag": [],
        "empty": {},
        "n": None,
        "ok": True,
        "eps": 1e-6,
        "中文键": "值\n换行",
        "true": "看起来像布尔的键",
    }
    text = yaml_lite.dump(value)
    assert yaml_lite.load(text) == value
    assert 'time: "1000ms"' in text  # 字符串一律 JSON 风格双引号
    assert yaml_lite.load(yaml_lite.dump([1, {"a": [1, 2]}, [3]])) == [1, {"a": [1, 2]}, [3]]
