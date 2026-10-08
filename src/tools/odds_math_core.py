from __future__ import annotations

from tools.rust_native_core import market_weights, poisson, score_matrix as _score_matrix


def score_matrix(
    home_xg: float, away_xg: float, maximum: int = 10, rho: float = 0.04,
) -> list[list[float]]:
    return _score_matrix(home_xg, away_xg, maximum, rho)


def asian_weights(
    matrix: list[list[float]], line: float, side: str,
) -> tuple[float, float, float]:
    if side not in {"home", "away"}:
        raise ValueError("side must be 'home' or 'away'")
    return market_weights(matrix, line, 0, int(side == "away"), 1)


def total_weights(
    matrix: list[list[float]], line: float, over: bool,
) -> tuple[float, float, float]:
    return market_weights(matrix, line, 1, 0, int(over))


def team_total_weights(
    matrix: list[list[float]], line: float, side: str, over: bool,
) -> tuple[float, float, float]:
    if side not in {"home", "away"}:
        raise ValueError("side must be 'home' or 'away'")
    return market_weights(matrix, line, 2, int(side == "away"), int(over))
