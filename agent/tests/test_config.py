"""配置解析与策略合并测试。

配置是运维人员唯一直接接触的接口。这里错一点，现场表现是"Agent 起来了但什么都不传"
——最难排查的那类故障。所以宁可把校验做严。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import make_tree
from syncoj_agent.config import AgentConfig, ConfigError
from syncoj_agent.policy import DEFAULT_POLICY, merge_policy

MINIMAL = """\
[server]
url = https://10.0.0.1:8443
verify_tls = true
ca_file =

[agent]
enroll_code = AAAA-BBBB
state_dir = {state_dir}
deploy_root = {deploy_root}

[scan]
roots = {roots}
interval = 20
max_file_size = 4096

[log]
level = DEBUG
file =
to_stderr = false
"""


def write_config(workdir: Path, **values) -> Path:
    """在 workdir 里铺好目录并写出配置文件。"""
    state_dir = workdir / "state"
    deploy_root = workdir / "deploy"
    code_dir = workdir / "code"
    for path in (state_dir, deploy_root, code_dir):
        path.mkdir(parents=True, exist_ok=True)

    values.setdefault("state_dir", state_dir.as_posix())
    values.setdefault("deploy_root", deploy_root.as_posix())
    values.setdefault("roots", code_dir.as_posix())

    path = workdir / "agent.ini"
    path.write_text(MINIMAL.format(**values), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #


def test_parses_explicit_config(workdir: Path) -> None:
    path = write_config(workdir)
    config = AgentConfig.load(path)

    assert config.server_url == "https://10.0.0.1:8443"
    assert config.verify_tls is True
    assert config.enroll_code == "AAAA-BBBB"
    assert config.scan_interval == 20
    assert config.max_file_size == 4096
    assert config.log_level == "DEBUG"
    assert len(config.scan_roots) == 1


def test_defaults_load_without_file() -> None:
    """不带 --config 时必须能起来 —— 否则 Agent 无法用于自检排障。"""
    config = AgentConfig.load(None)
    assert config.server_url.startswith("http")
    assert config.state_dir
    assert config.deploy_root


def test_missing_config_file_raises(workdir: Path) -> None:
    with pytest.raises(ConfigError):
        AgentConfig.load(workdir / "nope.ini")


def test_multiple_roots_comma_separated(workdir: Path) -> None:
    a = workdir / "code"
    b = workdir / "backup"
    a.mkdir(exist_ok=True)
    b.mkdir(exist_ok=True)
    path = write_config(workdir, roots="%s, %s" % (a.as_posix(), b.as_posix()))

    config = AgentConfig.load(path)
    assert config.scan_roots == [a, b]


def test_multiple_roots_newline_separated(workdir: Path) -> None:
    a = workdir / "code"
    b = workdir / "backup"
    a.mkdir(exist_ok=True)
    b.mkdir(exist_ok=True)
    config_dir = workdir / "cfg"
    config_dir.mkdir()

    path = write_config(workdir, roots="\n  %s\n  %s\n" % (a.as_posix(), b.as_posix()))
    config = AgentConfig.load(path)
    assert config.scan_roots == [a, b]


def test_derived_paths(workdir: Path) -> None:
    config = AgentConfig.load(write_config(workdir))
    assert config.credential_path == config.state_dir / "credential.json"
    assert config.hash_cache_path == config.state_dir / "hash_cache.json"
    assert config.resolved_log_file == config.state_dir / "agent.log"
    assert config.base_url == config.server_url.rstrip("/")


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #


def test_validate_accepts_good_config(workdir: Path) -> None:
    AgentConfig.load(write_config(workdir)).validate()


def test_validate_rejects_relative_scan_root(workdir: Path) -> None:
    config = AgentConfig.load(write_config(workdir))
    config.scan_roots = [Path("relative/code")]
    with pytest.raises(ConfigError, match="绝对路径"):
        config.validate()


def test_validate_rejects_missing_scan_root(workdir: Path) -> None:
    config = AgentConfig.load(write_config(workdir))
    config.scan_roots = [workdir / "does-not-exist"]
    with pytest.raises(ConfigError, match="不存在"):
        config.validate()


def test_validate_rejects_state_dir_inside_scan_root(workdir: Path) -> None:
    """状态目录若在扫描根之内，Agent 会追着自己写的日志和哈希缓存传，
    形成自我放大的循环。必须在启动时就拦住。"""
    config = AgentConfig.load(write_config(workdir))
    config.state_dir = config.scan_roots[0] / "state"
    with pytest.raises(ConfigError, match="不能位于扫描目录"):
        config.validate()


def test_validate_rejects_bad_url_scheme(workdir: Path) -> None:
    config = AgentConfig.load(write_config(workdir))
    config.server_url = "ftp://example.com"
    with pytest.raises(ConfigError, match="http"):
        config.validate()


def test_validate_rejects_missing_ca_file(workdir: Path) -> None:
    config = AgentConfig.load(write_config(workdir))
    config.verify_tls = True
    config.ca_file = workdir / "missing-ca.pem"
    with pytest.raises(ConfigError, match="ca_file"):
        config.validate()


def test_validate_rejects_too_small_interval(workdir: Path) -> None:
    config = AgentConfig.load(write_config(workdir))
    config.scan_interval = 1
    with pytest.raises(ConfigError, match="5"):
        config.validate()


def test_validate_rejects_relative_deploy_root(workdir: Path) -> None:
    config = AgentConfig.load(write_config(workdir))
    config.deploy_root = Path("relative/deploy")
    with pytest.raises(ConfigError, match="deploy_root"):
        config.validate()


def test_validate_reports_all_problems_at_once(workdir: Path) -> None:
    """一次列全所有问题，而不是让人改一个跑一次。"""
    config = AgentConfig.load(write_config(workdir))
    config.scan_roots = []
    config.scan_interval = 1
    config.server_url = "nonsense"

    with pytest.raises(ConfigError) as excinfo:
        config.validate()
    message = str(excinfo.value)
    assert "scan.roots" in message
    assert "scan.interval" in message
    assert "server.url" in message


# --------------------------------------------------------------------------- #
# 扫描根重名
# --------------------------------------------------------------------------- #


def test_duplicate_root_names_are_rejected(workdir: Path) -> None:
    """两个根目录同名会让上报路径前缀冲突，必须拒绝并给出可操作的错误。"""
    from syncoj_agent.main import Agent

    a = workdir / "x" / "code"
    b = workdir / "y" / "code"
    a.mkdir(parents=True)
    b.mkdir(parents=True)

    config = AgentConfig.load(write_config(workdir))
    config.scan_roots = [a, b]

    with pytest.raises(ConfigError, match="重名"):
        Agent(config)


# --------------------------------------------------------------------------- #
# 策略合并
# --------------------------------------------------------------------------- #


def test_merge_policy_uses_remote_values() -> None:
    merged = merge_policy(
        {
            "extensions": [".CPP", ".PAS"],
            "max_file_size": 123,
            "scan_interval": 5,
            "max_files": 10,
        }
    )
    assert merged["extensions"] == [".cpp", ".pas"], "扩展名应统一转小写"
    assert merged["max_file_size"] == 123
    assert merged["scan_interval"] == 5


def test_merge_policy_falls_back_per_key_not_wholesale() -> None:
    """某一项不合法只回退该项 —— 否则服务端一个字段的疏漏会让整个策略退化。"""
    merged = merge_policy(
        {
            "extensions": [".cpp"],          # 合法
            "max_file_size": -1,             # 非法 -> 回退
            "scan_interval": "sixty",        # 非法 -> 回退
        }
    )
    assert merged["extensions"] == [".cpp"]
    assert merged["max_file_size"] == DEFAULT_POLICY["max_file_size"]
    assert merged["scan_interval"] == DEFAULT_POLICY["scan_interval"]


def test_merge_policy_ignores_empty_and_wrong_types() -> None:
    for bad in ({}, None, [], "string", 42):
        merged = merge_policy(bad)
        assert merged["extensions"] == DEFAULT_POLICY["extensions"]


def test_merge_policy_rejects_empty_extension_list() -> None:
    """空扩展名列表意味着"什么也不回收"，几乎肯定是配置事故而非本意，回退更安全。"""
    merged = merge_policy({"extensions": []})
    assert merged["extensions"] == DEFAULT_POLICY["extensions"]


def test_merge_policy_rejects_bool_as_number() -> None:
    """Python 里 True 是 int 的子类，不显式排除的话会被当成 1。"""
    merged = merge_policy({"max_file_size": True})
    assert merged["max_file_size"] == DEFAULT_POLICY["max_file_size"]
