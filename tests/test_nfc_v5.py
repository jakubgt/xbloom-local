"""Synthetic coverage for the current Proxmark JSON format; no hardware access.

Format evidence: RRG/proxmark3 754bbae3d946429a6354cd34a97a2dd29ce6f157,
client/src/fileutils.c json15_save_tag and client/src/cmdhf15.c CmdHF15Dump.
"""

import copy
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.nfc import NFCError, make_dump, parse_dump, preview_recipe
from bridge.main import BridgeError, Proxmark, synthetic_dump, validate_write
from tests.test_nfc import recipe


@pytest.fixture
def v4_dump():
    original = synthetic_dump("E011223344556677")
    memory = bytearray.fromhex(parse_dump(original)["memory_hex"])
    # Distinct configuration bytes must survive format parsing unchanged.
    memory[160:] = bytes(range(96))
    return make_dump("E011223344556677", bytes(memory))


@pytest.fixture
def v5_dump(v4_dump):
    dump = copy.deepcopy(v4_dump)
    dump["FileType"] = "15693 v5"
    dump["Card"]["pagescount"] = "4000"
    return dump


def test_v5_matches_v4_uid_header_memory_configuration_and_locks(v4_dump, v5_dump):
    locks = bytes([0, 1, 2, 255] * 16).hex().upper()
    v4_dump["Card"]["locks"] = v5_dump["Card"]["locks"] = locks
    original = copy.deepcopy(v5_dump)
    old, current = parse_dump(v4_dump), parse_dump(v5_dump)
    assert current["format"] == "proxmark3-json-v5"
    assert old["format"] == "proxmark3-json-v4"
    assert {k: v for k, v in current.items() if k != "format"} == {
        k: v for k, v in old.items() if k != "format"
    }
    assert current["block_count"] == 64
    assert current["uid"] == "E011223344556677"
    assert bytes.fromhex(current["memory_hex"])[160:] == bytes(range(96))
    assert current["locks"] == [0, 1, 2, 255] * 16
    assert v5_dump == original
    for serialized in (json.dumps(v5_dump), json.dumps(v5_dump).encode("utf-8")):
        assert parse_dump(serialized) == current


@pytest.mark.parametrize("count", [13, 64, 160])
def test_v5_keeps_supported_four_byte_memory_bounds(count):
    dump = make_dump("E011223344556677", bytes(count * 4))
    dump["FileType"] = "15693 v5"
    dump["Card"]["pagescount"] = count.to_bytes(2, "little").hex()
    assert parse_dump(dump)["block_count"] == count


@pytest.mark.parametrize("value", [None, 64, "40", "400", "400000", "40 00", "GG00", "0x40"])
def test_v5_rejects_malformed_page_count_width_or_type(v5_dump, value):
    v5_dump["Card"]["pagescount"] = value
    with pytest.raises(NFCError):
        parse_dump(v5_dump)


@pytest.mark.parametrize("value", ["0040", "4001", "0000", "0C00", "A100", "0001", "FFFF"])
def test_v5_does_not_truncate_high_byte_swap_endianness_or_expand_caps(v5_dump, value):
    v5_dump["Card"]["pagescount"] = value
    with pytest.raises(NFCError, match="13–160 four-byte blocks"):
        parse_dump(v5_dump)


@pytest.mark.parametrize("field,value", [
    ("bytesperpage", "08"), ("bytesperpage", "0400"), ("bytesperpage", 4),
    ("uid", "E011223344556677"), ("uid", "not-a-uid"), ("uid", None),
])
def test_v5_preserves_metadata_validation(v5_dump, field, value):
    v5_dump["Card"][field] = value
    with pytest.raises(NFCError):
        parse_dump(v5_dump)


@pytest.mark.parametrize("extra", ["64", "064", "-1", "config", 64])
def test_v5_rejects_blocks_outside_declared_complete_image(v5_dump, extra):
    v5_dump["blocks"][extra] = "AABBCCDD"
    with pytest.raises(NFCError, match="exactly match"):
        parse_dump(v5_dump)


def test_v5_rejects_missing_block_even_when_key_count_is_unchanged(v5_dump):
    v5_dump["blocks"]["64"] = v5_dump["blocks"].pop("63")
    with pytest.raises(NFCError):
        parse_dump(v5_dump)


@pytest.mark.parametrize("value", [None, 0, "", "AABBCC", "AABBCCDDEE", "GG000000", "00 00 00"])
def test_v5_rejects_incomplete_or_malformed_block_data(v5_dump, value):
    v5_dump["blocks"]["12"] = value
    with pytest.raises(NFCError):
        parse_dump(v5_dump)


@pytest.mark.parametrize("value", [None, 0, "", "00" * 63, "00" * 65, "  " * 64, "GG" * 64])
def test_v5_requires_exactly_one_hex_lock_byte_per_block(v5_dump, value):
    v5_dump["Card"]["locks"] = value
    with pytest.raises(NFCError):
        parse_dump(v5_dump)


@pytest.mark.parametrize("value", [None, "15693 v3", "15693 v6", "15693", ["15693 v5"]])
def test_unknown_formats_are_not_reinterpreted_as_v5(v5_dump, value):
    v5_dump["FileType"] = value
    with pytest.raises(NFCError):
        parse_dump(v5_dump)


def test_v4_format_width_and_legacy_extra_key_behavior_stay_unchanged(v4_dump):
    baseline = parse_dump(v4_dump)
    v4_dump["blocks"]["64"] = "AABBCCDD"
    assert parse_dump(v4_dump) == baseline
    v4_dump["Card"]["pagescount"] = "4000"
    with pytest.raises(NFCError):
        parse_dump(v4_dump)


def test_v5_does_not_enable_unknown_binary_layouts():
    with pytest.raises(NFCError, match="Unsupported binary dump"):
        parse_dump(bytes(2332))


def test_v5_lock_and_protected_region_checks_still_block_write_plans(v5_dump):
    current = parse_dump(v5_dump)
    memory = bytes.fromhex(current["memory_hex"])
    preview = preview_recipe(recipe(), memory)
    assert preview["compatible"]
    job = {"kind": "write", "uid": current["uid"],
           "confirmed_at": datetime.now(timezone.utc).isoformat(),
           "expected_memory_sha256": current["sha256"],
           "memory_hex": preview["memory_hex"], "write_blocks": preview["write_blocks"]}
    validate_write(job, current)
    locked_block = preview["write_blocks"][0]["block"]
    locks = bytearray(64)
    locks[locked_block] = 1
    v5_dump["Card"]["locks"] = locks.hex()
    with pytest.raises(BridgeError, match="locked"):
        validate_write(job, parse_dump(v5_dump))
    for offset in (0, 160, 224):
        altered = bytearray.fromhex(job["memory_hex"])
        altered[offset] ^= 1
        with pytest.raises(BridgeError, match="header, UID/configuration"):
            validate_write({**job, "memory_hex": altered.hex()}, current)


@pytest.mark.parametrize("output,accepted", [
    ("[+] blk 1\r[+] blk 64\n[+] Saved to json file", True),
    ("[+] blk 1\r[+] blk 63\n[+] Saved to json file", False),
    ("[+] Saved to json file", False),
    ("[+] blk 64\n[-] crc ( fail )", False),
])
def test_bridge_v5_read_requires_complete_progress_and_keeps_native_json(tmp_path, v5_dump, output, accepted):
    executable = tmp_path / "proxmark3.exe"
    executable.write_bytes(b"Synthetic placeholder; subprocess is mocked")
    reader = Proxmark(str(executable), "COM7", tmp_path)
    saved = []

    def fake_run(args, **kwargs):
        assert args[1:6] == ["-p", "COM7", "--incognito", "-f", "-c"]
        assert args[-1].startswith("hf 15 dump -f read-")
        assert kwargs["shell"] is False
        filename = args[-1].removeprefix("hf 15 dump -f ") + ".json"
        path = Path(kwargs["cwd"]) / filename
        path.write_text(json.dumps(v5_dump), encoding="utf-8")
        saved.append(path)
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    with patch("bridge.main.subprocess.run", side_effect=fake_run) as run:
        if accepted:
            assert reader.read({"kind": "read"}) == v5_dump
        else:
            with pytest.raises(BridgeError):
                reader.read({"kind": "read"})
        assert run.call_count == 1
    assert json.loads(saved[0].read_text(encoding="utf-8")) == v5_dump
    assert list(reader.directory.glob("*.bin")) == []
