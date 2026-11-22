"""前端构建是否过期的检测（syncoj_server/webdist.py 与 web/scripts/build-info-plugin.mjs）。"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from syncoj_server import cli, webdist
from syncoj_server.config import Settings

REPO = Path(__file__).resolve().parents[2]
PLUGIN = REPO / "web" / "scripts" / "build-info-plugin.mjs"


def make_web(root: Path) -> Path:
    web = root / "web"
    (web / "src" / "views").mkdir(parents=True)
    (web / "scripts").mkdir()
    (web / "index.html").write_bytes(b"<html>\r\n</html>\r\n")
    (web / "package.json").write_text('{"version": "9.9.9"}', encoding="utf-8")
    (web / "src" / "main.ts").write_text("console.log(1)\n", encoding="utf-8")
    (web / "src" / "views" / "A.vue").write_bytes("<template>中文</template>\r\n".encode("utf-8"))
    (web / "src" / ".hidden").write_text("ignored", encoding="utf-8")
    (web / "scripts" / "x.mjs").write_text("export {}\n", encoding="utf-8")
    (web / "node_modules").mkdir()
    (web / "node_modules" / "junk.js").write_text("ignored", encoding="utf-8")
    return web


def build_dist(web: Path, version: str = "9.9.9", source: str = None) -> Path:
    dist = web / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html></html>", encoding="utf-8")
    info = {"version": version, "source_sha256": source or webdist.source_sha256(web), "built_at": "x"}
    (dist / webdist.BUILD_INFO_NAME).write_text(json.dumps(info), encoding="utf-8")
    return dist


def test_fresh_build_is_not_stale(tmp_path: Path) -> None:
    dist = build_dist(make_web(tmp_path))
    assert not webdist.check(dist, "9.9.9").stale


def test_no_dist_is_not_stale(tmp_path: Path) -> None:
    assert not webdist.check(None, "9.9.9").stale
    assert not webdist.check(tmp_path / "missing", "9.9.9").stale


def test_old_build_without_info_is_stale(tmp_path: Path) -> None:
    dist = build_dist(make_web(tmp_path))
    (dist / webdist.BUILD_INFO_NAME).unlink()
    status = webdist.check(dist, "9.9.9")
    assert status.stale and webdist.BUILD_INFO_NAME in status.reason


def test_version_mismatch_is_stale(tmp_path: Path) -> None:
    dist = build_dist(make_web(tmp_path), version="0.1.0")
    status = webdist.check(dist, "9.9.9")
    assert status.stale and "0.1.0" in status.reason


def test_source_change_is_stale(tmp_path: Path) -> None:
    web = make_web(tmp_path)
    dist = build_dist(web)
    (web / "src" / "main.ts").write_text("console.log(2)\n", encoding="utf-8")
    assert webdist.check(dist, "9.9.9").stale


def test_line_endings_and_ignored_files_do_not_matter(tmp_path: Path) -> None:
    web = make_web(tmp_path)
    before = webdist.source_sha256(web)
    (web / "index.html").write_bytes(b"<html>\n</html>\n")  # CRLF → LF
    (web / "src" / ".hidden").write_text("changed", encoding="utf-8")
    (web / "node_modules" / "junk.js").write_text("changed", encoding="utf-8")
    assert webdist.source_sha256(web) == before


def test_release_layout_without_sources_checks_version_only(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("x", encoding="utf-8")
    (dist / webdist.BUILD_INFO_NAME).write_text(json.dumps({"version": "9.9.9", "source_sha256": "abc"}))
    assert not webdist.check(dist, "9.9.9").stale
    assert webdist.check(dist, "1.0.0").stale


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node")
def test_python_and_node_fingerprints_agree(tmp_path: Path) -> None:
    web = make_web(tmp_path)
    node = subprocess.run(
        ["node", str(PLUGIN), str(web)], stdout=subprocess.PIPE, check=True, universal_newlines=True
    ).stdout.strip()
    assert node == webdist.source_sha256(web)


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node")
def test_fingerprint_of_real_web_dir_agrees() -> None:
    node = subprocess.run(
        ["node", str(PLUGIN), str(REPO / "web")], stdout=subprocess.PIPE, check=True, universal_newlines=True
    ).stdout.strip()
    assert node == webdist.source_sha256(REPO / "web")


def test_serve_warns_when_stale(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    dist = build_dist(make_web(tmp_path), version="0.1.0")
    settings = Settings()
    settings.web_dist = dist
    assert cli._check_web_dist(settings) == 0
    err = capsys.readouterr().err
    assert "前端构建已过期" in err and "syncoj-server build-web" in err


def test_build_without_npm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(webdist.shutil, "which", lambda name: None)
    assert webdist.build(make_web(tmp_path)) == 2
