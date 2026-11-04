"""Synthetic API checks. Hardware mode here is a mocked bridge, never a device."""
import copy
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.initialization import CAPABILITY, CHIP_PROFILE
from app.main import create_app
from tests.test_initialization import native_dump, recipe, target_dump


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("XBLOOM_AUTH_MODE", "trusted_lan")
    monkeypatch.setenv("XBLOOM_LAN_ONLY", "true")
    monkeypatch.setenv("XBLOOM_ALLOWED_HOSTS", "testserver")
    with TestClient(create_app(tmp_path / "data")) as client:
        assert client.get("/api/session").status_code == 200
        token = client.post("/api/tokens", json={"kind": "bridge"}).json()["token"]
        state = {"client": client, "headers": {"Authorization": f"Bearer {token}"}, "mode": "hardware"}
        heartbeat(state, capable=False)
        state["card"] = client.post("/api/cards", json={"label": "Synthetic blank", "uid": "E000000000000001"}).json()
        state["source"] = client.post("/api/cards", json={"label": "Synthetic initialized template", "uid": "E000000000000002"}).json()
        state["blank_dump"] = native_dump()
        state["source_dump"] = native_dump(state["source"]["uid"], blank=False)
        state["original"] = read_card(state, state["card"], state["blank_dump"])
        state["template"] = read_card(state, state["source"], state["source_dump"])
        state["recipe"] = client.post("/api/recipes", json=recipe()).json()
        yield state


def heartbeat(state, *, capable=True, mode=None):
    if mode is not None:
        state["mode"] = mode
    response = state["client"].post("/api/bridge/heartbeat", headers=state["headers"], json={
        "name": "Synthetic initialization test bridge", "mode": state["mode"], "version": "test",
        "capabilities": [CAPABILITY] if capable else []})
    assert response.status_code == 200, response.text


def read_card(state, card, dump, *, success=True):
    client, headers = state["client"], state["headers"]
    queued = client.post("/api/jobs", json={"kind": "read", "card_id": card["id"], "bridge_mode": state["mode"]})
    assert queued.status_code == 200, queued.text
    job = client.get("/api/bridge/jobs/next", headers=headers).json()["job"]
    response = client.post(f"/api/bridge/jobs/{job['id']}/result", headers=headers,
        json={"success": True, "mode": state["mode"], "readback_dump": dump})
    assert response.status_code == 200, response.text
    assert response.json()["job"]["status"] == ("succeeded" if success else "failed"), response.text
    return client.get(f"/api/cards/{card['id']}/backups").json()["items"][0]


def preview_request(state, **changes):
    return {"template_backup_id": state["template"]["id"], "recipe_id": state["recipe"]["id"],
        "version": state["recipe"]["version"], "bridge_mode": state["mode"], "chip_profile": CHIP_PROFILE, **changes}


def preview(state, **changes):
    response = state["client"].post(f"/api/cards/{state['card']['id']}/initialize-preview", json=preview_request(state, **changes))
    assert response.status_code == 200, response.text
    return response.json()


def queue_request(state, plan, **changes):
    return {**preview_request(state), "kind": "initialize", "card_id": state["card"]["id"],
        "confirm": True, "confirm_label": state["card"]["label"], "confirm_initialize": True,
        "confirm_template_label": state["source"]["label"], "accept_special_configuration_changes": True,
        "accept_encoding_changes": True, "expected_preview_sha256": plan["preview_sha256"], **changes}


def claim(state):
    heartbeat(state)
    response = state["client"].post("/api/jobs", json=queue_request(state, preview(state)))
    assert response.status_code == 200, response.text
    job = state["client"].get("/api/bridge/jobs/next?kind=initialize", headers=state["headers"]).json()["job"]
    assert job["kind"] == "initialize"
    return job


def acknowledge(state, job, dump=None):
    return state["client"].post(f"/api/bridge/jobs/{job['id']}/backup", headers=state["headers"],
        json={"dump": dump or state["blank_dump"], "mode": state["mode"]})


def finish(state, job, **changes):
    return state["client"].post(f"/api/bridge/jobs/{job['id']}/result", headers=state["headers"],
        json={"success": True, "mode": state["mode"], "readback_dump": target_dump(job), **changes})


def test_read_only_bridge_can_preview_without_mutation_but_cannot_queue(setup):
    state = setup
    client = state["client"]
    before = client.get("/api/jobs").json()
    plan = preview(state)
    assert plan["initialization_context"]["original_backup_id"] == state["original"]["id"]
    assert plan["target_uid"] == state["source"]["uid"]
    assert plan["hidden_configuration_backup_available"] is False
    assert client.get("/api/jobs").json() == before
    assert client.post("/api/jobs", json=queue_request(state, plan)).status_code == 409
    assert state["original"]["initialization_blank_candidate"] is True
    assert state["template"]["initialization_template_candidate"] is True
    assert state["original"]["initialization_native_json"] is True
    assert state["original"]["native_metadata"]["ic"] == "8B"


@pytest.mark.parametrize("change", [
    {"confirm": False}, {"confirm_label": "wrong"}, {"confirm_initialize": False},
    {"confirm_template_label": "wrong"}, {"accept_special_configuration_changes": False},
    {"expected_preview_sha256": "0" * 64}, {"target_uid": "E000000000000009"},
    {"template_dump": {}}, {"chip_profile": "iso15693_gen2_64_uid_only"}, {"version": None},
])
def test_queue_requires_exact_explicit_confirmations_and_references(setup, change):
    heartbeat(setup)
    plan = preview(setup)
    response = setup["client"].post("/api/jobs", json=queue_request(setup, plan, **change))
    assert response.status_code in {409, 422}, response.text
    assert not any(job["kind"] == "initialize" for job in setup["client"].get("/api/jobs").json()["items"])


def test_success_adopts_only_verified_target_identity_and_preserves_original_and_template(setup):
    state = setup
    store = state["client"].app.state.store
    original = copy.deepcopy(store.get("backups", state["original"]["id"]))
    original_bytes = (store.root / original["file"]).read_bytes()
    source = copy.deepcopy(store.get("cards", state["source"]["id"]))
    template = copy.deepcopy(store.get("backups", state["template"]["id"]))
    job = claim(state)
    assert acknowledge(state, job).status_code == 200
    response = finish(state, job)
    assert response.status_code == 200, response.text
    result = response.json()["job"]
    assert result["status"] == "succeeded", result
    assert result["hardware_verified"] is True
    assert result["hidden_configuration_preservation_verified"] is False
    assert result["machine_acceptance_verified"] is False
    actual = store.get("cards", state["card"]["id"])
    assert actual["uid"] == state["source"]["uid"]
    assert actual["original_uid"] == state["card"]["uid"]
    assert actual["initialization_job_id"] == job["id"]
    assert actual["template_backup_id"] == template["id"]
    assert actual["hidden_configuration_verified"] is False and actual["offline_acceptance"] == "not_tested"
    assert actual["recipe_id"] == state["recipe"]["id"]
    assert store.get("backups", original["id"]) == original and original["original"] is True
    assert (store.root / original["file"]).read_bytes() == original_bytes
    assert store.get("cards", source["id"]) == source
    assert store.get("backups", template["id"]) == template
    before = len(store.all("backups"))
    assert finish(state, job).json()["already_completed"] is True
    assert len(store.all("backups")) == before


def test_missing_acknowledged_original_prevents_success_even_if_target_matches(setup):
    job = claim(setup)
    result = finish(setup, job).json()["job"]
    assert result["status"] == "failed" and "not backed up" in result["error"]
    card = setup["client"].app.state.store.get("cards", setup["card"]["id"])
    assert card["uid"] == setup["card"]["uid"]
    assert card["requires_manual_identity_reconciliation"] is True


@pytest.mark.parametrize("change", ["locks", "metadata", "uid"])
def test_before_write_backup_rechecks_full_original_identity_and_metadata(setup, change):
    job = claim(setup)
    dump = copy.deepcopy(setup["blank_dump"])
    if change == "locks":
        dump["Card"]["locks"] = "00" * 63 + "01"
    elif change == "metadata":
        dump["Card"]["afilock"] = "01"
    else:
        dump["Card"]["uid"] = "03000000000000E0"
    assert acknowledge(setup, job, dump).status_code == 409
    assert not setup["client"].app.state.store.get("jobs", job["id"]).get("prewrite_backup_id")


@pytest.mark.parametrize("change", ["uid", "tail", "locks", "metadata"])
def test_failed_verification_retains_evidence_and_blocks_mutations_after_fresh_read(setup, change):
    state = setup
    job = claim(state)
    assert acknowledge(state, job).status_code == 200
    dump = target_dump(job)
    if change == "uid":
        dump["Card"]["uid"] = "09000000000000E0"  # Even an unexpected partial UID is evidence only.
    elif change == "tail":
        dump["blocks"]["63"] = "00000001"
    elif change == "locks":
        dump["Card"]["locks"] = "00" * 63 + "01"
    else:
        dump["Card"]["dsfidlock"] = "01"
    result = finish(state, job, readback_dump=dump).json()["job"]
    assert result["status"] == "failed" and result["failed_readback_backup_id"]
    store = state["client"].app.state.store
    assert store.get("cards", state["card"]["id"])["uid"] == state["card"]["uid"]
    evidence = store.get("backups", result["failed_readback_backup_id"])
    assert evidence["source"] == "failed-readback-hardware" and evidence["original"] is False
    assert evidence["initialization_evidence_only"] is True
    # A read of the old identity is safe but cannot silently authorize a retry.
    read_card(state, state["card"], state["blank_dump"])
    card = store.get("cards", state["card"]["id"])
    assert card["requires_manual_identity_reconciliation"] is True
    assert card["needs_fresh_read"] is True
    response = state["client"].post(f"/api/cards/{card['id']}/initialize-preview", json=preview_request(state))
    assert response.status_code == 409
    assert state["client"].post("/api/jobs", json={"kind": "write", "card_id": card["id"],
        "recipe_id": state["recipe"]["id"], "confirm": True, "confirm_label": card["label"],
        "bridge_mode": "hardware"}).status_code == 409


def test_unparseable_partial_identity_is_preserved_as_raw_failure_evidence(setup):
    job = claim(setup)
    assert acknowledge(setup, job).status_code == 200
    dump = target_dump(job)
    dump["Card"]["uid"] = "0000000000000000"
    result = finish(setup, job, success=False, readback_dump=dump).json()["job"]
    assert result["status"] == "failed" and "failed_readback_evidence_file" in result
    store = setup["client"].app.state.store
    assert json.loads((store.root / result["failed_readback_evidence_file"]).read_text()) == dump
    assert store.get("cards", setup["card"]["id"])["uid"] == setup["card"]["uid"]


def test_evidence_storage_error_does_not_skip_manual_reconciliation(setup, monkeypatch):
    job = claim(setup)
    assert acknowledge(setup, job).status_code == 200
    original_open = Path.open

    def fail_evidence(path, *args, **kwargs):
        if path.name.endswith("-initialization-evidence.json"):
            raise OSError("Synthetic evidence disk failure")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_evidence)
    result = finish(setup, job, success=False).json()["job"]
    assert result["status"] == "failed" and "disk failure" in result["evidence_error"]
    assert result["manual_reconciliation_required"] is True
    assert setup["client"].app.state.store.get("cards", setup["card"]["id"])["requires_manual_identity_reconciliation"] is True


def test_verified_readback_storage_failure_does_not_adopt_identity(setup, monkeypatch):
    job = claim(setup)
    assert acknowledge(setup, job).status_code == 200
    original_open = Path.open

    def fail_snapshot(path, *args, **kwargs):
        if (path.parent.name == setup["card"]["id"] and args and args[0] == "x"
                and not path.name.endswith("-initialization-evidence.json")):
            raise OSError("Synthetic snapshot disk failure")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_snapshot)
    response = finish(setup, job)
    assert response.status_code == 200
    result = response.json()["job"]
    assert result["status"] == "failed" and "disk failure" in result["error"]
    assert result["hardware_verified"] is False
    card = setup["client"].app.state.store.get("cards", setup["card"]["id"])
    assert card["uid"] == setup["card"]["uid"]
    assert card["requires_manual_identity_reconciliation"] is True


def test_normal_read_of_new_uid_after_failure_retains_evidence_without_identity_adoption(setup):
    job = claim(setup)
    assert acknowledge(setup, job).status_code == 200
    finish(setup, job, success=False)
    evidence = read_card(setup, setup["card"], target_dump(job), success=False)
    assert evidence["initialization_evidence_only"] is True
    assert setup["client"].app.state.store.get("cards", setup["card"]["id"])["uid"] == setup["card"]["uid"]


def test_interrupted_post_uid_evidence_is_kept_without_adoption_or_live_baseline(setup):
    job = claim(setup)
    assert acknowledge(setup, job).status_code == 200
    intermediate = native_dump(job["target_uid"])
    result = finish(setup, job, success=False, readback_dump=None,
        initialization_attempted=True, initialization_identity_readback_dump=intermediate).json()["job"]
    store = setup["client"].app.state.store
    assert result["status"] == "failed"
    assert result["initialization_attempted"] is True
    assert result["initialization_identity_observed_uid"] == job["target_uid"]
    evidence = store.get("backups", result["initialization_identity_readback_backup_id"])
    assert evidence["source"] == "initialization-identity-hardware" and evidence["original"] is False
    assert json.loads((store.root / evidence["file"]).read_text()) == intermediate
    assert store.get("cards", setup["card"]["id"])["uid"] == job["original_uid"]
    assert store.get("cards", setup["card"]["id"])["requires_manual_identity_reconciliation"] is True


@pytest.mark.parametrize("claim", [{"mode": "simulation"}, {"mode": "hardware", "simulated": True}, {"simulated": True}])
def test_normal_hardware_read_rejects_explicit_simulation_backup(setup, claim):
    client, headers = setup["client"], setup["headers"]
    response = client.post("/api/jobs", json={"kind": "read", "card_id": setup["card"]["id"], "bridge_mode": "hardware"})
    assert response.status_code == 200
    job = client.get("/api/bridge/jobs/next", headers=headers).json()["job"]
    before = client.app.state.store.all("backups")
    response = client.post(f"/api/bridge/jobs/{job['id']}/backup", headers=headers,
        json={"dump": setup["blank_dump"], **claim})
    assert response.status_code == 409
    assert client.app.state.store.all("backups") == before


def test_hardware_result_cannot_claim_simulated_data_as_verified_read(setup):
    client, headers = setup["client"], setup["headers"]
    client.post("/api/jobs", json={"kind": "read", "card_id": setup["card"]["id"], "bridge_mode": "hardware"})
    job = client.get("/api/bridge/jobs/next", headers=headers).json()["job"]
    before = [backup["id"] for backup in client.app.state.store.all("backups") if backup["source"] == "hardware"]
    result = client.post(f"/api/bridge/jobs/{job['id']}/result", headers=headers,
        json={"success": True, "mode": "hardware", "simulated": True, "readback_dump": setup["blank_dump"]}).json()["job"]
    assert result["status"] == "failed" and result["hardware_verified"] is False
    assert [backup["id"] for backup in client.app.state.store.all("backups") if backup["source"] == "hardware"] == before


def test_old_read_only_bridge_cannot_claim_queued_initialization(setup):
    heartbeat(setup)
    response = setup["client"].post("/api/jobs", json=queue_request(setup, preview(setup)))
    assert response.status_code == 200
    heartbeat(setup, capable=False)
    assert setup["client"].get("/api/bridge/jobs/next", headers=setup["headers"]).json()["job"] is None
    heartbeat(setup)
    assert setup["client"].get("/api/bridge/jobs/next?kind=read", headers=setup["headers"]).json()["job"] is None
    assert setup["client"].get("/api/bridge/jobs/next?kind=initialize", headers=setup["headers"]).json()["job"]["kind"] == "initialize"


def test_interrupted_initialization_requires_manual_reconciliation(setup):
    job = claim(setup)
    store = setup["client"].app.state.store
    status = store.get("system", "bridge")
    status["last_seen_epoch"] = time.time() - 60
    store.put("system", status)
    response = setup["client"].post(f"/api/jobs/{job['id']}/resolve", json={
        "confirm_bridge_stopped": True, "confirm_label": setup["card"]["label"]})
    assert response.status_code == 200
    assert store.get("cards", setup["card"]["id"])["requires_manual_identity_reconciliation"] is True


def test_imported_template_cannot_authorize_initialization(setup):
    imported = setup["client"].post(f"/api/cards/{setup['source']['id']}/backups", json={"dump": setup["source_dump"]}).json()
    response = setup["client"].post(f"/api/cards/{setup['card']['id']}/initialize-preview",
        json=preview_request(setup, template_backup_id=imported["id"]))
    assert response.status_code == 422


def test_recipe_version_and_fresh_backup_are_bound_to_preview(setup):
    old = preview(setup)
    updated = setup["client"].patch(f"/api/recipes/{setup['recipe']['id']}", json={"grind_setting": 60}).json()
    assert updated["version"] == 2
    new = preview(setup, version=2)
    assert new["preview_sha256"] != old["preview_sha256"]
    assert new["initialization_context"]["recipe_version"] == 2
    assert preview(setup)["preview_sha256"] == old["preview_sha256"]  # Old immutable version still addresses exactly itself.
    read_card(setup, setup["card"], setup["blank_dump"])
    heartbeat(setup)
    response = setup["client"].post("/api/jobs", json=queue_request(setup, old))
    assert response.status_code == 409


def test_initialization_preview_is_dashboard_only_and_import_identity_guard_unchanged(setup):
    client = setup["client"]
    assert client.post(f"/api/cards/{setup['card']['id']}/backups", json={"dump": setup["source_dump"]}).status_code == 409
    assert client.patch(f"/api/cards/{setup['card']['id']}", json={"uid": setup["source"]["uid"]}).status_code == 409
    api = client.post("/api/tokens", json={"kind": "api"}).json()["token"]
    client.delete("/api/session")
    response = client.post(f"/api/cards/{setup['card']['id']}/initialize-preview", json=preview_request(setup),
        headers={"Authorization": f"Bearer {api}"})
    assert response.status_code == 401
