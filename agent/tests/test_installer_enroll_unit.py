"""注册单元 ``syncoj-agent-enroll.service``：**有限重试**，而且失败要能照着修。

为什么重试写在单元自己的 ``ExecStart`` 里、而不是用 systemd 的重启策略：

* ``Type=oneshot`` 配重启策略在各 systemd 版本上行为不一致；
* 一个 ``while`` 循环在哪都能跑，而且它能自己数清"试了几次"；
* 真机上见过"重启 249 次"把日志刷满，``systemctl status`` 里反而看不清原因
  —— 所以必须有上限，并且失败时给出**能直接抄的手工步骤**。

这些用例既审渲染出来的文本，也**真的用 POSIX ``sh`` 把那串循环跑一遍**
（``sh`` 不在就跳过）：光看文本很容易写出一个"看起来对、实际永不退出"的循环。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_ROOT = REPO_ROOT / "agent"
if str(AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_ROOT))

PREFIX = Path("/opt/syncoj")
CONFIG = Path("/etc/syncoj/agent.ini")
STATE = Path("/var/lib/syncoj")
ENROLL_UNIT = "syncoj-agent-enroll.service"

needs_sh = pytest.mark.skipif(shutil.which("sh") is None, reason="需要 POSIX sh")


def render(installer_module) -> str:
    return installer_module.render_enroll_unit(
        prefix=PREFIX,
        config_path=CONFIG,
        state_dir=STATE,
        run_user="noi",
        python="/usr/bin/python3",
    )


def exec_start_line(text: str) -> str:
    lines = [line for line in text.splitlines() if line.startswith("ExecStart=")]
    assert len(lines) == 1, lines
    return lines[0]


def inner_shell(text: str) -> str:
    """把 ``ExecStart=/bin/sh -c '<这里>'`` 里那段 shell 抠出来。"""
    body = exec_start_line(text)[len("ExecStart=") :]
    assert body.startswith("/bin/sh -c '"), body
    assert body.endswith("'"), body
    inner = body[len("/bin/sh -c '") : -1]
    assert "'" not in inner, "外层用单引号包着，里面不能再出现单引号"
    return inner


# --------------------------------------------------------------------------- #
# 文本：有限重试 + 可执行的失败文案
# --------------------------------------------------------------------------- #


def test_注册单元里的重试有上限(installer_module) -> None:
    text = render(installer_module)
    retries = installer_module.ENROLL_RETRIES
    delay = installer_module.ENROLL_RETRY_DELAY_SECONDS

    assert "/bin/sh -c" in exec_start_line(text), "没有套一层 shell 做重试"
    assert "attempt=$((attempt + 1))" in text
    assert "[ \"$attempt\" -ge %d ]" % retries in text, "重试次数没有上限"
    assert "sleep %d" % delay in text
    # 上限必须是个小数字：真机上"重启 249 次"就是这么来的
    assert 1 < retries <= 10


def test_失败文案给出能直接抄的手工步骤(installer_module) -> None:
    text = render(installer_module)

    assert "已达上限" in text
    for step in (
        "systemctl reset-failed %s" % ENROLL_UNIT,
        "systemctl start %s" % ENROLL_UNIT,
    ):
        assert step in text, "失败文案里没有可执行的下一步：%s" % step
    assert "bootstrap.key" in text, "要提示去检查统一密钥"
    assert "/etc/syncoj/agent.ini" in text, "要提示去检查配置"


def test_单元自己算得清超时时间(installer_module) -> None:
    """几次尝试加上网络超时可能超过默认的 90s —— 别让 systemd 中途把它杀掉。"""
    text = render(installer_module)
    timeout = [line for line in text.splitlines() if line.startswith("TimeoutStartSec=")]
    assert timeout, "注册单元没有显式的 TimeoutStartSec"

    value = int(timeout[0].split("=", 1)[1])
    worst = installer_module.ENROLL_RETRY_DELAY_SECONDS * (
        installer_module.ENROLL_RETRIES - 1
    )
    assert value > worst, "超时时间没覆盖重试间隔：%s <= %s" % (value, worst)


# --------------------------------------------------------------------------- #
# 真的把它跑起来
# --------------------------------------------------------------------------- #


def build_loop(installer_module, tmp: Path, ok_at: int) -> "tuple":
    """造一个"前 N 次失败、第 ok_at 次成功"的假 Agent，返回可执行的 shell 脚本。"""
    counter = tmp / "counter"
    fake_agent = tmp / "fake_agent.sh"
    with fake_agent.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(
            "#!/bin/sh\n"
            "n=$(cat %s 2>/dev/null || echo 0)\n"
            "n=$((n + 1))\n"
            "echo \"$n\" > %s\n"
            "if [ \"$n\" -lt %d ]; then echo boom >&2; exit 1; fi\n"
            "echo ok\n" % (counter.as_posix(), counter.as_posix(), ok_at)
        )

    text = render(installer_module)
    inner = inner_shell(text)
    real_command = (
        "/usr/bin/python3 -E -s /opt/syncoj/current/run_agent.py "
        "--config /etc/syncoj/agent.ini --provision --chown-to noi"
    )
    assert real_command in inner, inner
    shell_script = tmp / "enroll.sh"
    with shell_script.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(inner.replace(real_command, "/bin/sh " + fake_agent.as_posix()) + "\n")
    return shell_script, counter


def run_loop(installer_module, tmp: Path, ok_at: int) -> "tuple":
    # 间隔压到 0 只为跑得快：被执行的循环逻辑与真机完全一样
    monkey = installer_module.ENROLL_RETRY_DELAY_SECONDS
    installer_module.ENROLL_RETRY_DELAY_SECONDS = 0
    try:
        script, counter = build_loop(installer_module, tmp, ok_at)
        proc = subprocess.run(
            ["sh", script.as_posix()],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    finally:
        installer_module.ENROLL_RETRY_DELAY_SECONDS = monkey
    attempts = int(counter.read_text(encoding="utf-8").strip() or 0)
    return proc.returncode, proc.stderr, attempts


@needs_sh
def test_第一次就成功时只跑一次(installer_module, workdir: Path) -> None:
    code, _err, attempts = run_loop(installer_module, workdir, ok_at=1)

    assert code == 0
    assert attempts == 1


@needs_sh
def test_失败之后会重试到成功(installer_module, workdir: Path) -> None:
    retries = installer_module.ENROLL_RETRIES
    code, _err, attempts = run_loop(installer_module, workdir, ok_at=retries)

    assert code == 0, "第 %d 次成功却仍然失败" % retries
    assert attempts == retries


@needs_sh
def test_一直失败时在_上限_处停下并说清下一步(installer_module, workdir: Path) -> None:
    """**这是这条契约的核心**：有限次、退出码非 0、文案可照抄。"""
    retries = installer_module.ENROLL_RETRIES
    code, err, attempts = run_loop(installer_module, workdir, ok_at=retries + 99)

    assert code == 1, "一直失败必须以非 0 退出（systemd 才知道它失败了）"
    assert attempts == retries, "重试次数没有被上限卡住：%d 次" % attempts
    assert "已达上限" in err
    assert "systemctl start %s" % ENROLL_UNIT in err
