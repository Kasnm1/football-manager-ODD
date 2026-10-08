from __future__ import annotations

from typing import Sequence


FM24_RCA_MODEL = "fm24-verified-rca-v1"
FM26_RCA_MODEL = "fm26-verified-rca-v1"

# Position keys match FMRTE PlayerGeneralPosition. Each tuple contains the
# non-zero attribute weights followed by the weaker-foot weight.
FMRTE_RCA_WEIGHTS: dict[int, tuple[dict[int, float], float]] = {
    0: (
        {0x12: 1, 0x16: 3, 0x19: 1, 0x1A: 8, 0x1B: 6, 0x1C: 6, 0x1D: 5,
         0x1E: 5, 0x1F: 3, 0x20: 3, 0x21: 10, 0x22: 4, 0x23: 5, 0x24: 8,
         0x25: 1, 0x26: 1, 0x2B: 2, 0x2C: 1, 0x31: 6, 0x33: 4, 0x34: 1,
         0x35: 3, 0x36: 1, 0x37: 2, 0x39: 2, 0x3A: 6, 0x3D: 8, 0x43: 2,
         0x44: 6},
        3,
    ),
    1: (
        {0x0F: 1, 0x10: 1, 0x11: 1, 0x12: 5, 0x13: 1, 0x14: 8, 0x15: 1,
         0x16: 2, 0x17: 1, 0x18: 5, 0x19: 1, 0x20: 5, 0x21: 10, 0x23: 8,
         0x25: 2, 0x26: 1, 0x2A: 1, 0x2B: 1, 0x2C: 2, 0x2D: 1, 0x31: 6,
         0x32: 1, 0x33: 6, 0x34: 3, 0x35: 5, 0x36: 6, 0x37: 2, 0x39: 2,
         0x3A: 2, 0x3D: 6, 0x43: 2, 0x44: 4},
        4.5,
    ),
    2: (
        {0x0F: 2, 0x10: 1, 0x11: 1, 0x12: 2, 0x13: 1, 0x14: 3, 0x15: 1,
         0x16: 2, 0x17: 1, 0x18: 4, 0x19: 2, 0x20: 3, 0x21: 7, 0x23: 4,
         0x25: 3, 0x26: 2, 0x2A: 1, 0x2B: 2, 0x2C: 2, 0x2D: 1, 0x31: 7,
         0x32: 1, 0x33: 4, 0x34: 6, 0x35: 5, 0x36: 2, 0x37: 1, 0x39: 2,
         0x3A: 2, 0x3D: 6, 0x43: 2, 0x44: 4},
        4,
    ),
    3: (
        {0x0F: 3, 0x10: 2, 0x11: 1, 0x12: 1, 0x13: 1, 0x14: 2, 0x15: 2,
         0x16: 3, 0x17: 1, 0x18: 3, 0x19: 2, 0x20: 3, 0x21: 5, 0x23: 3,
         0x25: 3, 0x26: 3, 0x2A: 1, 0x2B: 2, 0x2C: 2, 0x2D: 1, 0x31: 8,
         0x32: 1, 0x33: 4, 0x34: 7, 0x35: 6, 0x36: 1, 0x37: 1, 0x39: 2,
         0x3A: 1, 0x3D: 5, 0x43: 2, 0x44: 3},
        4,
    ),
    4: (
        {0x0F: 1, 0x10: 2, 0x11: 2, 0x12: 1, 0x13: 3, 0x14: 3, 0x15: 1,
         0x16: 4, 0x17: 1, 0x18: 7, 0x19: 4, 0x20: 5, 0x21: 8, 0x23: 5,
         0x25: 4, 0x26: 3, 0x2A: 1, 0x2B: 2, 0x2C: 4, 0x2D: 1, 0x31: 6,
         0x32: 1, 0x33: 5, 0x34: 4, 0x35: 4, 0x36: 1, 0x37: 1, 0x39: 2,
         0x3A: 1, 0x3D: 6, 0x43: 2, 0x44: 3},
        5,
    ),
    5: (
        {0x0F: 1, 0x10: 2, 0x11: 2, 0x12: 1, 0x13: 3, 0x14: 3, 0x15: 3,
         0x16: 6, 0x17: 1, 0x18: 3, 0x19: 6, 0x20: 3, 0x21: 7, 0x23: 3,
         0x25: 6, 0x26: 4, 0x2A: 1, 0x2B: 2, 0x2C: 3, 0x2D: 1, 0x31: 6,
         0x32: 1, 0x33: 4, 0x34: 6, 0x35: 5, 0x36: 1, 0x37: 1, 0x39: 2,
         0x3A: 1, 0x3D: 6, 0x43: 3, 0x44: 2},
        6,
    ),
    6: (
        {0x0F: 5, 0x10: 3, 0x11: 2, 0x12: 1, 0x13: 2, 0x14: 1, 0x15: 2,
         0x16: 3, 0x17: 1, 0x18: 2, 0x19: 3, 0x20: 3, 0x21: 5, 0x23: 1,
         0x25: 4, 0x26: 4, 0x2A: 1, 0x2B: 2, 0x2C: 3, 0x2D: 1, 0x31: 8,
         0x32: 1, 0x33: 3, 0x34: 5, 0x35: 6, 0x36: 1, 0x37: 1, 0x39: 2,
         0x3A: 1, 0x3D: 6, 0x43: 3, 0x44: 2},
        5,
    ),
    7: (
        {0x0F: 1, 0x10: 3, 0x11: 3, 0x12: 1, 0x13: 3, 0x14: 1, 0x15: 3,
         0x16: 4, 0x17: 1, 0x18: 2, 0x19: 6, 0x20: 3, 0x21: 6, 0x23: 2,
         0x25: 5, 0x26: 5, 0x2A: 1, 0x2B: 2, 0x2C: 3, 0x2D: 1, 0x31: 9,
         0x32: 1, 0x33: 3, 0x34: 6, 0x35: 7, 0x36: 1, 0x37: 1, 0x39: 2,
         0x3A: 1, 0x3D: 6, 0x43: 3, 0x44: 2},
        7,
    ),
    8: (
        {0x0F: 5, 0x10: 5, 0x11: 2, 0x12: 1, 0x13: 2, 0x14: 1, 0x15: 2,
         0x16: 2, 0x17: 1, 0x18: 2, 0x19: 3, 0x20: 3, 0x21: 5, 0x23: 1,
         0x25: 5, 0x26: 4, 0x2A: 1, 0x2B: 2, 0x2C: 3, 0x2D: 1, 0x31: 10,
         0x32: 1, 0x33: 3, 0x34: 7, 0x35: 10, 0x36: 1, 0x37: 1, 0x39: 2,
         0x3A: 1, 0x3D: 6, 0x43: 3, 0x44: 2},
        5.5,
    ),
    9: (
        {0x0F: 2, 0x10: 5, 0x11: 8, 0x12: 6, 0x13: 2, 0x14: 1, 0x15: 6,
         0x16: 2, 0x17: 1, 0x18: 1, 0x19: 2, 0x20: 5, 0x21: 5, 0x23: 2,
         0x25: 6, 0x26: 4, 0x2A: 1, 0x2B: 1, 0x2C: 2, 0x2D: 1, 0x31: 10,
         0x32: 1, 0x33: 6, 0x34: 6, 0x35: 7, 0x36: 5, 0x37: 1, 0x39: 2,
         0x3A: 1, 0x3D: 6, 0x43: 6, 0x44: 2},
        7.5,
    ),
}

# FMRTE GetBestPosition checks these player-position slots in this order and
# replaces the result only for a strictly higher familiarity value.
FMRTE_POSITION_PRIORITY = (
    (0, 0), (3, 1), (2, 2), (4, 2), (13, 3), (14, 3), (5, 4),
    (7, 5), (6, 6), (8, 6), (10, 7), (11, 8), (9, 8), (12, 9),
)


def _display_attribute(raw_value: int, display_bias: int) -> int:
    return max(1, min(20, (int(raw_value) + int(display_bias)) // 5))


def _raw_attribute_target(
    current_raw: int, target_display: int, display_bias: int,
) -> int:
    """Return a raw value for an exact display target, preserving valid residue."""
    target = int(target_display)
    current = _display_attribute(current_raw, display_bias)
    preserved = int(current_raw) + (target - current) * 5
    if 0 <= preserved <= 100 and _display_attribute(preserved, display_bias) == target:
        return preserved
    raw = target * 5 - int(display_bias)
    if not 1 <= target <= 20 or not 0 <= raw <= 100:
        raise ValueError("球员属性目标无法安全换算")
    if _display_attribute(raw, display_bias) != target:
        raise ValueError("球员属性目标无法安全换算")
    return raw


def fm24_best_position_key(position_ratings: Sequence[int]) -> int:
    if len(position_ratings) < 15:
        raise ValueError("球员位置数据无法安全读取")
    best_rating = -1
    best_position = 0
    for index, general_position in FMRTE_POSITION_PRIORITY:
        rating = int(position_ratings[index])
        if rating > best_rating:
            best_rating = rating
            best_position = general_position
    return best_position


def _recommended_ca(
    raw_attributes: bytes, position_ratings: Sequence[int], display_bias: int = 0,
    model: str = FM24_RCA_MODEL,
) -> dict[str, float | int | str]:
    if len(raw_attributes) != 54:
        raise ValueError("球员属性块无法安全读取")
    position_key = fm24_best_position_key(position_ratings)
    weights, weaker_foot_weight = FMRTE_RCA_WEIGHTS[position_key]
    formula_attribute_ids = (*weights, 0x27, 0x28)
    if any(raw_attributes[attribute_id - 0x0F] > 100 for attribute_id in formula_attribute_ids):
        raise ValueError("球员公式属性无法安全读取")
    weighted_value = 0.0
    total_weight = 0.0
    for attribute_id, weight in weights.items():
        value = _display_attribute(raw_attributes[attribute_id - 0x0F], display_bias)
        weighted_value += float(weight) * value
        total_weight += float(weight)
    weaker_foot = min(
        _display_attribute(raw_attributes[0x27 - 0x0F], display_bias),
        _display_attribute(raw_attributes[0x28 - 0x0F], display_bias),
    )
    weighted_value += weaker_foot_weight * weaker_foot
    total_weight += weaker_foot_weight
    average = weighted_value / total_weight
    value = 1.0 if average <= 6 else 200.0 if average > 16 else 19.929 * average - 119.38
    return {
        "model": model,
        "position_key": position_key,
        "weighted_average": average,
        "recommended_ca_value": value,
        "recommended_ca": max(1, min(200, int(value))),
    }


def fm24_recommended_ca(
    raw_attributes: bytes, position_ratings: Sequence[int], display_bias: int = 0,
) -> dict[str, float | int | str]:
    return _recommended_ca(
        raw_attributes, position_ratings, display_bias, FM24_RCA_MODEL,
    )


def fm26_recommended_ca(
    raw_attributes: bytes, position_ratings: Sequence[int], display_bias: int = 0,
) -> dict[str, float | int | str]:
    return _recommended_ca(
        raw_attributes, position_ratings, display_bias, FM26_RCA_MODEL,
    )


def _training_ca_change(
    raw_attributes: bytes, position_ratings: Sequence[int], attribute_id: int,
    amount: int, display_bias: int, model: str,
) -> dict[str, float | int | str]:
    attribute_id = int(attribute_id)
    amount = int(amount)
    if not 0x0F <= attribute_id <= 0x44 or amount < 1:
        raise ValueError("训练属性或提升点数无效")
    if len(raw_attributes) != 54:
        raise ValueError("球员属性块无法安全读取")
    index = attribute_id - 0x0F
    if raw_attributes[index] > 100:
        raise ValueError("训练属性无法安全读取")
    current_display = _display_attribute(raw_attributes[index], display_bias)
    target_display = current_display + amount
    try:
        target_raw = _raw_attribute_target(
            raw_attributes[index], target_display, display_bias,
        )
    except ValueError:
        raise ValueError("训练后所选属性将超过20")
    before = _recommended_ca(raw_attributes, position_ratings, display_bias, model)
    position_key = int(before["position_key"])
    ca_affected = attribute_id in FMRTE_RCA_WEIGHTS[position_key][0]
    updated = bytearray(raw_attributes)
    updated[index] = target_raw
    after = _recommended_ca(bytes(updated), position_ratings, display_bias, model)
    return {
        "model": model,
        "verified": True,
        "position_key": position_key,
        "ca_affected": ca_affected,
        "recommended_ca_before": int(before["recommended_ca"]),
        "recommended_ca_after": int(after["recommended_ca"]),
        "ca_cost": max(0, int(after["recommended_ca"]) - int(before["recommended_ca"])),
    }


def _attribute_ca_milestones(
    raw_attributes: bytes, position_ratings: Sequence[int], attribute_id: int,
    direction: int = 1, display_bias: int = 0, model: str = FM24_RCA_MODEL,
) -> list[dict[str, int]]:
    """Return every reachable display-point change and its product CA settlement."""
    attribute_id = int(attribute_id)
    direction = int(direction)
    if not 0x0F <= attribute_id <= 0x44 or direction not in {-1, 1}:
        raise ValueError("球员属性或变化方向无效")
    if len(raw_attributes) != 54:
        raise ValueError("球员属性块无法安全读取")
    index = attribute_id - 0x0F
    if raw_attributes[index] > 100:
        raise ValueError("球员目标属性无法安全读取")
    current_display = _display_attribute(raw_attributes[index], display_bias)
    available = 20 - current_display if direction > 0 else current_display - 1
    before = _recommended_ca(raw_attributes, position_ratings, display_bias, model)
    rows: list[dict[str, int]] = []
    for points in range(1, available + 1):
        updated = bytearray(raw_attributes)
        updated[index] = _raw_attribute_target(
            raw_attributes[index], current_display + direction * points,
            display_bias,
        )
        target = _display_attribute(updated[index], display_bias)
        if target != current_display + direction * points:
            break
        after = _recommended_ca(bytes(updated), position_ratings, display_bias, model)
        rows.append({
            "attribute_points": direction * points,
            "attribute_before": current_display,
            "attribute_after": target,
            "ca_change": int(after["recommended_ca"]) - int(before["recommended_ca"]),
            "recommended_ca_before": int(before["recommended_ca"]),
            "recommended_ca_after": int(after["recommended_ca"]),
        })
    if direction > 0 and rows and not any(int(row["ca_change"]) > 0 for row in rows):
        for row in rows:
            row["formula_ca_change"] = int(row["ca_change"])
            row["ca_change"] = int(row["attribute_points"])
            row["minimum_ca_fallback"] = 1
    return rows


def _ca_budget_attribute_change(
    raw_attributes: bytes, position_ratings: Sequence[int], attribute_id: int,
    ca_units: int, direction: int = 1, display_bias: int = 0,
    model: str = FM24_RCA_MODEL,
) -> dict[str, int | str]:
    """Find the first attribute target whose formula CA change equals the budget."""
    ca_units = int(ca_units)
    direction = int(direction)
    if not 1 <= ca_units <= 99:
        raise ValueError("培元丹数量无效")
    rows = _attribute_ca_milestones(
        raw_attributes, position_ratings, attribute_id, direction,
        display_bias, model,
    )
    for row in rows:
        magnitude = abs(int(row["ca_change"]))
        if int(row["ca_change"]) * direction < 0:
            continue
        if magnitude == ca_units:
            return {"model": model, "verified": True, **row}
        if magnitude > ca_units:
            raise ValueError(
                f"该属性下一次变化至少需要{magnitude}颗培元丹（CA{int(row['ca_change']):+d}）"
            )
    raise ValueError("该属性在有效范围内无法产生对应的 CA 变化")


def fm24_training_ca_change(
    raw_attributes: bytes, position_ratings: Sequence[int], attribute_id: int,
    amount: int = 1, display_bias: int = 0,
) -> dict[str, float | int | str]:
    return _training_ca_change(
        raw_attributes, position_ratings, attribute_id, amount, display_bias,
        FM24_RCA_MODEL,
    )


def fm26_training_ca_change(
    raw_attributes: bytes, position_ratings: Sequence[int], attribute_id: int,
    amount: int = 1, display_bias: int = 0,
) -> dict[str, float | int | str]:
    return _training_ca_change(
        raw_attributes, position_ratings, attribute_id, amount, display_bias,
        FM26_RCA_MODEL,
    )


def fm24_attribute_ca_milestones(
    raw_attributes: bytes, position_ratings: Sequence[int], attribute_id: int,
    direction: int = 1, display_bias: int = 0,
) -> list[dict[str, int]]:
    return _attribute_ca_milestones(
        raw_attributes, position_ratings, attribute_id, direction,
        display_bias, FM24_RCA_MODEL,
    )


def fm26_attribute_ca_milestones(
    raw_attributes: bytes, position_ratings: Sequence[int], attribute_id: int,
    direction: int = 1, display_bias: int = 0,
) -> list[dict[str, int]]:
    return _attribute_ca_milestones(
        raw_attributes, position_ratings, attribute_id, direction,
        display_bias, FM26_RCA_MODEL,
    )


def fm24_ca_budget_attribute_change(
    raw_attributes: bytes, position_ratings: Sequence[int], attribute_id: int,
    ca_units: int, direction: int = 1, display_bias: int = 0,
) -> dict[str, int | str]:
    return _ca_budget_attribute_change(
        raw_attributes, position_ratings, attribute_id, ca_units, direction,
        display_bias, FM24_RCA_MODEL,
    )


def fm26_ca_budget_attribute_change(
    raw_attributes: bytes, position_ratings: Sequence[int], attribute_id: int,
    ca_units: int, direction: int = 1, display_bias: int = 0,
) -> dict[str, int | str]:
    return _ca_budget_attribute_change(
        raw_attributes, position_ratings, attribute_id, ca_units, direction,
        display_bias, FM26_RCA_MODEL,
    )
