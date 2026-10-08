from __future__ import annotations

import ctypes
import math
import unittest

from tools.rust_native_core import ABI_VERSION, _LIBRARY, decode_vector_header, verified_write
from tools.odds_math_core import asian_weights, score_matrix, team_total_weights, total_weights


def reference_score_matrix(
    home_xg: float, away_xg: float, maximum: int, rho: float,
) -> list[list[float]]:
    def poisson(k: int, mean: float) -> float:
        return math.exp(-mean) * mean**k / math.factorial(k)

    matrix = [
        [poisson(home, home_xg) * poisson(away, away_xg) for away in range(maximum + 1)]
        for home in range(maximum + 1)
    ]
    for (home, away), factor in {
        (0, 0): 1 - home_xg * away_xg * rho,
        (0, 1): 1 + home_xg * rho,
        (1, 0): 1 + away_xg * rho,
        (1, 1): 1 - rho,
    }.items():
        matrix[home][away] *= max(factor, 0.01)
    total = sum(sum(row) for row in matrix)
    return [[probability / total for probability in row] for row in matrix]


class RustNativeCoreTests(unittest.TestCase):
    def test_expected_abi_is_loaded(self) -> None:
        self.assertEqual(_LIBRARY.fmodd_native_abi_version(), ABI_VERSION)

    def test_vector_header_is_decoded_in_rust(self) -> None:
        raw = (0x1000).to_bytes(8, "little") + (0x1020).to_bytes(8, "little") + (0x1040).to_bytes(8, "little")
        self.assertEqual(decode_vector_header(raw, 8, 4, 8), (0x1000, 0x1020, 0x1040, 4, 8))
        self.assertIsNone(decode_vector_header(raw, 7, 4, 8))

    def test_score_matrices_match_python_reference(self) -> None:
        for home_xg, away_xg, maximum, rho in (
            (0.2, 0.3, 4, 0.0),
            (1.55, 1.1, 10, 0.04),
            (3.8, 0.65, 16, -0.08),
        ):
            with self.subTest(home_xg=home_xg, away_xg=away_xg):
                actual = score_matrix(home_xg, away_xg, maximum, rho)
                expected = reference_score_matrix(home_xg, away_xg, maximum, rho)
                for actual_row, expected_row in zip(actual, expected):
                    for actual_value, expected_value in zip(actual_row, expected_row):
                        self.assertTrue(math.isclose(
                            actual_value, expected_value, rel_tol=1e-14, abs_tol=1e-16,
                        ))

    def test_all_market_weight_routes_preserve_probability(self) -> None:
        matrix = score_matrix(1.83, 1.27)
        values = (
            asian_weights(matrix, -0.75, "home"),
            asian_weights(matrix, 0.25, "away"),
            total_weights(matrix, 2.75, True),
            total_weights(matrix, 3.0, False),
            team_total_weights(matrix, 1.25, "home", True),
            team_total_weights(matrix, 0.75, "away", False),
        )
        for weights in values:
            self.assertTrue(math.isclose(sum(weights), 1.0, abs_tol=1e-12))

    def test_verified_write_updates_current_process_memory(self) -> None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.argtypes = []
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        process = int(kernel32.GetCurrentProcess() or 0)
        target = ctypes.create_string_buffer(b"ABCD")

        verified_write(process, ctypes.addressof(target), b"ABCD", b"WXYZ")

        self.assertEqual(target.raw, b"WXYZ\x00")

    def test_expected_mismatch_does_not_write(self) -> None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.argtypes = []
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        process = int(kernel32.GetCurrentProcess() or 0)
        target = ctypes.create_string_buffer(b"ABCD")

        with self.assertRaisesRegex(RuntimeError, "expected bytes mismatch"):
            verified_write(process, ctypes.addressof(target), b"FAIL", b"WXYZ")

        self.assertEqual(target.raw, b"ABCD\x00")

    def test_invalid_lengths_fail_before_native_write(self) -> None:
        with self.assertRaises(ValueError):
            verified_write(1, 1, b"A", b"BC")


if __name__ == "__main__":
    unittest.main()
