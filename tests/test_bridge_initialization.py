"""Initialization safety and recovery using synthetic cards only; no devices."""
import copy
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.initialization import (CAPABILITY, CHIP_PROFILE, bind_initialization_plan,
                                build_initialization_plan)
from app.nfc import make_dump, parse_dump
from bridge.main import BridgeError, Proxmark, Worker, durable_json, main, synthetic_dump
from tests.test_nfc import recipe


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.job = None
        self.acknowledge = True

    def call(self, method, path, body=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        if path.endswith("/backup"):
            return {"saved": self.acknowledge, "sha256": parse_dump(body["dump"])["sha256"]}
        if path.startswith("/api/bridge/jobs/next"):
            job, self.job = self.job, None
            return {"job": job}
        return {"saved": True}


class SyntheticHardware:
    """No transport or hardware methods; holds an in-memory native-format dump."""
    def __init__(self, dump):
        self.dump = copy.deepcopy(dump)
        self.events = []
        self.reads = 0
        self.read_hook = None
        self.uid_hook = None
        self.fail_block = None

    def read(self, job):
        self.reads += 1
        self.events.append("read")
        if self.read_hook:
            self.read_hook(self, self.reads)
        return copy.deepcopy(self.dump)

    def initialize_uid(self, job):
        self.events.append("factory_uid")
        self.dump["Card"]["uid"] = bytes.fromhex(job["target_uid"])[::-1].hex().upper()
        if self.uid_hook:
            self.uid_hook(self, job)

    def initialize_block(self, job, block, data):
        assert parse_dump(self.dump)["uid"] == job["target_uid"]
        assert 0 <= block <= 39
        self.events.append(("block", block))
        self.dump["blocks"][str(block)] = data
        if block == self.fail_block:
            raise BridgeError("Synthetic lost write acknowledgement")

    def write_block(self, *args):
        raise AssertionError("Initialization must not use the ordinary recipe-write method")


@pytest.fixture
def setup(tmp_path):
    blank = make_dump("E053011011223344", bytes(256))
    blank["Card"]["ic"] = "8B"
    template = synthetic_dump("E077112233445566")
    template["Card"]["ic"] = "8B"
    context = {"card_id": "blank", "card_label": "Blank blue", "baseline_backup_id": "baseline",
        "original_backup_id": "original", "template_card_id": "template", "template_label": "Template",
        "template_backup_id": "template-original", "recipe_id": "recipe", "recipe_version": 1,
        "mode": "hardware"}
    plan = bind_initialization_plan(build_initialization_plan(blank, template, recipe(),
                                   chip_profile=CHIP_PROFILE), context)
    job = {**plan, **{key: value for key, value in context.items() if key != "card_label"},
        "id": "initialize-test", "kind": "initialize", "label": context["card_label"],
        "uid": plan["original_uid"], "confirmed_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": time.time() + 300, "confirm_initialize": True,
        "accept_special_configuration_changes": True, "accept_encoding_changes": True,
        "expected_preview_sha256": plan["preview_sha256"]}
    api = FakeAPI()
    card = SyntheticHardware(blank)
    worker = Worker(api, card, tmp_path, hardware=True, allow_writes=True, allow_initialization=True,
                    confirmer=lambda prompt: "INITIALIZE Blank blue")
    return job, api, card, worker, tmp_path


def test_initialization_requires_distinct_enable_and_hardware_before_any_read(setup):
    job, api, card, worker, directory = setup
    for hardware, writes, initialization in ((False, False, True), (True, False, True), (True, True, False)):
        guarded = Worker(api, card, directory, hardware=hardware, allow_writes=writes,
                         allow_initialization=initialization)
        assert guarded.capabilities == []
        with pytest.raises(BridgeError):
            guarded.execute(job)
    assert card.events == []


def test_success_requires_post_uid_full_read_before_header_blocks_and_final_readback(setup):
    job, api, card, worker, directory = setup
    result = worker.execute(job)
    assert result["success"] and result["hardware_verified"]
    assert result["machine_acceptance_verified"] is False
    assert result["hidden_configuration_preservation_verified"] is False
    assert not result["manual_reconciliation_required"]
    assert card.events[:4] == ["read", "read", "factory_uid", "read"]
    assert card.events[4:-1] == [("block", item["block"]) for item in job["write_blocks"]]
    assert card.events[-1] == "read"
    assert parse_dump(result["readback_dump"])["uid"] == job["target_uid"]
    assert parse_dump(result["readback_dump"])["memory_hex"] == job["memory_hex"]
    assert bytes.fromhex(job["memory_hex"])[160:] == bytes(96)
    for suffix in ("-before", "-approved-initialization", "-after-uid", "-after", "-initialization-state"):
        assert (directory / "backups/blank" / (job["id"] + suffix + ".json")).is_file()


def test_prompt_discloses_unbacked_config_and_exact_initialization_phrase(setup):
    job, api, card, worker, directory = setup
    prompts = []
    worker.confirmer = lambda prompt: prompts.append(prompt) or "WRITE Blank blue"
    result = worker.execute(job)
    assert not result["success"] and not result["initialization_attempted"]
    assert card.events == ["read"]
    for text in ("47=3F038B00", "52=00000000", "NOT backed up", "INITIALIZE Blank blue", job["preview_sha256"]):
        assert text in prompts[0]


@pytest.mark.parametrize("changes", [
    {"accept_special_configuration_changes": False}, {"confirm_initialize": False},
    {"expected_preview_sha256": "0" * 64}, {"confirmed_at": "2000-01-01T00:00:00+00:00"},
    {"expires_at": 0}, {"target_uid": "E077000000000000"},
    {"write_blocks": [{"block": 56, "data": "00000000"}]},
])
def test_tampered_or_expired_approval_never_attempts_factory_uid(setup, changes):
    job, api, card, worker, directory = setup
    result = worker.execute({**job, **changes})
    assert not result["success"]
    assert "factory_uid" not in card.events


def test_server_backup_acknowledgement_is_required_before_confirmation(setup):
    job, api, card, worker, directory = setup
    api.acknowledge = False
    worker.confirmer = Mock(side_effect=AssertionError("Must not prompt without acknowledged backup"))
    result = worker.execute(job)
    assert not result["success"] and card.events == ["read"]
    worker.confirmer.assert_not_called()


@pytest.mark.parametrize("field", ["memory", "uid", "metadata", "locks"])
def test_confirmation_delay_rechecks_complete_native_snapshot(setup, field):
    job, api, card, worker, directory = setup
    def alter(card, number):
        if number != 2:
            return
        if field == "memory": card.dump["blocks"]["0"] = "01000000"
        elif field == "uid": card.dump["Card"]["uid"] = "00000000000077E0"
        elif field == "metadata": card.dump["Card"]["dsfid"] = "01"
        else: card.dump["Card"]["locks"] = "01" + "00" * 63
    card.read_hook = alter
    result = worker.execute(job)
    assert not result["success"] and "factory_uid" not in card.events


def test_expiry_is_rechecked_after_human_confirmation(setup):
    job, api, card, worker, directory = setup
    def confirm(prompt):
        job["expires_at"] = 0
        return "INITIALIZE Blank blue"
    worker.confirmer = confirm
    assert not worker.execute(job)["success"]
    assert "factory_uid" not in card.events


@pytest.mark.parametrize("field", ["memory", "uid", "metadata", "locks"])
def test_unexpected_post_uid_changes_stop_before_header_and_preserve_evidence(setup, field):
    job, api, card, worker, directory = setup
    def alter(card, job):
        if field == "memory": card.dump["blocks"]["56"] = "01000000"
        elif field == "uid": card.dump["Card"]["uid"] = "00000000000077E0"
        elif field == "metadata": card.dump["Card"]["afilock"] = "01"
        else: card.dump["Card"]["locks"] = "00" * 63 + "01"
    card.uid_hook = alter
    result = worker.execute(job)
    assert not result["success"] and result["manual_reconciliation_required"]
    assert result["initialization_attempted"] and not result["hardware_verified"]
    assert not any(isinstance(event, tuple) for event in card.events)
    assert "initialization_identity_readback_dump" in result and "readback_dump" in result
    assert (directory / "backups/blank/initialize-test-failed-readback.json").exists()


@pytest.mark.parametrize("observed", ["old", "target", "partial"])
def test_uncertain_uid_command_is_not_retried_and_records_any_observed_identity(setup, observed):
    job, api, card, worker, directory = setup
    uid = {"old": job["original_uid"], "target": job["target_uid"], "partial": "E077112211223344"}[observed]
    def fail(card, job):
        card.dump["Card"]["uid"] = bytes.fromhex(uid)[::-1].hex().upper()
        raise BridgeError("Synthetic uncertain factory response")
    card.uid_hook = fail
    result = worker.execute(job)
    assert not result["success"] and result["manual_reconciliation_required"]
    assert result["observed_uid"] == uid
    assert card.events.count("factory_uid") == 1
    assert not any(isinstance(event, tuple) for event in card.events)


def test_expiry_after_uid_change_stops_before_memory_writes(setup):
    job, api, card, worker, directory = setup
    card.uid_hook = lambda card, job: job.update(expires_at=0)
    result = worker.execute(job)
    assert not result["success"] and result["observed_uid"] == job["target_uid"]
    assert not any(isinstance(event, tuple) for event in card.events)


def test_lost_memory_ack_is_not_retried_and_saves_partial_readback(setup):
    job, api, card, worker, directory = setup
    card.fail_block = job["write_blocks"][0]["block"]
    result = worker.execute(job)
    assert not result["success"] and result["manual_reconciliation_required"]
    assert len([event for event in card.events if isinstance(event, tuple)]) == 1
    assert result["readback_dump"]["blocks"][str(card.fail_block)] == job["write_blocks"][0]["data"]


def test_invalid_failure_readback_is_retained_as_raw_evidence(setup):
    job, api, card, worker, directory = setup
    def fail(card, job):
        card.dump["Card"]["uid"] = "broken"
        card.last_read_evidence = copy.deepcopy(card.dump)
        raise BridgeError("Synthetic interrupted UID")
    card.uid_hook = fail
    result = worker.execute(job)
    assert not result["success"] and result["manual_reconciliation_required"]
    assert result["readback_dump"]["Card"]["uid"] == "broken"
    assert (directory / "backups/blank/initialize-test-failed-raw-readback.json").exists()


def test_final_readback_metadata_mismatch_is_not_verified(setup):
    job, api, card, worker, directory = setup
    def alter(card, number):
        if number == 4: card.dump["Card"]["dsfidlock"] = "01"
    card.read_hook = alter
    result = worker.execute(job)
    assert not result["success"] and not result["hardware_verified"]
    assert result["manual_reconciliation_required"]


def test_filtered_claim_and_durable_journal_prevent_initialization_replay(setup):
    job, api, card, worker, directory = setup
    api.job = job
    assert worker.poll()
    assert ("GET", "/api/bridge/jobs/next?kind=initialize", None) in api.calls
    api.job = job
    assert not worker.poll()
    assert card.events.count("factory_uid") == 1


def test_interrupted_initialization_posts_evidence_and_manual_flag_without_hardware(setup):
    job, api, card, worker, directory = setup
    durable_json(directory / "jobs/initialize-test.json", {"id": job["id"], "card_id": job["card_id"],
        "kind": "initialize", "mode": "hardware", "posted": False})
    durable_json(directory / "backups/blank/initialize-test-after-uid.json", {"dump": card.dump})
    worker.post_pending()
    payload = api.calls[-1][2]
    assert not payload["success"] and payload["manual_reconciliation_required"]
    assert payload["initialization_identity_readback_dump"] == card.dump
    assert card.events == []


def test_recovery_read_uid_mismatch_remains_failed_but_preserves_native_evidence(setup):
    job, api, card, worker, directory = setup
    card.dump["Card"]["uid"] = bytes.fromhex(job["target_uid"])[::-1].hex().upper()
    result = worker.execute({**job, "kind": "read"})
    assert not result["success"] and not result["hardware_verified"]
    assert result["readback_dump"] == card.dump
    assert "does not match" in result["error"]
    assert not api.calls  # No matching-identity backup was acknowledged or adopted.
    assert (directory / "backups/blank/initialize-test-failed-read.json").exists()
    assert card.events == ["read"]


def make_proxmark(tmp_path):
    executable = tmp_path / "proxmark3.exe"
    executable.write_bytes(b"synthetic placeholder, never execute")
    return Proxmark(str(executable), "COM9", tmp_path)


def test_adapter_uses_official_factory_command_and_saves_output(setup, monkeypatch):
    job, api, card, worker, directory = setup
    pm = make_proxmark(directory)
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout="Setting new UID ( ok )", stderr=""))
    monkeypatch.setattr("bridge.main.subprocess.run", run)
    pm.initialize_uid(job)
    assert run.call_args.args[0][-1] == "hf 15 csetuid --v2 -u " + job["target_uid"]
    assert run.call_args.kwargs["shell"] is False
    assert (pm.directory / "initialize-test-factory-uid-command.json").exists()


@pytest.mark.parametrize("output", ["Setting new UID ( fail )", "Setting new UID ( ok )\ntimeout", "Verifying..."])
def test_adapter_rejects_ambiguous_identity_success(setup, monkeypatch, output):
    job, api, card, worker, directory = setup
    pm = make_proxmark(directory)
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout=output, stderr=""))
    monkeypatch.setattr("bridge.main.subprocess.run", run)
    with pytest.raises(BridgeError): pm.initialize_uid(job)
    assert run.call_count == 1


def test_adapter_preserves_partial_timeout_output(setup, monkeypatch):
    job, api, card, worker, directory = setup
    pm = make_proxmark(directory)
    monkeypatch.setattr("bridge.main.subprocess.run", Mock(side_effect=subprocess.TimeoutExpired(
        "synthetic", 180, output=b"UID E0 77 11 22 11 22 33 44")))
    with pytest.raises(BridgeError): pm.initialize_uid(job)
    proof = json.loads((pm.directory / "initialize-test-factory-uid-command.json").read_text())
    assert proof["timeout"] and "E0 77" in proof["output"]


def test_separate_block_method_is_plan_bounded_and_normal_header_write_stays_forbidden(setup, monkeypatch):
    job, api, card, worker, directory = setup
    pm = make_proxmark(directory)
    block = job["write_blocks"][0]
    run = Mock(return_value=SimpleNamespace(returncode=0,
        stdout=f"Writing to page {block['block']} | {block['data']} ( ok )", stderr=""))
    monkeypatch.setattr("bridge.main.subprocess.run", run)
    pm.initialize_block(job, block["block"], block["data"])
    assert run.call_args.args[0][-1] == f"hf 15 wrbl -u {job['target_uid']} -b {block['block']} -d {block['data']}"
    for number, data in ((40, "00000000"), (56, "00000000"), (0, "FFFFFFFF"), (True, "00000000")):
        with pytest.raises(BridgeError): pm.initialize_block(job, number, data)
    with pytest.raises(BridgeError): pm.write_block(job, 0, "00010203")
    assert run.call_count == 1


@pytest.mark.parametrize("args", [
    ["--allow-initialization"], ["--hardware", "--allow-initialization"],
    ["--hardware", "--allow-writes", "--allow-initialization"],
    ["--wait-for-job", "1"], ["--once", "--wait-for-job", "121"],
    ["--once", "--wait-for-job", "-1"],
])
def test_cli_rejects_unsafe_or_unbounded_initialization_startup(monkeypatch, args):
    monkeypatch.setattr("bridge.main.sys.stdin.isatty", lambda: False)
    with pytest.raises(SystemExit) as failure: main(args)
    assert failure.value.code == 2


def test_wait_once_keeps_gated_heartbeat_and_stops_after_one_job(tmp_path, monkeypatch):
    api = FakeAPI()
    monkeypatch.setenv("XBLOOM_BRIDGE_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("bridge.main.sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("bridge.main.API", lambda *args: api)
    monkeypatch.setattr("bridge.main.Proxmark", lambda *args: object())
    poll = Mock(side_effect=[False, True])
    monkeypatch.setattr("bridge.main.Worker.poll", poll)
    monkeypatch.setattr("bridge.main.threading.Thread", lambda **kwargs: SimpleNamespace(start=lambda: None))
    monkeypatch.setattr("bridge.main.time.sleep", lambda seconds: None)
    assert main(["--hardware", "--allow-writes", "--allow-initialization", "--once", "--wait-for-job", "120"]) == 0
    assert poll.call_count == 2
    assert api.calls[0][2]["capabilities"] == [CAPABILITY]


def test_normal_once_never_advertises_initialization(tmp_path, monkeypatch):
    api = FakeAPI()
    monkeypatch.setenv("XBLOOM_BRIDGE_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("bridge.main.API", lambda *args: api)
    monkeypatch.setattr("bridge.main.Worker.poll", lambda self: False)
    monkeypatch.setattr("bridge.main.threading.Thread", lambda **kwargs: SimpleNamespace(start=lambda: None))
    assert main(["--once"]) == 0
    assert api.calls[0][2]["capabilities"] == []


def test_wait_once_timeout_stops_without_claiming_or_reading_a_card(tmp_path, monkeypatch, capsys):
    api = FakeAPI()
    monkeypatch.setenv("XBLOOM_BRIDGE_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("bridge.main.API", lambda *args: api)
    poll = Mock(return_value=False)
    monkeypatch.setattr("bridge.main.Worker.poll", poll)
    monkeypatch.setattr("bridge.main.threading.Thread", lambda **kwargs: SimpleNamespace(start=lambda: None))
    monkeypatch.setattr("bridge.main.time.monotonic", Mock(side_effect=[10, 11, 12]))
    monkeypatch.setattr("bridge.main.time.sleep", lambda seconds: None)
    assert main(["--once", "--wait-for-job", "2"]) == 0
    assert poll.call_count == 2
    assert "No eligible job arrived" in capsys.readouterr().out


def test_synthetic_worker_completes_real_api_initialization_contract(tmp_path, monkeypatch):
    from tests.test_initialization_api import (heartbeat, preview, queue_request,
                                               setup as api_setup)
    from tests.test_recovery import ClientAPI
    with contextmanager(api_setup.__wrapped__)(tmp_path / "api", monkeypatch) as state:
        heartbeat(state)
        response = state["client"].post("/api/jobs", json=queue_request(state, preview(state)))
        assert response.status_code == 200, response.text
        job = response.json()
        card = SyntheticHardware(state["blank_dump"])
        api = ClientAPI(state["client"], state["headers"])
        worker = Worker(api, card, tmp_path / "bridge", hardware=True, allow_writes=True,
            allow_initialization=True, confirmer=lambda prompt: "INITIALIZE " + state["card"]["label"])
        assert worker.poll()
        completed = api.call("GET", f"/api/bridge/jobs/{job['id']}")["job"]
        assert completed["status"] == "succeeded", completed
        assert completed["initialization_identity_readback_backup_id"]
        assert completed["hardware_verified"] is True
        assert completed["machine_acceptance_verified"] is False
        inventory = state["client"].app.state.store.get("cards", state["card"]["id"])
        assert inventory["uid"] == job["target_uid"]
        assert inventory["original_uid"] == job["original_uid"]
        assert card.events.count("factory_uid") == 1
