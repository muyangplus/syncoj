#!/usr/bin/env python3
"""systemd 245 冒烟：在 Ubuntu 20.04 容器（真 systemd 当 PID 1）里把 Agent 的一生走一遍。

    python scripts/smoke_systemd245.py            # 需要 docker 与已安装的服务端（pip install -e server）
    python scripts/smoke_systemd245.py --keep     # 失败后留着容器与临时目录，便于进去看

托管 CI runner 的 systemd 不是 NOI Linux 2.0 的 245，单元加固、NoNewPrivileges、路径单元
这些行为只能在 245 上证明。这里在宿主上起一台真服务端，容器用宿主网络连它，依次验证：

1. **在 umask 0 下安装**（docker exec / 某些镜像构建器的现实）：服务起来、以运行账号跑、
   ``NoNewPrivileges=yes``；/opt/syncoj、/etc/syncoj、/usr/lib/syncoj 不对组 / 其他人可写；
2. 日志属运行账号且在写；桌面上出现配对码；
3. root 跑的注册单元不顺着选手放在状态目录里的符号链接写文件、改属主；
4. 服务端铺开新版本 → Agent 验签后交给 root 管理单元 → 切换并以新版本重启；新版本平稳
   跑几轮后经交接报告，root 清掉回滚状态；
5. 再铺开一个**一启动就崩**的版本 → 单元的 ``ExecStartPre=-+`` 启动守卫（root）数够次数后
   切回上一版，服务恢复；
6. 服务端换端口重启并关掉局域网发现 → 机器只能靠主动通告（§11）+ 挑战应答转过去；
7. 服务端授权卸载（机器未配对）→ root 管理单元验令牌后卸干净，运行账号与家目录不动；
8. 评测机（§12.10）：一键安装（rlimit，服务以 root 跑）→ 在线 → 管理端「卸载」→ 评测机告别、
   写标记退出 → 单元的 ``ExecStopPost=+uninstall.sh --if-requested`` 经 ``systemd-run`` 卸干净
   （单元、程序、令牌、缓存、两个账号）。

本脚本只用标准库，跑在宿主上。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Callable, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
IMAGE = "syncoj-smoke:ubuntu20.04"
CONTAINER = "syncoj-smoke-245"
RUN_USER = "noi"
ADMIN_PASSWORD = "smoke-admin-pass"


class SmokeFailed(Exception):
    pass


def log(text: str) -> None:
    print("==> %s" % text, flush=True)


def run(argv: List[str], check: bool = True, **kwargs) -> subprocess.CompletedProcess:
    result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            universal_newlines=True, **kwargs)
    if check and result.returncode != 0:
        raise SmokeFailed("命令失败（%d）：%s\n%s" % (result.returncode, " ".join(argv), result.stdout))
    return result


def sh(script: str, check: bool = True) -> str:
    """在容器里以 root 跑一段 shell。"""
    return run(["docker", "exec", CONTAINER, "bash", "-c", script], check=check).stdout


def wait_for(what: str, probe: Callable[[], bool], timeout: float = 120.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if probe():
            return
        time.sleep(2)
    raise SmokeFailed("等了 %.0f 秒还没等到：%s" % (timeout, what))


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailed(message)
    print("    ✓ %s" % message, flush=True)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def bump(version: str) -> str:
    parts = version.split(".")
    parts[-1] = str(int(parts[-1]) + 1)
    return ".".join(parts)


class Admin:
    def __init__(self, base: str) -> None:
        self.base = base
        self.token = self._call("POST", "/login", {"username": "admin", "password": ADMIN_PASSWORD})["token"]

    def _call(self, method: str, path: str, body=None, raw: Optional[bytes] = None,
              content_type: str = "application/json"):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        request = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", content_type)
        if getattr(self, "token", None):
            request.add_header("Authorization", "Bearer " + self.token)
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))

    def upload_release(self, bundle: Path, version: str) -> int:
        boundary = "syncojsmoke%d" % os.getpid()
        parts = []
        for name, value in (("version", version),):
            parts.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                          % (boundary, name, value)).encode())
        parts.append(("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"%s\"\r\n"
                      "Content-Type: application/gzip\r\n\r\n" % (boundary, bundle.name)).encode())
        parts.append(bundle.read_bytes())
        parts.append(("\r\n--%s--\r\n" % boundary).encode())
        release = self._call("POST", "/releases", raw=b"".join(parts),
                             content_type="multipart/form-data; boundary=%s" % boundary)
        return int(release["id"])

    def rollout(self, release_id: int) -> None:
        self._call("POST", "/releases/%d/rollout" % release_id)

    def pending_by_uuid(self, machine_uuid: str) -> Optional[dict]:
        for item in self._call("GET", "/machines/pending?limit=200")["items"]:
            if item.get("machine_uuid") == machine_uuid:
                return item
        return None

    def uninstall(self, agent_id: int, hostname: str) -> None:
        from urllib.parse import quote

        self._call("POST", "/agents/%d/uninstall?confirm=%s" % (agent_id, quote(hostname)))

    def judge_workers(self) -> dict:
        return {item["name"]: item for item in self._call("GET", "/judge-workers?limit=100")["items"]}

    def create_judge_worker(self, name: str) -> dict:
        return self._call("POST", "/judge-workers", {"name": name})

    def uninstall_judge_worker(self, worker_id: int, name: str) -> None:
        from urllib.parse import quote

        self._call("POST", "/judge-workers/%d/uninstall?confirm=%s" % (worker_id, quote(name)))


def build_bundles(work: Path, server_url: str, key_dir: Path):
    from importlib import util

    spec = util.spec_from_file_location("build_bundle", REPO_ROOT / "agent" / "packaging" / "build_bundle.py")
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    base_version = module.read_version(REPO_ROOT / "agent")
    next_version = bump(base_version)

    common = ["--server-url", server_url, "--public-key", str(key_dir / "release-key.pub.json")]
    base = work / ("agent-%s.tar.gz" % base_version)
    run([sys.executable, str(REPO_ROOT / "agent/packaging/build_bundle.py"), "--out", str(base)] + common)

    source = work / "agent-next"
    shutil.copytree(str(REPO_ROOT / "agent" / "syncoj_agent"), str(source / "syncoj_agent"),
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(str(REPO_ROOT / "agent" / "run_agent.py"), str(source / "run_agent.py"))
    init = source / "syncoj_agent" / "__init__.py"
    init.write_text(init.read_text(encoding="utf-8").replace(
        '__version__ = "%s"' % base_version, '__version__ = "%s"' % next_version), encoding="utf-8")
    upgrade = work / ("agent-%s.tar.gz" % next_version)
    run([sys.executable, str(REPO_ROOT / "agent/packaging/build_bundle.py"),
         "--agent-root", str(source), "--out", str(upgrade)] + common)

    # 一启动就崩的版本：签名、校验都没问题，就是起不来 —— 启动守卫要把它切回去
    broken_version = bump(next_version)
    init.write_text(init.read_text(encoding="utf-8").replace(
        '__version__ = "%s"' % next_version, '__version__ = "%s"' % broken_version), encoding="utf-8")
    launcher = source / "run_agent.py"
    launcher.write_text("import sys\nsys.exit('冒烟：故意起不来')\n", encoding="utf-8")
    broken = work / ("agent-%s.tar.gz" % broken_version)
    run([sys.executable, str(REPO_ROOT / "agent/packaging/build_bundle.py"),
         "--agent-root", str(source), "--out", str(broken)] + common)
    return base, upgrade, next_version, broken, broken_version


def start_container(work: Path) -> None:
    run(["docker", "rm", "-f", CONTAINER], check=False)
    run(["docker", "run", "-d", "--name", CONTAINER, "--privileged", "--cgroupns=host",
         "--network", "host", "-v", "/sys/fs/cgroup:/sys/fs/cgroup:rw",
         "--tmpfs", "/run", "--tmpfs", "/run/lock",
         "-v", "%s:/src:ro" % REPO_ROOT, "-v", "%s:/work:ro" % work, IMAGE])

    def booted() -> bool:
        state = sh("systemctl is-system-running 2>/dev/null || true", check=False).strip()
        return state in ("running", "degraded")

    wait_for("容器里的 systemd 启动完成", booted, timeout=90)
    if "degraded" in sh("systemctl is-system-running || true", check=False):
        print(sh("systemctl --failed --no-pager || true", check=False))
    log(sh("systemctl --version | head -1").strip())


def smoke(work: Path) -> None:
    key_dir = work / "key"
    os.environ["SYNCOJ_KEY_DIR"] = str(key_dir)
    data = work / "data"
    port = free_port()
    server_url = "http://127.0.0.1:%d" % port

    log("初始化服务端（数据与密钥都在临时目录里）")
    run(["syncoj-server", "--data-root", str(data), "init", "--admin-password", ADMIN_PASSWORD])

    def start_server(port: int, url: str, extra_env: Optional[dict] = None) -> subprocess.Popen:
        env = dict(os.environ)
        env.update(extra_env or {})
        return subprocess.Popen(
            ["syncoj-server", "--data-root", str(data), "serve", "--host", "127.0.0.1",
             "--port", str(port), "--public-url", url, "--quiet-access"],
            stdout=open(str(work / "server.log"), "a"), stderr=subprocess.STDOUT, env=env,
        )

    server = start_server(port, server_url)
    try:
        wait_for("服务端起来", lambda: _http_ok(server_url + "/api/v1/meta"), timeout=60)
        admin = Admin(server_url + "/api/v1/admin")

        log("打包当前版本与一个更高的版本")
        base, upgrade, next_version, broken, broken_version = build_bundles(work, server_url, key_dir)

        log("构建并启动 systemd 245 容器")
        run(["docker", "build", "-t", IMAGE, str(REPO_ROOT / "scripts" / "smoke")])
        start_container(work)

        _install(base)
        _check_running()
        _check_symlink_attacks()

        machine_uuid = sh("cat /etc/syncoj/machine_uuid").strip()
        machine = admin.pending_by_uuid(machine_uuid)
        expect(machine is not None, "服务端里能看到这台待配对的机器")

        log("铺开 %s，等 root 管理单元切换" % next_version)
        admin.rollout(admin.upload_release(upgrade, next_version))
        wait_for(
            "切到 %s" % next_version,
            lambda: sh("readlink /opt/syncoj/current", check=False).strip().endswith(next_version),
            timeout=180,
        )
        wait_for(
            "以 %s 重启" % next_version,
            lambda: ("SyncOJ Agent %s 启动" % next_version) in sh("cat /var/lib/syncoj/agent.log", check=False),
            timeout=120,
        )
        journal = sh("journalctl -u syncoj-agent-admin.service --no-pager")
        expect("已激活版本 %s" % next_version in journal, "切换是 root 管理单元做的（journal 里有记录）")
        expect(sh("systemctl is-active syncoj-agent").strip() == "active", "升级后服务在跑")
        wait_for(
            "平稳运行几轮后回滚状态被 root 清掉",
            lambda: sh("test -e /opt/syncoj/upgrade-state.json && echo y || echo n").strip() == "n",
            timeout=300,
        )
        expect(True, "新版本报告平稳运行，root 清掉了回滚状态")

        log("铺开一个一启动就崩的版本 %s，等启动守卫切回 %s" % (broken_version, next_version))
        admin.rollout(admin.upload_release(broken, broken_version))
        wait_for(
            "启动守卫回滚到 %s" % next_version,
            lambda: "已回滚到 %s" % next_version in sh("journalctl -u syncoj-agent --no-pager", check=False),
            timeout=240,
        )
        expect(sh("readlink /opt/syncoj/current").strip().endswith(next_version),
               "current 指回 %s" % next_version)
        wait_for("回滚后服务在跑",
                 lambda: sh("systemctl is-active syncoj-agent || true", check=False).strip() == "active",
                 timeout=60)
        expect(True, "启动守卫（ExecStartPre=-+，root）在 systemd 245 上生效")
        expect(broken_version in sh("cat /opt/syncoj/rolled-back.json"), "回滚过的版本记下来了")
        wait_for(
            "Agent 不再请求坏版本",
            lambda: "曾启动失败被自动回滚" in sh("cat /var/lib/syncoj/agent.log", check=False)
            or sh("grep -c '已下载发布包 %s' /var/lib/syncoj/agent.log || true" % broken_version,
                  check=False).strip() == "1",
            timeout=120,
        )
        time.sleep(20)
        expect(sh("readlink /opt/syncoj/current").strip().endswith(next_version),
               "回滚之后没有再切回坏版本（不循环）")

        log("服务端换端口重启（关掉局域网发现），等机器听到通告转过来")
        server.terminate()
        server.wait(timeout=30)
        server_url = "http://127.0.0.1:%d" % free_port()
        server = start_server(int(server_url.rsplit(":", 1)[1]), server_url, {"SYNCOJ_DISCOVERY": "0"})
        wait_for("新服务端起来", lambda: _http_ok(server_url + "/api/v1/meta"), timeout=60)
        admin = Admin(server_url + "/api/v1/admin")
        wait_for(
            "机器经服务端通告转到 %s" % server_url,
            lambda: ("→ %s（服务端通告" % server_url) in sh("cat /var/lib/syncoj/agent.log", check=False),
            timeout=120,
        )
        expect(True, "机器听到通告、挑战应答验签后转到新地址（发现已关，只能靠通告）")
        wait_for("机器在新地址上心跳",
                 lambda: (admin.pending_by_uuid(machine_uuid) or {}).get("seconds_since_seen", 99) < 15,
                 timeout=90)

        log("授权卸载（机器未配对）")
        admin.uninstall(int(machine["id"]), machine["hostname"])
        wait_for("卸载完成", lambda: sh("test -e /opt/syncoj && echo y || echo n").strip() == "n", timeout=180)
        wait_for("单元被删掉",
                 lambda: sh("ls /etc/systemd/system | grep -c syncoj || true", check=False).strip() == "0",
                 timeout=60)
        leftovers = sh("ls -d /etc/syncoj /var/lib/syncoj /usr/lib/syncoj 2>/dev/null || true", check=False).strip()
        expect(leftovers == "", "配置、状态、管理单元代码都清掉了（剩下：%r）" % leftovers)
        expect(sh("id -u %s" % RUN_USER, check=False).strip().isdigit(), "运行账号还在")
        expect(sh("test -d /home/%s && echo y" % RUN_USER).strip() == "y", "家目录还在")

        _judge_uninstall(admin, server_url)
        log("冒烟全部通过")
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()


JUDGE_NAME = "冒烟评测机"


def _judge_uninstall(admin: "Admin", server_url: str) -> None:
    log("评测机：一键安装（rlimit）→ 管理端卸载 → 单元的 ExecStopPost=+ 卸干净")
    issued = admin.create_judge_worker(JUDGE_NAME)
    # 命令里的地址取自请求的 Host 头，就是 server_url；容器用宿主网络连得上
    sh("curl -fsSL %s/judge/install.sh | bash -s -- %s --backend rlimit --yes" % (server_url, issued["token"]))
    wait_for("评测机上线", lambda: admin.judge_workers().get(JUDGE_NAME, {}).get("online") is True, timeout=60)
    row = admin.judge_workers()[JUDGE_NAME]
    expect(row["uninstall_control"] is True, "评测机报了卸载能力")
    unit = sh("systemctl cat syncoj-judge")
    expect("ExecStopPost=+/opt/syncoj-judge/uninstall.sh --if-requested" in unit, "单元带 ExecStopPost=+ 卸载钩子")
    expect("--install-manifest /opt/syncoj-judge/install.json" in unit, "服务命令行带安装清单")
    expect(sh("stat -c '%U %a' /opt/syncoj-judge/uninstall.sh").strip() == "root 755",
           "uninstall.sh 属 root、0755")

    admin.uninstall_judge_worker(int(row["id"]), JUDGE_NAME)
    wait_for("评测机告别、从管理端消失", lambda: JUDGE_NAME not in admin.judge_workers(), timeout=60)
    wait_for("程序目录被删", lambda: sh("test -e /opt/syncoj-judge && echo y || echo n").strip() == "n", timeout=90)
    wait_for("单元被删", lambda: sh("test -e /etc/systemd/system/syncoj-judge.service && echo y || echo n",
                                    check=False).strip() == "n", timeout=60)
    leftovers = sh("ls -d /etc/syncoj-judge /var/cache/syncoj-judge 2>/dev/null || true", check=False).strip()
    expect(leftovers == "", "令牌与缓存目录都删了（剩下：%r）" % leftovers)
    expect("could not be found" in sh("systemctl status syncoj-judge 2>&1 || true", check=False)
           or "not-found" in sh("systemctl show -p LoadState syncoj-judge", check=False),
           "systemd 里没有 syncoj-judge 了")
    for account in ("syncoj-judge", "syncoj-judge-run"):
        expect(sh("id -u %s >/dev/null 2>&1 && echo y || echo n" % account, check=False).strip() == "n",
               "账号 %s 删了" % account)
    journal = sh("journalctl -u syncoj-judge-uninstall --no-pager", check=False)
    expect("评测机已卸载" in journal, "卸载是 systemd-run 起的临时单元做的（journal 里有记录）")


def _http_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            return response.status == 200
    except OSError:
        return False


def _install(base: Path) -> None:
    log("以 umask 0 安装（docker exec 的默认）")
    sh("useradd -m -s /bin/bash %(u)s && su %(u)s -c 'mkdir -p /home/%(u)s/Desktop'" % {"u": RUN_USER})
    sh("umask 0 && python3 /src/agent/packaging/install.py --bundle /work/%s "
       "--bootstrap-key-file /work/key/bootstrap.key --user %s --quiet" % (base.name, RUN_USER))


def _check_running() -> None:
    wait_for("Agent 服务在跑", lambda: sh("systemctl is-active syncoj-agent || true", check=False).strip() == "active")
    props = sh("systemctl show syncoj-agent -p NoNewPrivileges -p User")
    expect("NoNewPrivileges=yes" in props, "NoNewPrivileges=yes")
    expect("User=%s" % RUN_USER in props, "以运行账号跑")
    for path in ("/opt/syncoj", "/opt/syncoj/releases", "/etc/syncoj", "/usr/lib/syncoj"):
        mode = int(sh("stat -c %%a %s" % path).strip(), 8)
        expect(not mode & 0o022, "%s 是 %o，组 / 其他人不可写" % (path, mode))
    wait_for("配对码写到桌面",
             lambda: sh("test -f '/home/%s/Desktop/配对码.txt' && echo y || true" % RUN_USER, check=False).strip() == "y")
    owner = sh("stat -c %%U '/home/%s/Desktop/配对码.txt'" % RUN_USER).strip()
    expect(owner == RUN_USER, "配对码文件属运行账号（root 不碰桌面）")
    wait_for("日志里有启动记录", lambda: "启动" in sh("cat /var/lib/syncoj/agent.log 2>/dev/null || true", check=False))
    expect(sh("stat -c %U /var/lib/syncoj/agent.log").strip() == RUN_USER, "agent.log 属运行账号")


def _check_symlink_attacks() -> None:
    log("选手在状态目录里放符号链接，再让 root 的注册单元跑一次")
    sh("echo 原内容 > /etc/smoke-victim && chmod 0600 /etc/smoke-victim && cp -p /etc/smoke-victim /etc/smoke-victim2")
    sh("su %s -c 'cd /var/lib/syncoj && rm -f credential.json && "
       "ln -s /etc/smoke-victim credential.json.tmp && ln -s /etc/smoke-victim2 machine_id && "
       "ln -sf /etc/smoke-victim2 agent.log.1'" % RUN_USER)
    sh("systemctl restart syncoj-agent-enroll.service", check=False)
    for victim in ("/etc/smoke-victim", "/etc/smoke-victim2"):
        expect(sh("cat %s" % victim).strip() == "原内容", "%s 内容没被 root 改写" % victim)
        expect(sh("stat -c %%U %s" % victim).strip() == "root", "%s 属主还是 root" % victim)
    expect(sh("test -f /var/lib/syncoj/credential.json && echo y || true", check=False).strip() == "y",
           "注册单元照样换回了凭据")
    sh("rm -f /var/lib/syncoj/machine_id /var/lib/syncoj/agent.log.1 /etc/smoke-victim /etc/smoke-victim2")
    sh("systemctl restart syncoj-agent")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--keep", action="store_true", help="结束后不删容器与临时目录")
    args = parser.parse_args(argv)
    if shutil.which("docker") is None or shutil.which("syncoj-server") is None:
        print("需要 docker 与 syncoj-server（pip install -e server）", file=sys.stderr)
        return 2

    work = Path(tempfile.mkdtemp(prefix="syncoj-smoke-"))
    os.chmod(str(work), 0o755)
    ok = False
    try:
        smoke(work)
        ok = True
        return 0
    except SmokeFailed as exc:
        print("\n✗ 冒烟失败：%s" % exc, file=sys.stderr, flush=True)
        for unit in ("syncoj-agent", "syncoj-agent-admin.service", "syncoj-agent-enroll.service",
                     "syncoj-judge", "syncoj-judge-uninstall"):
            print("\n--- journalctl -u %s ---" % unit)
            print(sh("journalctl -u %s --no-pager -n 40 || true" % unit, check=False))
        print("\n--- agent.log ---\n" + sh("tail -40 /var/lib/syncoj/agent.log 2>/dev/null || true", check=False))
        server_log = work / "server.log"
        if server_log.exists():
            print("\n--- server.log ---\n" + server_log.read_text(encoding="utf-8")[-4000:])
        return 1
    finally:
        if args.keep and not ok:
            print("保留：容器 %s，临时目录 %s" % (CONTAINER, work))
        else:
            run(["docker", "rm", "-f", CONTAINER], check=False)
            shutil.rmtree(str(work), ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
