from __future__ import annotations

import unittest

from tools.player_rca import (
    FM26_RCA_MODEL, fm24_attribute_ca_milestones, fm24_best_position_key,
    fm24_recommended_ca,
    fm24_ca_budget_attribute_change,
    fm24_training_ca_change, fm26_recommended_ca, fm26_training_ca_change,
)


ENDRICK_ATTRIBUTES = {
    0x0F: 10, 0x10: 14, 0x11: 14, 0x12: 9, 0x13: 11, 0x14: 6,
    0x15: 12, 0x16: 10, 0x17: 11, 0x18: 6, 0x19: 11, 0x20: 13,
    0x21: 12, 0x23: 6, 0x25: 14, 0x26: 13, 0x27: 20, 0x28: 14,
    0x29: 18, 0x2A: 7, 0x2B: 9, 0x2C: 11, 0x2D: 4, 0x31: 15,
    0x32: 11, 0x33: 15, 0x34: 12, 0x35: 14, 0x36: 6, 0x37: 7,
    0x39: 13, 0x3A: 12, 0x3C: 12, 0x3D: 13, 0x41: 18, 0x42: 13,
    0x43: 13, 0x44: 10,
}


def endrick_sample() -> tuple[bytes, bytes]:
    raw = bytearray([5] * 54)
    for attribute_id, value in ENDRICK_ATTRIBUTES.items():
        raw[attribute_id - 0x0F] = value * 5
    positions = bytearray(15)
    positions[12] = 20
    return bytes(raw), bytes(positions)


def jeferson_sample() -> tuple[bytes, bytes]:
    values = {
        0x0F: 3, 0x10: 1, 0x11: 1, 0x12: 13, 0x13: 2, 0x14: 7,
        0x15: 3, 0x16: 7, 0x17: 1, 0x18: 13, 0x19: 4, 0x20: 5,
        0x21: 13, 0x23: 9, 0x25: 4, 0x26: 5, 0x27: 9, 0x28: 20,
        0x29: 5, 0x2A: 2, 0x2B: 5, 0x2C: 8, 0x2D: 5, 0x31: 13,
        0x32: 1, 0x33: 8, 0x34: 12, 0x35: 12, 0x36: 12, 0x37: 14,
        0x39: 6, 0x3A: 8, 0x3C: 13, 0x3D: 11, 0x41: 11, 0x42: 8,
        0x43: 5, 0x44: 9,
    }
    raw = bytearray([5] * 54)
    for attribute_id, value in values.items():
        raw[attribute_id - 0x0F] = value * 5
    positions = bytearray(15)
    positions[3] = 20
    return bytes(raw), bytes(positions)


class PlayerRcaTests(unittest.TestCase):
    def test_endrick_matches_fmrte_recommended_ca(self) -> None:
        raw, positions = endrick_sample()
        result = fm24_recommended_ca(raw, positions)
        self.assertEqual(result["position_key"], 9)
        self.assertAlmostEqual(result["recommended_ca_value"], 123.8022105263)
        self.assertEqual(result["recommended_ca"], 123)

    def test_attribute_ca_cost_depends_on_weight_and_rounding(self) -> None:
        raw, positions = endrick_sample()
        acceleration = fm24_training_ca_change(raw, positions, 0x31)
        acceleration_to_20 = fm24_training_ca_change(raw, positions, 0x31, 5)
        pace = fm24_training_ca_change(raw, positions, 0x35)
        corners = fm24_training_ca_change(raw, positions, 0x2A)
        self.assertEqual(acceleration["ca_cost"], 2)
        self.assertEqual(acceleration_to_20["ca_cost"], 8)
        self.assertEqual(pace["ca_cost"], 1)
        self.assertEqual(corners["ca_cost"], 0)

    def test_training_identifies_attributes_excluded_from_the_position_ca_formula(self) -> None:
        raw, positions = endrick_sample()

        for attribute_id in (0x29, 0x3C, 0x41, 0x42):
            fm24 = fm24_training_ca_change(raw, positions, attribute_id)
            fm26 = fm26_training_ca_change(raw, positions, attribute_id)
            self.assertFalse(fm24["ca_affected"])
            self.assertFalse(fm26["ca_affected"])
            self.assertEqual((fm24["ca_cost"], fm26["ca_cost"]), (0, 0))

        self.assertTrue(fm24_training_ca_change(raw, positions, 0x31)["ca_affected"])

    def test_ca_budget_can_require_two_pills_for_one_attribute_point(self) -> None:
        raw, positions = endrick_sample()

        with self.assertRaisesRegex(ValueError, "至少需要2颗培元丹"):
            fm24_ca_budget_attribute_change(raw, positions, 0x31, 1)

        result = fm24_ca_budget_attribute_change(raw, positions, 0x31, 2)
        self.assertEqual(result["attribute_points"], 1)
        self.assertEqual(result["ca_change"], 2)

    def test_ca_plus_one_can_raise_a_low_weight_attribute_multiple_points(self) -> None:
        raw, positions = endrick_sample()

        result = fm24_ca_budget_attribute_change(raw, positions, 0x2A, 1)

        self.assertEqual(result["attribute_points"], 2)
        self.assertEqual(result["attribute_after"], 9)
        self.assertEqual(result["ca_change"], 1)

    def test_zero_weight_attribute_uses_one_ca_per_attribute_point_floor(self) -> None:
        raw, positions = endrick_sample()

        result = fm24_ca_budget_attribute_change(raw, positions, 0x3C, 1)

        self.assertEqual(result["attribute_points"], 1)
        self.assertEqual(result["ca_change"], 1)
        self.assertEqual(result["formula_ca_change"], 0)
        self.assertEqual(result["minimum_ca_fallback"], 1)

    def test_fm26_jeferson_matches_fmrte_recommended_ca(self) -> None:
        raw, positions = jeferson_sample()
        result = fm26_recommended_ca(raw, positions)
        self.assertEqual(result["model"], FM26_RCA_MODEL)
        self.assertEqual(result["position_key"], 1)
        self.assertAlmostEqual(result["recommended_ca_value"], 63.5033348837)
        self.assertEqual(result["recommended_ca"], 63)

    def test_training_to_20_caps_raw_attribute_at_100(self) -> None:
        raw, positions = jeferson_sample()
        raw = bytearray(raw)
        raw[0x35 - 0x0F] = 88
        updated = bytearray(raw)
        updated[0x35 - 0x0F] = 100

        result = fm26_training_ca_change(bytes(raw), positions, 0x35, 3)

        expected = fm26_recommended_ca(bytes(updated), positions)
        self.assertEqual(result["recommended_ca_after"], expected["recommended_ca"])

    def test_unused_sentinel_does_not_invalidate_formula_snapshot(self) -> None:
        raw, positions = endrick_sample()
        baseline = fm24_recommended_ca(raw, positions)
        raw = bytearray(raw)
        raw[0x2E - 0x0F] = 255

        result = fm24_recommended_ca(bytes(raw), positions)
        milestone = fm24_ca_budget_attribute_change(bytes(raw), positions, 0x2A, 1)

        self.assertEqual(result["recommended_ca"], baseline["recommended_ca"])
        self.assertEqual(milestone["ca_change"], 1)

    def test_milestones_cap_near_maximum_raw_attribute_at_100(self) -> None:
        raw, positions = endrick_sample()
        raw = bytearray(raw)
        raw[0x35 - 0x0F] = 98

        result = fm24_attribute_ca_milestones(bytes(raw), positions, 0x35)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["attribute_before"], 19)
        self.assertEqual(result[0]["attribute_after"], 20)

    def test_minimum_raw_attribute_can_reach_displayed_two(self) -> None:
        raw = bytes([0] * 54)
        positions = bytes([0] * 15)

        training = fm24_training_ca_change(raw, positions, 0x0F)
        milestones = fm24_attribute_ca_milestones(raw, positions, 0x0F)

        self.assertEqual(training["ca_cost"], 0)
        self.assertEqual(milestones[0]["attribute_before"], 1)
        self.assertEqual(milestones[0]["attribute_after"], 2)
        self.assertEqual(milestones[0]["ca_change"], 1)
        self.assertEqual(milestones[0]["minimum_ca_fallback"], 1)

    def test_best_position_uses_fmrte_priority_for_ties(self) -> None:
        positions = bytearray(15)
        positions[3] = positions[12] = 20
        self.assertEqual(fm24_best_position_key(positions), 1)


if __name__ == "__main__":
    unittest.main()
