"""Pure, explicit plans for initializing one backed-up blank custom card.

This operation is separate from recipe editing. The documented factory Gen2 UID
command also writes two special configuration selectors that a normal dump does
not preserve. No function here performs I/O or claims those settings are backed
up, reversible, or verified by an ordinary memory read.
"""
import copy
import hashlib
import json
import math
import re
import time
from datetime import datetime, timezone

from .models import Recipe
from .nfc import decode_recipe, parse_dump, preview_recipe


CHIP_PROFILE = "iso15693_gen2_64_factory"
CAPABILITY = "init_factory_gen2_v1"
SPECIAL_CONFIGURATION_WRITES = [
    {"selector": "47", "data": "3F038B00"},
    {"selector": "52", "data": "00000000"},
]
CONTEXT_FIELDS = {
    "card_id", "card_label", "baseline_backup_id", "original_backup_id",
    "template_card_id", "template_label", "template_backup_id", "recipe_id",
    "recipe_version", "mode",
}


class InitializationError(ValueError):
    pass


def initialization_snapshot(dump: dict) -> dict:
    """Normalize a native JSON dump without discarding identity/lock metadata."""
    if not isinstance(dump, dict) or not isinstance(dump.get("Card"), dict):
        raise InitializationError("Initialization requires a complete native JSON backup")
    try:
        parsed = parse_dump(dump)
    except (ValueError, TypeError, KeyError) as exc:
        raise InitializationError(f"Invalid initialization dump: {exc}") from exc
    metadata = {}
    for field in ("ic", "afi", "dsfid", "afilock", "dsfidlock"):
        value = dump["Card"].get(field)
        if not isinstance(value, str) or not re.fullmatch(r"[0-9A-Fa-f]{2}", value):
            raise InitializationError(f"Native backup is missing valid {field} metadata")
        metadata[field] = value.upper()
    return {key: copy.deepcopy(parsed[key]) for key in
            ("uid", "memory_hex", "sha256", "block_count", "block_size", "locks")} | {"metadata": metadata}


def _profile(snapshot: dict):
    if snapshot["block_count"] != 64 or snapshot["block_size"] != 4:
        raise InitializationError("Factory initialization supports exactly 64 four-byte blocks")
    if any(snapshot["metadata"][key] != value for key, value in
           (("ic", "8B"), ("afi", "00"), ("dsfid", "00"))):
        raise InitializationError("Native metadata does not match the explicit factory Gen2 profile")


def build_initialization_plan(current_dump: dict, template_dump: dict, recipe: dict,
                              *, chip_profile: str) -> dict:
    if chip_profile != CHIP_PROFILE:
        raise InitializationError("Select the explicit factory Gen2 initialization profile")
    current = initialization_snapshot(current_dump)
    template = initialization_snapshot(template_dump)
    _profile(current)
    _profile(template)
    before = bytes.fromhex(current["memory_hex"])
    source = bytes.fromhex(template["memory_hex"])
    if any(before):
        raise InitializationError("Initialization requires an entirely blank 256-byte card; normal editing or recovery is separate")
    if not any(source[:32]):
        raise InitializationError("The saved template must contain a nonzero header")
    try:
        decoded = decode_recipe(source)
        if not decoded["checksum_valid"]:
            raise InitializationError("The saved template recipe checksum is invalid")
        checked = Recipe.model_validate({key: value for key, value in recipe.items() if key in Recipe.model_fields}).model_dump()
    except (ValueError, TypeError, AttributeError) as exc:
        raise InitializationError(str(exc)) from exc
    preview = preview_recipe(checked, source)
    if not preview["compatible"]:
        raise InitializationError("; ".join(preview["errors"]))
    # Copy only the explicitly reviewed header/recipe region. In particular,
    # never transplant the source card's ordinary blocks 40–63.
    target = bytes.fromhex(preview["memory_hex"])[:160] + before[160:]
    blocks = [{"block": index // 4, "data": target[index:index + 4].hex().upper()}
              for index in range(0, 160, 4) if target[index:index + 4] != before[index:index + 4]]
    if any(current["locks"][item["block"]] for item in blocks):
        raise InitializationError("An initialization target block is locked")
    return {
        "compatible": True, "chip_profile": chip_profile,
        "original_uid": current["uid"], "target_uid": template["uid"],
        "memory_hex": target.hex().upper(), "write_blocks": blocks,
        "expected_memory_sha256": current["sha256"],
        "target_memory_sha256": hashlib.sha256(target).hexdigest(),
        "original_locks": current["locks"], "target_locks": copy.deepcopy(current["locks"]),
        "original_metadata": current["metadata"], "target_metadata": copy.deepcopy(current["metadata"]),
        "template_snapshot": template, "template_dump": copy.deepcopy(template_dump),
        "initialization_recipe": checked, "effective": preview["effective"],
        "differences": preview["differences"], "requires_review": preview["requires_review"],
        "special_configuration_writes": copy.deepcopy(SPECIAL_CONFIGURATION_WRITES),
        "hidden_configuration_backup_available": False,
        "hidden_configuration_preservation_verified": False,
        "machine_acceptance_verified": False,
        "warnings": [
            "Initialization copies the saved template header and UID; physical card labels remain distinct even when UIDs match.",
            "The factory Gen2 UID command also writes special selector 47=3F038B00 and selector 52=00000000.",
            "Those special configuration settings are not present in the 64-block backup: their previous values cannot be verified or recovered from it.",
            "Ordinary memory bytes 160–255, block locks, IC, AFI and DSFID metadata must remain unchanged; this is not a claim that all configuration is preserved.",
            "A timeout or interruption can leave a partially initialized card. There is no automatic retry or rollback.",
            "This initialization method has not been physically validated here. xBloom compatibility must be checked on the offline brewer after verification.",
        ],
    }


def bind_initialization_plan(plan: dict, context: dict) -> dict:
    """Bind the pure plan to exact saved records, inventory labels, and mode."""
    if not isinstance(context, dict) or set(context) != CONTEXT_FIELDS:
        raise InitializationError("Initialization provenance is incomplete")
    for key in CONTEXT_FIELDS - {"recipe_version"}:
        if not isinstance(context[key], str) or not context[key]:
            raise InitializationError("Initialization provenance is invalid")
    if (type(context["recipe_version"]) is not int or context["recipe_version"] < 1
            or context["mode"] not in {"hardware", "simulation"}
            or context["card_id"] == context["template_card_id"]):
        raise InitializationError("Initialization needs a different inventory template and an explicit recipe version/mode")
    result = {**copy.deepcopy(plan), "initialization_context": copy.deepcopy(context)}
    result["preview_sha256"] = hashlib.sha256(json.dumps(
        result, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()
    return result


def validate_initialization_job(job: dict, current_dump: dict) -> dict:
    """Recompute every reviewed byte and provenance field before any mutation."""
    if (job.get("kind") != "initialize" or job.get("confirm_initialize") is not True
            or job.get("accept_special_configuration_changes") is not True):
        raise InitializationError("Initialization and special configuration changes need explicit confirmation")
    try:
        confirmed = datetime.fromisoformat(str(job["confirmed_at"]).replace("Z", "+00:00"))
        if confirmed.tzinfo is None:
            confirmed = confirmed.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - confirmed).total_seconds()
        expires = float(job["expires_at"])
        if age < -60 or age > 1800 or not math.isfinite(expires) or expires < time.time():
            raise InitializationError("Initialization approval expired; review a fresh preview")
        context = job["initialization_context"]
        plan = bind_initialization_plan(build_initialization_plan(
            current_dump, job["template_dump"], job["initialization_recipe"], chip_profile=job["chip_profile"]), context)
        for key, value in plan.items():
            if job.get(key) != value:
                raise InitializationError(f"Initialization {key} no longer matches the reviewed plan")
        aliases = {"card_id": "card_id", "card_label": "label", "baseline_backup_id": "baseline_backup_id",
                   "original_backup_id": "original_backup_id", "template_card_id": "template_card_id",
                   "template_label": "template_label", "template_backup_id": "template_backup_id",
                   "recipe_id": "recipe_id", "recipe_version": "recipe_version", "mode": "mode"}
        if any(context[key] != job.get(alias) for key, alias in aliases.items()):
            raise InitializationError("Initialization provenance does not match this job")
        if job.get("uid") != plan["original_uid"] or job.get("expected_preview_sha256") != plan["preview_sha256"]:
            raise InitializationError("Initialization identity or approval hash does not match")
        if plan["differences"] and job.get("accept_encoding_changes") is not True:
            raise InitializationError("Initialization encoding changes were not accepted")
        return plan
    except InitializationError:
        raise
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise InitializationError("Incomplete initialization job") from exc


def validate_initialization_readback(job: dict, dump: dict) -> dict:
    observed = initialization_snapshot(dump)
    if (observed["uid"] != job.get("target_uid") or observed["memory_hex"] != job.get("memory_hex")
            or observed["sha256"] != job.get("target_memory_sha256")
            or observed["block_count"] != 64 or observed["block_size"] != 4
            or observed["locks"] != job.get("target_locks") or observed["metadata"] != job.get("target_metadata")):
        raise InitializationError("Initialization readback differs from the approved UID, memory, locks, or metadata")
    return observed
