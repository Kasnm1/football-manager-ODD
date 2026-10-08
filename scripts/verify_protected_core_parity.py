from __future__ import annotations

import argparse
import importlib.util
import math
import os
import struct
import sys
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 compatibility.
    import tomli as tomllib
from pathlib import Path
from types import ModuleType
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _compiled_module(protected_root: Path, leaf_name: str) -> ModuleType:
    matches = list((protected_root / "tools").glob(f"{leaf_name}.*.pyd"))
    if len(matches) != 1:
        raise RuntimeError(f"Expected one compiled {leaf_name} module, got {matches}")
    return _load_module(leaf_name, matches[0])


def _assert_equal(reference: Any, compiled: Any, path: str = "result") -> None:
    if isinstance(reference, float):
        if not isinstance(compiled, (int, float)) or not math.isclose(
            reference, float(compiled), rel_tol=1e-12, abs_tol=1e-14,
        ):
            raise AssertionError(f"{path}: {reference!r} != {compiled!r}")
        return
    if isinstance(reference, (list, tuple)):
        if type(reference) is not type(compiled) or len(reference) != len(compiled):
            raise AssertionError(f"{path}: container mismatch")
        for index, (left, right) in enumerate(zip(reference, compiled)):
            _assert_equal(left, right, f"{path}[{index}]")
        return
    if reference != compiled:
        raise AssertionError(f"{path}: {reference!r} != {compiled!r}")


def verify(protected_root: Path) -> None:
    protection = tomllib.loads(
        (ROOT / "build" / "protection.toml").read_text(encoding="utf-8")
    )
    os.environ["FMODD_RUST_NATIVE_DLL"] = str((
        ROOT / str(protection["rust_native_output"])
        / str(protection["rust_native_dll"])
    ).resolve())
    os.environ["FMODD_CPP_HOOK_DLL"] = str((
        ROOT / str(protection["cpp_native_output"])
        / str(protection["cpp_hook_dll"])
    ).resolve())
    for module_name in protection["protected_modules"]:
        _compiled_module(protected_root, module_name.rsplit(".", 1)[-1])

    layout_ref = _load_module(
        "native_layout_core_reference", ROOT / "tools" / "native_layout_core.py",
    )
    layout_native = _compiled_module(protected_root, "native_layout_core")
    vector_cases = [
        (struct.pack("<QQQ", 0, 0, 0), 8, 16, 16, True, True),
        (struct.pack("<QQQ", 0x1000, 0x1020, 0x1040), 8, 4, 8, False, True),
        (struct.pack("<QQQ", 0x1000, 0x1003, 0x1007), 1, 8, 8, False, False),
        (struct.pack("<QQQ", 0x1000, 0x0FF8, 0x1010), 8, 8, 8, False, True),
        (struct.pack("<QQQ", 0x1000, 0x1010, 0x1008), 8, 8, 8, False, True),
        (struct.pack("<QQQ", 0x1000, 0x1009, 0x1010), 8, 8, 8, False, True),
    ]
    for index, args in enumerate(vector_cases):
        _assert_equal(
            layout_ref.decode_vector_header(*args),
            layout_native.decode_vector_header(*args),
            f"vector_cases[{index}]",
        )

    odds_ref = _load_module(
        "odds_math_core_reference", ROOT / "tools" / "odds_math_core.py",
    )
    odds_native = _compiled_module(protected_root, "odds_math_core")
    for index, (home_xg, away_xg, maximum, rho) in enumerate([
        (0.2, 0.4, 2, 0.04),
        (1.55, 1.1, 10, 0.04),
        (3.8, 0.65, 12, 0.0),
        (0.01, 0.01, 4, -0.08),
    ]):
        reference_matrix = odds_ref.score_matrix(home_xg, away_xg, maximum, rho)
        compiled_matrix = odds_native.score_matrix(home_xg, away_xg, maximum, rho)
        _assert_equal(reference_matrix, compiled_matrix, f"score_matrix[{index}]")
        for line in (-2.25, -1.0, -0.25, 0.0, 0.75, 2.5, 4.25):
            for side in ("home", "away"):
                _assert_equal(
                    odds_ref.asian_weights(reference_matrix, line, side),
                    odds_native.asian_weights(compiled_matrix, line, side),
                    f"asian_weights[{index},{line},{side}]",
                )
                for over in (False, True):
                    _assert_equal(
                        odds_ref.team_total_weights(reference_matrix, line, side, over),
                        odds_native.team_total_weights(compiled_matrix, line, side, over),
                        f"team_total_weights[{index},{line},{side},{over}]",
                    )
            for over in (False, True):
                _assert_equal(
                    odds_ref.total_weights(reference_matrix, line, over),
                    odds_native.total_weights(compiled_matrix, line, over),
                    f"total_weights[{index},{line},{over}]",
                )


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare protected cores with Python references.")
    parser.add_argument("--protected-root", type=Path, required=True)
    args = parser.parse_args()
    verify((ROOT / args.protected_root).resolve() if not args.protected_root.is_absolute() else args.protected_root)
    print("Protected core parity: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
