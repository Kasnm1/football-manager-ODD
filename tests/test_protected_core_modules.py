from __future__ import annotations

import math
import struct
import unittest

from tools.native_layout_core import decode_vector_header
from tools.odds_math_core import (
    asian_weights,
    score_matrix,
    team_total_weights,
    total_weights,
)


class NativeLayoutCoreTests(unittest.TestCase):
    def test_accepts_valid_vector_boundaries(self) -> None:
        raw = struct.pack("<QQQ", 0x1000, 0x1020, 0x1040)
        self.assertEqual(
            decode_vector_header(raw, 8, 4, 8),
            (0x1000, 0x1020, 0x1040, 4, 8),
        )

    def test_accepts_only_explicit_null_empty_vector(self) -> None:
        raw = bytes(0x18)
        self.assertIsNone(decode_vector_header(raw, 8, 16))
        self.assertEqual(
            decode_vector_header(raw, 8, 16, allow_null_empty=True),
            (0, 0, 0, 0, 0),
        )

    def test_rejects_invalid_order_alignment_and_limits(self) -> None:
        invalid = [
            (struct.pack("<QQQ", 0x1000, 0x0FF8, 0x1010), 8, 8, 8),
            (struct.pack("<QQQ", 0x1000, 0x1010, 0x1008), 8, 8, 8),
            (struct.pack("<QQQ", 0x1000, 0x1009, 0x1010), 8, 8, 8),
            (struct.pack("<QQQ", 0x1000, 0x1028, 0x1028), 8, 4, 8),
            (struct.pack("<QQQ", 0x1000, 0x1008, 0x1048), 8, 8, 8),
        ]
        for raw, item_size, max_count, max_capacity in invalid:
            with self.subTest(raw=raw):
                self.assertIsNone(
                    decode_vector_header(raw, item_size, max_count, max_capacity),
                )

    def test_can_validate_byte_span_without_item_alignment(self) -> None:
        raw = struct.pack("<QQQ", 0x1000, 0x1003, 0x1007)
        self.assertEqual(
            decode_vector_header(raw, 1, 8, 8, require_alignment=False),
            (0x1000, 0x1003, 0x1007, 3, 7),
        )


class OddsMathCoreTests(unittest.TestCase):
    def test_score_matrix_is_normalized_and_keeps_model_default(self) -> None:
        matrix = score_matrix(1.55, 1.1)
        self.assertEqual((len(matrix), len(matrix[0])), (11, 11))
        self.assertTrue(math.isclose(sum(map(sum, matrix)), 1.0, abs_tol=1e-12))
        self.assertAlmostEqual(matrix[0][0], 0.06583285184264932, places=14)

    def test_market_weights_on_known_matrix(self) -> None:
        matrix = [[0.25, 0.25], [0.25, 0.25]]
        self.assertEqual(asian_weights(matrix, 0.0, "home"), (0.25, 0.5, 0.25))
        self.assertEqual(total_weights(matrix, 1.0, True), (0.25, 0.5, 0.25))
        self.assertEqual(team_total_weights(matrix, 0.5, "home", True), (0.5, 0.0, 0.5))

    def test_quarter_lines_preserve_total_probability(self) -> None:
        matrix = score_matrix(2.1, 0.8)
        weights = [
            asian_weights(matrix, -0.25, "home"),
            total_weights(matrix, 2.75, True),
            team_total_weights(matrix, 1.25, "away", False),
        ]
        for value in weights:
            with self.subTest(value=value):
                self.assertTrue(math.isclose(sum(value), 1.0, abs_tol=1e-12))


if __name__ == "__main__":
    unittest.main()
