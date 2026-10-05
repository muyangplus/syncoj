"""配置解析与策略合并测试。

配置是运维人员唯一直接接触的接口。这里错一点，现场表现是"Agent 起来了但什么都不传"
——最难排查的那类故障。所以宁可把校验做严。
"""

from __future__ import annotations

from pathlib import Path
import re

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


def test_duplicate_root_names_are_rejected_under_auto_prefix(workdir: Path) -> None:
    """前缀取根目录名时，两个同名根目录会让上报路径冲突 —— 必须拒绝。

    注意这只在 ``prefix = auto`` 下成立。默认的 ``prefix = none`` 不加前缀，
    多个根目录互不干扰，重名无所谓。
    """
    from syncoj_agent.main import Agent

    a = workdir / "x" / "code"
    b = workdir / "y" / "code"
    a.mkdir(parents=True)
    b.mkdir(parents=True)

    config = AgentConfig.load(write_config(workdir))
    config.scan_roots = [a, b]
    config.scan_prefix = "auto"

    agent = Agent(config)
    with pytest.raises(ConfigError, match="前缀重复"):
        agent._resolve_roots(player_no="S001")


def test_duplicate_root_names_are_fine_without_prefix(workdir: Path) -> None:
    """不加前缀时多个同名根目录完全没问题 —— 这是默认配置。"""
    from syncoj_agent.main import Agent

    a = workdir / "x" / "code"
    b = workdir / "y" / "code"
    a.mkdir(parents=True)
    b.mkdir(parents=True)

    config = AgentConfig.load(write_config(workdir))
    config.scan_roots = [a, b]
    config.scan_prefix = "none"

    agent = Agent(config)
    roots = agent._resolve_roots(player_no="S001")
    assert [name for name, _root in roots] == ["", ""]


# --------------------------------------------------------------------------- #
# 路径占位符
# --------------------------------------------------------------------------- #


def test_expand_placeholders_for_desktop_and_player() -> None:
    from syncoj_agent.config import expand_placeholders

    desktop = Path("/home/student/桌面")
    text = expand_placeholders(
        "{desktop}/{player_no}", desktop=desktop, player_no="S001"
    )
    # 用 Path 比较而不是字符串：Windows 上 str(Path("/d")) 是 "\\d"，
    # 直接比字符串会得到与实现无关的失败
    assert Path(text) == desktop / "S001"


def test_expand_placeholders_keeps_player_no_when_unknown() -> None:
    """注册之前准考证号还不知道，占位符要**原样保留**而不是报错 ——
    配置校验发生在注册之前。"""
    from syncoj_agent.config import expand_placeholders

    text = expand_placeholders("{desktop}/{player_no}", desktop=Path("/d"))
    assert text.endswith("/{player_no}")
    assert Path(text.split("{player_no}")[0]) == Path("/d")


def test_default_config_uses_desktop_layout() -> None:
    """默认就是 桌面/<准考证号>，且不加前缀（否则 source/ 里准考证号出现两次）。"""
    config = AgentConfig.load(None)
    assert len(config.scan_roots) == 1
    root = str(config.scan_roots[0])
    assert "{player_no}" in root, root
    assert "{desktop}" not in root, "载入时应当已经把 {desktop} 展开了"
    assert config.scan_prefix == "none"
    assert "{desktop}" not in str(config.deploy_root)


def test_resolved_roots_substitutes_player_no(workdir: Path) -> None:
    config = AgentConfig.load(write_config(workdir))
    config.scan_roots = [workdir / "{player_no}" / "code"]

    resolved = config.resolved_roots("S042")
    assert resolved == [workdir / "S042" / "code"]
    assert config.needs_player_no is True


def test_needs_player_no_is_false_for_static_root(workdir: Path) -> None:
    config = AgentConfig.load(write_config(workdir))
    config.scan_roots = [workdir / "code"]
    assert config.needs_player_no is False


def test_prefix_none_produces_bare_relative_paths(workdir: Path) -> None:
    """前缀为空时上报路径就是纯相对路径。"""
    from syncoj_agent.main import join_report_path

    assert join_report_path("", "p1/p1.cpp") == "p1/p1.cpp"
    assert join_report_path("S001", "p1/p1.cpp") == "S001/p1/p1.cpp"


def test_prefix_literal_can_reference_player_no(workdir: Path) -> None:
    from syncoj_agent.main import Agent

    root = workdir / "code"
    root.mkdir()

    config = AgentConfig.load(write_config(workdir))
    config.scan_roots = [root]
    config.scan_prefix = "{player_no}"

    agent = Agent(config)
    roots = agent._resolve_roots(player_no="S007")
    assert roots == [("S007", root)]


def test_prefix_literal_can_reference_contest_slug(workdir: Path) -> None:
    """上报前缀也能用 ``{contest_slug}`` —— 一个考点跑多场次时可以靠它区分。"""
    from syncoj_agent.main import Agent

    root = workdir / "code"
    root.mkdir()

    config = AgentConfig.load(write_config(workdir))
    config.scan_roots = [root]
    config.scan_prefix = "{contest_slug}/{player_no}"

    roots = Agent(config)._resolve_roots(player_no="S007", contest_slug="mock-1")
    assert roots == [("mock-1/S007", root)]


def test_scan_root_with_contest_slug_is_created(workdir: Path) -> None:
    """含 ``{contest_slug}`` 的扫描根同样会被建出来。"""
    from syncoj_agent.main import Agent

    config = AgentConfig.load(write_config(workdir))
    config.scan_roots = [workdir / "{contest_slug}" / "{player_no}"]

    Agent(config)._resolve_roots(player_no="S001", contest_slug="mock-1")
    assert (workdir / "mock-1" / "S001").is_dir()


def test_scan_root_is_created_if_missing(workdir: Path) -> None:
    """扫描根不存在时创建它，而不是报错。

    它是选手的工作目录，开考前可能还没建；报错会在开考前刷一屏"目录不存在"，
    把真正的问题淹掉。
    """
    from syncoj_agent.main import Agent

    missing = workdir / "还没建的目录"
    assert not missing.exists()

    config = AgentConfig.load(write_config(workdir))
    config.scan_roots = [missing]
    config.scan_prefix = "none"

    Agent(config)._resolve_roots(player_no="S001")
    assert missing.is_dir()


def test_templated_root_validation_skips_existence(workdir: Path) -> None:
    """带 {player_no} 的目录没法检查存在性，但父目录要检查。"""
    config = AgentConfig.load(write_config(workdir))
    config.scan_roots = [workdir / "{player_no}"]
    config.validate()  # workdir 存在，不该报错

    config.scan_roots = [workdir / "不存在" / "{player_no}"]
    with pytest.raises(ConfigError, match="父目录"):
        config.validate()


def test_expand_placeholders_home_is_known_immediately() -> None:
    """``{home}`` 和 ``{desktop}`` 一样，载入配置时就能展开。"""
    from syncoj_agent.config import expand_placeholders

    text = expand_placeholders("{home}/code", home=Path("/home/student"))
    assert Path(text) == Path("/home/student/code")


def test_expand_placeholders_contest_slug(workdir: Path) -> None:
    from syncoj_agent.config import expand_placeholders

    text = expand_placeholders(
        "{desktop}/{contest_slug}/{player_no}",
        desktop=Path("/d"),
        player_no="S001",
        contest_slug="mock-1",
    )
    assert Path(text) == Path("/d/mock-1/S001")

    # 场次标识未知时同样原样保留
    pending = expand_placeholders("{desktop}/{contest_slug}", desktop=Path("/d"))
    assert pending.endswith("{contest_slug}")


def test_resolved_roots_substitutes_contest_slug(workdir: Path) -> None:
    config = AgentConfig.load(write_config(workdir))
    config.scan_roots = [workdir / "{contest_slug}" / "{player_no}" / "code"]

    resolved = config.resolved_roots("S042", "mock-1")
    assert resolved == [workdir / "mock-1" / "S042" / "code"]
    assert config.needs_credential is True


def test_needs_credential_is_false_for_static_root(workdir: Path) -> None:
    """全是静态路径（或用 {home}/{desktop}）时，不需要等注册就能定位目录。"""
    config = AgentConfig.load(write_config(workdir))
    config.scan_roots = [workdir / "code"]
    assert config.needs_credential is False

    config.scan_roots = [workdir / "{home}" / "code"]  # 已经展开成绝对路径了
    assert config.needs_credential is False


def test_contest_slug_root_validation_checks_leftmost_prefix(workdir: Path) -> None:
    """两个待展开占位符时，检查**最靠左**那个左边的目录。

    取最右边那个的左边，会连带检查一段本身还不存在的路径，于是"父目录不存在"
    这种错误永远报不出来。
    """
    config = AgentConfig.load(write_config(workdir))
    config.scan_roots = [workdir / "{contest_slug}" / "{player_no}"]
    config.validate()

    config.scan_roots = [workdir / "不存在" / "{contest_slug}" / "{player_no}"]
    with pytest.raises(ConfigError, match="父目录"):
        config.validate()


def test_env_scan_roots_are_templated_and_are_real_paths(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``SYNCOJ_SCAN_ROOTS`` 注入的路径也要走同一条模板展开路径。

    早先这里直接塞字符串进来，于是 validate() 会在 str 上调 .is_absolute()
    直接崩 —— 而镜像预装时用环境变量注入恰恰是最常见的方式。
    """
    code = workdir / "code"
    code.mkdir()
    monkeypatch.setenv("SYNCOJ_SCAN_ROOTS", "%s,%s/{player_no}" % (code, workdir))

    config = AgentConfig.load(write_config(workdir))
    assert all(hasattr(root, "is_absolute") for root in config.scan_roots)
    assert all(root.is_absolute() for root in config.scan_roots)
    assert "{desktop}" not in " ".join(str(r) for r in config.scan_roots)
    config.validate()  # 不能抛 AttributeError


def test_env_scan_roots_expand_desktop_and_home(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from syncoj_agent.config import PLACEHOLDER_DESKTOP, PLACEHOLDER_HOME

    monkeypatch.setenv(
        "SYNCOJ_SCAN_ROOTS", "%s/code,%s/code2" % (PLACEHOLDER_DESKTOP, PLACEHOLDER_HOME)
    )
    config = AgentConfig.load(write_config(workdir))

    joined = " ".join(str(root) for root in config.scan_roots)
    assert PLACEHOLDER_DESKTOP not in joined
    assert PLACEHOLDER_HOME not in joined


def test_shipped_example_config_is_loadable() -> None:
    """仓库里那份 config.example.ini 必须真的能被读进来。

    它是最多人照抄的东西，却最容易腐烂 —— 没人 import 它，改代码时也想不到
    它。这里把它当成契约来守。
    """
    example = Path(__file__).resolve().parents[1] / "config.example.ini"
    assert example.is_file(), "示例配置不见了"

    config = AgentConfig.load(example)
    assert config.server_url.startswith("https://")
    assert config.scan_roots, "示例里至少得有一个扫描目录"
    assert config.deploy_root.is_absolute()


def test_shipped_example_config_uses_only_known_placeholders() -> None:
    """示例里出现的每个 ``{xxx}`` 都必须是真占位符。

    写错一个字母（``{player_no}`` 写成 ``{player}``）不会报错，只会变成一个字面
    目录名 —— 而且是在考场上才发现扫不到文件。这里把它挡在提交之前。
    """
    from syncoj_agent.config import CREDENTIAL_PLACEHOLDERS, PLACEHOLDER_DESKTOP, PLACEHOLDER_HOME

    example = Path(__file__).resolve().parents[1] / "config.example.ini"
    text = example.read_text(encoding="utf-8")

    # 只看非注释行 —— 注释里会举反例、写说明
    active = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith((";", "#"))
    )
    known = {PLACEHOLDER_DESKTOP, PLACEHOLDER_HOME} | set(CREDENTIAL_PLACEHOLDERS)
    used = set(re.findall(r"\{[a-z_]+\}", active))
    assert used <= known, "示例配置里出现了未定义的占位符：%s" % (used - known)


def test_builtin_default_config_passes_check(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """README 的「快速开始」里那条 ``run_agent.py --check`` 必须真的能过。

    它用的是内置默认配置。这条路径最容易腐烂：模板里改个字段名、加一条必填的
    校验，README 不会跟着变，而新人的第一印象就是"这玩意儿装不起来"。

    ``SYNCOJ_STATE_DIR`` 指向临时目录 —— 默认的 ``/var/lib/syncoj`` 在开发机上
    会落到盘根，测试不该在仓库外面拉屎。
    """
    from syncoj_agent.main import main

    monkeypatch.setenv("SYNCOJ_STATE_DIR", str(workdir / "state"))
    assert main(["--check"]) == 0


def test_default_ini_is_the_source_of_truth_for_the_runbook() -> None:
    """README 里承诺的默认值要和内置模板对得上。"""
    from syncoj_agent.config import DEFAULT_INI

    config = AgentConfig.load(None)
    assert config.scan_prefix == "none"
    # deploy_root 默认为桌面，且载入时就展开
    assert config.deploy_root.is_absolute()
    assert "{desktop}" not in str(config.deploy_root)
    # 默认不启用自更新 —— 静默升级是高风险动作
    assert "mode = off" in DEFAULT_INI
    assert config.upgrade_mode == "off"


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
