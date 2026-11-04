"""Conservative xBloom coffee NFC codec; no hardware access.

Protocol references and supported limits are documented in docs/nfc-format.md.
The original 32-byte authentication header is opaque and is never generated.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from typing import Any

RECIPE_START = 32
RECIPE_LIMIT = 160  # Keep custom-chip configuration blocks out of recipe writes.
MAX_POURS = 14
PATTERNS = {"center": 0, "circle": 1, "spiral": 2}
CUPS = {"xpod": 0, "other": 1, "omni": 2}


class NFCError(ValueError):
    pass


def crc8_maxim(data: bytes) -> int:
    crc = 0
    for value in data:
        crc ^= value
        for _ in range(8):
            crc = (crc >> 1) ^ (0x8C if crc & 1 else 0)
    return crc


def normalize_uid(uid: str, *, little_endian: bool = False) -> str:
    uid = re.sub(r"[\s:-]", "", str(uid)).upper()
    if not re.fullmatch(r"[0-9A-F]{16}", uid):
        raise NFCError("A card UID must contain exactly eight hexadecimal bytes.")
    if little_endian:
        uid = bytes.fromhex(uid)[::-1].hex().upper()
    if not uid.startswith("E0"):
        raise NFCError("ISO 15693 UID must start with E0 in display order.")
    return uid


def memory_sha256(memory: bytes) -> str:
    return hashlib.sha256(memory).hexdigest()


def decode_recipe(memory: bytes) -> dict[str, Any]:
    if len(memory) < 52 or len(memory) % 4:
        raise NFCError("A complete card image with four-byte blocks is required.")
    count = memory[40] >> 3
    if not 1 <= count <= MAX_POURS or memory[40] & 7:
        raise NFCError("Unsupported recipe pour count or flags.")
    end = 44 + count * 8
    if end > min(len(memory), RECIPE_LIMIT):
        raise NFCError("Recipe extends beyond the supported recipe region.")
    cup = memory[39]
    if cup not in CUPS.values():
        raise NFCError("Only xPod, Other, and Omni coffee recipes are supported.")
    pours = []
    for i in range(count):
        p = memory[41 + i * 8:49 + i * 8]
        pours.append({
            "volume_ml": p[0], "temperature_c": p[1],
            "pattern": next((k for k, v in PATTERNS.items() if v == p[2]), "unknown"),
            "agitation_before": bool(p[3] & 1), "agitation_after": bool(p[3] & 2),
            "pause_s": ((-p[4]) & 255) + (p[5] >> 5) * 60,
            "flow_ml_s": p[7] / 10,
        })
    dose = memory[46] & 31
    rpm = memory[47]
    grind = memory[end - 3]
    return {
        "xid": memory[32:39].rstrip(b"\0").decode("ascii", errors="replace"),
        "cup_type": next(k for k, v in CUPS.items() if v == cup),
        "dose_g": dose if 1 <= dose <= 31 else 15,
        "grinder_rpm": rpm if 60 <= rpm <= 120 else 120,
        "grind_setting": grind + 40, "grinder_enabled": grind != 41,
        "ratio": memory[end - 2], "total_water_ml": sum(p["volume_ml"] for p in pours),
        "pours": pours, "checksum": f"{memory[end - 1]:02X}",
        "checksum_valid": crc8_maxim(memory[:end - 1]) == memory[end - 1],
        "recipe_end": end,
        "warnings": (["Legacy card omits dose; decoded using 15 g fallback."] if dose == 0 else []),
    }


def _hex_byte(value: Any, name: str) -> int:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{2}", value):
        raise NFCError(f"Invalid {name} byte.")
    return int(value, 16)


def parse_dump(data: dict | bytes | str) -> dict[str, Any]:
    """Parse Proxmark v4/v5 JSON or the known 2235-byte v4 binary format.

    The .bin wrapper is NOT raw tag memory. Unknown binary formats are rejected.
    All blocks and lock bytes must be present; truncated reads cannot be backups.
    """
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (ValueError, TypeError) as exc:
            raise NFCError("Invalid dump JSON.") from exc
    if isinstance(data, bytes):
        if data.lstrip().startswith(b"{"):
            try:
                return parse_dump(json.loads(data.decode("utf-8-sig")))
            except (ValueError, UnicodeError) as exc:
                raise NFCError("Invalid dump JSON.") from exc
        if len(data) != 2235:
            raise NFCError("Unsupported binary dump. Use Proxmark 15693 v4/v5 JSON or the legacy 2235-byte binary.")
        block_size, count = data[12], data[13]
        uid = normalize_uid(data[:8].hex(), little_endian=True)
        locks = list(data[15:15 + count])
        memory = data[175:175 + count * block_size]
        format_name = "proxmark3-bin-v4"
    elif isinstance(data, dict):
        file_type = data.get("FileType")
        if file_type not in ("15693 v4", "15693 v5"):
            raise NFCError("Only Proxmark FileType '15693 v4' or '15693 v5' dumps are supported.")
        is_v5 = file_type == "15693 v5"
        card = data.get("Card")
        blocks = data.get("blocks")
        if not isinstance(card, dict) or not isinstance(blocks, dict):
            raise NFCError("Dump must contain Card metadata and numbered blocks.")
        uid = normalize_uid(card.get("uid", ""), little_endian=True)
        block_size = _hex_byte(card.get("bytesperpage"), "bytesperpage")
        if is_v5:
            # v5 changes only pagescount to two bytes in little-endian order.
            page_count = card.get("pagescount")
            if not isinstance(page_count, str) or not re.fullmatch(r"[0-9a-fA-F]{4}", page_count):
                raise NFCError("Invalid v5 pagescount; expected exactly two little-endian hex bytes.")
            count = int.from_bytes(bytes.fromhex(page_count), "little")
        else:
            count = _hex_byte(card.get("pagescount"), "pagescount")
        if block_size != 4 or not 13 <= count <= 160:
            raise NFCError("Expected 13–160 four-byte blocks.")
        if is_v5 and set(blocks) != {str(i) for i in range(count)}:
            raise NFCError("v5 blocks must exactly match the declared page count.")
        try:
            values = [blocks[str(i)] for i in range(count)]
            if any(not isinstance(v, str) or not re.fullmatch(r"[0-9A-Fa-f]{8}", v) for v in values):
                raise NFCError("Each block must contain exactly four bytes.")
            memory = bytes.fromhex("".join(values))
            lock_hex = card.get("locks", "")
            if not isinstance(lock_hex, str) or len(lock_hex) != count * 2:
                raise NFCError("Dump must include lock status for every block.")
            if is_v5 and not re.fullmatch(r"[0-9a-fA-F]+", lock_hex):
                raise NFCError("v5 lock status must contain one hex byte per block.")
            locks = list(bytes.fromhex(lock_hex))
        except (KeyError, ValueError) as exc:
            raise NFCError("Dump is incomplete or contains invalid hexadecimal data.") from exc
        format_name = "proxmark3-json-v5" if is_v5 else "proxmark3-json-v4"
    else:
        raise NFCError("Expected a Proxmark JSON object or binary dump.")
    if block_size != 4 or not 13 <= count <= 160 or len(memory) != count * 4:
        raise NFCError("Expected a complete image of 13–160 four-byte blocks.")
    warnings = []
    try:
        decoded = decode_recipe(memory)
        if not decoded["checksum_valid"]:
            warnings.append("Recipe checksum is invalid; preserve this backup but do not write a new recipe yet.")
    except NFCError as exc:
        decoded = None
        warnings.append(str(exc))
    return {"uid": uid, "blocks": [memory[i:i+4].hex().upper() for i in range(0, len(memory), 4)],
            "memory_hex": memory.hex().upper(), "sha256": memory_sha256(memory),
            "block_count": count, "block_size": block_size, "locks": locks,
            "decoded": decoded, "format": format_name, "warnings": warnings}


def make_dump(uid: str, memory: bytes, locks: list[int] | None = None) -> dict:
    """Create a portable JSON memory backup; not a replacement for native metadata."""
    uid = normalize_uid(uid)
    if len(memory) % 4 or not 13 <= len(memory) // 4 <= 160:
        raise NFCError("Unsupported memory size.")
    count = len(memory) // 4
    locks = [0] * count if locks is None else locks
    if len(locks) != count or any(x not in range(256) for x in locks):
        raise NFCError("Invalid lock status.")
    return {"Created": "xbloom-local", "FileType": "15693 v4", "Card": {
        "uid": bytes.fromhex(uid)[::-1].hex().upper(), "dsfid": "00", "afi": "00",
        "dsfidlock": "00", "afilock": "00", "bytesperpage": "04",
        "pagescount": f"{count:02X}", "ic": "00", "locks": bytes(locks).hex().upper(),
        "random": "0000", "privacypasswd": "00000000", "state": "00"},
        "blocks": {str(i//4): memory[i:i+4].hex().upper() for i in range(0, len(memory), 4)}}


def _number(value: Any, name: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise NFCError(f"{name} must be a finite number.")
    if not low <= value <= high:
        raise NFCError(f"{name} must be between {low:g} and {high:g}.")
    return float(value)


def _round(value: float) -> int:
    return math.floor(value + 0.5)


def _boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise NFCError(f"{name} must be true or false, not text or a number.")
    return value


def preview_recipe(recipe: dict, memory: bytes | None = None) -> dict[str, Any]:
    """Return an explicit encoding proposal. No caller should discard differences.

    With no card image, the checksum is unbound (null) and no write payload exists.
    Fractional ratios are proposed as nearest integer with proportional pours.
    """
    result: dict[str, Any] = {"compatible": False, "effective": None, "differences": [],
        "errors": [], "warnings": [], "recipe_hex": None, "checksum": None,
        "write_blocks": [], "memory_hex": None, "requires_review": False,
        "card_bound": memory is not None, "hardware_validated": False}
    try:
        if not isinstance(recipe, dict):
            raise NFCError("Recipe must be an object.")
        dose = _number(recipe.get("dose_g", 18), "Dose", 1, 31)
        if dose != int(dose):
            raise NFCError("NFC dose requires whole grams; choose an integer dose explicitly.")
        ratio = _number(recipe.get("ratio", 16), "Ratio", 5, 100)
        grind = _number(recipe.get("grind_setting", 60), "Grind setting", 40, 80)
        rpm = _number(recipe.get("grinder_rpm", 120), "Grinder RPM", 60, 120)
        grinder_enabled = _boolean(recipe.get("grinder_enabled", True), "Grinder enabled")
        cup = recipe.get("cup_type", "omni")
        if cup not in CUPS:
            raise NFCError("Unsupported cup type. Choose omni, other, or xpod.")
        xid = recipe.get("xid", "") or ""
        if not isinstance(xid, str) or len(xid) > 7 or any(ord(x) < 32 or ord(x) > 126 for x in xid):
            raise NFCError("XID must be at most seven printable ASCII characters.")
        pours = recipe.get("pours")
        if not isinstance(pours, list) or not 1 <= len(pours) <= MAX_POURS:
            raise NFCError(f"Recipes require 1–{MAX_POURS} pours.")
        raw_volumes = []
        effective_pours = []
        for i, p in enumerate(pours):
            if not isinstance(p, dict):
                raise NFCError(f"Pour {i+1} must be an object.")
            raw_volumes.append(_number(p.get("volume_ml"), f"Pour {i+1} volume", 1, 240))
            temp = _number(p.get("temperature_c"), f"Pour {i+1} temperature", 39, 99)
            flow = _number(p.get("flow_ml_s", 3), f"Pour {i+1} flow", 3, 3.5)
            pause = _number(p.get("pause_s", 0), f"Pour {i+1} pause", 0, 59)
            if p.get("pattern", "spiral") not in PATTERNS:
                raise NFCError(f"Pour {i+1} pattern must be center, circle, or spiral.")
            ep = copy.deepcopy(p)
            ep.update(temperature_c=_round(temp), flow_ml_s=_round(flow * 10)/10,
                pause_s=_round(pause), pattern=p.get("pattern", "spiral"),
                agitation_before=_boolean(p.get("agitation_before", False), f"Pour {i+1} agitation before"),
                agitation_after=_boolean(p.get("agitation_after", False), f"Pour {i+1} agitation after"))
            effective_pours.append(ep)
        requested_total = sum(raw_volumes)
        if abs(requested_total - dose * ratio) > 0.011:
            raise NFCError("Pour volumes must add up to dose × intended ratio before encoding.")
        total = recipe.get("total_water_ml", requested_total)
        if abs(_number(total, "Total water", 1, 3360) - requested_total) > 0.011:
            raise NFCError("Total water must match the sum of pour volumes.")
        effective_ratio = _round(ratio)
        target_total = int(dose) * effective_ratio
        scaled = [v * target_total / requested_total for v in raw_volumes]
        allocated = [math.floor(v) for v in scaled]
        remainder = target_total - sum(allocated)
        order = sorted(range(len(scaled)), key=lambda i: (scaled[i] - allocated[i], -i), reverse=True)
        for i in order[:remainder]:
            allocated[i] += 1
        if any(not 1 <= v <= 240 for v in allocated):
            raise NFCError("Encoding would put a pour outside 1–240 ml. Adjust the pour schedule.")
        for i, volume in enumerate(allocated):
            effective_pours[i]["volume_ml"] = volume
        effective = copy.deepcopy(recipe)
        effective.update(dose_g=int(dose), ratio=effective_ratio, total_water_ml=target_total,
            grind_setting=_round(grind), grinder_rpm=_round(rpm / 10) * 10,
            grinder_enabled=grinder_enabled, cup_type=cup, xid=xid,
            pours=effective_pours)
        for field in ("ratio", "total_water_ml", "grind_setting", "grinder_rpm"):
            old = recipe.get(field, {"total_water_ml": requested_total, "grind_setting": grind, "grinder_rpm": rpm}.get(field, ratio))
            if old != effective[field]:
                result["differences"].append({"field": field, "intended": old, "encoded": effective[field]})
        for i, (before, after) in enumerate(zip(pours, effective_pours)):
            for field in ("volume_ml", "temperature_c", "flow_ml_s", "pause_s"):
                old = before.get(field, 3 if field == "flow_ml_s" else 0)
                if old != after[field]:
                    result["differences"].append({"field": f"pours[{i}].{field}", "intended": old, "encoded": after[field]})
        result.update(compatible=True, effective=effective, requires_review=bool(result["differences"]))
        result["warnings"].append("Protocol-compatible proposal; a brew on your offline machine is required to validate firmware behavior.")
        if memory is None:
            result["warnings"].append("Read and back up the selected card to bind its header and calculate the final checksum.")
            return result
        old_recipe = decode_recipe(memory)
        if not old_recipe["checksum_valid"]:
            raise NFCError("Existing card checksum is invalid. Resolve the card backup before writing.")
        data = bytearray(memory[:32])
        data.extend(xid.encode("ascii").ljust(7, b"\0"))
        data.extend((CUPS[cup], len(pours) << 3))
        for i, p in enumerate(effective_pours):
            data.extend((p["volume_ml"], p["temperature_c"], PATTERNS[p["pattern"]],
                int(p["agitation_before"]) | (int(p["agitation_after"]) << 1),
                (-p["pause_s"]) & 255, int(dose) if i == 0 else 0,
                effective["grinder_rpm"] if i == 0 else 0, _round(p["flow_ml_s"] * 10)))
        data.extend((effective["grind_setting"] - 40 if effective["grinder_enabled"] else 41, effective_ratio))
        data.append(crc8_maxim(data))
        stop = math.ceil(max(len(data), old_recipe["recipe_end"]) / 4) * 4
        if stop > min(len(memory), RECIPE_LIMIT):
            raise NFCError("This recipe does not fit the selected card's supported recipe region.")
        updated = bytearray(memory)
        updated[32:stop] = data[32:] + bytes(stop-len(data))
        result.update(recipe_hex=bytes(data[32:]).hex().upper(), checksum=f"{data[-1]:02X}",
            memory_hex=bytes(updated).hex().upper(),
            write_blocks=[{"block": i//4, "data": bytes(updated[i:i+4]).hex().upper()}
                for i in range(32, stop, 4) if updated[i:i+4] != memory[i:i+4]])
        return result
    except NFCError as exc:
        # NFCError contains deliberate user-facing validation guidance.
        message = str(exc)
    except (TypeError, KeyError, OverflowError):
        # Unexpected codec failures can contain internal values or type details.
        message = "Recipe could not be encoded. Check its fields and try again."
    result.update(compatible=False, recipe_hex=None, checksum=None, write_blocks=[], memory_hex=None)
    result["errors"].append(message)
    return result
