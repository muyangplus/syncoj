"""评测机远程卸载（§12.10）：命令下发、评完手上的任务再告别、服务端删行回收任务、按安装方式清理，
以及 install.sh / uninstall.sh / install.ps1 / uninstall.ps1 里对应的那一段。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import zipfile
from datetime import timedelta
from pathlib import Path

import pytest

from syncoj_server.judging import runner, uninstall, worker
from syncoj_server.models import EventLog, JudgeTask, JudgeWorker, utcnow
from syncoj_server.services import judge_dist
from syncoj_server.services.local_judge import LocalJudge

from test_builtin_judge import ClientTransport
from test_judge_workers import FakeProc, bearer, issued, poll, queued, workers  # noqa: F401 - fixture

TEMPLATES = Path(judge_dist.TEMPLATES)


def uninstall_url(worker_id: int) -> str:
    return "/api/v1/admin/judge-workers/%d/uninstall" % worker_id


def ask_uninstall(client, admin_headers, row: dict, confirm=None):
    return client.post(uninstall_url(row["id"]), params={"confirm": confirm or row["name"]}, headers=admin_headers)


def events(client, category: str) -> list:
    with client.app.state.ctx.db.session() as session:
        return [row.message for row in session.query(EventLog).filter(EventLog.category == category)]


# --------------------------------------------------------------------------- #
# 服务端：下发与告别
# --------------------------------------------------------------------------- #


def test_卸载命令_旧评测机与离线都409_在线才下发(client, admin_headers, issued) -> None:
    response = ask_uninstall(client, admin_headers, issued)
    assert response.status_code == 409 and response.json()["code"] == "judge_worker_unsupported"

    poll(client, issued["token"], features=["slots"])  # 报过状态，但不认卸载
    assert workers(client, admin_headers)["评测机1"]["uninstall_control"] is False
    response = ask_uninstall(client, admin_headers, issued)
    assert response.status_code == 409 and response.json()["code"] == "judge_worker_unsupported"

    poll(client, issued["token"], features=["slots", "uninstall"])
    assert workers(client, admin_headers)["评测机1"]["uninstall_control"] is True
    assert ask_uninstall(client, admin_headers, issued, confirm="别的名字").status_code != 200

    with client.app.state.ctx.db.session() as session:
        session.query(JudgeWorker).one().last_seen_at = utcnow() - timedelta(minutes=10)
    response = ask_uninstall(client, admin_headers, issued)
    assert response.status_code == 409 and response.json()["code"] == "judge_worker_offline"

    poll(client, issued["token"], features=["slots", "uninstall"])
    response = ask_uninstall(client, admin_headers, issued)
    assert response.status_code == 200, response.text
    assert workers(client, admin_headers)["评测机1"]["desired"] == "uninstall"


def test_卸载命令不复位_换了进程也照样下发_不发任务(client, admin_headers, issued, queued) -> None:
    poll(client, issued["token"], features=["uninstall"], boot_id="a" * 32)
    assert ask_uninstall(client, admin_headers, issued).status_code == 200
    reply = poll(client, issued["token"], features=["uninstall"], boot_id="a" * 32, want=1)
    assert reply["command"] == "uninstall" and reply["task"] is None
    # 崩了被 systemd 拉起：新进程照样收到
    reply = poll(client, issued["token"], features=["uninstall"], boot_id="b" * 32, want=1)
    assert reply["command"] == "uninstall" and reply["task"] is None
    assert workers(client, admin_headers)["评测机1"]["desired"] == "uninstall"


def test_卸载中不能被停止重启盖掉_仍可吊销(client, admin_headers, issued) -> None:
    poll(client, issued["token"], features=["uninstall"])
    assert ask_uninstall(client, admin_headers, issued).status_code == 200
    base = "/api/v1/admin/judge-workers/%d" % issued["id"]
    for action in ("stop", "restart"):
        response = client.post(base + "/" + action, headers=admin_headers)
        assert response.status_code == 409 and response.json()["code"] == "judge_worker_uninstalling"
    response = client.delete(base, params={"confirm": "评测机1"}, headers=admin_headers)
    assert response.status_code == 200 and "评测机1" not in workers(client, admin_headers)


def test_没要求卸载时告别409(client, issued) -> None:
    response = client.post("/api/v1/judge/goodbye", json={"kind": "manual"}, headers=bearer(issued["token"]))
    assert response.status_code == 409 and response.json()["code"] == "judge_worker_not_uninstalling"
    assert client.post("/api/v1/judge/goodbye", json={"kind": "x"}, headers=bearer(issued["token"])).status_code == 422


def test_告别_删行_任务回队列_令牌失效_记审计(client, admin_headers, issued, queued) -> None:
    task = poll(client, issued["token"], features=["uninstall"], want=1)["task"]
    assert task is not None
    assert ask_uninstall(client, admin_headers, issued).status_code == 200
    response = client.post("/api/v1/judge/goodbye", json={"kind": "systemd"}, headers=bearer(issued["token"]))
    assert response.status_code == 200, response.text
    assert "评测机1" not in workers(client, admin_headers)
    assert client.post("/api/v1/judge/poll", json={}, headers=bearer(issued["token"])).status_code == 401
    with client.app.state.ctx.db.session() as session:
        again = session.get(JudgeTask, task["id"])
        assert (again.status, again.worker_id) == ("queued", None)
    logged = events(client, "judge_worker_uninstall")
    assert len(logged) == 1 and "评测机1" in logged[0] and "systemd" in logged[0]


class FakeOutcome(runner.JudgeOutcome):
    pass


def test_worker收到卸载_评完手上的交了结果再告别(client, admin_headers, issued, queued, tmp_path, monkeypatch) -> None:
    transport = ClientTransport(client, issued["token"])
    judge = worker.Worker(transport, lambda slot=0: None, tmp_path / "cache", backend="isolate", poll_seconds=0.5)
    task = judge.poll(want=1)
    assert task is not None
    assert workers(client, admin_headers)["评测机1"]["uninstall_control"] is True  # 新评测机报了能力

    assert ask_uninstall(client, admin_headers, issued).status_code == 200
    assert judge.poll(want=0) is None
    assert judge.command == "uninstall"
    assert judge.poll(want=1) is None  # 卸载中不再领任务

    # 手上那个照常评完、交结果
    monkeypatch.setattr(judge, "_judge", lambda t, slot=0: FakeOutcome(score=100, max_score=100, status="AC"))
    judge._run_task(task, 0)
    assert judge.run_forever() == 0

    token_file = tmp_path / "token"
    token_file.write_text(issued["token"] + "\n", encoding="utf-8")
    code = uninstall.finish(transport, uninstall.Manifest("manual"), token_file, tmp_path / "cache",
                            sleep=lambda _s: None)
    assert code == 0
    with client.app.state.ctx.db.session() as session:
        done = session.get(JudgeTask, task["id"])
        assert done.status == "done" and done.score == 100
    assert "评测机1" not in workers(client, admin_headers)
    assert not token_file.exists()
    assert client.post("/api/v1/judge/poll", json={}, headers=bearer(issued["token"])).status_code == 401
    assert "手动运行" in events(client, "judge_worker_uninstall")[0]


def test_卸载优先于等着的重启(tmp_path) -> None:
    replies = [{"command": "restart"}, {"command": "uninstall"}]

    class Server(worker.Transport):
        def __init__(self) -> None:
            pass

        def request(self, method, path, body=None):
            assert "uninstall" in body["features"]
            return 200, json.dumps(dict(replies.pop(0), task=None)).encode()

    judge = worker.Worker(Server(), lambda: None, tmp_path / "cache", backend="rlimit")
    judge.poll(want=0)
    assert judge.command == "restart"
    judge.poll(want=0)
    assert judge.command == "uninstall" and judge.restart_requested is False


# --------------------------------------------------------------------------- #
# worker：安装清单与按安装方式清理（不碰真系统）
# --------------------------------------------------------------------------- #


class Goodbye:
    """只回告别的假服务端：依次给出 ``statuses``，记下请求。"""

    def __init__(self, *statuses: int) -> None:
        self.statuses = list(statuses)
        self.calls = []

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return status, b'{"ok": true}'


def write_manifest(path: Path, **data) -> Path:
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.mark.parametrize("kind", ["systemd", "schtask"])
def test_服务装的_告别后只写标记(tmp_path, kind) -> None:
    cache = tmp_path / "cache"
    marker = tmp_path / "state" / ".uninstall-requested"
    manifest = uninstall.load_manifest(write_manifest(tmp_path / "install.json", kind=kind, marker=str(marker)))
    token = tmp_path / "token"
    token.write_text("t", encoding="utf-8")
    server = Goodbye(200)
    assert uninstall.finish(server, manifest, token, cache, sleep=lambda _s: None) == 0
    assert server.calls == [("POST", "/api/v1/judge/goodbye", {"kind": kind})]
    assert marker.is_file() and token.exists()  # 令牌与程序留给 root / SYSTEM 去删
    assert uninstall.already_requested(manifest, cache) is True


def test_清单没写marker时标记在缓存目录(tmp_path) -> None:
    manifest = uninstall.load_manifest(write_manifest(tmp_path / "install.json", kind="systemd"))
    assert uninstall.finish(Goodbye(200), manifest, tmp_path / "token", tmp_path / "cache") == 0
    assert (tmp_path / "cache" / ".uninstall-requested").is_file()


def test_本机评测机_告别后自己什么都不删(tmp_path) -> None:
    manifest = uninstall.load_manifest(write_manifest(tmp_path / "install.json", kind="local"))
    token = tmp_path / "token"
    token.write_text("t", encoding="utf-8")
    assert uninstall.finish(Goodbye(200), manifest, token, tmp_path / "cache") == 0
    assert token.exists() and not (tmp_path / "cache" / ".uninstall-requested").exists()


def test_手动运行_删令牌与缓存里自己的东西(tmp_path) -> None:
    cache = tmp_path / "cache"
    (cache / "syncoj-judge-src-abc").mkdir(parents=True)
    (cache / ("a" * 64 + ".zip")).write_bytes(b"zip")
    (cache / ("b" * 64 + ".zip.part")).write_bytes(b"part")
    token = tmp_path / "token"
    token.write_text("t", encoding="utf-8")
    assert uninstall.finish(Goodbye(200), uninstall.load_manifest(None), token, cache) == 0
    assert not token.exists() and not cache.exists()

    # 用户把 --cache-dir 指到一个有别的东西的目录：只删自己写的
    cache.mkdir()
    (cache / "keep.txt").write_text("别删", encoding="utf-8")
    (cache / ("c" * 64 + ".zip")).write_bytes(b"zip")
    assert uninstall.finish(Goodbye(401), uninstall.load_manifest(None), token, cache) == 0
    assert sorted(p.name for p in cache.iterdir()) == ["keep.txt"]


def test_告别401也清理_连不上不清理(tmp_path) -> None:
    manifest = uninstall.Manifest("systemd", str(tmp_path / "m"))
    assert uninstall.finish(Goodbye(401), manifest, tmp_path / "t", tmp_path / "c") == 0
    assert (tmp_path / "m").exists()

    (tmp_path / "m").unlink()
    slept = []
    server = Goodbye(503)
    assert uninstall.finish(server, manifest, tmp_path / "t", tmp_path / "c", sleep=slept.append) == 1
    assert len(server.calls) == uninstall.GOODBYE_ATTEMPTS and len(slept) == uninstall.GOODBYE_ATTEMPTS - 1
    assert not (tmp_path / "m").exists()

    class Down:
        def request(self, *args, **kwargs):
            raise OSError("connection refused")

    assert uninstall.finish(Down(), manifest, tmp_path / "t", tmp_path / "c", sleep=lambda _s: None) == 1
    # 服务端说没要求卸载：也不清理
    assert uninstall.finish(Goodbye(409), manifest, tmp_path / "t", tmp_path / "c") == 1
    assert not (tmp_path / "m").exists()


def test_安装清单_缺失损坏都按手动运行_默认在pyz旁边找(tmp_path) -> None:
    assert uninstall.load_manifest(tmp_path / "none.json").kind == "manual"
    (tmp_path / "bad.json").write_text("{", encoding="utf-8")
    assert uninstall.load_manifest(tmp_path / "bad.json").kind == "manual"
    assert uninstall.load_manifest(write_manifest(tmp_path / "k.json", kind="rm -rf")).kind == "manual"
    relative = uninstall.load_manifest(write_manifest(tmp_path / "r.json", kind="systemd", marker="rel/x"))
    assert relative.kind == "systemd" and relative.marker is None

    pyz = tmp_path / "opt" / "syncoj-judge.pyz"
    pyz.parent.mkdir()
    with zipfile.ZipFile(str(pyz), "w") as archive:
        archive.writestr("__main__.py", "")
    assert uninstall.default_manifest_path(str(pyz)) == pyz.resolve().parent / "install.json"
    assert uninstall.default_manifest_path(str(tmp_path / "r.json")) is None
    assert uninstall.default_manifest_path("") is None


def test_标记已在时评测机不再启动_也不读令牌(tmp_path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / ".uninstall-requested").write_text("x", encoding="utf-8")
    manifest = write_manifest(tmp_path / "install.json", kind="systemd", marker=str(cache / ".uninstall-requested"))
    argv = ["--server", "http://127.0.0.1:1", "--token-file", str(tmp_path / "gone"), "--backend", "rlimit",
            "--run-as", "nobody", "--cache-dir", str(cache), "--install-manifest", str(manifest)]
    assert worker.main(argv) == 0


# --------------------------------------------------------------------------- #
# 本机评测机
# --------------------------------------------------------------------------- #


def test_本机评测机_没在跑时直接卸(client, admin_headers, monkeypatch) -> None:
    ctx = client.app.state.ctx
    ctx.local_judge = LocalJudge(popen=FakeProc, platform="linux")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    started = client.post("/api/v1/admin/judge-local/start", headers=admin_headers).json()
    root = Path(ctx.settings.data_root) / "judge-local"
    assert (root / "install.json").is_file() and json.loads((root / "install.json").read_text())["kind"] == "local"
    client.post("/api/v1/admin/judge-local/stop", headers=admin_headers)

    row = workers(client, admin_headers)["本机评测机"]
    assert row["uninstall_control"] is True and row["id"] == started["worker_id"]
    response = ask_uninstall(client, admin_headers, row)
    assert response.status_code == 200, response.text
    assert "本机评测机" not in workers(client, admin_headers)
    assert not root.exists()
    assert len(events(client, "judge_worker_uninstall")) == 1


def test_本机评测机_在跑时下命令_告别时删行删目录(client, admin_headers, monkeypatch) -> None:
    spawned = []

    def fake_popen(argv, **kwargs):
        spawned.append(FakeProc(argv, **kwargs))
        return spawned[-1]

    ctx = client.app.state.ctx
    ctx.local_judge = LocalJudge(popen=fake_popen, platform="linux")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    client.post("/api/v1/admin/judge-local/start", headers=admin_headers)
    argv = spawned[0].argv
    manifest = Path(argv[argv.index("--install-manifest") + 1])
    assert uninstall.load_manifest(manifest).kind == "local"
    token = Path(argv[argv.index("--token-file") + 1]).read_text(encoding="utf-8").strip()

    row = workers(client, admin_headers)["本机评测机"]
    assert ask_uninstall(client, admin_headers, row).status_code == 200  # 刚启动、还没 poll 也能下
    reply = poll(client, token, features=["slots", "uninstall"], want=1)
    assert reply["command"] == "uninstall"
    response = client.post("/api/v1/judge/goodbye", json={"kind": "local"}, headers=bearer(token))
    assert response.status_code == 200
    assert "本机评测机" not in workers(client, admin_headers)
    assert not manifest.parent.exists()


# --------------------------------------------------------------------------- #
# Linux：install.sh 与 uninstall.sh
# --------------------------------------------------------------------------- #


def rendered_install_sh(client) -> str:
    return client.get("/judge/install.sh", headers={"Host": "10.0.0.5:8000"}).content.decode("utf-8")


def test_install_sh_带卸载脚本与ExecStopPost(client, tmp_path) -> None:
    body = rendered_install_sh(client)
    assert "ExecStopPost=+$INSTALL_DIR/uninstall.sh --if-requested" in body
    assert 'ARGS="$ARGS --install-manifest $INSTALL_DIR/install.json"' in body
    assert '"kind": "systemd"' in body and 'MARKER="$CACHE_DIR/.uninstall-requested"' in body
    assert "chmod 0755 \"$UNINSTALL_TMP\"" in body and "chown root:root \"$UNINSTALL_TMP\"" in body
    # 卸载脚本原样嵌进 heredoc，占位符没了
    start = body.index("<<'SYNCOJ_UNINSTALL_EOF'\n") + len("<<'SYNCOJ_UNINSTALL_EOF'\n")
    end = body.index("\nSYNCOJ_UNINSTALL_EOF\n", start) + 1
    assert body[start:end] == (TEMPLATES / "uninstall.sh").read_text(encoding="utf-8")
    assert "\n__SYNCOJ_UNINSTALL_SH__\n" not in body
    if shutil.which("bash"):
        script = tmp_path / "install.sh"
        script.write_text(body, encoding="utf-8")
        assert subprocess.run(["bash", "-n", str(script)]).returncode == 0
        assert subprocess.run(["bash", "-n", str(TEMPLATES / "uninstall.sh")]).returncode == 0


FAKE_TOOL = """#!/bin/bash
echo "$(basename "$0") $*" >> "{log}"
if [ "$(basename "$0")" = id ] && [ "${{1:-}}" = -u ]; then echo 0; fi
exit 0
"""


@pytest.fixture()
def fake_root(tmp_path):
    """临时根目录下装好的样子 + PATH 里假的 systemctl / systemd-run / userdel / id（只记调用）。"""
    root = tmp_path / "root"
    for path in ("opt/syncoj-judge", "etc/syncoj-judge", "var/cache/syncoj-judge", "etc/systemd/system"):
        (root / path).mkdir(parents=True)
    shutil.copy2(str(TEMPLATES / "uninstall.sh"), str(root / "opt/syncoj-judge/uninstall.sh"))
    (root / "opt/syncoj-judge/syncoj-judge.pyz").write_bytes(b"pyz")
    (root / "etc/syncoj-judge/token").write_text("t", encoding="utf-8")
    (root / "var/cache/syncoj-judge/abc.zip").write_bytes(b"zip")
    (root / "etc/systemd/system/syncoj-judge.service").write_text("[Unit]\n", encoding="utf-8")
    (root / "etc/systemd/system/other.service").write_text("[Unit]\n", encoding="utf-8")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "calls.log"
    log.write_text("", encoding="utf-8")
    for tool in ("systemctl", "systemd-run", "userdel", "id"):
        (bindir / tool).write_text(FAKE_TOOL.format(log=log), encoding="utf-8")
        os.chmod(str(bindir / tool), 0o755)

    def run(*args):
        env = {"PATH": "%s:/usr/bin:/bin" % bindir, "SYNCOJ_JUDGE_ROOT": str(root), "LANG": "C.UTF-8"}
        result = subprocess.run(["bash", str(root / "opt/syncoj-judge/uninstall.sh")] + list(args), env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, timeout=30)
        calls = log.read_text(encoding="utf-8").splitlines()
        log.write_text("", encoding="utf-8")
        return result, calls

    return root, run


@pytest.mark.skipif(shutil.which("bash") is None, reason="需要 bash")
def test_uninstall_sh_没有标记时什么都不做(fake_root) -> None:
    root, run = fake_root
    result, calls = run("--if-requested")
    assert result.returncode == 0 and calls == []
    assert (root / "opt/syncoj-judge/syncoj-judge.pyz").exists()


@pytest.mark.skipif(shutil.which("bash") is None, reason="需要 bash")
def test_uninstall_sh_有标记时脱离本单元_再真卸(fake_root) -> None:
    root, run = fake_root
    (root / "var/cache/syncoj-judge/.uninstall-requested").write_text("任何内容都不执行 $(touch /x)", encoding="utf-8")
    result, calls = run("--if-requested")
    assert result.returncode == 0, result.stdout
    runs = [call for call in calls if call.startswith("systemd-run ")]
    assert len(runs) == 1, calls
    assert "--unit=syncoj-judge-uninstall" in runs[0] and "--on-active=2" in runs[0]
    assert runs[0].endswith("%s/opt/syncoj-judge/uninstall.sh --now" % root)
    assert "--setenv=SYNCOJ_JUDGE_ROOT=%s" % root in runs[0]
    assert (root / "opt/syncoj-judge").exists()  # 这一步只排期，不删

    result, calls = run("--now")
    assert result.returncode == 0, result.stdout
    assert "systemctl disable --now syncoj-judge.service" in calls
    assert "systemctl daemon-reload" in calls
    assert "userdel syncoj-judge" in calls and "userdel syncoj-judge-run" in calls
    for path in ("opt/syncoj-judge", "etc/syncoj-judge", "var/cache/syncoj-judge",
                 "etc/systemd/system/syncoj-judge.service"):
        assert not (root / path).exists(), path
    assert (root / "etc/systemd/system/other.service").exists()


@pytest.mark.skipif(shutil.which("bash") is None, reason="需要 bash")
def test_uninstall_sh_手动运行_幂等(fake_root, tmp_path) -> None:
    root, _run = fake_root
    # 手动运行（不带参数）走 --now；脚本自己也会被删，所以拿一份副本跑第二遍
    copy = tmp_path / "uninstall-copy.sh"
    shutil.copy2(str(root / "opt/syncoj-judge/uninstall.sh"), str(copy))
    result, calls = _run()
    assert result.returncode == 0 and "评测机已卸载" in result.stdout
    assert not (root / "opt/syncoj-judge").exists()
    (root / "opt/syncoj-judge").mkdir()
    shutil.copy2(str(copy), str(root / "opt/syncoj-judge/uninstall.sh"))
    result, calls = _run()
    assert result.returncode == 0 and "评测机已卸载" in result.stdout


# --------------------------------------------------------------------------- #
# Windows：install.ps1 与 uninstall.ps1（只查文本：这里没有 PowerShell）
# --------------------------------------------------------------------------- #


def test_install_ps1_带卸载脚本与清理任务(client) -> None:
    raw = client.get("/judge/install.ps1", headers={"Host": "10.0.0.5:8000"}).content
    assert raw.startswith(b"\xef\xbb\xbf")
    body = raw[3:].decode("utf-8")
    assert "\n" not in body.replace("\r\n", "")
    for needle in ("$CleanupTaskName = 'SyncOJ Judge Cleanup'",
                   "Register-ScheduledTask -TaskName $CleanupTaskName",
                   "'--install-manifest', \"`\"$Manifest`\"\"",
                   "kind = 'schtask'",
                   "-IfRequested",
                   "/d /c if exist `\"$Marker`\"",
                   "-RepetitionInterval (New-TimeSpan -Minutes 2)",
                   "& icacls.exe $UninstallPs1 /inheritance:r /grant:r '*S-1-5-18:F' '*S-1-5-32-544:F'"):
        assert needle in body, needle
    # 卸载脚本原样嵌进 here-string
    uninstall_ps1 = (TEMPLATES / "uninstall.ps1").read_bytes()[3:].decode("utf-8")
    assert "@'\r\n" + uninstall_ps1 + "'@\r\n" in body
    assert "__SYNCOJ_UNINSTALL_PS1__" not in body


def test_uninstall_ps1_BOM_CRLF_不读标记内容_不跟随联接() -> None:
    raw = (TEMPLATES / "uninstall.ps1").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    body = raw[3:].decode("utf-8")
    assert "\n" not in body.replace("\r\n", "")
    assert "$InstallDir = $PSScriptRoot" in body
    assert "Unregister-ScheduledTask -TaskName $TaskName" in body
    assert "Unregister-ScheduledTask -TaskName $CleanupTaskName" in body
    assert "rmdir /s /q" in body and "Remove-Item -Recurse -" not in body and "Remove-Item -LiteralPath" not in body
    assert "Get-Content" not in body and "Invoke-Expression" not in body
    assert not [line for line in body.split("\r\n") if line.startswith("'@")]


def test_卸载脚本不能提前结束嵌入它的heredoc(monkeypatch, tmp_path) -> None:
    (tmp_path / "uninstall.sh").write_text("echo\nSYNCOJ_UNINSTALL_EOF\n", encoding="utf-8")
    monkeypatch.setattr(judge_dist, "TEMPLATES", tmp_path)
    with pytest.raises(RuntimeError):
        judge_dist.uninstall_script("uninstall.sh")
    (tmp_path / "uninstall.ps1").write_text("'@\n", encoding="utf-8")
    with pytest.raises(RuntimeError):
        judge_dist.uninstall_script("uninstall.ps1")
