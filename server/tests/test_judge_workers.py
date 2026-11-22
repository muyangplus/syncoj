"""评测机管理（§12.10）：自报状态与在线判定、暂停、远程停止 / 重启、吊销、
评测机程序（zipapp）与一键安装脚本、本机评测机。"""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
import zipfile
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text

from syncoj_server import migrations
from syncoj_server.config import Settings
from syncoj_server.judging import worker
from syncoj_server.main import create_app
from syncoj_server.models import JudgeWorker, utcnow
from syncoj_server.services import judge_dist
from syncoj_server.services.local_judge import LocalJudge

from test_builtin_judge import AB, ClientTransport, set_builtin, submit, upload

JUDGING = Path(__file__).resolve().parents[1] / "syncoj_server" / "judging"


@pytest.fixture()
def issued(client: TestClient, admin_headers: dict) -> dict:
    response = client.post("/api/v1/admin/judge-workers", json={"name": "评测机1"}, headers=admin_headers)
    assert response.status_code == 200, response.text
    return response.json()


def bearer(token: str) -> dict:
    return {"Authorization": "Bearer " + token}


def poll(client, token: str, **body):
    payload = {"version": "9.9.9", "backend": "isolate", "isolated": True, "hostname": "judge-a",
               "platform": "linux", "slots": 2, "poll_seconds": 5, "want": 0}
    payload.update(body)
    response = client.post("/api/v1/judge/poll", json=payload, headers=bearer(token))
    assert response.status_code == 200, response.text
    return response.json()


def workers(client, admin_headers) -> dict:
    items = client.get("/api/v1/admin/judge-workers", headers=admin_headers).json()["items"]
    return {item["name"]: item for item in items}


@pytest.fixture()
def queued(client, admin_headers, contest, enrolled) -> dict:
    """一道配好的自带评测题，S001 交了代码，已排上一个任务。"""
    response = client.post("/api/v1/admin/contests/%d/problems" % contest["id"],
                           json=[{"ident": "ab"}], headers=admin_headers)
    problem = response.json()["problems"][0]
    submit(client, enrolled, {"ab/ab.cpp": AB})
    set_builtin(client, admin_headers, problem)
    upload(client, admin_headers, problem)
    body = client.post("/api/v1/admin/contests/%d/judge/builtin" % contest["id"],
                       json={"force": True}, headers=admin_headers).json()
    assert body["queued"] == 1
    return problem


# --------------------------------------------------------------------------- #
# 迁移 023
# --------------------------------------------------------------------------- #


def test_迁移023_老评测机补列_照常领任务(workdir: Path) -> None:
    engine = create_engine("sqlite:///%s" % (workdir / "v22.db"))
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE judge_worker (id INTEGER PRIMARY KEY, name VARCHAR(64) NOT NULL,"
            " token_hash VARCHAR(64) NOT NULL UNIQUE, created_at DATETIME NOT NULL,"
            " last_seen_at DATETIME, last_seen_ip VARCHAR(64))"
        ))
        conn.execute(text(
            "INSERT INTO judge_worker (id, name, token_hash, created_at)"
            " VALUES (1, '老评测机', 'x', '2026-01-01 00:00:00')"
        ))
        conn.execute(text("PRAGMA user_version = 22"))

    applied = migrations.migrate(engine)

    columns = {c["name"] for c in inspect(engine).get_columns("judge_worker")}
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT enabled, desired, is_local, version, backend FROM judge_worker WHERE id = 1"
        )).one()
    assert migrations.current_version(engine) == migrations.LATEST_VERSION
    engine.dispose()
    assert any(step.startswith("023 ") for step in applied)
    assert {"version", "backend", "isolated", "hostname", "platform", "slots", "poll_seconds",
            "enabled", "desired", "is_local"} <= columns
    assert tuple(row) == (1, "run", 0, None, None)


# --------------------------------------------------------------------------- #
# 自报状态与在线判定
# --------------------------------------------------------------------------- #


def test_签发时给出两条安装命令_含地址与令牌(issued) -> None:
    token = issued["token"]
    assert issued["linux_command"] == (
        "curl -fsSL http://testserver/judge/install.sh | sudo bash -s -- %s" % token
    )
    assert "http://testserver/judge/install.ps1" in issued["windows_command"]
    # 令牌是 base64url、可能以 - 开头：PowerShell 里加引号，写文件用 printf 而不是 echo
    assert issued["windows_command"].endswith("-Token '%s'" % token)
    assert issued["manual_command"].startswith("printf '%%s\\n' '%s' > judge.token\n" % token)
    assert "syncoj-judge --server http://testserver" in issued["manual_command"]
    assert issued["online"] is False and issued["enabled"] is True and issued["remote_control"] is False


def test_poll_记下自报状态_算在线(client, admin_headers, issued) -> None:
    reply = poll(client, issued["token"])
    assert reply == {"task": None, "command": "run", "enabled": True}
    row = workers(client, admin_headers)["评测机1"]
    assert row["online"] is True and row["remote_control"] is True
    assert (row["version"], row["backend"], row["isolated"], row["hostname"], row["platform"], row["slots"]) == (
        "9.9.9", "isolate", True, "judge-a", "linux", 2)
    assert row["last_seen_ip"]


def test_在线判定按自报的轮询间隔(client, admin_headers, issued) -> None:
    poll(client, issued["token"], poll_seconds=60)
    ctx = client.app.state.ctx
    with ctx.db.session() as session:
        row = session.query(JudgeWorker).one()
        row.last_seen_at = utcnow() - timedelta(seconds=170)
    assert workers(client, admin_headers)["评测机1"]["online"] is True
    with ctx.db.session() as session:
        session.query(JudgeWorker).one().last_seen_at = utcnow() - timedelta(seconds=190)
    assert workers(client, admin_headers)["评测机1"]["online"] is False


def test_旧评测机只调lease_也算心跳(client, admin_headers, issued) -> None:
    assert client.post("/api/v1/judge/lease", headers=bearer(issued["token"])).status_code == 204
    row = workers(client, admin_headers)["评测机1"]
    assert row["online"] is True and row["version"] is None and row["remote_control"] is False


def test_poll_领任务_列表显示正在评的(client, admin_headers, issued, queued) -> None:
    reply = poll(client, issued["token"], want=1)
    assert reply["task"]["problem"]["ident"] == "ab" and reply["command"] == "run"
    running = workers(client, admin_headers)["评测机1"]["running"]
    assert [(item["player_no"], item["problem"]) for item in running] == [("S001", "ab")]
    assert running[0]["id"] == reply["task"]["id"]


# --------------------------------------------------------------------------- #
# 暂停、改名、停止 / 重启、吊销
# --------------------------------------------------------------------------- #


def test_暂停领不到任务_照常心跳_恢复后领到(client, admin_headers, issued, queued) -> None:
    url = "/api/v1/admin/judge-workers/%d" % issued["id"]
    paused = client.patch(url, json={"enabled": False}, headers=admin_headers).json()
    assert paused["enabled"] is False
    assert poll(client, issued["token"], want=1) == {"task": None, "command": "run", "enabled": False}
    assert client.post("/api/v1/judge/lease", headers=bearer(issued["token"])).status_code == 204
    assert workers(client, admin_headers)["评测机1"]["online"] is True

    client.patch(url, json={"enabled": True}, headers=admin_headers)
    assert poll(client, issued["token"], want=1)["task"] is not None


def test_改名(client, admin_headers, issued) -> None:
    url = "/api/v1/admin/judge-workers/%d" % issued["id"]
    assert client.patch(url, json={"name": "  机房 B  "}, headers=admin_headers).json()["name"] == "机房 B"
    assert client.patch(url, json={"name": ""}, headers=admin_headers).status_code == 422
    assert client.patch("/api/v1/admin/judge-workers/999", json={"name": "x"},
                        headers=admin_headers).status_code == 404


def test_停止与重启_随下一次poll送达一次(client, admin_headers, issued, queued) -> None:
    base = "/api/v1/admin/judge-workers/%d" % issued["id"]
    # 还没报过状态（旧评测机 / 没连过）：收不到命令
    response = client.post(base + "/stop", headers=admin_headers)
    assert response.status_code == 409 and response.json()["code"] == "judge_worker_unsupported"

    poll(client, issued["token"])
    assert client.post(base + "/stop", headers=admin_headers).json()["desired"] == "stop"
    reply = poll(client, issued["token"], want=1)
    assert reply["command"] == "stop" and reply["task"] is None  # 有命令时不发任务
    assert poll(client, issued["token"])["command"] == "run"  # 一次性
    assert workers(client, admin_headers)["评测机1"]["desired"] == "run"

    assert client.post(base + "/restart", headers=admin_headers).json()["desired"] == "restart"
    assert poll(client, issued["token"])["command"] == "restart"

    ctx = client.app.state.ctx
    with ctx.db.session() as session:
        session.query(JudgeWorker).one().last_seen_at = utcnow() - timedelta(minutes=10)
    response = client.post(base + "/restart", headers=admin_headers)
    assert response.status_code == 409 and response.json()["code"] == "judge_worker_offline"


def test_带boot_id的评测机_命令重发到换了进程为止(client, admin_headers, issued) -> None:
    """响应丢了命令不丢：同一个进程每次 poll 都收到，换了进程（命令已生效）才复位。"""
    base = "/api/v1/admin/judge-workers/%d" % issued["id"]
    poll(client, issued["token"], boot_id="a" * 32)
    client.post(base + "/restart", headers=admin_headers)
    assert poll(client, issued["token"], boot_id="a" * 32)["command"] == "restart"
    assert poll(client, issued["token"], boot_id="a" * 32)["command"] == "restart"  # 上一次的响应丢了也还在
    assert workers(client, admin_headers)["评测机1"]["desired"] == "restart"
    assert poll(client, issued["token"], boot_id="b" * 32)["command"] == "run"  # 新进程：已重启
    assert workers(client, admin_headers)["评测机1"]["desired"] == "run"


def test_吊销_手上的任务立刻回队列(client, admin_headers, contest, issued, queued) -> None:
    task = poll(client, issued["token"], want=1)["task"]
    response = client.delete("/api/v1/admin/judge-workers/%d" % issued["id"],
                             params={"confirm": "评测机1"}, headers=admin_headers)
    assert response.status_code == 200 and "1 个任务" in response.json()["detail"]
    assert client.post("/api/v1/judge/poll", json={}, headers=bearer(issued["token"])).status_code == 401

    other = client.post("/api/v1/admin/judge-workers", json={"name": "评测机2"}, headers=admin_headers).json()
    again = poll(client, other["token"], want=1)["task"]
    assert again["id"] == task["id"] and again["attempt"] == 2


# --------------------------------------------------------------------------- #
# Worker 一侧
# --------------------------------------------------------------------------- #


def test_worker_收到停止命令后退出循环(client, admin_headers, issued, tmp_path) -> None:
    judge = worker.Worker(ClientTransport(client, issued["token"]), lambda: None, tmp_path / "cache",
                          backend="rlimit", poll_seconds=0.5)
    assert judge.run_once() is False  # 先报一次状态
    row = workers(client, admin_headers)["评测机1"]
    assert row["backend"] == "rlimit" and row["isolated"] is False and row["version"]
    client.post("/api/v1/admin/judge-workers/%d/restart" % issued["id"], headers=admin_headers)
    assert judge.run_forever() == 0
    assert judge.restart_requested is True


def test_worker_对旧服务端退回lease(tmp_path) -> None:
    calls = []

    class OldServer(worker.Transport):
        def __init__(self) -> None:
            pass

        def request(self, method, path, body=None):
            calls.append(path)
            return (404, b"") if path.endswith("/poll") else (204, b"")

    judge = worker.Worker(OldServer(), lambda: None, tmp_path / "cache")
    assert judge.run_once() is False
    assert calls == ["/api/v1/judge/poll", "/api/v1/judge/lease"]


def test_重启命令行(monkeypatch, tmp_path) -> None:
    pyz = tmp_path / "syncoj-judge.pyz"
    with zipfile.ZipFile(str(pyz), "w") as archive:
        archive.writestr("__main__.py", "")
    monkeypatch.setattr(sys, "argv", [str(pyz), "--server", "http://x"])
    assert worker.restart_argv() == [sys.executable, str(pyz), "--server", "http://x"]
    monkeypatch.setattr(sys, "argv", ["/usr/local/bin/syncoj-judge", "--once"])
    assert worker.restart_argv() == [sys.executable, "-m", "syncoj_server.judging.worker", "--once"]


# --------------------------------------------------------------------------- #
# 评测机程序与安装脚本
# --------------------------------------------------------------------------- #


def _stdlib_or_relative(tree: ast.AST) -> list:
    """judging/ 里 import 了哪些既不是标准库、也不是包内相对 import 的模块。"""
    stdlib = getattr(sys, "stdlib_module_names", None)
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # 相对 import：包内（``..`` 只用于取版本号，包根 __init__ 也打进了 pyz）
                continue
            names = [node.module or ""]
        else:
            continue
        for name in names:
            top = name.split(".")[0]
            if top == "__future__":
                continue
            if stdlib is not None and top not in stdlib:
                bad.append(name)
            elif stdlib is None and top in ("syncoj_server", "fastapi", "sqlalchemy", "pydantic", "httpx"):
                bad.append(name)
    return bad


def test_judging只用标准库() -> None:
    for path in JUDGING.rglob("*.py"):
        bad = _stdlib_or_relative(ast.parse(path.read_text(encoding="utf-8")))
        assert not bad, "%s import 了标准库以外的模块：%s" % (path.name, bad)
    root_init = ast.parse((JUDGING.parent / "__init__.py").read_text(encoding="utf-8"))
    assert not [n for n in ast.walk(root_init) if isinstance(n, (ast.Import, ast.ImportFrom))]


def test_worker_pyz_可下载可运行(client, tmp_path) -> None:
    response = client.get("/judge/worker.pyz")
    assert response.status_code == 200
    pyz = tmp_path / "dl" / "syncoj-judge.pyz"
    pyz.parent.mkdir()
    pyz.write_bytes(response.content)
    with zipfile.ZipFile(str(pyz)) as archive:
        names = {name for name in archive.namelist() if not name.endswith("/")}
    assert {"__main__.py", "syncoj_server/__init__.py", "syncoj_server/judging/worker.py",
            "syncoj_server/judging/vendor/testlib.h", "syncoj_server/judging/vendor/LICENSE-testlib",
            "syncoj_server/judging/checkers/realcmp.cpp.tmpl"} <= names
    assert not [n for n in names if n.startswith("syncoj_server/") and not n.startswith("syncoj_server/judging/")
                and n != "syncoj_server/__init__.py"]
    assert not [n for n in names if "__pycache__" in n]
    # -I：不读环境变量与用户 site-packages，确认它不靠本机装的 syncoj_server 跑起来
    result = subprocess.run([sys.executable, "-I", str(pyz), "--help"], cwd=str(pyz.parent),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert "--slots" in result.stdout and "--token-file" in result.stdout

    # 缓存：同一份源码只打一次
    again = client.get("/judge/worker.pyz")
    assert again.content == response.content
    cached = list((client.app.state.ctx.settings.data_root / "cache" / "judge-worker").glob("*.pyz"))
    assert len(cached) == 1 and judge_dist.fingerprint()[:16] in cached[0].name


def test_install_sh_嵌入地址与指纹_语法正确(client, tmp_path) -> None:
    response = client.get("/judge/install.sh", headers={"Host": "10.0.0.5:8000"})
    assert response.status_code == 200
    body = response.content.decode("utf-8")
    pyz = client.get("/judge/worker.pyz").content
    import hashlib

    assert body.startswith("#!/bin/bash\n") and "\r" not in body
    assert 'EMBEDDED_SERVER="http://10.0.0.5:8000"' in body
    assert 'PYZ_SHA256="%s"' % hashlib.sha256(pyz).hexdigest() in body
    for needle in ("useradd --system", "/etc/systemd/system/$SERVICE.service", "chmod 0600", "isolate",
                   "--run-as", "systemctl enable", "set -euo pipefail"):
        assert needle in body, needle
    if shutil.which("bash"):
        script = tmp_path / "install.sh"
        script.write_text(body, encoding="utf-8")
        assert subprocess.run(["bash", "-n", str(script)]).returncode == 0


@pytest.mark.skipif(shutil.which("bash") is None, reason="需要 bash")
def test_install_sh_参数解析_令牌可以以短横线开头(client, tmp_path) -> None:
    """只跑到参数校验就失败退出的组合（在检查 root 之前），不会真的去装。"""
    script = tmp_path / "install.sh"
    script.write_bytes(client.get("/judge/install.sh").content)

    def run(*args):
        result = subprocess.run(["bash", str(script)] + list(args), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, universal_newlines=True, timeout=30)
        assert result.returncode != 0
        return result.stderr

    assert "令牌太短" in run("abc")
    assert "缺少评测机令牌" in run()
    # 以 - 开头的完整令牌被当成令牌收下：错误停在后面的 --slots 校验上
    assert "--slots 要是正整数" in run("-" + "a" * 42, "--slots", "x")
    assert "多了一个参数" in run("a" * 43, "b" * 43)


def test_install_sh_不嵌入形状可疑的Host(client) -> None:
    body = client.get("/judge/install.sh", headers={"Host": "evil\";rm -rf /;\""}).content.decode("utf-8")
    assert "rm -rf" not in body
    assert 'EMBEDDED_SERVER="http://127.0.0.1:' in body


def test_install_ps1_带BOM与CRLF_嵌入地址(client) -> None:
    response = client.get("/judge/install.ps1", headers={"Host": "10.0.0.5:8000"})
    assert response.status_code == 200
    raw = response.content
    assert raw.startswith(b"\xef\xbb\xbf")
    body = raw[3:].decode("utf-8")
    assert "\r\n" in body and "\n" not in body.replace("\r\n", "")
    assert "$EmbeddedServer = 'http://10.0.0.5:8000'" in body
    for needle in ("Register-ScheduledTask", "'--backend', $Backend", "[string]$Backend = 'winjob'",
                   "S-1-5-18", "--log-file", "3, 8"):
        assert needle in body, needle


def test_ps1模板本身带BOM() -> None:
    template = Path(judge_dist.TEMPLATES) / "install.ps1"
    assert template.read_bytes().startswith(b"\xef\xbb\xbf")


def test_公开端口上拿不到评测机程序与脚本(workdir: Path) -> None:
    settings = Settings()
    settings.data_root = workdir / "data"
    settings.web_dist = None
    app = create_app(settings)
    app.state.public_port = 80
    with TestClient(app, base_url="http://testserver") as client:
        for path in ("/judge/worker.pyz", "/judge/install.sh", "/judge/install.ps1"):
            response = client.get(path)
            assert response.status_code == 404 and response.json()["detail"] == "Not Found", path
    with TestClient(app, base_url="http://testserver:8000") as client:
        assert client.get("/judge/install.sh").status_code == 200


# --------------------------------------------------------------------------- #
# 本机评测机
# --------------------------------------------------------------------------- #


class FakeProc:
    def __init__(self, argv, **kwargs) -> None:
        self.argv = argv
        self.kwargs = kwargs
        self.pid = 4321
        self.returncode = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


def test_本机评测机_启动停止(client, admin_headers, monkeypatch) -> None:
    spawned = []

    def fake_popen(argv, **kwargs):
        spawned.append(FakeProc(argv, **kwargs))
        return spawned[-1]

    ctx = client.app.state.ctx
    ctx.local_judge = LocalJudge(popen=fake_popen, platform="linux")
    monkeypatch.setattr(shutil, "which", lambda name: None)  # 没有 isolate → rlimit

    status = client.get("/api/v1/admin/judge-local", headers=admin_headers).json()
    assert status["supported"] is True and status["running"] is False

    started = client.post("/api/v1/admin/judge-local/start", headers=admin_headers).json()
    assert started["running"] is True and started["pid"] == 4321 and started["backend"] == "rlimit"
    argv = spawned[0].argv
    assert argv[1:3] == ["-m", "syncoj_server.judging.worker"]
    assert argv[argv.index("--backend") + 1] == "rlimit"
    assert argv[argv.index("--server") + 1].startswith("http://127.0.0.1:")
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        assert argv[argv.index("--run-as") + 1] == "nobody"
    token_file = Path(argv[argv.index("--token-file") + 1])
    assert oct(token_file.stat().st_mode & 0o777) == "0o600"

    # 记为本机评测机那一行，令牌就是文件里那一枚
    rows = workers(client, admin_headers)
    assert rows["本机评测机"]["local"] is True and rows["本机评测机"]["id"] == started["worker_id"]
    token = token_file.read_text(encoding="utf-8").strip()
    assert client.post("/api/v1/judge/lease", headers=bearer(token)).status_code == 204

    # 再点一次启动：已在运行，不重复拉起
    client.post("/api/v1/admin/judge-local/start", headers=admin_headers)
    assert len(spawned) == 1

    stopped = client.post("/api/v1/admin/judge-local/stop", headers=admin_headers).json()
    assert spawned[0].terminated and stopped["running"] is False and stopped["exit_code"] == 0

    # 第二次启动：同一行、换一枚令牌
    client.post("/api/v1/admin/judge-local/start", headers=admin_headers)
    assert len(spawned) == 2 and len(workers(client, admin_headers)) == 1
    assert client.post("/api/v1/judge/lease", headers=bearer(token)).status_code == 401


def test_本机评测机_随服务端退出停掉(settings) -> None:
    proc = FakeProc([])
    app = create_app(settings)
    with TestClient(app):
        judge = LocalJudge(popen=lambda argv, **kw: proc, platform="linux")
        judge._proc = proc
        app.state.ctx.local_judge = judge
    assert proc.terminated is True


def test_本机评测机_非Linux不提供(client, admin_headers) -> None:
    client.app.state.ctx.local_judge = LocalJudge(popen=FakeProc, platform="win32")
    status = client.get("/api/v1/admin/judge-local", headers=admin_headers).json()
    assert status == {**status, "supported": False, "reason": "仅 Linux", "running": False}
    response = client.post("/api/v1/admin/judge-local/start", headers=admin_headers)
    assert response.status_code == 409 and response.json()["code"] == "local_judge_unsupported"


def test_worker_多个并发位同时评(tmp_path, monkeypatch) -> None:
    import json
    import threading

    pending = [{"id": 1}, {"id": 2}]
    both = threading.Barrier(2, timeout=10)
    seen = []

    class Server(worker.Transport):
        def __init__(self) -> None:
            pass

        def request(self, method, path, body=None):
            assert path.endswith("/poll")
            seen.append(body["want"])
            if body["want"] and pending:
                return 200, json.dumps({"task": pending.pop(0), "command": "run"}).encode()
            return 200, json.dumps({"task": None, "command": "run" if pending else "stop"}).encode()

    judge = worker.Worker(Server(), lambda slot=0: None, tmp_path / "cache", slots=2, poll_seconds=0.5)
    finished = []
    # 两个任务都在跑才放行：只有真并发时才过得去
    monkeypatch.setattr(judge, "_run_task", lambda task, slot: (both.wait(), finished.append((task["id"], slot))))
    assert judge.run_forever() == 0
    assert sorted(finished) == [(1, 0), (2, 1)]
    assert seen[:2] == [1, 1] and judge.command == "stop"


def test_多槽位只允许isolate(tmp_path, capsys) -> None:
    from syncoj_server.judging import worker as judge_worker

    token = tmp_path / "token"
    token.write_text("x", encoding="utf-8")
    argv = ["--server", "http://127.0.0.1:1", "--token-file", str(token), "--backend", "rlimit",
            "--run-as", "nobody", "--slots", "2"]
    assert judge_worker.main(argv) == 2
    assert "只能用 isolate" in capsys.readouterr().err


def test_本机评测机连回服务端的地址跟着监听地址走() -> None:
    from syncoj_server.services.local_judge import loopback_host

    assert loopback_host("0.0.0.0") == "127.0.0.1"
    assert loopback_host("::") == "127.0.0.1"
    assert loopback_host("192.168.1.5") == "192.168.1.5"
    assert loopback_host("fe80::1") == "[fe80::1]"
