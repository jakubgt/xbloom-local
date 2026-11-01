import copy
import unittest

from app.nfc import (NFCError, crc8_maxim, decode_recipe, make_dump, parse_dump,
                     preview_recipe)
from bridge.main import synthetic_dump


def recipe():
    return {"name": "Synthetic recipe", "dose_g": 18, "ratio": 16, "total_water_ml": 288,
            "grind_setting": 60, "grinder_rpm": 100, "cup_type": "omni", "xid": "TEST",
            "pours": [{"volume_ml": v, "temperature_c": 92, "flow_ml_s": 3.2,
                       "pattern": "spiral", "pause_s": 40 if i == 0 else 10,
                       "agitation_before": False, "agitation_after": i == 0}
                      for i, v in enumerate((54, 117, 117))]}


class NFCCodecTests(unittest.TestCase):
    def setUp(self):
        self.dump = synthetic_dump("E011223344556677")
        self.memory = bytes.fromhex(parse_dump(self.dump)["memory_hex"])

    def test_crc_external_check_vector(self):
        self.assertEqual(crc8_maxim(b"123456789"), 0xA1)

    def test_encode_layout_and_preserve_custom_configuration(self):
        memory = bytearray(self.memory)
        memory[160:] = bytes(range(96))
        preview = preview_recipe(recipe(), bytes(memory))
        self.assertTrue(preview["compatible"], preview["errors"])
        encoded = bytes.fromhex(preview["memory_hex"])
        self.assertEqual(encoded[:32], memory[:32])
        self.assertEqual(encoded[160:], memory[160:])
        self.assertEqual(encoded[32:41], b"TEST\0\0\0\x02\x18")
        self.assertEqual(encoded[41:49], bytes((54, 92, 2, 2, 216, 18, 100, 32)))
        self.assertEqual(encoded[65:67], bytes((20, 16)))
        self.assertTrue(decode_recipe(encoded)["checksum_valid"])
        self.assertTrue(all(8 <= b["block"] <= 39 for b in preview["write_blocks"]))

    def test_checksum_bound_to_card_header(self):
        other = bytearray(self.memory)
        other[0] ^= 1
        other[67] = crc8_maxim(other[:67])
        first = preview_recipe(recipe(), self.memory)
        second = preview_recipe(recipe(), bytes(other))
        self.assertNotEqual(first["checksum"], second["checksum"])
        unbound = preview_recipe(recipe())
        self.assertTrue(unbound["compatible"])
        self.assertIsNone(unbound["checksum"])
        self.assertEqual(unbound["write_blocks"], [])

    def test_fractional_ratio_proposes_explicit_consistent_changes(self):
        value = recipe()
        value.update(ratio=16.5, total_water_ml=297)
        value["pours"][2]["volume_ml"] += 9
        preview = preview_recipe(value, self.memory)
        self.assertTrue(preview["compatible"], preview["errors"])
        self.assertTrue(preview["requires_review"])
        self.assertEqual(preview["effective"]["ratio"], 17)
        self.assertEqual(preview["effective"]["total_water_ml"], 306)
        self.assertEqual(sum(x["volume_ml"] for x in preview["effective"]["pours"]), 306)
        self.assertEqual(value["ratio"], 16.5)
        self.assertIn("ratio", [x["field"] for x in preview["differences"]])

    def test_inconsistent_volumes_and_unsupported_values_rejected(self):
        for field, value in (("dose_g", 18.5), ("grind_setting", 10), ("ratio", float("nan")),
                             ("cup_type", "tea"), ("xid", "TOOLONG8"), ("grinder_enabled", "false")):
            data = recipe()
            data[field] = value
            self.assertFalse(preview_recipe(data)["compatible"], field)
        data = recipe()
        data["pours"][0]["volume_ml"] = 53
        self.assertFalse(preview_recipe(data)["compatible"])
        data = recipe()
        data["pours"][0]["pause_s"] = 255
        self.assertFalse(preview_recipe(data)["compatible"])

    def test_uid_order_and_binary_wrapper(self):
        parsed = parse_dump(self.dump)
        self.assertEqual(parsed["uid"], "E011223344556677")
        packed = bytearray(2235)
        packed[:8] = bytes.fromhex("E011223344556677")[::-1]
        packed[12:14] = bytes((4, 64))
        packed[175:431] = self.memory
        binary = parse_dump(bytes(packed))
        self.assertEqual(binary["memory_hex"], parsed["memory_hex"])
        self.assertEqual(binary["sha256"], parsed["sha256"])
        with self.assertRaises(NFCError):
            parse_dump(self.memory)

    def test_missing_block_lock_status_or_oversized_block_rejected(self):
        missing = copy.deepcopy(self.dump)
        del missing["blocks"]["63"]
        with self.assertRaises(NFCError):
            parse_dump(missing)
        bad = copy.deepcopy(self.dump)
        bad["blocks"]["10"] = "00"
        with self.assertRaises(NFCError):
            parse_dump(bad)
        bad = copy.deepcopy(self.dump)
        bad["Card"]["locks"] = ""
        with self.assertRaises(NFCError):
            parse_dump(bad)

    def test_invalid_existing_checksum_blocks_write_but_preserves_backup(self):
        memory = bytearray(self.memory)
        memory[41] ^= 1
        parsed = parse_dump(make_dump("E011223344556677", bytes(memory)))
        self.assertFalse(parsed["decoded"]["checksum_valid"])
        self.assertFalse(preview_recipe(recipe(), bytes(memory))["compatible"])

    def test_shorter_recipe_clears_old_payload_only(self):
        value = recipe()
        value.update(ratio=10, total_water_ml=180)
        value["pours"] = [{**value["pours"][0], "volume_ml": 180}]
        preview = preview_recipe(value, self.memory)
        target = bytes.fromhex(preview["memory_hex"])
        self.assertEqual(target[52:68], bytes(16))
        self.assertEqual(target[68:], self.memory[68:])
        self.assertTrue(decode_recipe(target)["checksum_valid"])


if __name__ == "__main__":
    unittest.main()
