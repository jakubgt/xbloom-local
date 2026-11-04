"""Pure synthetic plans; no real card identities, templates, or hardware access."""
import copy
import time
from datetime import datetime, timezone

import pytest

from app.initialization import (CHIP_PROFILE, InitializationError, SPECIAL_CONFIGURATION_WRITES,
    bind_initialization_plan, build_initialization_plan, initialization_snapshot,
    validate_initialization_job, validate_initialization_readback)
from app.nfc import crc8_maxim, decode_recipe, make_dump, parse_dump, preview_recipe
from bridge.main import synthetic_dump


def recipe():
    return {"name": "Synthetic initialization recipe", "dose_g": 18, "ratio": 16,
        "total_water_ml": 288, "grind_setting": 58, "grinder_rpm": 80, "cup_type": "other",
        "pours": [{"volume_ml": volume, "temperature_c": 92 - min(index, 2), "flow_ml_s": 3,
                   "pattern": "circle", "pause_s": 30 if index == 0 else 0}
                  for index, volume in enumerate((54, 90, 90, 54))]}


def native_dump(uid="E000000000000001", *, blank=True):
    dump = make_dump(uid, bytes(256)) if blank else synthetic_dump(uid)
    dump["Card"]["ic"] = "8B"
    return dump


def plan_fixture():
    current = native_dump()
    source = native_dump("E000000000000002", blank=False)
    # A donor tail must never be copied into the new card's ordinary config.
    source["blocks"]["63"] = "DEADBEEF"
    context = {"card_id": "target", "card_label": "Blank target", "baseline_backup_id": "fresh",
        "original_backup_id": "original", "template_card_id": "source", "template_label": "Synthetic source",
        "template_backup_id": "template", "recipe_id": "recipe", "recipe_version": 2, "mode": "simulation"}
    plan = bind_initialization_plan(build_initialization_plan(current, source, recipe(), chip_profile=CHIP_PROFILE), context)
    job = {**plan, "id": "job", "kind": "initialize", "uid": plan["original_uid"],
        "label": context["card_label"], "confirmed_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": time.time() + 600, "confirm_initialize": True, "accept_special_configuration_changes": True,
        "accept_encoding_changes": True, "expected_preview_sha256": plan["preview_sha256"]}
    job.update({key: value for key, value in context.items() if key != "card_label"})
    return current, source, job


def target_dump(job):
    dump = make_dump(job["target_uid"], bytes.fromhex(job["memory_hex"]), job["target_locks"])
    dump["Card"].update(job["target_metadata"])
    return dump


def test_plan_copies_only_header_recipe_and_discloses_unbacked_special_configuration():
    current, source, job = plan_fixture()
    before, donor = copy.deepcopy(current), copy.deepcopy(source)
    actual = validate_initialization_job(job, current)
    memory = bytes.fromhex(actual["memory_hex"])
    assert memory[:32] == bytes.fromhex(parse_dump(source)["memory_hex"])[:32]
    assert memory[160:] == bytes(96)
    assert decode_recipe(memory)["checksum_valid"]
    assert actual["target_uid"] == parse_dump(source)["uid"]
    assert all(0 <= block["block"] <= 39 for block in actual["write_blocks"])
    assert any(block["block"] < 8 for block in actual["write_blocks"])
    assert actual["special_configuration_writes"] == SPECIAL_CONFIGURATION_WRITES
    assert actual["hidden_configuration_backup_available"] is False
    assert actual["hidden_configuration_preservation_verified"] is False
    assert actual["machine_acceptance_verified"] is False
    assert actual["original_metadata"] == actual["target_metadata"]
    assert actual["original_locks"] == actual["target_locks"]
    assert current == before and source == donor
    assert validate_initialization_readback(job, target_dump(job))["sha256"] == job["target_memory_sha256"]


@pytest.mark.parametrize("change", ["nonblank", "locked", "count", "ic", "afi", "missing_metadata", "template_crc", "template_header"])
def test_plan_rejects_nonblank_locked_unsupported_or_unproven_template(change):
    current, source, _ = plan_fixture()
    if change == "nonblank":
        current["blocks"]["63"] = "00000001"
    elif change == "locked":
        current["Card"]["locks"] = "01" + "00" * 63
    elif change == "count":
        current = make_dump("E000000000000001", bytes(160))
    elif change in {"ic", "afi"}:
        current["Card"][change] = "01"
    elif change == "missing_metadata":
        del current["Card"]["dsfidlock"]
    elif change == "template_crc":
        source["blocks"]["12"] = "FFFFFFFF"
    else:
        memory = bytearray.fromhex(parse_dump(source)["memory_hex"])
        memory[:32] = bytes(32)
        memory[67] = crc8_maxim(memory[:67])
        source = make_dump("E000000000000002", bytes(memory))
        source["Card"]["ic"] = "8B"
    with pytest.raises(InitializationError):
        build_initialization_plan(current, source, recipe(), chip_profile=CHIP_PROFILE)


@pytest.mark.parametrize("field,value", [("target_uid", "E000000000000009"), ("write_blocks", []),
    ("special_configuration_writes", []), ("hidden_configuration_backup_available", True),
    ("original_metadata", {}), ("preview_sha256", "0" * 64), ("recipe_version", 1),
    ("confirm_initialize", False), ("accept_special_configuration_changes", False), ("expires_at", 1),
    ("expires_at", float("nan")), ("expires_at", float("inf")), ("expires_at", "invalid"),
    ("confirmed_at", "invalid")])
def test_job_cannot_change_reviewed_identity_bytes_disclosures_or_approval(field, value):
    current, _, job = plan_fixture()
    job[field] = value
    with pytest.raises(InitializationError):
        validate_initialization_job(job, current)


def test_new_lock_metadata_even_on_unchanged_block_invalidates_review():
    current, _, job = plan_fixture()
    current["Card"]["locks"] = "00" * 63 + "01"
    with pytest.raises(InitializationError):
        validate_initialization_job(job, current)


@pytest.mark.parametrize("change", ["uid", "tail", "locks", "afi", "ic", "afilock", "dsfidlock"])
def test_full_readback_requires_uid_all_memory_all_locks_and_metadata(change):
    _, _, job = plan_fixture()
    dump = target_dump(job)
    if change == "uid":
        dump["Card"]["uid"] = "09000000000000E0"
    elif change == "tail":
        dump["blocks"]["63"] = "00000001"
    elif change == "locks":
        dump["Card"]["locks"] = "00" * 63 + "01"
    else:
        dump["Card"][change] = "01"
    with pytest.raises(InitializationError):
        validate_initialization_readback(job, dump)


def test_normal_recipe_preview_still_rejects_blank_card():
    assert not preview_recipe(recipe(), bytes(256))["compatible"]


def test_only_explicit_factory_profile_is_supported():
    current, source, _ = plan_fixture()
    with pytest.raises(InitializationError):
        build_initialization_plan(current, source, recipe(), chip_profile="iso15693_gen2_64_uid_only")


def test_context_hash_binds_labels_ids_modes_and_recipe_version():
    current, source, job = plan_fixture()
    plan = build_initialization_plan(current, source, recipe(), chip_profile=CHIP_PROFILE)
    for field, replacement in (("card_label", "Other blank"), ("template_backup_id", "other-source"),
        ("original_backup_id", "other-original"), ("recipe_version", 3), ("mode", "hardware")):
        context = {**job["initialization_context"], field: replacement}
        assert bind_initialization_plan(plan, context)["preview_sha256"] != job["preview_sha256"]
