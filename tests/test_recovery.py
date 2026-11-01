"""Backend/bridge recovery integration with synthetic cards only; no devices."""
import copy
import time
from urllib.error import HTTPError

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.nfc import make_dump, parse_dump
from bridge.main import SimulatedCard, Worker, synthetic_dump


@pytest.fixture
def recovery(tmp_path):
    app = create_app(tmp_path / "server")
    with TestClient(app) as client:
        setup = client.post("/api/setup", json={"username": "recovery", "password": "synthetic-recovery-password"})
        assert setup.status_code == 200, setup.text
        card = client.post("/api/cards", json={"label": "Recovery blue", "uid": "E011223344556677"}).json()
        recipe = client.post("/api/recipes", json={
            "name": "Recovery synthetic", "dose_g": 18, "ratio": 16, "total_water_ml": 288,
            "grind_setting": 62, "grinder_rpm": 80,
            "pours": [{"volume_ml": volume, "temperature_c": 92, "flow_ml_s": 3.2,
                       "pattern": "circle", "pause_s": 30 if i == 0 else 10}
                      for i, volume in enumerate((54, 117, 117))],
        }).json()
        token = client.post("/api/tokens", json={"kind": "bridge"}).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        api = ClientAPI(client, headers)
        api.call("POST", "/api/bridge/heartbeat", heartbeat("simulation"))
        worker = Worker(api, SimulatedCard(tmp_path / "bridge"), tmp_path / "bridge")
        yield client, card, recipe, api, worker


class ClientAPI:
    """Use the bridge's real JSON contract against the in-process FastAPI app."""
    def __init__(self, client, headers):
        self.client, self.headers = client, headers
        self.results = []

    def call(self, method, path, body=None):
        response = self.client.request(method, path, headers=self.headers, json=body)
        if response.status_code >= 400:
            raise HTTPError("https://synthetic.invalid" + path, response.status_code,
                            response.text, response.headers, None)
        if path.endswith("/result") and method == "POST":
            self.results.append((path, copy.deepcopy(body)))
        return response.json()


def heartbeat(mode):
    return {"name": "Synthetic test bridge", "mode": mode, "version": "test", "port": None}


def enqueue_read(client, card):
    response = client.post("/api/jobs", json={"kind": "read", "card_id": card["id"],
        "bridge_mode": client.get("/api/status").json()["bridge"]["mode"]})
    assert response.status_code == 200, response.text
    return response.json()


def enqueue_write(client, card, recipe):
    return client.post("/api/jobs", json={"kind": "write", "card_id": card["id"],
        "recipe_id": recipe["id"], "confirm": True, "confirm_label": card["label"],
        "accept_encoding_changes": True, "bridge_mode": client.get("/api/status").json()["bridge"]["mode"]})


def fixture_read(client, card, api, dump):
    enqueue_read(client, card)
    job = api.call("GET", "/api/bridge/jobs/next")["job"]
    api.call("POST", f"/api/bridge/jobs/{job['id']}/backup", {"dump": dump, "mode": "simulation"})
    result = api.call("POST", f"/api/bridge/jobs/{job['id']}/result", {
        "success": True, "mode": "simulation", "readback_dump": dump,
        "simulated": True, "hardware_verified": False})
    assert result["job"]["status"] == "succeeded"


def test_worker_result_replay_is_idempotent_and_does_not_duplicate_backups(recovery):
    client, card, recipe, api, worker = recovery
    enqueue_read(client, card)
    assert worker.poll()
    response = enqueue_write(client, card, recipe)
    assert response.status_code == 200, response.text
    assert worker.poll()
    path, payload = api.results[-1]
    completed = api.call("GET", path.removesuffix("/result"))["job"]
    assert completed["status"] == "succeeded"
    assert completed["simulated"] and not completed["hardware_verified"]
    before = client.get(f"/api/cards/{card['id']}/backups").json()["items"]
    replay = api.call("POST", path, payload)
    assert replay["already_completed"] is True
    assert replay["job"]["result_digest"] == completed["result_digest"]
    after = client.get(f"/api/cards/{card['id']}/backups").json()["items"]
    assert [b["id"] for b in after] == [b["id"] for b in before]
    with pytest.raises(HTTPError) as conflict:
        api.call("POST", path, {**payload, "success": False})
    assert conflict.value.code == 409


def test_interrupted_simulation_requires_and_accepts_fresh_simulated_read(recovery):
    client, card, recipe, api, worker = recovery
    enqueue_read(client, card)
    assert worker.poll()
    assert enqueue_write(client, card, recipe).status_code == 200
    interrupted = api.call("GET", "/api/bridge/jobs/next")["job"]
    store = client.app.state.store
    bridge = store.get("system", "bridge")
    bridge["last_seen_epoch"] = time.time() - 60
    store.put("system", bridge)
    resolved = client.post(f"/api/jobs/{interrupted['id']}/resolve", json={
        "confirm_bridge_stopped": True, "confirm_label": card["label"]})
    assert resolved.status_code == 200, resolved.text
    assert store.get("cards", card["id"])["needs_fresh_read"] is True
    api.call("POST", "/api/bridge/heartbeat", heartbeat("simulation"))
    assert enqueue_write(client, card, recipe).status_code == 409
    enqueue_read(client, card)
    assert worker.poll()
    assert store.get("cards", card["id"])["needs_fresh_read"] is False
    assert enqueue_write(client, card, recipe).status_code == 200
    assert store.get("jobs", interrupted["id"])["status"] == "failed"


def test_restore_corrupt_target_is_rejected_before_job_creation(recovery):
    client, card, recipe, api, worker = recovery
    good = synthetic_dump(card["uid"])
    memory = bytearray.fromhex(parse_dump(good)["memory_hex"])
    memory[41] ^= 1  # Deliberately invalidate CRC while leaving layout/header intact.
    bad = make_dump(card["uid"], bytes(memory))
    fixture_read(client, card, api, bad)
    original = client.get(f"/api/cards/{card['id']}/backups").json()["items"][0]
    assert original["decoded"]["checksum_valid"] is False
    fixture_read(client, card, api, good)
    before = client.get("/api/jobs").json()["items"]
    response = client.post("/api/jobs", json={"kind": "restore", "card_id": card["id"],
        "backup_id": original["id"], "confirm": True, "confirm_label": card["label"], "bridge_mode": "simulation"})
    assert response.status_code == 422, response.text
    assert "checksum" in response.json()["detail"].lower()
    assert client.get("/api/jobs").json()["items"] == before


def test_simulation_cannot_establish_hardware_baseline_or_complete_hardware_job(recovery):
    client, card, recipe, api, worker = recovery
    enqueue_read(client, card)
    assert worker.poll()
    api.call("POST", "/api/bridge/heartbeat", heartbeat("hardware"))
    assert enqueue_write(client, card, recipe).status_code == 409
    read_job = enqueue_read(client, card)
    # The worker still runs with a SimulatedCard. It must reject the hardware job
    # before card access and the server must reject a simulation-mode success.
    assert worker.poll()
    state = api.call("GET", f"/api/bridge/jobs/{read_job['id']}")["job"]
    assert state["status"] == "failed"
    assert state["hardware_verified"] is False
    assert "mode" in state["error"].lower()
    backups = client.get(f"/api/cards/{card['id']}/backups").json()["items"]
    assert all(backup["source"] != "hardware" for backup in backups)
    stored = client.app.state.store.get("cards", card["id"])
    assert stored["verified_at"] is None
    assert stored["recipe_id"] is None
