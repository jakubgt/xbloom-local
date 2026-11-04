"""Poll a LAN server; execute bounded Proxmark operations with durable backups.

Run from the repository root: python -m bridge.main
No physical device is touched unless --hardware is passed. Real writes additionally
require --allow-writes, a confirmed server job, and a local typed card-label check.
Blank-card factory initialization additionally requires --allow-initialization;
it changes identity and explicitly disclosed settings outside the ordinary backup.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import ssl
import subprocess
import sys
import time
import threading
from typing import Any
from urllib.parse import urlsplit
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener
from uuid import uuid4

from app.nfc import (NFCError, crc8_maxim, decode_recipe, make_dump, memory_sha256,
                     normalize_uid, parse_dump)
from app.initialization import (CAPABILITY as INITIALIZATION_CAPABILITY, CHIP_PROFILE,
                                initialization_snapshot, validate_initialization_job,
                                validate_initialization_readback)

VERSION = "0.1.0"
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


class BridgeError(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise BridgeError("Bridge API redirects are disabled; configure the final LAN origin.")


def safe_id(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value):
        raise BridgeError("Invalid inventory or job ID.")
    return value


def durable_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def acquire_instance_lock(directory: Path):
    """An OS lock is released on process death, unlike a stale sentinel file."""
    directory.mkdir(parents=True, exist_ok=True)
    handle = (directory / "bridge.lock").open("a+b")
    handle.seek(0, 2)
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise BridgeError("Another bridge is using this data directory. Run only one bridge per Proxmark.") from exc
    return handle


class API:
    def __init__(self, url: str, token: str, ca_file: str | None = None):
        parsed = urlsplit(url)
        if parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username or parsed.password:
            raise BridgeError("Server URL must be an HTTP(S) origin without embedded credentials.")
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise BridgeError("Server URL must be an origin, without a path or query.")
        if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise BridgeError("Use HTTPS for LAN connections; HTTP is allowed only for local loopback preview.")
        if not token:
            raise BridgeError("Set XBLOOM_BRIDGE_TOKEN to a bridge token issued by Settings.")
        self.url = url.rstrip("/")
        self.token = token
        self.context = ssl.create_default_context(cafile=ca_file or None)

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        raw = None if body is None else json.dumps(body, allow_nan=False).encode()
        request = Request(self.url + path, data=raw, method=method, headers={
            "Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        opener = build_opener(NoRedirect(), HTTPSHandler(context=self.context))
        with opener.open(request, timeout=30) as response:
            data = response.read(2_000_001)
            if len(data) > 2_000_000:
                raise BridgeError("Server response exceeded the size limit.")
            return json.loads(data) if data else {}


def synthetic_dump(uid: str) -> dict:
    """Deliberately synthetic authentication bytes: this is not a real xBloom card."""
    memory = bytearray(256)
    memory[:32] = bytes(range(32))
    memory[32:39] = b"SIMULAT"
    memory[39] = 2
    memory[40] = 24
    for i, volume in enumerate((54, 117, 117)):
        start = 41 + i * 8
        memory[start:start+8] = bytes((volume, 92, 2, 0, 216 if i == 0 else 246,
                                    18 if i == 0 else 0, 120 if i == 0 else 0, 30))
    memory[65:67] = bytes((20, 16))
    memory[67] = crc8_maxim(memory[:67])
    return make_dump(uid, bytes(memory))


class SimulatedCard:
    def __init__(self, directory: Path):
        self.directory = directory / "simulation"

    def read(self, job: dict) -> dict:
        path = self.directory / (safe_id(job["card_id"]) + ".json")
        if not path.exists():
            durable_json(path, synthetic_dump(normalize_uid(job.get("uid") or "E000000000000001")))
        return json.loads(path.read_text(encoding="utf-8"))

    def write_block(self, job: dict, block: int, data: str) -> None:
        dump = self.read(job)
        dump["blocks"][str(block)] = data
        durable_json(self.directory / (safe_id(job["card_id"]) + ".json"), dump)


class Proxmark:
    def __init__(self, executable: str, port: str, directory: Path):
        self.executable = Path(executable).expanduser().resolve()
        if not self.executable.is_file() or self.executable.suffix.lower() in (".bat", ".cmd", ".ps1"):
            raise BridgeError("PM3_EXECUTABLE must point to the real Proxmark client executable, not a shell wrapper.")
        if os.name == "nt" and self.executable.suffix.lower() != ".exe":
            raise BridgeError("Windows Proxmark client must be an .exe file.")
        if not re.fullmatch(r"COM[1-9][0-9]*", port, re.IGNORECASE):
            raise BridgeError("PM3_PORT must be a Windows COM port, such as COM6.")
        self.port = port.upper()
        self.directory = directory / "native-dumps"
        self.directory.mkdir(parents=True, exist_ok=True)

    def command(self, command: str, *, evidence_path: Path | None = None) -> str:
        # command is constructed exclusively by read/write_block from validated
        # numeric, hexadecimal, and UUID values. No server-provided shell scripts.
        try:
            completed = subprocess.run([str(self.executable), "-p", self.port,
                "--incognito", "-f", "-c", command], cwd=self.directory,
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=180, shell=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired as exc:
            if evidence_path is not None:
                def as_text(value):
                    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value or ""
                durable_json(evidence_path, {"command": command, "timeout": True,
                    "output": ANSI.sub("", as_text(exc.stdout) + "\n" + as_text(exc.stderr))})
            raise BridgeError("Proxmark command timed out; the card may already have changed. No retry attempted.") from exc
        output = ANSI.sub("", completed.stdout + "\n" + completed.stderr)
        if evidence_path is not None:
            durable_json(evidence_path, {"command": command, "returncode": completed.returncode,
                                          "output": output})
        if completed.returncode:
            raise BridgeError(f"Proxmark client failed with exit code {completed.returncode}.")
        return output

    def read(self, job: dict) -> dict:
        self.last_read_evidence = None
        filename = "read-" + uuid4().hex
        output = self.command(f"hf 15 dump -f {filename}")
        path = self.directory / (filename + ".json")
        if not path.is_file():
            raise BridgeError("Proxmark did not create a fresh JSON dump. Check the card and port.")
        dump = json.loads(path.read_text(encoding="utf-8"))
        self.last_read_evidence = dump
        parsed = parse_dump(dump)
        # This client can save a full-size zero-filled structure after an incomplete
        # read. Require its successful-read progress counter to reach every page.
        progress = [int(x) for x in re.findall(r"\bblk\s+(\d+)", output)]
        if not progress or max(progress) != parsed["block_count"]:
            raise BridgeError("Incomplete card read; dump retained locally, but not accepted as a write backup.")
        if re.search(r"no tag found|Tag returned Error|crc\s*\(\s*fail|command failed|timeout", output, re.I):
            raise BridgeError("Proxmark reported a read error; repeat a read before any write.")
        return dump

    def write_block(self, job: dict, block: int, data: str) -> None:
        uid = normalize_uid(job["uid"])
        if not 8 <= block <= 39 or not re.fullmatch(r"[0-9A-F]{8}", data):
            raise BridgeError("Refusing write outside the supported recipe region.")
        output = self.command(f"hf 15 wrbl -u {uid} -b {block} -d {data}")
        if not re.search(rf"Writing to page\s+0*{block}\b.*\(\s*ok\s*\)", output, re.I):
            raise BridgeError(f"Block {block} write was not acknowledged. Card is unverified.")

    @staticmethod
    def _initialization_target(job: dict) -> str:
        if (job.get("kind") != "initialize" or job.get("chip_profile") != CHIP_PROFILE
                or job.get("confirm_initialize") is not True
                or job.get("accept_special_configuration_changes") is not True):
            raise BridgeError("Factory initialization requires its separate reviewed approval.")
        return normalize_uid(job.get("target_uid", ""))

    def initialize_uid(self, job: dict) -> None:
        """The official four-frame factory sequence, including disclosed config writes."""
        uid = self._initialization_target(job)
        job_id = safe_id(job.get("id"))
        output = self.command(f"hf 15 csetuid --v2 -u {uid}",
            evidence_path=self.directory / (job_id + "-factory-uid-command.json"))
        if (not re.search(r"Setting new UID\s*\(\s*ok\s*\)", output, re.I)
                or re.search(r"\bfail(?:ed|ure)?\b|\berror\b|\btimeout\b|timed out|no tag found", output, re.I)):
            raise BridgeError("Factory UID command was not unambiguously verified. No retry or fallback attempted.")

    def initialize_block(self, job: dict, block: int, data: str) -> None:
        """Separate, bounded header/recipe writes; normal recipe writes stay at 8–39."""
        uid = self._initialization_target(job)
        if (type(block) is not int or not 0 <= block <= 39
                or not isinstance(data, str) or not re.fullmatch(r"[0-9A-F]{8}", data)
                or {"block": block, "data": data} not in job.get("write_blocks", [])):
            raise BridgeError("Refusing a block outside the reviewed initialization plan.")
        output = self.command(f"hf 15 wrbl -u {uid} -b {block} -d {data}")
        if (not re.search(rf"Writing to page\s+0*{block}\b.*\(\s*ok\s*\)", output, re.I)
                or re.search(r"\bfail(?:ed|ure)?\b|\berror\b|\btimeout\b|timed out|no tag found", output, re.I)):
            raise BridgeError(f"Initialization block {block} was not acknowledged. No retry attempted.")


def validate_write(job: dict, current: dict) -> tuple[bytes, list[dict]]:
    if not job.get("confirmed_at"):
        raise BridgeError("Write has no explicit dashboard confirmation.")
    try:
        confirmed = datetime.fromisoformat(str(job["confirmed_at"]).replace("Z", "+00:00"))
        if confirmed.tzinfo is None:
            confirmed = confirmed.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - confirmed).total_seconds()
        if age < -60 or age > 1800:
            raise BridgeError("Write approval expired. Preview and confirm a new job.")
    except ValueError as exc:
        raise BridgeError("Invalid write confirmation timestamp.") from exc
    if normalize_uid(job.get("uid", "")) != current["uid"]:
        raise BridgeError("Card UID does not match the selected inventory card.")
    if job.get("expected_memory_sha256") != current["sha256"]:
        raise BridgeError("Card changed since the saved backup. Read it again and review a fresh preview.")
    try:
        target = bytes.fromhex(job["memory_hex"])
    except (KeyError, TypeError, ValueError) as exc:
        raise BridgeError("Missing or invalid target memory.") from exc
    before = bytes.fromhex(current["memory_hex"])
    if len(target) != len(before) or target[:32] != before[:32] or target[160:] != before[160:]:
        raise BridgeError("Write would change header, UID/configuration, or memory size.")
    decoded = decode_recipe(target)
    if not decoded["checksum_valid"]:
        raise BridgeError("Target recipe checksum is invalid.")
    if job.get("kind") != "restore":
        # A normal recipe edit may only touch the old/new recipe extents. An
        # explicitly approved restore instead recovers the known recipe region
        # (blocks 8–39) from a saved original. The current recipe may be damaged
        # by an interrupted write, including an undecodable pour-count byte.
        previous = decode_recipe(before)
        stop = ((max(previous["recipe_end"], decoded["recipe_end"]) + 3) // 4) * 4
        if target[stop:] != before[stop:]:
            raise BridgeError("Write would change bytes beyond the current and target recipes.")
    blocks = job.get("write_blocks")
    if not isinstance(blocks, list) or len(blocks) > 32:
        raise BridgeError("Invalid block write plan.")
    expected = [{"block": i//4, "data": target[i:i+4].hex().upper()}
                for i in range(32, min(160, len(target)), 4) if before[i:i+4] != target[i:i+4]]
    if blocks != expected:
        raise BridgeError("Block write plan does not match the reviewed memory image.")
    if any(current["locks"][b["block"]] != 0 for b in blocks):
        raise BridgeError("A target recipe block is locked; no write attempted.")
    return target, blocks


class Worker:
    def __init__(self, api: API, card: Any, directory: Path, *, hardware=False,
                 allow_writes=False, allow_initialization=False, confirmer=input):
        self.api, self.card, self.directory = api, card, directory
        self.hardware, self.allow_writes, self.confirmer = hardware, allow_writes, confirmer
        self.allow_initialization = allow_initialization

    @property
    def capabilities(self) -> list[str]:
        return ([INITIALIZATION_CAPABILITY]
                if self.hardware and self.allow_writes and self.allow_initialization else [])

    def _execute_initialization(self, job: dict, dump: dict, result: dict) -> dict:
        """A separately approved factory operation; never used by ordinary writes."""
        job_id, card_id = safe_id(job["id"]), safe_id(job["card_id"])
        folder = self.directory / "backups" / card_id
        state_path = folder / (job_id + "-initialization-state.json")
        changed = False
        result.update(initialization_attempted=False, manual_reconciliation_required=False,
            hidden_configuration_backup_available=False,
            hidden_configuration_preservation_verified=False, machine_acceptance_verified=False)

        def record_readback(native: dict, suffix: str, field: str) -> dict:
            # Save native evidence before parsing: even invalid/partial identities
            # must survive a failed initialization without being adopted as truth.
            result[field] = native
            durable_json(folder / (job_id + suffix + ".json"), {"mode": "hardware", "dump": native})
            snapshot = initialization_snapshot(native)
            result["observed_uid"] = snapshot["uid"]
            return snapshot

        try:
            plan = validate_initialization_job(job, dump)
            current = initialization_snapshot(dump)
            durable_json(folder / (job_id + "-approved-initialization.json"), job)
            label = job["label"]
            phrase = f"INITIALIZE {label}"
            prompt = (f"\nOnly the physical blank card labeled {label!r} may be on the antenna.\n"
                f"Factory initialization changes UID {plan['original_uid']} to {plan['target_uid']}, "
                f"copies the saved template header, and writes {len(plan['write_blocks'])} memory blocks.\n"
                "It ALSO writes special configuration 47=3F038B00 and 52=00000000. "
                "Those previous settings are NOT backed up and cannot be verified or recovered from the memory dump.\n"
                "No automatic retry or rollback is available. Offline xBloom acceptance is untested.\n"
                f"Reviewed plan {plan['preview_sha256']}; original backup {current['sha256']}.\n"
                f"Type {phrase!r} to approve this exact initialization and special configuration changes: ")
            if self.confirmer(prompt) != phrase:
                raise BridgeError("Local initialization confirmation declined; no write attempted.")
            rechecked_dump = self.card.read(job)
            # Recomputes identity, full memory, metadata, locks, provenance, plan
            # digest and expiry after the human delay, before any special write.
            plan = validate_initialization_job(job, rechecked_dump)
            durable_json(state_path, {"phase": "factory_uid_command_starting",
                "preview_sha256": plan["preview_sha256"], "original_uid": plan["original_uid"],
                "target_uid": plan["target_uid"], "manual_reconciliation_required": True})
            changed = True  # A lost response can still mean a special write happened.
            result["initialization_attempted"] = True
            self.card.initialize_uid(job)
            identity_dump = self.card.read(job)
            identity = record_readback(identity_dump, "-after-uid", "initialization_identity_readback_dump")
            if (identity["uid"] != plan["target_uid"] or identity["memory_hex"] != current["memory_hex"]
                    or identity["locks"] != plan["original_locks"]
                    or identity["metadata"] != plan["original_metadata"]
                    or identity["block_count"] != 64 or identity["block_size"] != 4):
                raise BridgeError("Factory UID operation changed unexpected ordinary memory, locks, or metadata; initialization stopped.")
            durable_json(state_path, {"phase": "identity_readback_verified",
                "preview_sha256": plan["preview_sha256"], "observed_uid": identity["uid"],
                "manual_reconciliation_required": True})
            for block in plan["write_blocks"]:
                # Use the immutable prewrite snapshot to recheck approval expiry
                # and plan integrity, without treating a partially written card as blank.
                validate_initialization_job(job, rechecked_dump)
                self.card.initialize_block(job, block["block"], block["data"])
            readback = self.card.read(job)
            record_readback(readback, "-after", "readback_dump")
            validate_initialization_readback(job, readback)
            result.update(success=True, hardware_verified=True, initialized=True,
                          ordinary_memory_verified=True, manual_reconciliation_required=False)
            durable_json(state_path, {"phase": "verified", "preview_sha256": plan["preview_sha256"],
                "observed_uid": result["observed_uid"], "manual_reconciliation_required": False,
                "hidden_configuration_preservation_verified": False, "machine_acceptance_verified": False})
        except Exception as exc:
            result.update(success=False, hardware_verified=False, error=str(exc),
                          initialized=False, ordinary_memory_verified=False,
                          manual_reconciliation_required=True)
            if changed:
                try:
                    record_readback(self.card.read(job), "-failed-readback", "readback_dump")
                except Exception as read_exc:
                    result["recovery_read_error"] = str(read_exc)
                    raw = getattr(self.card, "last_read_evidence", None)
                    if isinstance(raw, dict):
                        result["readback_dump"] = raw
                        durable_json(folder / (job_id + "-failed-raw-readback.json"),
                                     {"mode": "hardware", "dump": raw, "error": str(read_exc)})
            durable_json(state_path, {"phase": "failed", "result": result,
                                     "manual_reconciliation_required": True})
        return result

    def execute(self, job: dict) -> dict:
        job_id = safe_id(job.get("id"))
        card_id = safe_id(job.get("card_id"))
        kind = job.get("kind")
        result = {"success": False, "mode": "hardware" if self.hardware else "simulation",
                  "simulated": not self.hardware, "hardware_verified": False}
        if kind not in ("read", "write", "restore", "initialize"):
            raise BridgeError("Unsupported job kind.")
        if job.get("mode", result["mode"]) != result["mode"]:
            raise BridgeError("Job mode does not match this bridge; simulated and physical cards are separate.")
        if self.hardware and kind != "read" and not self.allow_writes:
            raise BridgeError("Hardware writes are disabled. Start with --hardware --allow-writes for an approved job.")
        if kind == "initialize" and INITIALIZATION_CAPABILITY not in self.capabilities:
            raise BridgeError("Initialization requires --hardware --allow-writes --allow-initialization and its separate approval.")
        changed = False
        dump = None
        try:
            dump = self.card.read(job)
            current = parse_dump(dump)
            if job.get("uid") and normalize_uid(job["uid"]) != current["uid"]:
                raise BridgeError("Detected card UID does not match the selected card.")
            path = self.directory / "backups" / card_id / (job_id + "-before.json")
            durable_json(path, {"mode": result["mode"], "dump": dump, "sha256": current["sha256"]})
            acknowledgement = self.api.call("POST", f"/api/bridge/jobs/{job_id}/backup",
                {"dump": dump, "mode": result["mode"], "simulated": not self.hardware})
            if acknowledgement.get("saved") is not True or acknowledgement.get("sha256") != current["sha256"]:
                raise BridgeError("Server did not acknowledge the exact backup; no write attempted.")
            result["backup_dump"] = dump
            if kind == "read":
                result.update(success=True, readback_dump=dump, hardware_verified=self.hardware)
                return result
            if kind == "initialize":
                return self._execute_initialization(job, dump, result)
            target, blocks = validate_write(job, current)
            if self.hardware:
                label = str(job.get("label", card_id))
                phrase = f"{'RESTORE' if kind == 'restore' else 'WRITE'} {label}"
                prompt = (f"\nOnly the physical card labeled {label!r} may be on the antenna.\n"
                          f"Job {job_id}: {len(blocks)} recipe blocks; saved backup {current['sha256'][:12]}.\n"
                          f"Type {phrase!r} to approve this physical {'restore' if kind == 'restore' else 'write'}: ")
                if self.confirmer(prompt) != phrase:
                    raise BridgeError("Local card confirmation declined; no write attempted.")
                # Recheck the full card after the human confirmation delay.
                rechecked = parse_dump(self.card.read(job))
                if rechecked["uid"] != current["uid"] or rechecked["sha256"] != current["sha256"]:
                    raise BridgeError("Card changed while waiting for confirmation; no write attempted.")
                validate_write(job, rechecked)
            for block in blocks:
                changed = True  # A failing response can still mean bytes reached the card.
                self.card.write_block(job, block["block"], block["data"])
            readback = self.card.read(job)
            checked = parse_dump(readback)
            result["readback_dump"] = readback
            durable_json(self.directory / "backups" / card_id / (job_id + "-after.json"),
                {"mode": result["mode"], "dump": readback, "sha256": checked["sha256"]})
            if checked["uid"] != current["uid"] or checked["memory_hex"] != target.hex().upper():
                raise BridgeError("Full readback differs from the approved target. Card is unverified; do not brew.")
            result.update(success=True, hardware_verified=self.hardware)
            return result
        except Exception as exc:
            result["error"] = str(exc)
            if kind == "read":
                evidence = dump if isinstance(dump, dict) else getattr(self.card, "last_read_evidence", None)
                if isinstance(evidence, dict):
                    # A changed/partial UID after initialization is evidence, not
                    # permission to adopt identity or clear server reconciliation.
                    result["readback_dump"] = evidence
                    durable_json(self.directory / "backups" / card_id / (job_id + "-failed-read.json"),
                                 {"mode": result["mode"], "dump": evidence, "error": str(exc)})
            if kind == "initialize":
                result.update(manual_reconciliation_required=True,
                    initialization_attempted=result.get("initialization_attempted", False),
                    hidden_configuration_backup_available=False,
                    hidden_configuration_preservation_verified=False, machine_acceptance_verified=False)
            if changed and "readback_dump" not in result:
                try:
                    result["readback_dump"] = self.card.read(job)
                except Exception:
                    pass
            return result

    def post_pending(self) -> None:
        for path in (self.directory / "jobs").glob("*.json"):
            record = json.loads(path.read_text(encoding="utf-8"))
            if record.get("posted"):
                continue
            if "result" not in record:
                record["result"] = {"success": False, "error": "Bridge interrupted during job; card unverified. Read it again before retrying.",
                                    "mode": record.get("mode", "hardware"), "hardware_verified": False}
                if record.get("kind") == "initialize":
                    record["result"].update(manual_reconciliation_required=True,
                        initialization_attempted=True, machine_acceptance_verified=False,
                        hidden_configuration_preservation_verified=False)
                    # A crash may leave stronger evidence in the operation log.
                    # Report it as failed, never retry a UID or memory write.
                    evidence = self.directory / "backups" / safe_id(record["card_id"])
                    state_path = evidence / (safe_id(record["id"]) + "-initialization-state.json")
                    if state_path.is_file():
                        record["result"]["initialization_phase"] = json.loads(state_path.read_text())["phase"]
                    for suffix, key in (("-after-uid", "initialization_identity_readback_dump"),
                                        ("-after", "readback_dump"),
                                        ("-failed-readback", "readback_dump"),
                                        ("-failed-raw-readback", "readback_dump")):
                        path_evidence = evidence / (safe_id(record["id"]) + suffix + ".json")
                        if path_evidence.is_file():
                            record["result"][key] = json.loads(path_evidence.read_text())["dump"]
                durable_json(path, record)
            try:
                self.api.call("POST", f"/api/bridge/jobs/{safe_id(record['id'])}/result", record["result"])
            except HTTPError as exc:
                if exc.code != 409:
                    raise
                state = self.api.call("GET", f"/api/bridge/jobs/{safe_id(record['id'])}")
                server_job = state.get("job", {})
                if server_job.get("status") not in ("succeeded", "failed", "cancelled", "expired"):
                    raise BridgeError(f"Job {record['id']} has a nonterminal result conflict; inspect the dashboard.") from exc
                record["reconciliation"] = "server_terminal_conflict"
                record["server_status"] = server_job["status"]
                print(f"Job {record['id']} was already {server_job['status']} on the server. Local result retained for diagnosis; no operation replayed.", file=sys.stderr, flush=True)
            record["posted"] = True
            durable_json(path, record)

    def poll(self) -> bool:
        self.post_pending()
        path = "/api/bridge/jobs/next" + ("?kind=initialize" if self.capabilities else "")
        response = self.api.call("GET", path)
        job = response.get("job")
        if not job:
            return False
        job_id = safe_id(job.get("id"))
        journal = self.directory / "jobs" / (job_id + ".json")
        if journal.exists():
            # Never replay a physical job, even if a response was lost.
            return False
        record = {"id": job_id, "card_id": safe_id(job.get("card_id")), "kind": job.get("kind"),
                  "mode": "hardware" if self.hardware else "simulation", "posted": False}
        durable_json(journal, record)
        try:
            record["result"] = self.execute(job)
        except Exception as exc:
            record["result"] = {"success": False, "error": str(exc), "mode": record["mode"], "hardware_verified": False}
            if job.get("kind") == "initialize":
                record["result"]["manual_reconciliation_required"] = True
        durable_json(journal, record)
        self.post_pending()
        print(f"Job {job_id}: {'completed' if record['result']['success'] else 'failed'} ({record['mode']})", flush=True)
        return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hardware", action="store_true", help="Use the real Proxmark; default is simulation")
    parser.add_argument("--allow-writes", action="store_true", help="Permit confirmed hardware write jobs with local typed approval")
    parser.add_argument("--allow-initialization", action="store_true",
                        help="Enable only separately approved factory initialization jobs, including disclosed special configuration writes")
    parser.add_argument("--once", action="store_true", help="Poll once, then exit")
    parser.add_argument("--wait-for-job", type=int, default=0,
                        help="With --once, wait up to this many seconds for one job (0–120)")
    parser.add_argument("--interval", type=float, default=3)
    args = parser.parse_args(argv)
    if args.allow_writes and not args.hardware:
        parser.error("--allow-writes is only meaningful with --hardware")
    if args.allow_initialization and not (args.hardware and args.allow_writes):
        parser.error("--allow-initialization requires --hardware --allow-writes")
    if args.allow_writes and not sys.stdin.isatty():
        parser.error("Hardware writing requires an interactive terminal for each card confirmation")
    if not 1 <= args.interval <= 60:
        parser.error("--interval must be between 1 and 60 seconds")
    if not 0 <= args.wait_for_job <= 120 or (args.wait_for_job and not args.once):
        parser.error("--wait-for-job must be 0–120 seconds and is only available with --once")
    directory = Path(os.environ.get("XBLOOM_BRIDGE_DATA_DIR", "data/bridge")).expanduser().resolve()
    instance_lock = None
    try:
        api = API(os.environ.get("XBLOOM_SERVER_URL", "http://127.0.0.1:8000"),
                  os.environ.get("XBLOOM_BRIDGE_TOKEN", ""), os.environ.get("XBLOOM_CA_FILE"))
        card = Proxmark(os.environ.get("PM3_EXECUTABLE", ""), os.environ.get("PM3_PORT", "COM6"), directory) if args.hardware else SimulatedCard(directory)
        instance_lock = acquire_instance_lock(directory)
        worker = Worker(api, card, directory, hardware=args.hardware, allow_writes=args.allow_writes,
                        allow_initialization=args.allow_initialization)
        mode = "hardware" if args.hardware else "simulation"
        print(f"xBloom Windows bridge: {mode}. {'Writes require local approval.' if args.allow_writes else 'No hardware writes enabled.'}", flush=True)
        heartbeat_data = {"name": "Windows Proxmark bridge", "mode": mode, "version": VERSION,
                          "port": os.environ.get("PM3_PORT", "COM6") if args.hardware else None,
                          "capabilities": worker.capabilities}
        if args.allow_initialization:
            print("Factory initialization mode: ordinary jobs are excluded. Special configuration changes require exact local approval.", flush=True)
        stop = threading.Event()

        def heartbeat():
            while not stop.wait(10):
                try:
                    api.call("POST", "/api/bridge/heartbeat", heartbeat_data)
                except Exception:
                    print("Bridge heartbeat could not reach the server; pending results will be retained.", file=sys.stderr, flush=True)

        api.call("POST", "/api/bridge/heartbeat", heartbeat_data)
        threading.Thread(target=heartbeat, daemon=True).start()
        wait_deadline = time.monotonic() + args.wait_for_job
        try:
            while True:
                try:
                    completed_job = worker.poll()
                except Exception as exc:
                    print(f"Bridge error: {exc}", file=sys.stderr, flush=True)
                    if args.once:
                        return 1
                if args.once:
                    remaining = wait_deadline - time.monotonic()
                    if completed_job or remaining <= 0:
                        if not completed_job and args.wait_for_job:
                            print("No eligible job arrived before the wait ended; no card operation performed.", flush=True)
                        return 0
                    time.sleep(min(args.interval, remaining))
                else:
                    time.sleep(args.interval)
        finally:
            stop.set()
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"Bridge error: {exc}", file=sys.stderr)
        return 1
    finally:
        if instance_lock is not None:
            instance_lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
