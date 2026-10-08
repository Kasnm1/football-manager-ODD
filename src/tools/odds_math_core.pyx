# cython: language_level=3

from tools.rust_native_core import (
    market_weights,
    poisson,
    score_matrix as _score_matrix,
)


def score_matrix(home_xg, away_xg, maximum=10, rho=0.04):
    return _score_matrix(home_xg, away_xg, maximum, rho)


def asian_weights(matrix, line, side):
    if side not in {"home", "away"}:
        raise ValueError("side must be 'home' or 'away'")
    return market_weights(matrix, line, 0, int(side == "away"), 1)


def total_weights(matrix, line, over):
    return market_weights(matrix, line, 1, 0, int(over))


def team_total_weights(matrix, line, side, over):
    if side not in {"home", "away"}:
        raise ValueError("side must be 'home' or 'away'")
    return market_weights(matrix, line, 2, int(side == "away"), int(over))
