from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from urllib.error import HTTPError

from app.nfc import parse_dump, preview_recipe
from bridge.main import (API, BridgeError, Proxmark, SimulatedCard, Worker, synthetic_dump, validate_write)
from tests.test_nfc import recipe


class FakeAPI:
    def __init__(self, acknowledge=True):
        self.calls = []
        self.acknowledge = acknowledge
        self.job = None

    def call(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path.endswith("/backup"):
            if not self.acknowledge:
                raise ConnectionError("Backup server unavailable")
            return {"saved": True, "sha256": parse_dump(body["dump"])["sha256"]}
        if path.endswith("/next"):
            job, self.job = self.job, None
            return {"job": job}
        return {"saved": True}


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.api = FakeAPI()
        self.card = SimulatedCard(self.directory)
        self.base = {"id": "job-test", "card_id": "card-test", "kind": "write",
                     "uid": "E011223344556677", "label": "Blue card",
                     "confirmed_at": datetime.now(timezone.utc).isoformat()}
        current = parse_dump(self.card.read(self.base))
        preview = preview_recipe(recipe(), bytes.fromhex(current["memory_hex"]))
        self.job = {**self.base, "expected_memory_sha256": current["sha256"],
                    "memory_hex": preview["memory_hex"], "write_blocks": preview["write_blocks"]}

    def test_simulation_backups_and_readback_are_explicitly_simulated(self):
        worker = Worker(self.api, self.card, self.directory)
        result = worker.execute(self.job)
        self.assertTrue(result["success"], result)
        self.assertTrue(result["simulated"])
        self.assertFalse(result["hardware_verified"])
        self.assertEqual(parse_dump(result["readback_dump"])["memory_hex"], self.job["memory_hex"])
        self.assertTrue((self.directory / "backups/card-test/job-test-before.json").exists())
        self.assertTrue(self.api.calls[0][1].endswith("/backup"))

    def test_no_write_when_remote_backup_fails(self):
        api = FakeAPI(acknowledge=False)
        worker = Worker(api, self.card, self.directory)
        before = parse_dump(self.card.read(self.job))["sha256"]
        result = worker.execute(self.job)
        self.assertFalse(result["success"])
        self.assertEqual(before, parse_dump(self.card.read(self.job))["sha256"])

    def test_no_write_without_confirmation_or_for_changed_card(self):
        worker = Worker(self.api, self.card, self.directory)
        for changes in ({"confirmed_at": None}, {"expected_memory_sha256": "wrong"},
                        {"uid": "E011223344556688"}, {"confirmed_at": "2000-01-01T00:00:00+00:00"}):
            result = worker.execute({**self.job, **changes})
            self.assertFalse(result["success"])
        self.assertEqual(parse_dump(self.card.read(self.job))["sha256"], self.job["expected_memory_sha256"])

    def test_forbidden_header_and_custom_config_changes_rejected(self):
        current = parse_dump(self.card.read(self.job))
        for position in (0, 224, 252):
            memory = bytearray.fromhex(self.job["memory_hex"])
            memory[position] ^= 1
            with self.assertRaises(BridgeError):
                validate_write({**self.job, "memory_hex": memory.hex()}, current)

    def test_mismatched_block_plan_rejected(self):
        current = parse_dump(self.card.read(self.job))
        with self.assertRaises(BridgeError):
            validate_write({**self.job, "write_blocks": [{"block": 0, "data": "00000000"}]}, current)

    def test_hardware_needs_enable_and_exact_local_label(self):
        worker = Worker(self.api, self.card, self.directory, hardware=True)
        with self.assertRaises(BridgeError):
            worker.execute(self.job)
        worker = Worker(self.api, self.card, self.directory, hardware=True, allow_writes=True,
                        confirmer=lambda prompt: "yes")
        result = worker.execute(self.job)
        self.assertFalse(result["success"])
        self.assertIn("confirmation declined", result["error"])

    def test_replay_journal_prevents_reexecution(self):
        worker = Worker(self.api, self.card, self.directory)
        self.api.job = self.job
        self.assertTrue(worker.poll())
        count = len([x for x in self.api.calls if x[1].endswith("/backup")])
        self.api.job = self.job
        self.assertFalse(worker.poll())
        self.assertEqual(count, len([x for x in self.api.calls if x[1].endswith("/backup")]))

    def test_interrupted_job_is_reported_unverified_without_replay(self):
        folder = self.directory / "jobs"
        folder.mkdir()
        (folder / "interrupted.json").write_text(json.dumps({"id": "interrupted", "mode": "hardware", "posted": False}))
        worker = Worker(self.api, self.card, self.directory)
        worker.post_pending()
        result = self.api.calls[-1][2]
        self.assertFalse(result["success"])
        self.assertFalse(result["hardware_verified"])
        self.assertIn("interrupted", result["error"])

    def test_transport_rejects_unencrypted_lan_and_embedded_credentials(self):
        for url in ("http://192.168.1.2:8000", "https://user:pass@192.168.1.2", "https://example.test/api"):
            with self.assertRaises(BridgeError):
                API(url, "dummy-token")

    def test_resolved_server_job_archives_pending_result_without_blocking(self):
        folder = self.directory / "jobs"
        folder.mkdir()
        journal = folder / "resolved.json"
        journal.write_text(json.dumps({"id": "resolved", "posted": False, "result": {"success": False}}))
        api = FakeAPI()
        def call(method, path, body=None):
            if method == "POST":
                raise HTTPError("https://lan.test", 409, "conflict", {}, None)
            return {"job": {"id": "resolved", "status": "failed"}}
        api.call = call
        Worker(api, self.card, self.directory).post_pending()
        record = json.loads(journal.read_text())
        self.assertTrue(record["posted"])
        self.assertEqual(record["reconciliation"], "server_terminal_conflict")
        self.assertEqual(record["result"], {"success": False})

    def test_hardware_client_never_uses_shell_and_validates_block_plan(self):
        executable = self.directory / "proxmark3.exe"
        executable.write_bytes(b"test placeholder - never executed")
        pm = Proxmark(str(executable), "COM6", self.directory)
        with patch("bridge.main.subprocess.run", return_value=SimpleNamespace(returncode=0,
                   stdout="[+] Writing to page 10 (0x0A) | AABBCCDD ( ok )", stderr="")) as run:
            pm.write_block(self.job, 10, "AABBCCDD")
            args = run.call_args
            self.assertFalse(args.kwargs["shell"])
            self.assertEqual(args.args[0][-1], "hf 15 wrbl -u E011223344556677 -b 10 -d AABBCCDD")
        with self.assertRaises(BridgeError):
            pm.write_block(self.job, 56, "AABBCCDD")
        with self.assertRaises(BridgeError):
            pm.write_block(self.job, 10, "AA; wipe")

    def test_failed_write_retains_readback_and_is_not_marked_verified(self):
        original = self.card.write_block
        def fail_after_change(job, block, data):
            original(job, block, data)
            raise BridgeError("Lost device response")
        self.card.write_block = fail_after_change
        result = Worker(self.api, self.card, self.directory).execute(self.job)
        self.assertFalse(result["success"])
        self.assertFalse(result["hardware_verified"])
        self.assertIn("readback_dump", result)

    def test_approved_restore_recovers_corrupt_pour_count_but_normal_write_refuses(self):
        original = parse_dump(self.card.read(self.job))
        target = bytes.fromhex(original["memory_hex"])
        damaged = bytearray(target)
        damaged[40] = 255  # Neither a supported pour count nor valid count flags.
        self.card.write_block(self.job, 10, bytes(damaged[40:44]).hex().upper())
        current = parse_dump(self.card.read(self.job))
        self.assertIsNone(current["decoded"])
        recovery = {**self.job, "id": "recover-corrupt-count", "kind": "restore",
                    "expected_memory_sha256": current["sha256"],
                    "memory_hex": original["memory_hex"],
                    "write_blocks": [{"block": 10, "data": target[40:44].hex().upper()}]}
        worker = Worker(self.api, self.card, self.directory)
        refused = worker.execute({**recovery, "id": "edit-corrupt-count", "kind": "write"})
        self.assertFalse(refused["success"])
        self.assertEqual(parse_dump(self.card.read(self.job))["sha256"], current["sha256"])
        restored = worker.execute(recovery)
        self.assertTrue(restored["success"], restored)
        readback = parse_dump(restored["readback_dump"])
        self.assertTrue(readback["decoded"]["checksum_valid"])
        self.assertEqual(readback["memory_hex"], original["memory_hex"])

    def test_restore_cannot_change_protected_header_or_configuration(self):
        current = parse_dump(self.card.read(self.job))
        for offset in (0, 224, 252):
            memory = bytearray.fromhex(self.job["memory_hex"])
            memory[offset] ^= 1
            with self.assertRaises(BridgeError):
                validate_write({**self.job, "kind": "restore", "memory_hex": memory.hex()}, current)


if __name__ == "__main__":
    unittest.main()
