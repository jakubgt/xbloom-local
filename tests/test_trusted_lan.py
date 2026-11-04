import sys

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.manage import main as manage, restore
from app.security import load_auth, valid_password
from bridge.main import synthetic_dump


@pytest.fixture
def trusted_app(monkeypatch, tmp_path):
    monkeypatch.setenv("XBLOOM_AUTH_MODE", "trusted_lan")
    monkeypatch.setenv("XBLOOM_LAN_ONLY", "true")
    monkeypatch.setenv("XBLOOM_ALLOWED_HOSTS", "testserver")
    return create_app(tmp_path / "data")


def test_trusted_lan_opens_without_credentials_and_renews_expired_session(trusted_app):
    with TestClient(trusted_app, client=("192.168.1.20", 50000)) as client:
        assert client.get("/api/status").status_code == 401
        response = client.get("/api/session", headers={"Sec-Fetch-Site": "same-origin"})
        assert response.json() == {"configured": True, "authenticated": True,
                                   "username": None, "access_mode": "trusted_lan"}
        assert "HttpOnly" in response.headers["set-cookie"]
        assert "SameSite=strict" in response.headers["set-cookie"]
        first_cookie = client.cookies.get("xbloom_session")
        assert first_cookie
        assert load_auth(trusted_app.state.store.root) == {"tokens": {}}
        assert client.get("/api/status").status_code == 200
        settings = client.get("/api/settings").json()
        assert settings["username"] is None and settings["access_mode"] == "trusted_lan"
        assert settings["default_dose_g"] == 18
        assert "set-cookie" not in client.get("/api/session").headers
        with trusted_app.state.store.connect() as db:
            db.execute("UPDATE sessions SET expires=0")
        assert client.get("/api/status").status_code == 401
        assert client.get("/api/session").json()["authenticated"] is True
        assert client.cookies.get("xbloom_session") != first_cookie
        assert client.get("/api/status").status_code == 200


@pytest.mark.parametrize("headers", [
    {"Origin": "https://evil.example"},
    {"Origin": "null"},
    {"Origin": "http://[malformed"},
    {"Sec-Fetch-Site": "cross-site"},
    {"Sec-Fetch-Site": "same-site"},
])
def test_trusted_lan_refuses_cross_site_session_bootstrap(trusted_app, headers):
    with TestClient(trusted_app) as client:
        response = client.get("/api/session", headers=headers)
        assert response.status_code == 403
        assert "set-cookie" not in response.headers
        assert not client.cookies.get("xbloom_session")
        with trusted_app.state.store.connect() as db:
            assert db.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0


def test_trusted_lan_rejects_public_clients_and_unrecognized_hosts(trusted_app):
    with TestClient(trusted_app, client=("8.8.8.8", 50000)) as client:
        response = client.get("/api/session", headers={"X-Forwarded-For": "192.168.1.20"})
        assert response.status_code == 403
        assert "set-cookie" not in response.headers
    with TestClient(trusted_app, client=("192.168.1.20", 50000)) as client:
        response = client.get("/api/session", headers={"Host": "evil.example"})
        assert response.status_code == 400
        assert "set-cookie" not in response.headers
        client.get("/api/session")
        assert client.post("/api/beans", json={"name": "Synthetic"}, headers={"Origin": "https://evil.example"}).status_code == 403


@pytest.mark.parametrize("name,value", [
    ("XBLOOM_LAN_ONLY", "false"),
    ("XBLOOM_ALLOWED_HOSTS", "*"),
    ("XBLOOM_ALLOWED_HOSTS", "*.example.test"),
    ("XBLOOM_ALLOWED_HOSTS", ""),
    ("XBLOOM_AUTH_MODE", "typo"),
])
def test_trusted_lan_fails_closed_for_unsafe_configuration(monkeypatch, tmp_path, name, value):
    monkeypatch.setenv("XBLOOM_AUTH_MODE", "trusted_lan")
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        create_app(tmp_path / "not-created")
    assert not (tmp_path / "not-created").exists()


def test_trusted_lan_keeps_api_and_bridge_token_scopes(trusted_app):
    with TestClient(trusted_app) as client:
        client.get("/api/session")
        api_token = client.post("/api/tokens", json={"kind": "api"}).json()["token"]
        bridge_token = client.post("/api/tokens", json={"kind": "bridge"}).json()["token"]
        assert client.get("/api/bridge/jobs/next").status_code == 401
        client.cookies.clear()
        api_headers = {"Authorization": f"Bearer {api_token}"}
        bridge_headers = {"Authorization": f"Bearer {bridge_token}"}
        assert client.get("/api/status", headers=api_headers).status_code == 200
        assert client.post("/api/tokens", json={"kind": "bridge"}, headers=api_headers).status_code == 401
        assert client.post("/api/jobs", json={}, headers=api_headers).status_code == 401
        assert client.get("/api/backup", headers=api_headers).status_code == 401
        assert client.get("/api/bridge/jobs/next", headers=api_headers).status_code == 401
        assert client.get("/api/status", headers=bridge_headers).status_code == 401
        for headers in (api_headers, bridge_headers):
            session = client.get("/api/session", headers=headers)
            assert session.json()["authenticated"] is False
            assert "set-cookie" not in session.headers


def test_trusted_lan_still_requires_write_approval_and_physical_backup(trusted_app):
    with TestClient(trusted_app) as client:
        client.get("/api/session")
        card = client.post("/api/cards", json={"label": "Synthetic physical card", "uid": "E011223344556677"}).json()
        token = client.post("/api/tokens", json={"kind": "bridge"}).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        client.post("/api/bridge/heartbeat", json={"mode": "hardware", "name": "Synthetic hardware"}, headers=headers)
        request = {"kind": "write", "card_id": card["id"], "bridge_mode": "hardware"}
        assert client.post("/api/jobs", json=request).status_code == 422
        request.update(confirm=True, confirm_label="wrong card")
        assert client.post("/api/jobs", json=request).status_code == 422
        request["confirm_label"] = card["label"]
        assert client.post("/api/jobs", json=request).status_code == 409
        client.post(f"/api/cards/{card['id']}/backups", json={"dump": synthetic_dump(card["uid"])})
        assert client.post("/api/jobs", json=request).status_code == 409
        request["kind"] = "restore"
        assert client.post("/api/jobs", json=request).status_code == 409
        assert client.get("/api/jobs").json()["items"] == []


def test_token_only_config_backup_restore_and_password_transition(trusted_app, tmp_path, monkeypatch):
    with TestClient(trusted_app) as client:
        client.get("/api/session")
        trusted_cookie = client.cookies.get("xbloom_session")
        token = client.post("/api/tokens", json={"kind": "bridge"}).json()["token"]
        assert client.post("/api/setup", json={"username": "unused", "password": "unused-password"}).status_code == 409
        assert client.post("/api/session", json={"username": "unused", "password": "unused-password"}).status_code == 409
        auth = load_auth(trusted_app.state.store.root)
        assert set(auth) == {"tokens"}
        assert valid_password(auth, "unused", "unused-password") is False
        backup = client.get("/api/backup")
        assert backup.status_code == 200
        archive = tmp_path / "backup.zip"
        archive.write_bytes(backup.content)
    restored_root = tmp_path / "restored"
    restore(archive, restored_root)
    assert load_auth(restored_root) == auth
    with TestClient(create_app(restored_root)) as client:
        assert client.get("/api/status").status_code == 401
        assert client.get("/api/session").json()["authenticated"] is True
        assert client.get("/api/settings").json()["tokens_configured"] == ["bridge"]
        assert client.post("/api/bridge/heartbeat", json={"mode": "simulation"}, headers={"Authorization": f"Bearer {token}"}).status_code == 200
    monkeypatch.setenv("XBLOOM_AUTH_MODE", "password")
    with TestClient(create_app(trusted_app.state.store.root)) as client:
        client.cookies.set("xbloom_session", trusted_cookie)
        assert client.get("/api/status").status_code == 401
        session = client.get("/api/session").json()
        assert session["configured"] is False and session["authenticated"] is False
        assert client.post("/api/session", json={"username": "unused", "password": "unused-password"}).status_code == 401
        assert client.post("/api/setup", json={"username": "owner", "password": "synthetic-new-password"}).status_code == 200
        assert load_auth(trusted_app.state.store.root)["tokens"] == auth["tokens"]
        assert client.get("/api/status").status_code == 200


def test_manage_check_describes_login_free_mode(trusted_app, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["manage", "check", "--data-dir", str(trusted_app.state.store.root)])
    manage()
    assert "no username or password" in capsys.readouterr().out
