from __future__ import annotations

from itertools import combinations
from typing import Any, Iterable


MAX_PARLAY_COMBINATIONS = 5000


# FMODD pass packages. Each tuple lists the leg sizes included in the package.
# X1 means one accumulator containing every match; X2, X3, ... select all
# combinations of that leg size. NXN is omitted because it would be single
# betting. 3X4 is the mixed package containing all doubles plus the treble.
PASS_METHODS: dict[int, dict[str, tuple[int, ...]]] = {
    2: {"2X1": (2,)},
    3: {"3X1": (3,), "3X2": (2,), "3X4": (2, 3)},
    4: {"4X1": (4,), "4X2": (2,), "4X3": (3,)},
    5: {"5X1": (5,), "5X2": (2,), "5X3": (3,), "5X4": (4,)},
    6: {"6X1": (6,), "6X2": (2,), "6X3": (3,), "6X4": (4,), "6X5": (5,)},
    7: {"7X1": (7,), "7X2": (2,), "7X3": (3,), "7X4": (4,), "7X5": (5,), "7X6": (6,)},
    8: {"8X1": (8,), "8X2": (2,), "8X3": (3,), "8X4": (4,), "8X5": (5,), "8X6": (6,), "8X7": (7,)},
    9: {"9X1": (9,)},
    10: {"10X1": (10,)},
}


def default_pass_code(match_count: int) -> str:
    return f"{match_count}X1"


def pass_leg_sizes(match_count: int, pass_code: Any = None) -> tuple[str, tuple[int, ...]]:
    methods = PASS_METHODS.get(match_count) or {}
    code = str(pass_code or default_pass_code(match_count)).upper().replace("×", "X")
    sizes = methods.get(code)
    if sizes is None:
        raise ValueError("过关方式与当前比赛场数不匹配")
    return code, sizes


def pass_subsets(match_count: int, leg_sizes: Iterable[int]) -> list[tuple[int, ...]]:
    return [
        subset
        for size in leg_sizes
        for subset in combinations(range(match_count), size)
    ]
