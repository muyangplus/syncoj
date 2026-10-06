"""``bootstrap.sh``：装机/卸载共用的那条自举入口。

两件容易出错、都必须钉住的事：

1. **默认值只有一份**。脚本曾经无条件把 ``/home/student/code`` /
   ``/home/student/exam`` 传给 install.py，把 install.py 里改好的默认（运行账号
   家目录下的桌面）整个覆盖掉 —— 现场扫不到东西，更糟的是那条写死的路径会被塞进
   systemd 单元的沙箱白名单，服务直接 ``226/NAMESPACE`` 起不来。
2. **卸载那条命令不带 ``--server``**（装机页生成的就是它），但 install.py 还是
   得先下下来；要卸载的机器上必然有 ``agent.ini``，就从里面读地址。

测试方式：把 ``curl`` / ``python3`` 换成两个假的脚本，跑真的 ``sh bootstrap.sh``，
断言**拼出来的 install.py 命令行**与**实际请求的下载地址** —— 不联网、不碰 root。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_ROOT = REPO_ROOT / "agent"
if str(AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_ROOT))

BOOTSTRAP = AGENT_ROOT / "packaging" / "bootstrap.sh"

#: 跑 bootstrap.sh 需要一个 POSIX sh；Windows 开发机上不一定有。
SH = shutil.which("sh")
requires_sh = pytest.mark.skipif(SH is None, reason="需要 POSIX sh 才能跑 bootstrap.sh")

_FAKE_CURL = """#!/bin/sh
# 假装把安装器下下来：记下请求的 URL，按 -o <路径> 写一个占位文件（不联网）。
url=""
out=""
while [ $# -gt 0 ]; do
    case "$1" in
        -o) out="$2"; shift 2 ;;
        -*) shift ;;
        *) url="$1"; shift ;;
    esac
done
[ -n "$url" ] && printf '%s\\n' "$url" > "$BOOTSTRAP_CAPTURE_URL"
[ -n "$out" ] && printf '# placeholder installer\\n' > "$out"
exit 0
"""

_FAKE_PYTHON = """#!/bin/sh
# 把 install.py 收到的参数原样记下来，供测试断言。
for a in "$@"; do
    printf '%s\\n' "$a"
done > "$BOOTSTRAP_CAPTURE"
exit 0
"""


def run_bootstrap(
    tmp_path: Path,
    args,
    config_dir: Path = None,
    expect_ok: bool = True,
) -> SimpleNamespace:
    """在只有假 curl / 假 python3 的 PATH 下跑一次 bootstrap.sh。"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    capture = tmp_path / "argv.txt"
    url_capture = tmp_path / "url.txt"

    for name, content in (("curl", _FAKE_CURL), ("python3", _FAKE_PYTHON)):
        target = bin_dir / name
        # 3.8 上 write_text 没有 newline 参数，用 open（check_py38 门禁盯着这条）
        with target.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        os.chmod(str(target), 0o755)

    env = dict(os.environ)
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
    env["BOOTSTRAP_CAPTURE"] = str(capture)
    env["BOOTSTRAP_CAPTURE_URL"] = str(url_capture)
    env["TMPDIR"] = str(tmp_path)
    # 默认 /etc/syncoj 在开发机上不存在（也不该去碰），测试用环境变量指到临时目录
    env.pop("SYNCOJ_CONFIG_DIR", None)
    if config_dir is not None:
        env["SYNCOJ_CONFIG_DIR"] = str(config_dir)

    result = subprocess.run(
        [SH, str(BOOTSTRAP)] + list(args),
        capture_output=True,
        cwd=str(REPO_ROOT),
        env=env,
    )
    stderr = result.stderr.decode("utf-8", "replace")
    argv = capture.read_text(encoding="utf-8").split() if capture.is_file() else None
    url = url_capture.read_text(encoding="utf-8").strip() if url_capture.is_file() else None

    if expect_ok:
        assert result.returncode == 0, stderr
        assert argv is not None, "假 python3 没被调用：%s" % stderr
    return SimpleNamespace(returncode=result.returncode, argv=argv, url=url, stderr=stderr)


def test_脚本里不再写死选手目录() -> None:
    """静态检查：这两条字面量就是现场 226 / 扫不到东西的根因。

    只查**代码行** —— 注释里可以讲这段历史，但绝不能出现在真的会执行的地方。
    """
    text = BOOTSTRAP.read_text(encoding="utf-8")
    code = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    assert "/home/student/code" not in code
    assert "/home/student/exam" not in code


# --------------------------------------------------------------------------- #
# 装机：默认值只由 install.py 持有
# --------------------------------------------------------------------------- #


@requires_sh
def test_只给_server_时不带_scan_root_与_deploy_root(tmp_path: Path) -> None:
    result = run_bootstrap(tmp_path, ["--server", "http://example.invalid"])

    assert "--from-server" in result.argv
    assert "--server" in result.argv
    assert "--scan-root" not in result.argv, result.argv
    assert "--deploy-root" not in result.argv, result.argv
    assert "/home/student" not in " ".join(result.argv)
    # 安装器仍然是从那个服务端取回来的
    assert result.url == "http://example.invalid/api/v1/agent/install/installer"


@requires_sh
def test_显式传了才往后传(tmp_path: Path) -> None:
    result = run_bootstrap(
        tmp_path,
        [
            "--server", "http://example.invalid",
            "--scan-root", "/srv/code",
            "--deploy-root", "/srv/exam",
        ],
    )

    argv = result.argv
    assert argv[argv.index("--scan-root") + 1] == "/srv/code"
    assert argv[argv.index("--deploy-root") + 1] == "/srv/exam"


@requires_sh
def test_只显式传一个时另一个仍然不出现(tmp_path: Path) -> None:
    result = run_bootstrap(
        tmp_path, ["--server", "http://example.invalid", "--scan-root", "/srv/code"]
    )

    assert result.argv[result.argv.index("--scan-root") + 1] == "/srv/code"
    assert "--deploy-root" not in result.argv, result.argv


# --------------------------------------------------------------------------- #
# 卸载：一条命令，不注入安装参数
# --------------------------------------------------------------------------- #


@requires_sh
def test_卸载只透传卸载开关(tmp_path: Path) -> None:
    """```--uninstall --yes``` 时：install.py 收到的只有卸载开关，没有安装那一套。"""
    result = run_bootstrap(
        tmp_path,
        [
            "--server", "http://example.invalid",
            "--uninstall", "--yes", "--dry-run", "--keep-state", "--keep-user",
        ],
    )

    argv = result.argv[1:]  # argv[0] 是下载下来的 install.py 路径
    assert argv == [
        "--uninstall", "--yes", "--dry-run", "--keep-state", "--keep-user"
    ], result.argv
    assert "--from-server" not in result.argv
    assert "--server" not in result.argv
    assert "--scan-root" not in result.argv
    assert "--deploy-root" not in result.argv


@requires_sh
def test_卸载没给_server_就从机器上的_agent_ini_读地址(tmp_path: Path) -> None:
    """装机页生成的那条卸载命令就是不带 --server 的。"""
    config_dir = tmp_path / "etc-syncoj"
    config_dir.mkdir()
    with (config_dir / "agent.ini").open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("[server]\nurl = http://from-config.invalid\n")

    result = run_bootstrap(
        tmp_path, ["--uninstall", "--yes"], config_dir=config_dir
    )

    assert result.url == "http://from-config.invalid/api/v1/agent/install/installer"
    assert result.argv[1:] == ["--uninstall", "--yes"]


@requires_sh
def test_卸载也找不到地址时明确拒绝(tmp_path: Path) -> None:
    result = run_bootstrap(
        tmp_path,
        ["--uninstall", "--yes"],
        config_dir=tmp_path / "not-installed",
        expect_ok=False,
    )

    assert result.returncode == 2
    assert "--server" in result.stderr
    assert result.argv is None, "没地址就不该去跑 install.py"


# --------------------------------------------------------------------------- #
# --help
# --------------------------------------------------------------------------- #


@requires_sh
def test_help_打印完整的用法块(tmp_path: Path) -> None:
    """以前用写死的行号（``2,16p``）打印注释，注释一增删就打印半截。"""
    result = subprocess.run(
        [SH, str(BOOTSTRAP), "--help"],
        capture_output=True,
        cwd=str(REPO_ROOT),
    )
    text = result.stdout.decode("utf-8", "replace")

    assert result.returncode == 0
    assert "--server" in text
    assert "--uninstall" in text
    assert "bootstrap.sh" in text
