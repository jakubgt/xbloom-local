import io
import json
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

import app.nfc as nfc
from app.main import create_app
from app.manage import restore
from app.nfc import make_dump, parse_dump, preview_recipe
from bridge.main import synthetic_dump


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path / "data")
    with TestClient(app) as client:
        response = client.post("/api/setup", json={"username": "tester", "password": "synthetic-test-password"})
        assert response.status_code == 200
        yield client


def recipe(**changes):
    result = {"name": "Synthetic test recipe", "dose_g": 18, "ratio": 16, "total_water_ml": 288,
        "grind_setting": 60, "grinder_rpm": 80,
        "pours": [{"volume_ml": volume, "temperature_c": 92, "flow_ml_s": 3.2,
                   "pattern": "circle", "pause_s": 30 if i == 0 else 10} for i, volume in enumerate((54, 117, 117))]}
    result.update(changes)
    return result


def card_and_recipe(client):
    card = client.post("/api/cards", json={"label": "Test card A", "uid": "E011223344556677"}).json()
    saved = client.post("/api/recipes", json=recipe()).json()
    return card, saved


def bridge_headers(client, mode="simulation"):
    token = client.post("/api/tokens", json={"kind": "bridge"}).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    response = client.post("/api/bridge/heartbeat", json={"name": "Synthetic bridge", "mode": mode, "version": "test"}, headers=headers)
    assert response.status_code == 200
    return headers


def complete_read(client, card, headers, mode="simulation", dump=None):
    dump = dump or synthetic_dump(card["uid"])
    response = client.post("/api/jobs", json={"kind": "read", "card_id": card["id"], "bridge_mode": mode})
    assert response.status_code == 200, response.text
    job = client.get("/api/bridge/jobs/next", headers=headers).json()["job"]
    result = client.post(f"/api/bridge/jobs/{job['id']}/result", headers=headers,
                         json={"success": True, "mode": mode, "readback_dump": dump})
    assert result.json()["job"]["status"] == "succeeded", result.text
    return job


def write_job(client, card, saved, **overrides):
    payload = {"kind": "write", "card_id": card["id"], "recipe_id": saved["id"],
               "confirm": True, "confirm_label": card["label"], "accept_encoding_changes": True,
               "bridge_mode": client.get("/api/status").json()["bridge"]["mode"]}
    return client.post("/api/jobs", json={**payload, **overrides})


def test_auth_origin_host_and_cookie_boundaries(client):
    assert client.cookies.get("xbloom_session")
    response = client.post("/api/beans", json={"name": "Synthetic"}, headers={"Origin": "https://evil.example"})
    assert response.status_code == 403
    assert client.get("/api/status", headers={"Host": "evil.example"}).status_code == 400
    assert client.post("/api/setup", json={"username": "again", "password": "does-not-replace"}).status_code == 409
    client.delete("/api/session")
    assert client.get("/api/status").status_code == 401
    assert client.post("/api/session", json={"username": "tester", "password": "wrong"}).status_code == 401
    login = client.post("/api/session", json={"username": "tester", "password": "synthetic-test-password"})
    assert login.status_code == 200
    assert "HttpOnly" in login.headers["set-cookie"] and "SameSite=strict" in login.headers["set-cookie"]


def test_login_rate_limit(client):
    client.delete("/api/session")
    for _ in range(10):
        assert client.post("/api/session", json={"username": "tester", "password": "bad"}).status_code == 401
    assert client.post("/api/session", json={"username": "tester", "password": "bad"}).status_code == 429


def test_recipe_history_and_water_validation(client):
    saved = client.post("/api/recipes", json=recipe()).json()
    changed = client.patch(f"/api/recipes/{saved['id']}", json={"grind_setting": 62}).json()
    assert changed["version"] == 2
    versions = client.get(f"/api/recipes/{saved['id']}/versions").json()["items"]
    assert [v["version"] for v in versions] == [2, 1]
    assert versions[1]["recipe"]["grind_setting"] == 60
    assert client.post("/api/recipes", json=recipe(total_water_ml=300)).status_code == 422


@pytest.mark.parametrize("kind,initial,changes,field", [
    ("beans", {"name": "Synthetic bean"}, {"rating": "SYNTHETIC_PRIVATE_VALUE"}, "rating"),
    ("recipes", recipe(), {"grind_setting": "SYNTHETIC_PRIVATE_VALUE"}, "grind_setting"),
    ("cards", {"label": "Synthetic card"}, {"uid": "SYNTHETIC_PRIVATE_VALUE"}, "uid"),
])
def test_manual_validation_reports_fields_without_echoing_input(client, kind, initial, changes, field):
    saved = client.post(f"/api/{kind}", json=initial).json()
    before = client.app.state.store.get(kind, saved["id"])
    response = client.patch(f"/api/{kind}/{saved['id']}", json=changes)
    assert response.status_code == 422
    assert field + ":" in response.json()["detail"]
    assert "SYNTHETIC_PRIVATE_VALUE" not in response.text
    assert all(text not in response.text for text in ("input_value", "input_type", "errors.pydantic.dev"))
    assert client.app.state.store.get(kind, saved["id"]) == before
    if kind == "recipes":
        assert len(client.get(f"/api/recipes/{saved['id']}/versions").json()["items"]) == 1


@pytest.mark.parametrize("endpoint", ["preview", "approve"])
def test_invalid_import_has_safe_validation_and_never_persists(client, endpoint):
    profile = {"schema_version": 1, "bean": {"name": "SYNTHETIC_PRIVATE_VALUE"},
               "recipe": recipe(total_water_ml=300)}
    response = client.post(f"/api/import/{endpoint}", json={"profile": profile, "approved": True})
    assert response.status_code == 422
    assert "recipe: Value error, Pour volumes must add up to total water" in response.json()["detail"]
    assert all(text not in response.text for text in ("SYNTHETIC_PRIVATE_VALUE", "input_value", "input_type", "errors.pydantic.dev"))
    assert client.get("/api/beans").json()["items"] == []
    assert client.get("/api/recipes").json()["items"] == []
    assert client.app.state.store.all("versions") == []


@pytest.mark.parametrize("endpoint", ["/api/recipes/preview", "/api/import/preview"])
def test_preview_hides_unexpected_codec_exception(client, monkeypatch, endpoint):
    def fail_round(value):
        raise TypeError("SYNTHETIC_INTERNAL_EXCEPTION /private/example.py")
    monkeypatch.setattr(nfc, "_round", fail_round)
    payload = recipe()
    if endpoint == "/api/import/preview":
        payload = {"profile": {"schema_version": 1, "bean": {"name": "Synthetic"}, "recipe": payload}}
    response = client.post(endpoint, json=payload)
    assert response.status_code == 200
    assert "SYNTHETIC_INTERNAL_EXCEPTION" not in response.text
    assert "/private/example.py" not in response.text
    result = response.json()
    preview = result["encoding"] if endpoint == "/api/import/preview" else result
    assert preview["compatible"] is False and preview["errors"]
    assert preview["recipe_hex"] is None and preview["memory_hex"] is None
    assert preview["checksum"] is None and preview["write_blocks"] == []
    assert client.get("/api/beans").json()["items"] == []
    assert client.get("/api/recipes").json()["items"] == []


def test_import_review_required_and_unknowns_preserved(client):
    profile = {"schema_version": 1, "bean": {"name": "Synthetic example", "country": None}, "recipe": recipe()}
    assert client.post("/api/import/preview", json={"profile": profile}).status_code == 200
    assert client.get("/api/beans").json()["items"] == []
    assert client.post("/api/import/approve", json={"profile": profile}).status_code == 422
    result = client.post("/api/import/approve", json={"profile": profile, "approved": True}).json()
    assert result["bean"]["country"] is None
    assert result["recipe"]["bean_id"] == result["bean"]["id"]
    assert client.delete(f"/api/beans/{result['bean']['id']}").status_code == 409


def test_api_and_bridge_tokens_are_scoped_and_rotatable(client):
    api = client.post("/api/tokens", json={"kind": "api"}).json()["token"]
    bridge = bridge_headers(client)
    client.delete("/api/session")
    header = {"Authorization": f"Bearer {api}"}
    assert client.get("/api/status", headers=header).status_code == 200
    assert client.post("/api/jobs", headers=header, json={}).status_code == 401
    assert client.post("/api/tokens", headers=header, json={"kind": "bridge"}).status_code == 401
    assert client.get("/api/bridge/jobs/next", headers=header).status_code == 401
    assert client.get("/api/status", headers=bridge).status_code == 401


def test_duplicate_uids_allowed_labels_unique_and_import_not_live_backup(client):
    card, saved = card_and_recipe(client)
    assert client.post("/api/cards", json={"label": "Test card B", "uid": card["uid"]}).status_code == 200
    assert client.post("/api/cards", json={"label": card["label"]}).status_code == 409
    response = client.post(f"/api/cards/{card['id']}/backups", json={"dump": synthetic_dump(card["uid"])})
    assert response.status_code == 200
    assert response.json()["source"] == "import"
    bridge_headers(client, "hardware")
    assert write_job(client, card, saved).status_code == 409
    preview = client.post(f"/api/cards/{card['id']}/preview", json={"recipe_id": saved["id"]}).json()
    assert preview["compatible"] and preview["card_bound"]
    assert client.delete(f"/api/cards/{card['id']}").status_code == 409


def test_simulated_write_full_lifecycle_is_never_hardware_verified(client):
    card, saved = card_and_recipe(client)
    headers = bridge_headers(client)
    dump = synthetic_dump(card["uid"])
    complete_read(client, card, headers, dump=dump)
    assert write_job(client, card, saved, confirm=False).status_code == 422
    assert write_job(client, card, saved, confirm_label="wrong card").status_code == 422
    response = write_job(client, card, saved)
    assert response.status_code == 200, response.text
    job = client.get("/api/bridge/jobs/next", headers=headers).json()["job"]
    assert client.get("/api/bridge/jobs/next", headers=headers).json()["job"] is None
    backup = client.post(f"/api/bridge/jobs/{job['id']}/backup", headers=headers, json={"dump": dump})
    assert backup.json()["saved"]
    target = make_dump(card["uid"], bytes.fromhex(job["memory_hex"]))
    result = client.post(f"/api/bridge/jobs/{job['id']}/result", headers=headers,
                         json={"success": True, "mode": "simulation", "readback_dump": target}).json()["job"]
    assert result["status"] == "succeeded" and result["simulated"] and not result["hardware_verified"]
    actual_card = client.get("/api/cards").json()["items"][0]
    assert actual_card["verified_at"] is None and actual_card["recipe_id"] is None
    assert client.post(f"/api/bridge/jobs/{job['id']}/result", headers=headers, json={"success": True}).status_code == 409


def test_readback_cannot_claim_success_without_prewrite_backup(client):
    card, saved = card_and_recipe(client)
    headers = bridge_headers(client)
    complete_read(client, card, headers)
    write_job(client, card, saved)
    job = client.get("/api/bridge/jobs/next", headers=headers).json()["job"]
    target = make_dump(card["uid"], bytes.fromhex(job["memory_hex"]))
    result = client.post(f"/api/bridge/jobs/{job['id']}/result", headers=headers,
                        json={"success": True, "mode": "simulation", "readback_dump": target}).json()["job"]
    assert result["status"] == "failed" and "not backed up" in result["error"]


def test_changed_contents_abort_and_mode_switch_needs_new_backup(client):
    card, saved = card_and_recipe(client)
    headers = bridge_headers(client)
    complete_read(client, card, headers)
    write_job(client, card, saved)
    job = client.get("/api/bridge/jobs/next", headers=headers).json()["job"]
    original = bytes.fromhex(parse_dump(synthetic_dump(card["uid"]))["memory_hex"])
    changed = make_dump(card["uid"], original[:-1] + b"\x01")
    assert client.post(f"/api/bridge/jobs/{job['id']}/backup", headers=headers, json={"dump": changed}).status_code == 409
    client.post(f"/api/bridge/jobs/{job['id']}/result", headers=headers, json={"success": False, "mode": "simulation"})
    bridge_headers(client, "hardware")
    assert write_job(client, card, saved).status_code == 409


def test_encoding_changes_require_separate_acknowledgment(client):
    card, _ = card_and_recipe(client)
    changed = recipe(ratio=16.5, total_water_ml=297)
    changed["pours"][2]["volume_ml"] += 9
    saved = client.post("/api/recipes", json=changed).json()
    headers = bridge_headers(client)
    complete_read(client, card, headers)
    assert write_job(client, card, saved, accept_encoding_changes=False).status_code == 422
    assert write_job(client, card, saved).status_code == 200


def test_job_expires_and_cannot_replay(client):
    card, saved = card_and_recipe(client)
    headers = bridge_headers(client)
    complete_read(client, card, headers)
    job = write_job(client, card, saved).json()
    job["expires_at"] = time.time() - 1
    client.app.state.store.put("jobs", job)
    assert client.get("/api/bridge/jobs/next", headers=headers).json()["job"] is None
    assert client.app.state.store.get("jobs", job["id"])["status"] == "expired"


def test_verified_card_cleared_on_hardware_claim_without_touching_device(client):
    card, saved = card_and_recipe(client)
    headers = bridge_headers(client, "hardware")
    complete_read(client, card, headers, mode="hardware")  # HTTP fixture only; no device access.
    store = client.app.state.store
    stored = store.get("cards", card["id"])
    stored.update(verified_at="previous", verification="verified")
    store.put("cards", stored)
    assert write_job(client, card, saved).status_code == 200
    client.get("/api/bridge/jobs/next", headers=headers)
    assert store.get("cards", card["id"])["verified_at"] is None


def test_full_backup_restore_and_no_active_job_replay(client, tmp_path):
    bean = client.post("/api/beans", json={"name": "Synthetic backup test"}).json()
    card, _ = card_and_recipe(client)
    bridge_headers(client)
    client.post("/api/jobs", json={"kind": "read", "card_id": card["id"], "bridge_mode": "simulation"})
    response = client.get("/api/backup")
    assert response.status_code == 200
    path = tmp_path / "backup.zip"
    path.write_bytes(response.content)
    destination = tmp_path / "restored"
    restore(path, destination)
    with TestClient(create_app(destination)) as restored:
        assert restored.get("/api/status").status_code == 401
        assert restored.post("/api/session", json={"username": "tester", "password": "synthetic-test-password"}).status_code == 200
        assert restored.get("/api/beans").json()["items"][0]["id"] == bean["id"]
        assert restored.get("/api/jobs").json()["items"][0]["status"] == "cancelled"
    with pytest.raises(ValueError, match="empty data directory"):
        restore(path, destination)


def test_restore_rejects_zip_slip(tmp_path):
    path = tmp_path / "malicious.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("../outside", "bad")
    with pytest.raises(ValueError, match="Unsafe backup path"):
        restore(path, tmp_path / "destination")


def test_photo_upload_rejects_html_and_requires_auth(client):
    bean = client.post("/api/beans", json={"name": "Synthetic photo test"}).json()
    assert client.post(f"/api/beans/{bean['id']}/photo", files={"file": ("bad.html", b"<script>bad</script>", "text/html")}).status_code == 422


def test_simulation_read_does_not_clear_hardware_recovery_requirement(client):
    card, saved = card_and_recipe(client)
    store = client.app.state.store
    item = store.get("cards", card["id"])
    item.update(needs_fresh_read=True, needs_fresh_read_modes=["hardware"])
    store.put("cards", item)
    headers = bridge_headers(client)
    complete_read(client, card, headers)
    assert store.get("cards", card["id"])["needs_fresh_read_modes"] == ["hardware"]
    bridge_headers(client, "hardware")
    result = write_job(client, card, saved)
    assert result.status_code == 409 and "Read this card again" in result.json()["detail"]


def test_recipe_restore_can_recover_corrupt_current_pour_count(client):
    card, _ = card_and_recipe(client)
    headers = bridge_headers(client, "hardware")  # Synthetic API fixture, no USB device.
    original = synthetic_dump(card["uid"])
    complete_read(client, card, headers, mode="hardware", dump=original)
    backup = client.get(f"/api/cards/{card['id']}/backups").json()["items"][0]
    damaged = bytearray.fromhex(parse_dump(original)["memory_hex"])
    damaged[40] = 255
    complete_read(client, card, headers, mode="hardware", dump=make_dump(card["uid"], bytes(damaged)))
    response = client.post("/api/jobs", json={"kind": "restore", "card_id": card["id"], "backup_id": backup["id"],
                                             "confirm": True, "confirm_label": card["label"], "bridge_mode": "hardware"})
    assert response.status_code == 200, response.text
    assert bytes.fromhex(response.json()["memory_hex"]) == bytes.fromhex(backup["memory_hex"])


def test_bridge_mode_cannot_change_after_confirmation(client):
    card, saved = card_and_recipe(client)
    headers = bridge_headers(client, "hardware")
    complete_read(client, card, headers, mode="hardware")
    result = write_job(client, card, saved, bridge_mode="simulation")
    assert result.status_code == 409
    assert "Bridge mode changed" in result.json()["detail"]
