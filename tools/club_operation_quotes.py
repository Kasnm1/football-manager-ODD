"""ODD-owned club pricing rules; no account, HTTP or FM memory access."""
from __future__ import annotations

from typing import Any


OWNED_CLUB_FACILITY_BASE_PRICE = 2_500_000
OWNED_CLUB_FACILITY_LEVEL_PRICE = 500_000
OWNED_CLUB_FACILITY_UPGRADE_DAYS = 12
OWNED_CLUB_SALE_PERCENT = 98
OWNED_BRAND_CAMPAIGN_DELTAS = (50, 100, 300)
OWNED_BRAND_PRICE_BANDS = (
    (9_500, 240_000_000),
    (9_000, 160_000_000),
    (8_500, 100_000_000),
    (7_000, 60_000_000),
    (5_500, 30_000_000),
    (4_000, 16_000_000),
    (2_500, 8_000_000),
    (0, 4_000_000),
)


def owned_club_facility_upgrade_price(level: Any) -> int | None:
    try:
        current = int(level)
    except (TypeError, ValueError):
        return None
    if not 1 <= current < 20:
        return None
    return (
        OWNED_CLUB_FACILITY_BASE_PRICE
        + current * OWNED_CLUB_FACILITY_LEVEL_PRICE
    ) // 2


def owned_club_facility_upgrade_quote(level: Any, levels: Any) -> dict[str, int]:
    try:
        current = int(level)
        requested = int(levels)
    except (TypeError, ValueError) as error:
        raise ValueError("设施等级或升级级数无效") from error
    if not 1 <= current <= 20:
        raise ValueError("无法读取该项俱乐部设施等级")
    if not 1 <= requested <= 20 - current:
        raise ValueError(f"请选择 1 至 {max(0, 20 - current)} 级")
    prices = [
        int(owned_club_facility_upgrade_price(current + offset) or 0)
        for offset in range(requested)
    ]
    return {
        "before": current,
        "after": current + requested,
        "levels": requested,
        "price": sum(prices),
        "days": requested * OWNED_CLUB_FACILITY_UPGRADE_DAYS,
    }


def owned_club_sale_price(metrics: dict[str, Any]) -> int | None:
    if metrics.get("unavailable"):
        return None
    try:
        valuation = int(metrics.get("current_valuation") or 0)
        competition_premium = max(0, int(metrics.get("competition_premium") or 0))
    except (TypeError, ValueError):
        return None
    if valuation <= 0:
        return None
    competition_premium = min(valuation, competition_premium)
    base_valuation = valuation - competition_premium
    return (
        base_valuation * OWNED_CLUB_SALE_PERCENT // 100
        + competition_premium
    )


def owned_club_record_sale_metrics(ownership: dict[str, Any]) -> dict[str, int]:
    """Build an FMODD-owned sale quote when live club data is unavailable."""
    valuation = ownership.get("acquisition_valuation")
    valuation = valuation if isinstance(valuation, dict) else {}
    portfolio_metrics = ownership.get("portfolio_metrics")
    portfolio_metrics = portfolio_metrics if isinstance(portfolio_metrics, dict) else {}

    def positive_int(*values: Any) -> int:
        for value in values:
            try:
                parsed = int(value or 0)
            except (TypeError, ValueError):
                continue
            if parsed > 0:
                return parsed
        return 0

    current_valuation = positive_int(
        portfolio_metrics.get("current_valuation"),
        ownership.get("current_valuation"),
        valuation.get("price"),
        ownership.get("acquisition_price"),
    )
    competition_premium = positive_int(
        portfolio_metrics.get("competition_premium"),
        ownership.get("competition_premium"),
        valuation.get("competition_premium"),
    )
    return {
        "current_valuation": current_valuation,
        "competition_premium": min(current_valuation, competition_premium),
    }


def owned_club_brand_quote(reputation: Any, delta: Any) -> dict[str, int]:
    try:
        current = max(0, min(10_000, int(reputation)))
        requested = int(delta)
    except (TypeError, ValueError) as error:
        raise ValueError("俱乐部声望或推广规格无效") from error
    if requested not in OWNED_BRAND_CAMPAIGN_DELTAS:
        raise ValueError("请选择已有的品牌推广规格")
    applied = min(requested, 10_000 - current)
    if applied <= 0:
        raise ValueError("俱乐部声望已经达到 10000")
    unit_price = next(
        price for minimum, price in OWNED_BRAND_PRICE_BANDS
        if current >= minimum
    )
    return {
        "before": current,
        "requested": requested,
        "after": current + applied,
        "applied": applied,
        "price": unit_price * applied // 50,
    }


def owned_club_brand_quotes(reputation: Any) -> list[dict[str, int]]:
    if int(reputation or 0) >= 10_000:
        return []
    return [
        owned_club_brand_quote(reputation, delta)
        for delta in OWNED_BRAND_CAMPAIGN_DELTAS
    ]
