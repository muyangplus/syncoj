"""内容回收：删资产 / 删选手 / 删场次都要回收没人再用的 blob；共享的留着。"""

from __future__ import annotations

from pathlib import Path

from conftest import agent_headers, do_tick, scan_entry, sha256_of

CODE = b"int main(){return 1;}\n"


def blob_exists(app, sha: str) -> bool:
    return app.state.ctx.blobs.path_for(sha).is_file()


def upload_asset(client, contest, headers, name: str, data: bytes) -> dict:
    response = client.post(
        "/api/v1/admin/contests/%d/assets" % contest["id"],
        files={"file": (name, data, "application/octet-stream")},
        headers=headers,
    )
    assert response.status_code in (200, 201), response.text
    return response.json()


def upload_code(client, enrolled) -> str:
    do_tick(client, enrolled["token"], [scan_entry("p1/p1.cpp", CODE)],
            machine_id=enrolled["machine_id"])
    response = client.post(
        "/api/v1/agent/files",
        data={"path": "p1/p1.cpp", "sha256": sha256_of(CODE)},
        files={"file": ("p1.cpp", CODE, "application/octet-stream")},
        headers=agent_headers(enrolled["token"]),
    )
    assert response.status_code == 200, response.text
    return sha256_of(CODE)


def test_删资产回收它的内容(client, contest, admin_headers, app) -> None:
    asset = upload_asset(client, contest, admin_headers, "a.txt", b"only-me")
    assert blob_exists(app, asset["sha256"])

    assert client.delete("/api/v1/admin/assets/%d" % asset["id"],
                         headers=admin_headers).status_code == 200

    assert not blob_exists(app, asset["sha256"])


def test_共享的内容不回收(client, contest, admin_headers, app) -> None:
    first = upload_asset(client, contest, admin_headers, "a.txt", b"shared")
    upload_asset(client, contest, admin_headers, "b.txt", b"shared")

    client.delete("/api/v1/admin/assets/%d" % first["id"], headers=admin_headers)

    assert blob_exists(app, first["sha256"]), "另一个资产还在用这份内容"


def test_删选手回收他的代码内容(client, contest, admin_headers, enrolled, app) -> None:
    sha = upload_code(client, enrolled)
    assert blob_exists(app, sha)

    players = client.get("/api/v1/admin/contests/%d/players" % contest["id"],
                         headers=admin_headers).json()["items"]
    player = [p for p in players if p["player_no"] == enrolled["player_no"]][0]
    response = client.delete(
        "/api/v1/admin/players/%d" % player["id"],
        params={"confirm": player["player_no"]}, headers=admin_headers,
    )
    assert response.status_code == 200, response.text

    assert not blob_exists(app, sha)


def test_删场次回收代码与资产内容(client, contest, admin_headers, enrolled, app) -> None:
    code_sha = upload_code(client, enrolled)
    asset = upload_asset(client, contest, admin_headers, "a.txt", b"contest-asset")

    response = client.delete(
        "/api/v1/admin/contests/%d" % contest["id"],
        params={"confirm": contest["slug"]}, headers=admin_headers,
    )
    assert response.status_code == 200, response.text

    assert not blob_exists(app, code_sha)
    assert not blob_exists(app, asset["sha256"])
