"""World-club directory and FMODD acquisition portfolio state."""

from __future__ import annotations

import json
import os
import re
import struct
import threading
import time
from bisect import bisect_right
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from tools.account_store import load_document, update_document
from tools.app_paths import cache_data_root, save_data_root
from fm_collector.win32 import (
    MEM_PRIVATE, iter_readable_regions, open_process, read_process_memory,
    write_process_memory,
)
from tools.initial_data_audit import (
    CLUB_TEAM_TYPE_NAMES, ENTITY_UID, MANAGEABLE_CLUB_TEAM_TYPES,
    NATION_CODES, NATION_NAMES, TEAM_CLUB, TEAM_COMP, TEAM_MANAGER,
    Reader, decode_date,
    parse_fixture, scan_fixture_addresses, select_process_layout,
)
from tools.game_session import borrow_game_reader
from tools.database_index import (
    database_index_for_reader, resolve_native_entity, resolve_team_club,
)


_COMPETITION_REGIONS = {
    "英超联赛": ("欧洲", "英格兰"), "英冠联赛": ("欧洲", "英格兰"),
    "西甲联赛": ("欧洲", "西班牙"), "西乙联赛": ("欧洲", "西班牙"),
    "德甲联赛": ("欧洲", "德国"), "意甲联赛": ("欧洲", "意大利"),
    "法甲联赛": ("欧洲", "法国"), "葡超联赛": ("欧洲", "葡萄牙"),
    "荷甲联赛": ("欧洲", "荷兰"), "比甲联赛": ("欧洲", "比利时"),
    "土超联赛": ("欧洲", "土耳其"), "苏超联赛": ("欧洲", "苏格兰"),
    "J1联赛": ("亚洲", "日本"), "J2联赛": ("亚洲", "日本"),
    "K联赛": ("亚洲", "韩国"), "中超联赛": ("亚洲", "中国"),
    "沙特职业联赛": ("亚洲", "沙特阿拉伯"), "澳超联赛": ("大洋洲", "澳大利亚"),
    "美职联": ("北美洲", "美国"), "巴甲联赛": ("南美洲", "巴西"),
    "阿甲联赛": ("南美洲", "阿根廷"), "墨西哥超级联赛": ("北美洲", "墨西哥"),
}

_NON_MENS_MARKERS = (
    "women", "woman", "womens", "ladies", "femin", "female",
    "女子", "女足", "女队", "女隊", "预备", "預備", "二队", "二隊",
    "u18", "u19", "u20", "u21", "u23", "under-18", "under-19",
    "under-20", "under-21", "under-23",
    "all-star", "all star", "allstar", "全明星", "全明星队",
)
_NATION_CONTINENTS = {
    "亚洲": {"CHN", "JPN", "KOR", "PRK", "VNM", "THA", "IDN", "MYS", "SGP", "PHL", "KHM", "LAO", "MMR", "IND", "PAK", "BGD", "IRN", "IRQ", "KSA", "QAT", "UAE", "ISR", "JOR", "SYR", "TUR", "KAZ", "UZB", "AUS"},
    "欧洲": {"ENG", "SCO", "WAL", "NIR", "IRL", "FRA", "ESP", "DEU", "ITA", "PRT", "NLD", "BEL", "CHE", "AUT", "DNK", "SWE", "NOR", "FIN", "ISL", "POL", "CZE", "SVK", "HUN", "ROU", "BGR", "GRC", "SRB", "HRV", "SVN", "UKR", "RUS", "ALB", "ARM", "GEO", "AZE", "BIH", "MNE", "MKD", "MDA", "LTU", "LVA", "EST", "BLR", "LUX", "MLT", "CYP", "AND", "SMR", "VAT", "FRO", "GIB", "KOS"},
    "非洲": {"EGY", "MAR", "DZA", "TUN", "LBY", "NGA", "GHA", "CIV", "SEN", "CMR", "COD", "ANG", "ZAF", "ZMB", "ZWE", "MOZ", "UGA", "KEN", "TZA", "ETH", "SDN", "MLI", "BFA", "BEN", "TOG", "GAB", "GUI", "ALG"},
    "北美洲": {"USA", "CAN", "MEX", "CRC", "HON", "PAN", "JAM", "HAI", "CUB", "GUA", "SLV", "NIC", "TRI", "BER", "BVI", "CAY"},
    "南美洲": {"ARG", "BRA", "URY", "PAR", "CHL", "COL", "PER", "ECU", "BOL", "VEN", "GUA", "SUR", "GUY"},
    "大洋洲": {"AUS", "NZL", "FIJ", "PNG", "SOL", "TAH", "NCL", "VAN", "SAM", "ASA"},
}
_EXTRA_NATION_CONTINENTS = {
    "非洲": {"ZAI", "ERI", "REU", "MAY", "COM", "ZAN", "SSD", "UPV", "UAR"},
    "亚洲": {"TI", "MNG", "BHU", "PLE", "VSO", "TLS", "BUR", "SYE", "SI", "BM", "WM"},
    "欧洲": {"CIS", "CSV", "GDR", "URS", "FRG", "GBR", "GIB", "CRM", "KOS", "UK", "MON", "MNE", "YUG"},
    "北美洲": {"AIA", "VGB", "MSR", "VIR", "TCA", "DOM", "BOE", "GLP", "MTQ", "SMA", "SMN", "SPM", "BLM", "ANT"},
    "南美洲": {"GUY", "SUR", "GUF"},
    "大洋洲": {"ASA", "GUM", "NCL", "KIR", "FSM", "NMI", "TUV", "WFI"},
}
_WORLD_CLUB_NATION_ALIASES = {
    "中华人民共和国": "中国",
}
_WORLD_CLUB_SEARCH_ALIASES = {
    "曼城": ("曼彻斯特城",),
    "曼联": ("曼彻斯特联",),
    "皇马": ("皇家马德里",),
    "巴萨": ("巴塞罗那",),
    "马竞": ("马德里竞技",),
    "国米": ("国际米兰",),
    "大巴黎": ("巴黎圣日耳曼",),
}
NATION_YOUTH_INVESTMENT_ANCHOR_RATING = 99
NATION_YOUTH_INVESTMENT_ANCHOR_PRICE = 10_000_000
NATION_YOUTH_INVESTMENT_HUNDRED_LEVEL_MULTIPLIER = 1_000
SAUDI_PRO_LEAGUE_PREMIUM = 15_000_000_000
SEMI_PROFESSIONAL_PRICE_DIVISOR = 16
AMATEUR_PRICE_DIVISOR = 48
PROFESSIONAL_REPUTATION_PRICE_DIVISORS = (
    (7_000, 1),
    (5_500, 2),
    (4_000, 3),
    (2_500, 4),
    (1_000, 5),
    (0, 6),
)
ACQUISITION_PRICING_MODEL_ID = "club-acquisition-pricing-2026-08-v2"
ACQUISITION_REBALANCE_ID = "acquisition-price-bands-2026-08-v2"
_LEGACY_ACQUISITION_REBALANCE_IDS = frozenset({
    "acquisition-price-bands-2026-07-31-v1",
})
_SAUDI_PRO_LEAGUE_NAMES = frozenset({
    "沙特超级联赛", "沙特职业联赛", "沙特阿拉伯职业足球联赛",
    "saudiproleague", "roshnsaudileague",
})
_CACHE_FILE_LOCK = threading.RLock()
_CLUB_FIXTURE_CACHE_LOCK = threading.RLock()
_CLUB_FIXTURE_CACHE: dict[tuple[Any, ...], dict[str, Any]] = {}
ACQUISITION_VALUE_TIERS = (
    # Reputation range, total-price range, inferred finance floor and scale cap.
    (9_000, 10_000, 30_000_000_000, 60_000_000_000, 10_000_000_000, 50_000_000_000),
    (8_500, 9_000, 10_000_000_000, 30_000_000_000, 6_000_000_000, 20_000_000_000),
    (7_000, 8_500, 1_000_000_000, 10_000_000_000, 2_000_000_000, 10_000_000_000),
    (5_500, 7_000, 500_000_000, 1_000_000_000, 250_000_000, 1_500_000_000),
    (4_000, 5_500, 250_000_000, 500_000_000, 150_000_000, 600_000_000),
    (2_500, 4_000, 100_000_000, 250_000_000, 50_000_000, 300_000_000),
    (1_000, 2_500, 25_000_000, 100_000_000, 15_000_000, 100_000_000),
    (0, 1_000, 5_000_000, 25_000_000, 5_000_000, 30_000_000),
)


def _unit(value: Any, maximum: float) -> float:
    try:
        return max(0.0, min(1.0, float(value) / maximum))
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def _acquisition_value_tier(reputation: Any) -> tuple[int, int, int, int, int, int]:
    try:
        value = max(0, min(10_000, int(reputation)))
    except (TypeError, ValueError):
        value = 0
    return next(tier for tier in ACQUISITION_VALUE_TIERS if value >= tier[0])


def _competition_acquisition_premium(competition_name: Any) -> int:
    normalized = re.sub(r"[\W_]+", "", str(competition_name or "").casefold())
    return SAUDI_PRO_LEAGUE_PREMIUM if normalized in _SAUDI_PRO_LEAGUE_NAMES else 0


def _professional_reputation_price_divisor(reputation: Any) -> int:
    try:
        value = max(0, min(10_000, int(reputation)))
    except (TypeError, ValueError):
        # Missing reputation is not evidence that a club belongs in the
        # deepest discount tier. Keep the legacy factor until it is readable.
        return 1
    return next(
        divisor for minimum, divisor in PROFESSIONAL_REPUTATION_PRICE_DIVISORS
        if value >= minimum
    )


def _club_pricing_divisors(
    information: dict[str, Any], reputation: Any,
) -> tuple[str, int, int, int]:
    """Return pricing basis plus mutually exclusive status/reputation divisors."""
    status = information.get("status") or {}
    try:
        status_code = int(status.get("code"))
    except (TypeError, ValueError):
        status_code = None
    status_name = re.sub(
        r"[\W_]+", "", str(status.get("name") or "").casefold(),
    )
    if status_code == 2 or status_name in {
        "半职业", "semipro", "semiprofessional",
    }:
        return "semi_professional", SEMI_PROFESSIONAL_PRICE_DIVISOR, 1, SEMI_PROFESSIONAL_PRICE_DIVISOR
    if status_code == 3 or status_name == "业余" or status_name == "amateur":
        return "amateur", AMATEUR_PRICE_DIVISOR, 1, AMATEUR_PRICE_DIVISOR
    reputation_divisor = _professional_reputation_price_divisor(reputation)
    return "professional", 1, reputation_divisor, reputation_divisor


def _club_price_divisor(information: dict[str, Any], reputation: Any = None) -> int:
    return _club_pricing_divisors(information, reputation)[3]


def _current_snapshot_price_divisor(snapshot: dict[str, Any], reputation: Any) -> int:
    """Resolve the current divisor while retaining status from older snapshots."""
    old_divisor = max(1, int(snapshot.get("pricing_divisor") or 1))
    basis = str(snapshot.get("pricing_basis") or "")
    if basis == "semi_professional" or old_divisor in {8, SEMI_PROFESSIONAL_PRICE_DIVISOR}:
        return SEMI_PROFESSIONAL_PRICE_DIVISOR
    if basis == "amateur" or old_divisor in {12, AMATEUR_PRICE_DIVISOR}:
        return AMATEUR_PRICE_DIVISOR
    return _professional_reputation_price_divisor(reputation)


def _scaled_price_components(
    components: dict[str, int], divisor: int,
) -> dict[str, int]:
    """Scale components while keeping their sum equal to floor(total/divisor)."""
    if divisor <= 1:
        return components
    scaled = {key: value // divisor for key, value in components.items()}
    remainder = sum(components.values()) // divisor - sum(scaled.values())
    if remainder:
        scaled["brand"] += remainder
    return scaled


def acquisition_rebalance_quote(acquisition: dict[str, Any] | None) -> dict[str, Any]:
    """Reprice an acquisition snapshot against the current reputation bands."""
    row = acquisition or {}
    def integer(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    original_price = max(0, integer(row.get("acquisition_price")))
    recorded = row.get("acquisition_rebalance") or {}
    recorded_id = str(recorded.get("id") or "") if isinstance(recorded, dict) else ""
    if (
        isinstance(recorded, dict)
        and recorded_id == ACQUISITION_REBALANCE_ID
        and str(recorded.get("status") or "") == "claimed"
    ):
        total_refund = max(0, min(original_price, integer(recorded.get("refund"))))
        return {
            "id": ACQUISITION_REBALANCE_ID,
            "status": "claimed",
            "eligible": False,
            "claimed": True,
            "original_price": original_price,
            "fair_price": max(0, integer(recorded.get("fair_price") or original_price - total_refund)),
            "refund": total_refund,
            "incremental_refund": max(0, integer(recorded.get("incremental_refund") or total_refund)),
            "claimed_refund": total_refund,
            "total_refund": total_refund,
            "net_acquisition_cost": max(0, original_price - total_refund),
            "claimed_at": recorded.get("claimed_at"),
            "claimed_game_date": recorded.get("claimed_game_date"),
        }

    snapshot = row.get("acquisition_valuation") or {}
    old_band = snapshot.get("price_band") or {}
    old_premium = max(0, integer(snapshot.get("competition_premium")))
    old_price_divisor = max(1, integer(snapshot.get("pricing_divisor")) or 1)
    try:
        price_divisor = _current_snapshot_price_divisor(snapshot, row.get("reputation"))
    except (TypeError, ValueError):
        price_divisor = 1
    prior_refund = (
        max(0, min(original_price, integer(recorded.get("refund"))))
        if recorded_id in _LEGACY_ACQUISITION_REBALANCE_IDS
        and str(recorded.get("status") or "") == "claimed"
        else 0
    )
    try:
        old_minimum = int(old_band.get("minimum")) - old_premium
        old_maximum = int(old_band.get("maximum")) - old_premium
        old_base_price = int(snapshot.get("base_price"))
        finance_snapshot = snapshot.get("finance") or {}
        actual_finance = (
            max(0, int(finance_snapshot.get("actual") or 0)) // price_divisor
        )
        # Older snapshots counted abs(negative balance) as an acquired asset.
        # Reconstruct the realizable amount so the existing compensation flow
        # can refund that overcharge without rewriting account history.
        if snapshot.get("negative_balance_reset"):
            actual_finance = (
                max(0, int(finance_snapshot.get("balance_asset") or 0))
                + max(0, int(finance_snapshot.get("transfer_budget") or 0))
            ) // price_divisor
        tier = _acquisition_value_tier(row.get("reputation"))
        new_minimum = int(tier[2]) // price_divisor
        new_maximum = int(tier[3]) // price_divisor
    except (TypeError, ValueError):
        old_minimum = old_maximum = old_base_price = 0
        actual_finance = new_minimum = new_maximum = 0
    if (
        original_price <= 0 or old_minimum < 0 or old_maximum <= old_minimum
        or old_base_price <= 0 or new_maximum <= new_minimum
    ):
        return {
            "id": ACQUISITION_REBALANCE_ID,
            "status": "unavailable",
            "eligible": False,
            "claimed": False,
            "original_price": original_price,
            "refund": 0,
            "incremental_refund": 0,
            "claimed_refund": prior_refund,
            "total_refund": prior_refund,
            "net_acquisition_cost": max(0, original_price - prior_refund) or None,
        }

    position = max(0.0, min(
        1.0, (old_base_price - old_minimum) / (old_maximum - old_minimum),
    ))
    repriced_model = round(new_minimum + (new_maximum - new_minimum) * position)
    new_premium = (
        _competition_acquisition_premium(row.get("competition")) // price_divisor
    )
    fair_price = max(repriced_model, actual_finance) + new_premium
    current_net_cost = max(0, original_price - prior_refund)
    refund = max(0, current_net_cost - fair_price)
    total_refund = min(original_price, prior_refund + refund)
    return {
        "id": ACQUISITION_REBALANCE_ID,
        "status": "available" if refund > 0 else "current",
        "eligible": refund > 0,
        "claimed": False,
        "original_price": original_price,
        "fair_price": fair_price,
        "refund": refund,
        "incremental_refund": refund,
        "claimed_refund": prior_refund,
        "total_refund": total_refund,
        "net_acquisition_cost": current_net_cost,
        "old_price_band": {"minimum": old_minimum, "maximum": old_maximum},
        "new_price_band": {"minimum": new_minimum, "maximum": new_maximum},
        "old_competition_premium": old_premium,
        "new_competition_premium": new_premium,
        "old_pricing_divisor": old_price_divisor,
        "new_pricing_divisor": price_divisor,
    }


def _display_squad_quality(raw_score: float) -> float:
    """Map the theoretical all-200 CA ratio onto a club-quality scale."""
    score = max(0.0, min(1.0, float(raw_score)))
    if score <= 0.50:
        return score
    # An 80% share of the impossible all-200 squad is already elite in FM.
    # Keep the lower half unchanged, then map 0.50 -> 50 and 0.80 -> 90.
    return min(1.0, 0.50 + (score - 0.50) * (4.0 / 3.0))


def club_acquisition_valuation(
    information: dict[str, Any] | None, roster: list[dict[str, Any]], reputation: Any,
    competition_name: Any = None,
) -> dict[str, Any]:
    """Price a club from native detail data without applying loan adjustments."""
    info = information or {}
    facilities = info.get("facilities") or {}
    facilities_observed = any(
        facilities.get(key) is not None
        for key in ("training", "youth", "junior_coaching", "youth_recruitment")
    )
    facility_score = (
        _unit(facilities.get("training"), 20) * 0.35
        + _unit(facilities.get("youth"), 20) * 0.25
        + _unit(facilities.get("junior_coaching"), 20) * 0.20
        + _unit(facilities.get("youth_recruitment"), 20) * 0.20
    )
    finances = info.get("finances") or {}
    balance = int(finances.get("balance") or 0)
    transfer_budget = max(0, int(finances.get("remaining_transfer_budget") or 0))
    stadium = info.get("stadium") or {}
    stadium_observed = any(stadium.get(key) is not None for key in (
        "capacity", "expansion_capacity", "state_raw", "pitch_condition",
    ))
    state_raw = stadium.get("state_raw")
    state_score = 1.0 - _unit(state_raw, 24) if state_raw is not None else 0.0
    stadium_score = (
        _unit(stadium.get("capacity"), 100_000) * 0.55
        + _unit(stadium.get("expansion_capacity"), 100_000) * 0.15
        + state_score * 0.20
        + _unit(stadium.get("pitch_condition"), 200) * 0.10
    )

    players = []
    for row in roster:
        try:
            ca = int(row.get("ca"))
            pa = int(row.get("pa")) if row.get("pa") is not None else ca
            player_id = int(row.get("id") or 0)
        except (TypeError, ValueError):
            continue
        if player_id > 0 and 0 <= ca <= 200 and 0 <= pa <= 200:
            players.append((ca, pa))
    players.sort(key=lambda item: item[0], reverse=True)

    def average_ca(start: int, end: int | None) -> float:
        values = [ca for ca, _pa in players[start:end]]
        return (sum(values) / len(values) / 200) if values else 0.0

    top_23 = players[:23]
    pa_score = (sum(pa for _ca, pa in top_23) / len(top_23) / 200) if top_23 else 0.0
    squad_value_score = min(1.0, max(0.0,
        average_ca(0, 11) * 0.45
        + average_ca(11, 23) * 0.25
        + average_ca(23, None) * 0.10
        + pa_score * 0.15
        + min(len(players) / 25, 1.0) * 0.05
    ))
    reputation_score = _unit(reputation, 10_000)
    tier_floor, tier_ceiling, price_floor, price_ceiling, finance_floor, finance_ceiling = (
        _acquisition_value_tier(reputation)
    )
    reputation_value = round(reputation_score * 10_000)
    reputation_progress = _unit(
        max(0, min(tier_ceiling, reputation_value)) - tier_floor,
        tier_ceiling - tier_floor,
    )

    # Detail reads can be incomplete. Conservative reputation proxies prevent
    # missing squad, facility or stadium data from collapsing a famous club's price.
    inferred_fields: list[str] = []
    if not players:
        squad_value_score = min(0.80, 0.25 + reputation_score * 0.55)
        squad_score = max(0.25, min(0.99, reputation_score))
        inferred_fields.append("squad")
    else:
        squad_score = _display_squad_quality(squad_value_score)
    if not facilities_observed:
        facility_score = min(0.80, 0.20 + reputation_score * 0.60)
        inferred_fields.append("facilities")
    if not stadium_observed:
        stadium_score = min(0.70, 0.15 + reputation_score * 0.55)
        inferred_fields.append("stadium")

    # A negative club balance is cleared by the native acquisition transaction;
    # it is a liability, not an asset received by the purchaser.  Counting its
    # absolute value made debt-heavy FM24 clubs cost billions more immediately
    # before that same balance was reset to zero, producing an artificial loss.
    balance_asset = max(0, balance)
    actual_finance = balance_asset + transfer_budget
    protected_finance = max(actual_finance, finance_floor)
    finance_score = _unit(
        protected_finance - finance_floor,
        max(1, finance_ceiling - finance_floor),
    )
    price_span = price_ceiling - price_floor
    raw_components = {
        "brand": round(price_floor + price_span * reputation_progress * 0.65),
        "squad": round(price_span * squad_value_score * 0.15),
        "facilities": round(price_span * facility_score * 0.10),
        "finances": round(price_span * finance_score * 0.07),
        "stadium": round(price_span * stadium_score * 0.03),
    }
    pricing_basis, status_divisor, reputation_divisor, price_divisor = (
        _club_pricing_divisors(info, reputation)
    )
    components = _scaled_price_components(raw_components, price_divisor)
    calculated_price = sum(components.values())
    # The asset floor may exceed the reputation band's normal maximum. Apply
    # the effective status-or-reputation discount after that protection so all
    # components and competition premiums use one mutually exclusive divisor.
    competition_premium = (
        _competition_acquisition_premium(competition_name) // price_divisor
    )
    protected_base_price = max(sum(raw_components.values()), actual_finance) // price_divisor
    price = protected_base_price + competition_premium
    return {
        "price": int(price),
        "base_price": int(calculated_price),
        "competition_premium": competition_premium,
        "multiplier": 1.0 / price_divisor,
        "pricing_model_id": ACQUISITION_PRICING_MODEL_ID,
        "pricing_basis": pricing_basis,
        "status_pricing_divisor": status_divisor,
        "reputation_pricing_divisor": reputation_divisor,
        "pricing_divisor": price_divisor,
        "components": components,
        "finance": {
            "actual": int(actual_finance),
            "floor": int(finance_floor),
            "protected": int(protected_finance),
            "balance_absolute": abs(balance),
            "balance_asset": int(balance_asset),
            "transfer_budget": transfer_budget,
        },
        "price_band": {
            "minimum": price_floor // price_divisor + competition_premium,
            "maximum": price_ceiling // price_divisor + competition_premium,
        },
        "scores": {
            "facilities": round(facility_score, 4),
            "stadium": round(stadium_score, 4),
            "reputation": round(reputation_score, 4),
            "squad": round(squad_score, 4),
        },
        "player_count": len(players),
        "inferred_fields": inferred_fields,
        "negative_balance_reset": balance < 0,
    }


def club_portfolio_metrics(
    players: list[dict[str, Any]], information: dict[str, Any] | None,
    valuation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from tools.club_reader import validated_finance_income_statement

    squad_component = ((valuation or {}).get("components") or {}).get("squad")
    squad_value = (
        int(squad_component)
        if squad_component is not None and int(squad_component) >= 0 else None
    )
    finances = (information or {}).get("finances") or {}
    supporters = (information or {}).get("supporters") or {}
    nation = (information or {}).get("nation") or {}
    nation_id = int(nation.get("id") or 0) if isinstance(nation, dict) else 0
    income_statement = validated_finance_income_statement(
        finances.get("income_statement") or {},
    )
    income_rows = income_statement.get("income") or []
    expenditure_rows = income_statement.get("expenditure") or []
    last_month_profit = None
    if income_rows and expenditure_rows:
        last_month_profit = (
            sum(int(row.get("last_month") or 0) for row in income_rows)
            - sum(int(row.get("last_month") or 0) for row in expenditure_rows)
        )
    monthly_profits = [
        {
            "month": str(row.get("month") or ""),
            "profit": int(row.get("profit") or 0),
        }
        for row in (finances.get("monthly_summary") or [])
        if row.get("month")
    ]
    debts = finances.get("debts")
    current_valuation = (valuation or {}).get("price")
    season_ticket_holders = supporters.get("season_ticket_holders")
    social_media_followers = supporters.get("social_media_followers")
    if int(social_media_followers or 0) > 0:
        fan_count = social_media_followers
        fan_count_source = "social_media_followers"
    else:
        fan_count = season_ticket_holders if season_ticket_holders is not None else 0
        fan_count_source = "season_ticket_holders"
    return {
        "current_valuation": (
            int(current_valuation) if current_valuation is not None else None
        ),
        "competition_premium": int((valuation or {}).get("competition_premium") or 0),
        "valuation_scores": dict((valuation or {}).get("scores") or {}),
        "valuation_inferred_fields": list(
            (valuation or {}).get("inferred_fields") or []
        ),
        "squad_value": squad_value,
        "squad_value_source": "ca_pa_reputation_model" if squad_value is not None else None,
        "valued_players": int((valuation or {}).get("player_count") or 0),
        "player_count": len(players),
        "nation": {
            "id": nation_id,
            "name": str(nation.get("name") or ""),
        } if nation_id > 0 else None,
        "balance": finances.get("balance"),
        "transfer_budget": finances.get("remaining_transfer_budget"),
        "fan_count": fan_count,
        "fan_count_source": fan_count_source,
        "season_ticket_holders": season_ticket_holders,
        "social_media_followers": social_media_followers,
        "season_revenue": (
            sum(int(row.get("this_season") or 0) for row in income_rows)
            if income_rows else None
        ),
        "last_month_profit": last_month_profit,
        "monthly_profits": monthly_profits,
        "debt": (
            sum(max(0, int(row.get("original_debt") or 0)) for row in debts)
            if debts is not None else None
        ),
    }


def club_investment_performance(
    metrics: dict[str, Any], acquisition: dict[str, Any],
    current_game_date: str | None = None,
) -> dict[str, Any]:
    """Compare a live valuation with its account-local acquisition cost."""
    result = dict(metrics)
    try:
        acquisition_price = int(acquisition.get("acquisition_price") or 0)
    except (TypeError, ValueError):
        acquisition_price = 0
    try:
        current_valuation = int(result.get("current_valuation"))
    except (TypeError, ValueError):
        current_valuation = 0
    rebalance = acquisition_rebalance_quote(acquisition)
    claimed_refund = int(rebalance.get("claimed_refund") or 0)
    net_acquisition_cost = max(0, acquisition_price - claimed_refund)
    result["acquisition_price"] = acquisition_price or None
    result["gross_acquisition_price"] = acquisition_price or None
    result["acquisition_refund"] = claimed_refund
    result["net_acquisition_cost"] = net_acquisition_cost or None
    result["acquisition_rebalance"] = rebalance
    result["acquired_at"] = acquisition.get("acquired_at")
    result["holding_days"] = None
    try:
        acquired_date = datetime.fromisoformat(
            str(acquisition.get("acquired_game_date") or "")
        ).date()
        current_date = datetime.fromisoformat(str(current_game_date or "")).date()
        result["holding_days"] = max(0, (current_date - acquired_date).days)
    except (TypeError, ValueError):
        pass
    if net_acquisition_cost > 0 and current_valuation > 0 and not result.get("unavailable"):
        change = current_valuation - net_acquisition_cost
        result["valuation_change"] = change
        result["valuation_change_percent"] = round(change * 100 / net_acquisition_cost, 1)
    else:
        result["valuation_change"] = None
        result["valuation_change_percent"] = None
    return result


def _portfolio_history_date(raw: Any) -> date | None:
    try:
        return date.fromisoformat(str(raw or "")[:10])
    except (TypeError, ValueError):
        return None


def _portfolio_season_window(
    game_date: str, season_start: str = "", season_end: str = "",
) -> dict[str, str]:
    observed = _portfolio_history_date(game_date)
    start = _portfolio_history_date(season_start)
    end = _portfolio_history_date(season_end)
    if not start or not end or (observed and not start <= observed <= end):
        if observed is None:
            return {}
        start_year = observed.year if observed.month >= 7 else observed.year - 1
        start = date(start_year, 7, 1)
        end = date(start_year + 1, 6, 30)
    return {
        "key": f"{start.year}-{end.year}",
        "start": start.isoformat(),
        "end": end.isoformat(),
    }


def _portfolio_metric_value(raw: Any) -> int | None:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _portfolio_metric_comparison(
    current: int | None, previous: int | None,
) -> dict[str, Any] | None:
    if current is None or previous is None:
        return None
    change = current - previous
    return {
        "previous": previous,
        "change": change,
        "change_percent": round(change * 100 / previous, 1) if previous else None,
    }


def _update_club_metric_history(
    raw_history: Any, metrics: dict[str, Any], *, game_date: str,
    season_start: str = "", season_end: str = "",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Update one holding's bounded snapshots and return adjacent-period deltas."""
    observed = _portfolio_history_date(game_date)
    if observed is None:
        return (
            dict(raw_history) if isinstance(raw_history, dict) else {},
            {},
        )
    observed_text = observed.isoformat()
    month_key = observed_text[:7]
    previous_month_key = (observed.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    season = _portfolio_season_window(
        observed_text, season_start, season_end,
    )
    history = dict(raw_history) if isinstance(raw_history, dict) else {}
    history["schema_version"] = 1

    month_rows = [
        dict(row) for row in history.get("months") or []
        if isinstance(row, dict)
        and str(row.get("month") or "") <= month_key
        and str(row.get("observed_game_date") or "") <= observed_text
    ]
    previous_month = next((
        row for row in reversed(sorted(
            month_rows,
            key=lambda item: (
                str(item.get("month") or ""),
                str(item.get("observed_game_date") or ""),
            ),
        ))
        if str(row.get("month") or "") == previous_month_key
    ), None)
    reputation = _portfolio_metric_value(metrics.get("reputation"))
    reputation_comparison = _portfolio_metric_comparison(
        reputation,
        _portfolio_metric_value((previous_month or {}).get("reputation")),
    )
    if reputation is not None:
        month_rows = [
            row for row in month_rows
            if str(row.get("month") or "") != month_key
        ]
        month_rows.append({
            "month": month_key,
            "observed_game_date": observed_text,
            "reputation": reputation,
        })
    month_rows.sort(key=lambda row: (
        str(row.get("month") or ""),
        str(row.get("observed_game_date") or ""),
    ))
    history["months"] = month_rows[-24:]

    season_rows = [
        dict(row) for row in history.get("seasons") or []
        if isinstance(row, dict)
        and str(row.get("season_start") or "") <= observed_text
        and str(row.get("observed_game_date") or "") <= observed_text
    ]
    current_season_key = str(season.get("key") or "")
    previous_season = None
    if current_season_key:
        current_start = _portfolio_history_date(season.get("start"))
        candidates = [
            row for row in season_rows
            if str(row.get("season_key") or "") != current_season_key
        ]
        candidates.sort(key=lambda row: str(row.get("season_start") or ""))
        if candidates and current_start:
            candidate_start = _portfolio_history_date(candidates[-1].get("season_start"))
            if candidate_start and candidate_start.year == current_start.year - 1:
                previous_season = candidates[-1]
    followers = _portfolio_metric_value(metrics.get("social_media_followers"))
    followers_comparison = _portfolio_metric_comparison(
        followers,
        _portfolio_metric_value(
            (previous_season or {}).get("social_media_followers"),
        ),
    )
    if current_season_key and followers is not None:
        season_rows = [
            row for row in season_rows
            if str(row.get("season_key") or "") != current_season_key
        ]
        season_rows.append({
            "season_key": current_season_key,
            "season_start": season.get("start"),
            "season_end": season.get("end"),
            "observed_game_date": observed_text,
            "social_media_followers": followers,
        })
    season_rows.sort(key=lambda row: (
        str(row.get("season_start") or ""),
        str(row.get("observed_game_date") or ""),
    ))
    history["seasons"] = season_rows[-4:]
    return history, {
        "reputation_comparison": reputation_comparison,
        "social_media_followers_comparison": followers_comparison,
    }


def sync_acquired_club_metric_history(
    metrics: dict[int, dict[str, Any]], *, game_date: str,
    season_start: str = "", season_end: str = "", scope_id: str = "",
) -> dict[int, dict[str, Any]]:
    """Persist account-local portfolio baselines and return display deltas."""
    if not _portfolio_history_date(game_date):
        return {}

    def mutate(payload: dict[str, Any]) -> dict[int, dict[str, Any]]:
        comparisons: dict[int, dict[str, Any]] = {}
        for club in payload.get("clubs") or []:
            if not isinstance(club, dict):
                continue
            team_id = int(club.get("id") or 0)
            current = metrics.get(team_id)
            if team_id <= 0 or not isinstance(current, dict) or current.get("unavailable"):
                continue
            history, comparison = _update_club_metric_history(
                club.get("metric_history"), current,
                game_date=game_date,
                season_start=season_start,
                season_end=season_end,
            )
            if history != club.get("metric_history"):
                club["metric_history"] = history
            comparisons[team_id] = comparison
        return comparisons

    return _update_portfolio(mutate, scope_id)


def club_portfolio_summary(
    clubs: list[dict[str, Any]], metrics: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    """Aggregate readable values while isolating acquisition-price comparisons."""
    rows = [
        club_investment_performance(
            metrics.get(int(club.get("id") or 0), {"unavailable": True}), club,
        )
        for club in clubs
    ]
    valued = [
        row for row in rows
        if row.get("current_valuation") is not None
        and not row.get("unavailable")
    ]
    comparable = [
        row for row in rows
        if row.get("current_valuation") is not None
        and row.get("net_acquisition_cost") is not None
        and not row.get("unavailable")
    ]

    def total(field: str, source: list[dict[str, Any]] = rows) -> int | None:
        values = [int(row[field]) for row in source if row.get(field) is not None]
        return sum(values) if values else None

    return {
        "club_count": len(clubs),
        "valued_club_count": len(valued),
        "comparable_club_count": len(comparable),
        "partial": len(valued) != len(clubs),
        "acquisition_cost": total("net_acquisition_cost"),
        "gross_acquisition_cost": total("gross_acquisition_price"),
        "acquisition_refunds": total("acquisition_refund"),
        "current_valuation": total("current_valuation", valued),
        "valuation_change": total("valuation_change", comparable),
        "balance": total("balance"),
        "season_revenue": total("season_revenue"),
        "estimated_dividend": total("estimated_dividend"),
        "debt": total("debt"),
    }


def _portfolio_path(scope_id: str = "") -> Path:
    return save_data_root(scope_id or None) / "world" / "acquired_clubs.json"


def _native_cache_path(game_key: str, save_id: str) -> Path:
    safe_save = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(save_id or "default"))[:80]
    return cache_data_root(save_id) / "world" / f"native_clubs_{game_key}_{safe_save}.json"


def invalidate_world_club_runtime_cache() -> None:
    """Drop process-local fixture/result hints without touching durable holdings."""
    with _CLUB_FIXTURE_CACHE_LOCK:
        _CLUB_FIXTURE_CACHE.clear()


def _looks_non_mens(name: str, competition: str) -> bool:
    text = f"{name} {competition}".casefold()
    if any(marker.casefold() in text for marker in _NON_MENS_MARKERS):
        return True
    if re.search(r"(?:u|under)[ ._-]*(?:18|19|20|21|23)\b", text):
        return True
    if re.search(r"(?:18|19|20|21|23)\s*[岁歲]以下", text):
        return True
    # Common reserve suffixes which are not reliably separated by a space.
    return bool(re.search(r"(?:^|[ ._-])(ii|iii|iv|b)(?:$|[ ._-])", text))


def _world_club_nation_name(value: Any) -> str:
    name = str(value or "").strip()
    return _WORLD_CLUB_NATION_ALIASES.get(name, name)


def _world_club_search_terms(search: str) -> tuple[str, ...]:
    query = str(search or "").strip().casefold()
    if not query:
        return ()
    aliases = tuple(
        str(value).strip().casefold()
        for value in _WORLD_CLUB_SEARCH_ALIASES.get(query, ())
        if str(value).strip()
    )
    return tuple(dict.fromkeys((*aliases, query)))


def _world_club_search_rank(club: dict[str, Any], terms: tuple[str, ...]) -> int | None:
    names = tuple(
        str(value or "").strip().casefold()
        for value in (club.get("name"), club.get("short_name"))
        if str(value or "").strip()
    )
    context = tuple(
        str(value or "").strip().casefold()
        for value in (
            club.get("continent"), club.get("nation"), club.get("competition"),
            *(club.get("competitions") or []),
        )
        if str(value or "").strip()
    )
    best: int | None = None
    for index, term in enumerate(terms):
        if not term:
            continue
        scores = []
        for value in names:
            if value == term:
                scores.append(index)
            elif value.startswith(term):
                scores.append(20 + index)
            elif term in value:
                scores.append(40 + index)
        if any(term in value for value in context):
            scores.append(80 + index)
        if scores:
            score = min(scores)
            best = score if best is None else min(best, score)
    return best


def _world_club_display_name(
    club: dict[str, Any], collisions: dict[str, list[dict[str, Any]]],
) -> str:
    name = str(club.get("name") or "").strip()
    group = collisions.get(name.casefold(), [])
    if not name or len(group) < 2:
        return name
    competition = str(club.get("competition") or "").strip()
    if competition and competition not in {"未分类", "未知"}:
        same_competition = sum(
            str(row.get("competition") or "").strip().casefold() == competition.casefold()
            for row in group
        )
        if same_competition == 1:
            return f"{name} · {competition}"
    nation = str(club.get("nation") or "").strip()
    qualifier = " · ".join(
        value for value in (competition, nation)
        if value and value not in {"未分类", "未知"}
    )
    if qualifier:
        return f"{name} · {qualifier} · ID {int(club.get('id') or 0)}"
    return f"{name} · ID {int(club.get('id') or 0)}"


def _native_region(reader: Reader, club_address: str, competition_name: str) -> tuple[str, str]:
    nation_id = None
    try:
        club = int(str(club_address), 16)
        nation_address = reader.ptr(club + int(reader.layout.club_nation_offset or 0))
        nation_id = reader.u32(nation_address + 0x0C) if nation_address else None
    except (TypeError, ValueError, OverflowError):
        pass
    return _native_region_from_nation(nation_id, competition_name)


def _native_region_from_nation(nation_id: int | None, competition_name: str) -> tuple[str, str]:
    identifier = int(nation_id or 0)
    nation = _world_club_nation_name(NATION_NAMES.get(identifier, "未分类"))
    code = NATION_CODES.get(identifier, "")
    if 5 <= identifier <= 55:
        return "非洲", nation
    if 106 <= identifier <= 146 or identifier == 1662:
        return "亚洲", nation
    if 359 <= identifier <= 390:
        if code in {"GUY", "SUR"}:
            return "南美洲", nation
        return "北美洲", nation
    if 752 <= identifier <= 802:
        return "欧洲", nation
    if 1435 <= identifier <= 1444:
        return "大洋洲", nation
    if 1649 <= identifier <= 1658:
        return "南美洲", nation
    for continent, codes in _EXTRA_NATION_CONTINENTS.items():
        if code in codes:
            return continent, nation
    for continent, codes in _NATION_CONTINENTS.items():
        if code in codes:
            return continent, nation
    return _COMPETITION_REGIONS.get(competition_name, ("未分类", nation))


def _nation_youth_point_price(current: int) -> int:
    exponent = (int(current) - NATION_YOUTH_INVESTMENT_ANCHOR_RATING) / 100
    return round(
        NATION_YOUTH_INVESTMENT_ANCHOR_PRICE
        * NATION_YOUTH_INVESTMENT_HUNDRED_LEVEL_MULTIPLIER ** exponent
    )


def nation_youth_investment_price(youth_rating: Any) -> int | None:
    """Quote one native youth-rating point without allowing a 200 cap bypass."""
    try:
        current = int(youth_rating)
    except (TypeError, ValueError):
        return None
    if not 0 <= current < 200:
        return None
    return _nation_youth_point_price(current)


def nation_youth_investment_quote(
    youth_rating: Any, requested_increase: Any,
) -> dict[str, int] | None:
    try:
        current = int(youth_rating)
        requested = int(requested_increase)
    except (TypeError, ValueError):
        return None
    if not 0 <= current <= 200 or requested not in {-5, -1, 1, 5}:
        return None
    if requested > 0:
        increase = min(requested, 200 - current)
        price = sum(
            _nation_youth_point_price(current + step)
            for step in range(increase)
        )
    else:
        increase = -min(abs(requested), current)
        price = round(sum(
            _nation_youth_point_price(current + step)
            for step in range(abs(increase))
        ) / 2)
    if increase == 0:
        return None
    return {
        "requested_increase": requested,
        "increase": increase,
        "before": current,
        "after": current + increase,
        "price": price,
    }


def _nation_youth_offset(layout: Any) -> int:
    offset = getattr(layout, "nation_youth_rating_offset", None)
    if offset is None:
        raise RuntimeError("当前 Football Manager 版本尚未适配国家青训评分")
    return int(offset)


def _nation_men_container_offset(layout: Any) -> int:
    offset = getattr(layout, "nation_men_container_offset", None)
    if offset is None:
        raise RuntimeError("当前 Football Manager 版本尚未适配男子国家队容器")
    return int(offset)


def _validated_nation_men_container(reader: Reader, nation_address: int) -> int:
    """Resolve the men's container only when its native type still matches."""
    vtable_rva = int(getattr(reader.layout, "nation_vtable_rva", 0) or 0)
    if vtable_rva <= 0:
        raise RuntimeError("当前 Football Manager 版本缺少国家容器类型信息")
    container = int(
        reader.ptr(
            int(nation_address) + _nation_men_container_offset(reader.layout)
        ) or 0
    )
    expected_vtable = int(reader.module_base) + vtable_rva
    if not container or int(reader.ptr(container) or 0) != expected_vtable:
        raise RuntimeError("男子国家队容器类型校验失败，请重新扫描世界数据")
    return container


def _resolve_world_nation_address(
    reader: Reader, nation_id: int, nation_address: int,
) -> int:
    """Resolve a Nation by UID while retaining the legacy container-base hint."""
    try:
        return resolve_native_entity(
            reader, "nation", int(nation_id), nation_address,
            vtable_attribute="nation",
        )
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as resolver_error:
        hint = int(nation_address or 0)
        if hint and int(reader.u32(hint + ENTITY_UID) or 0) == int(nation_id):
            return hint
        if hint:
            raise RuntimeError("国家地址已失效，请重新扫描世界数据") from resolver_error
        raise RuntimeError("无法按 ID 定位 nation 对象，请刷新后重试") from resolver_error


def _nation_row(
    reader: Reader, nation_address: int, continent: str, nation: str,
) -> dict[str, Any] | None:
    address = int(nation_address or 0)
    nation_id = int(reader.u32(address + ENTITY_UID) or 0) if address else 0
    if address <= 0 or nation_id <= 0:
        return None
    try:
        men_container = _validated_nation_men_container(reader, address)
    except RuntimeError:
        return None
    try:
        youth_rating = reader.u8(men_container + _nation_youth_offset(reader.layout))
    except (RuntimeError, TypeError, ValueError):
        youth_rating = None
    return {
        "id": nation_id,
        "name": _world_club_nation_name(nation or NATION_NAMES.get(nation_id, "未分类")),
        "continent": continent or "未分类",
        "address": hex(address),
        "men_container_address": hex(men_container),
        "youth_rating": (
            int(youth_rating) if youth_rating is not None and 0 <= int(youth_rating) <= 200 else None
        ),
        "reputation": None,
        "manager": "待读取",
        "source": "native_scan",
    }


def _add_native_nation(
    nations: dict[int, dict[str, Any]], reader: Reader, nation_address: int,
    continent: str, nation: str,
) -> None:
    row = _nation_row(reader, nation_address, continent, nation)
    if not row:
        return
    existing = nations.get(int(row["id"]))
    if existing is None or row.get("youth_rating") is not None:
        nations[int(row["id"])] = row


def _national_team_from_container(
    reader: Reader, container_address: int, nation_id: int,
) -> dict[str, Any] | None:
    """Find the validated national-team object in the container's native array."""
    holder = int(reader.ptr(container_address + 0x18) or 0)
    begin = int(reader.ptr(holder) or 0) if holder else 0
    end = int(reader.ptr(holder + 0x08) or 0) if holder else 0
    if not begin or end <= begin or end - begin > 16 * 1024 * 1024:
        return None
    module_base = int(reader.module_base)
    vtable_rva = int(getattr(reader.layout, "national_team_vtable_rva", 0) or 0)
    if vtable_rva <= 0:
        return None
    vtable = module_base + vtable_rva
    raw = read_process_memory(reader.process, begin, end - begin) or b""
    needle = struct.pack("<Q", vtable)
    position = raw.find(needle)
    while position >= 0:
        if position % 8 == 0:
            team = reader.team(begin + position)
            if (
                team and team.get("team_type") == "national"
                and int(team.get("id") or 0) == int(nation_id)
            ):
                return team
        position = raw.find(needle, position + 1)
    return None


def _read_native_world_nation_detail(
    reader: Reader, layout: Any, nation: dict[str, Any],
) -> dict[str, Any]:
    """Read one nation's details through an already-open process reader."""
    from tools.club_reader import _name

    nation_id = int(nation.get("id") or 0)
    try:
        nation_address = int(str(nation.get("address") or "0"), 16)
    except (TypeError, ValueError):
        nation_address = 0
    if nation_id <= 0:
        raise ValueError("国家地址无效，请重新扫描世界数据")
    nation_address = _resolve_world_nation_address(
        reader, nation_id, nation_address,
    )
    men_container = _validated_nation_men_container(reader, nation_address)
    youth_offset = _nation_youth_offset(layout)
    youth_rating = reader.u8(men_container + youth_offset)
    if youth_rating is None or not 0 <= int(youth_rating) <= 200:
        raise RuntimeError("无法读取国家青训评分")
    team = _national_team_from_container(reader, men_container, nation_id)
    manager = None
    if team and team.get("manager_address"):
        try:
            manager_address = int(str(team["manager_address"]), 16)
        except (TypeError, ValueError):
            manager_address = 0
        person = (
            manager_address + int(getattr(
                layout, "team_manager_person_offset", layout.staff_complete_object_offset,
            ))
            if manager_address else 0
        )
        expected_staff_vtable = (
            reader.module_base + int(layout.staff_person_vtable_rva)
            if layout.staff_person_vtable_rva is not None else 0
        )
        if person and expected_staff_vtable and reader.ptr(person) != expected_staff_vtable:
            person = 0
        manager_id = int(reader.u32(person + ENTITY_UID) or 0) if person else 0
        manager_name = _name(reader, person) if person else None
        if manager_id and manager_name:
            manager = {"id": manager_id, "name": manager_name}
    return {
        "nation": {
            **nation,
            "name": _world_club_nation_name(str(nation.get("name") or NATION_NAMES.get(nation_id, "未分类"))),
            "youth_rating": int(youth_rating),
            "youth_rating_offset": youth_offset,
            "reputation": team.get("reputation") if team else None,
        },
        "national_team": (
            {
                "id": int(team.get("id") or 0),
                "name": team.get("name"),
                "address": team.get("address"),
                "reputation": team.get("reputation"),
            }
            if team else None
        ),
        "manager": manager,
        "investment": {
            "available": nation_youth_investment_price(youth_rating) is not None,
            "price": nation_youth_investment_price(youth_rating),
            "increase": 1,
            "options": [
                nation_youth_investment_quote(youth_rating, increase)
                for increase in (-5, -1, 1, 5)
            ],
            "max_youth_rating": 200,
        },
    }


def read_native_world_nation_detail(nation: dict[str, Any]) -> dict[str, Any]:
    """Read one nation's native youth rating, reputation and head coach."""
    pid, _path, layout = select_process_layout()
    with borrow_game_reader((pid, _path, layout)) as reader:
        return _read_native_world_nation_detail(reader, layout, nation)


def read_native_world_nation_summaries(
    nations: list[dict[str, Any]],
) -> dict[int, dict[str, Any]]:
    """Read list-card details for several nations through one process handle."""
    pending = [row for row in nations if int(row.get("id") or 0) > 0]
    if not pending:
        return {}
    pid, _path, layout = select_process_layout()
    with borrow_game_reader((pid, _path, layout)) as reader:
        summaries: dict[int, dict[str, Any]] = {}
        for nation in pending:
            nation_id = int(nation.get("id") or 0)
            try:
                detail = _read_native_world_nation_detail(reader, layout, nation)
            except Exception as error:
                summaries[nation_id] = {
                    "detail_scanned": False,
                    "detail_error": str(error),
                }
                continue
            summaries[nation_id] = {
                "reputation": (detail.get("national_team") or {}).get("reputation"),
                "manager": (detail.get("manager") or {}).get("name") or "当前空缺",
                "youth_rating": (detail.get("nation") or {}).get("youth_rating"),
                "detail_scanned": True,
                "detail_error": None,
            }
        return summaries


def invest_native_world_nation_youth(
    nation: dict[str, Any], *, expected_youth_rating: int, increase: int = 1,
) -> dict[str, Any]:
    """Increase native nation youth rating and verify the write immediately."""
    nation_id = int(nation.get("id") or 0)
    try:
        nation_address = int(str(nation.get("address") or "0"), 16)
    except (TypeError, ValueError):
        nation_address = 0
    if nation_id <= 0:
        raise ValueError("国家地址无效，请重新扫描世界数据")
    pid, _path, layout = select_process_layout()
    with open_process(pid, write_memory=True) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} not loaded")
        reader = Reader(process, module.base_address, layout)
        nation_address = _resolve_world_nation_address(
            reader, nation_id, nation_address,
        )
        men_container = _validated_nation_men_container(reader, nation_address)
        offset = _nation_youth_offset(layout)
        before = reader.u8(men_container + offset)
        if before is None or int(before) != int(expected_youth_rating):
            raise RuntimeError("国家青训评分已变化，请刷新后重试")
        before = int(before)
        increase = int(increase)
        if increase == 0 or abs(increase) > 5:
            raise ValueError("国家青训评分每次只能调整 1 至 5 点")
        after = before + increase
        if not 0 <= after <= 200:
            raise ValueError("国家青训评分必须保持在 0 至 200")
        address = men_container + offset
        try:
            write_process_memory(process, address, bytes([after]))
            written = reader.u8(address)
            if written != after:
                raise RuntimeError("国家青训评分写入回读失败")
        except Exception as write_error:
            current = reader.u8(address)
            if current == before:
                raise RuntimeError(
                    f"国家青训评分写入失败，原值未变化：{write_error}"
                ) from write_error
            try:
                write_process_memory(process, address, bytes([before]))
                if reader.u8(address) != before:
                    raise RuntimeError("国家青训评分回滚后回读不一致")
            except Exception as rollback_error:
                raise RuntimeError(
                    f"国家青训评分写入失败且回滚失败：{write_error}；{rollback_error}"
                ) from write_error
            raise RuntimeError(
                f"国家青训评分写入失败，原值已恢复：{write_error}"
            ) from write_error
        return {
            "nation_id": nation_id,
            "before": before,
            "after": after,
            "increase": increase,
            "address": hex(address),
            "game_key": str(layout.key),
        }


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    try:
        for attempt in range(20):
            try:
                os.replace(temporary, path)
                return
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _runtime_identity(save_id: str = "") -> dict[str, Any]:
    with borrow_game_reader() as reader:
        layout = reader.layout
        return {
            "game_key": str(layout.key),
            "game_version": str(layout.game_version or layout.display_name),
            "build_identity": str(layout.executable_sha256 or layout.display_name),
            "save_id": str(save_id or ""),
            "process_id": int(reader.process.pid),
            "module_base": hex(reader.module_base),
        }


def resolve_native_team_addresses(
    team_ids: list[int] | set[int], team_hints: list[int] | set[int],
    *, scan_all_if_unresolved: bool = False,
    known_addresses: dict[int, Any] | None = None,
) -> dict[int, str]:
    """Resolve a few current Team addresses without rebuilding the world cache."""
    wanted = {int(value) for value in team_ids if int(value) > 0}
    hints = {int(value) for value in team_hints if int(value) > 0}
    known = {
        int(team_id): value for team_id, value in (known_addresses or {}).items()
        if int(team_id) in wanted
    }
    if not wanted:
        return {}
    with borrow_game_reader() as reader:
        layout = reader.layout
        module = reader.module
        process = reader.process
        found: dict[int, str] = {}
        # FMRTE-style fast path: a session address is a hint owned by the UID.
        # Validate that exact object first and only scan a slab if it no longer
        # represents the requested Team.
        for team_id, raw_address in known.items():
            try:
                address = int(str(raw_address or "0"), 0)
            except (TypeError, ValueError):
                address = 0
            team = reader.team(address) if address else None
            if (
                team and team.get("team_type") == "club"
                and int(team.get("id") or 0) == int(team_id)
            ):
                found[int(team_id)] = hex(address)
        if len(found) == len(wanted):
            return found

        directory = database_index_for_reader(reader)
        if directory is not None:
            for team_id in sorted(wanted.difference(found)):
                for address in directory.addresses_for_uid("team", team_id):
                    team = reader.team(address)
                    if (
                        team and team.get("team_type") == "club"
                        and int(team.get("id") or 0) == int(team_id)
                    ):
                        found[int(team_id)] = hex(int(address))
                        break
        if len(found) == len(wanted):
            return found

        regions = sorted(
            (region for region in iter_readable_regions(process) if region.type == MEM_PRIVATE),
            key=lambda region: region.base_address,
        )
        region_bases = [region.base_address for region in regions]
        selected_indexes: set[int] = set()
        for hint in hints:
            index = bisect_right(region_bases, hint) - 1
            if (
                index >= 0
                and hint < regions[index].base_address + regions[index].size
            ):
                selected_indexes.add(index)
        if not selected_indexes and not scan_all_if_unresolved:
            return {}

        team_vtable = module.base_address + int(layout.team_vtable_rva)
        club_vtable = module.base_address + int(layout.club_vtable_rva)
        needle = struct.pack("<Q", team_vtable)
        chunk_size = 8 * 1024 * 1024
        overlap_size = ENTITY_UID + 4
        scan_indexes = sorted(selected_indexes)
        if scan_all_if_unresolved:
            scan_indexes.extend(
                index for index in range(len(regions))
                if index not in selected_indexes
            )
        for index in scan_indexes:
            region = regions[index]
            carry = b""
            for offset in range(0, region.size, chunk_size):
                size = min(chunk_size, region.size - offset)
                raw = read_process_memory(process, region.base_address + offset, size)
                if not raw:
                    carry = b""
                    continue
                scan = carry + raw
                scan_base = region.base_address + offset - len(carry)
                start = scan.find(needle)
                while start >= 0:
                    address = scan_base + start
                    if address % 8 == 0:
                        uid_offset = start + ENTITY_UID
                        team_id = (
                            struct.unpack_from("<I", scan, uid_offset)[0]
                            if uid_offset + 4 <= len(scan)
                            else int(reader.u32(address + ENTITY_UID) or 0)
                        )
                        if team_id in wanted and team_id not in found:
                            club = int(reader.ptr(address + TEAM_CLUB) or 0)
                            if (
                                club
                                and reader.ptr(club) == club_vtable
                                and reader.u32(club + ENTITY_UID) == team_id
                            ):
                                found[team_id] = hex(address)
                                if len(found) == len(wanted):
                                    return found
                    start = scan.find(needle, start + 1)
                carry = scan[-overlap_size:]
        return found


def scan_native_world_clubs(
    save_id: str = "",
    progress: Callable[[int, str], None] | None = None,
    team_hints: list[int] | None = None,
) -> dict[str, Any]:
    """Read-only enumeration of validated native men's club team objects."""
    pid, _path, layout = select_process_layout()
    game_key = str(layout.key)
    with borrow_game_reader((pid, _path, layout)) as reader:
        process = reader.process
        module = reader.module
        needle = struct.pack("<Q", module.base_address + layout.team_vtable_rva)
        addresses: set[int] = set()
        bytes_scanned = 0
        directory = database_index_for_reader(reader)
        indexed_addresses = (
            set(directory.addresses("team")) if directory is not None else set()
        )
        addresses.update(indexed_addresses)
        index_returned_before = int(getattr(reader, "returned_bytes", 0) or 0)
        regions = (
            [] if indexed_addresses else sorted(
                (
                    region for region in iter_readable_regions(process)
                    if region.type == MEM_PRIVATE
                ),
                key=lambda region: region.base_address,
            )
        )
        region_bases = [region.base_address for region in regions]

        def region_indexes(values: list[int] | set[int]) -> set[int]:
            indexes: set[int] = set()
            for value in values:
                index = bisect_right(region_bases, int(value)) - 1
                if (
                    index >= 0
                    and int(value) < regions[index].base_address + regions[index].size
                ):
                    indexes.add(index)
            return indexes

        hint_indexes = region_indexes(indexed_addresses or set(team_hints or []))
        selected_regions = [regions[index] for index in sorted(hint_indexes)]
        scan_mode = (
            "database_index" if indexed_addresses
            else "team_slabs" if selected_regions else "full_heap"
        )
        total_bytes = sum(region.size for region in selected_regions or regions) or 1
        team_snapshots: list[tuple[int, int, bytes]] = []
        if progress:
            progress(
                1,
                "正在读取俱乐部数据库索引"
                if indexed_addresses else
                "正在定位俱乐部对象池" if selected_regions else "正在扫描俱乐部对象",
            )
        for region in selected_regions or regions:
            carry = b""
            offsets = [0] if selected_regions else range(0, region.size, 8 * 1024 * 1024)
            for offset in offsets:
                size = region.size if selected_regions else min(8 * 1024 * 1024, region.size - offset)
                raw = read_process_memory(process, region.base_address + offset, size)
                if not raw:
                    carry = b""
                    continue
                bytes_scanned += len(raw)
                if selected_regions:
                    team_snapshots.append((
                        region.base_address + offset,
                        region.base_address + offset + len(raw),
                        raw,
                    ))
                scan = carry + raw
                scan_base = region.base_address + offset - len(carry)
                start = scan.find(needle)
                while start >= 0:
                    address = scan_base + start
                    if address % 8 == 0:
                        addresses.add(address)
                    start = scan.find(needle, start + 1)
                carry = raw[-(len(needle) - 1):]
                if progress:
                    progress(min(40, 1 + int(bytes_scanned * 39 / total_bytes)), "正在读取俱乐部对象池")

        clubs: dict[int, dict[str, Any]] = {}
        nations: dict[int, dict[str, Any]] = {}
        sorted_addresses = sorted(addresses)
        address_count = len(sorted_addresses) or 1
        snapshot_bases = [item[0] for item in team_snapshots]
        indexed_team_snapshots = (
            reader._fixed_size_snapshots(sorted_addresses, 0xB0)
            if indexed_addresses else {}
        )

        def snapshot_at(
            snapshots: list[tuple[int, int, bytes]], bases: list[int],
            address: int, size: int,
        ) -> bytes | None:
            index = bisect_right(bases, address) - 1
            if index < 0:
                return None
            start, end, raw = snapshots[index]
            if address + size > end:
                return None
            offset = address - start
            return raw[offset:offset + size]

        if indexed_addresses or (selected_regions and len(addresses) >= 1000):
            team_headers: list[tuple[int, bytes]] = []
            club_addresses: set[int] = set()
            for address in sorted_addresses:
                raw = (
                    indexed_team_snapshots.get(address)
                    if indexed_addresses else
                    snapshot_at(team_snapshots, snapshot_bases, address, 0xB0)
                )
                if not raw or raw[:8] != needle:
                    continue
                club_address = struct.unpack_from("<Q", raw, 0x30)[0]
                if club_address:
                    club_addresses.add(club_address)
                    team_headers.append((address, raw))
            indexed_club_snapshots = (
                reader._fixed_size_snapshots(sorted(club_addresses), 0xE0)
                if indexed_addresses else {}
            )
            club_snapshots: list[tuple[int, int, bytes]] = []
            if not indexed_addresses:
                for index in sorted(region_indexes(club_addresses)):
                    region = regions[index]
                    raw = read_process_memory(process, region.base_address, region.size)
                    if raw and len(raw) == region.size:
                        bytes_scanned += len(raw)
                        club_snapshots.append((
                            region.base_address,
                            region.base_address + region.size,
                            raw,
                        ))
            club_bases = [item[0] for item in club_snapshots]
            club_vtable = struct.pack("<Q", module.base_address + layout.club_vtable_rva)
            valid_headers: list[tuple[int, bytes, int, bytes, int]] = []
            name_pointers: set[int] = set()
            for address, team_raw in team_headers:
                team_id = struct.unpack_from("<I", team_raw, 0x0C)[0]
                club_address = struct.unpack_from("<Q", team_raw, 0x30)[0]
                club_raw = (
                    indexed_club_snapshots.get(club_address)
                    if indexed_addresses else
                    snapshot_at(club_snapshots, club_bases, club_address, 0xE0)
                )
                if (
                    not team_id or not club_raw or club_raw[:8] != club_vtable
                    or struct.unpack_from("<I", club_raw, 0x0C)[0] != team_id
                ):
                    continue
                name_pointer = struct.unpack_from("<Q", club_raw, 0xC0)[0]
                if not name_pointer:
                    continue
                name_pointers.add(name_pointer)
                valid_headers.append((address, team_raw, club_address, club_raw, name_pointer))

            names: dict[int, str] = {}
            if indexed_addresses:
                name_snapshots = reader._fixed_size_snapshots(
                    sorted(name_pointers), 260,
                )
                for pointer, raw in name_snapshots.items():
                    length = struct.unpack_from("<I", raw, 0)[0]
                    if not 0 < length <= 256 or 4 + length > len(raw):
                        continue
                    try:
                        value = raw[4:4 + length].decode("utf-8")
                    except UnicodeDecodeError:
                        continue
                    if value and not any(ord(char) < 32 for char in value):
                        names[pointer] = value
            else:
                pointers_by_region: dict[int, list[int]] = {}
                for pointer in name_pointers:
                    indexes = region_indexes({pointer})
                    if indexes:
                        pointers_by_region.setdefault(next(iter(indexes)), []).append(pointer)
                for region_index, pointers in pointers_by_region.items():
                    region = regions[region_index]
                    pointers.sort()
                    start = 0
                    while start < len(pointers):
                        stop = start + 1
                        span_start = pointers[start]
                        while (
                            stop < len(pointers)
                            and pointers[stop] - pointers[stop - 1] <= 0x1000
                            and pointers[stop] - span_start <= 8 * 1024 * 1024
                        ):
                            stop += 1
                        span_end = min(region.base_address + region.size, pointers[stop - 1] + 260)
                        raw = read_process_memory(process, span_start, span_end - span_start)
                        if raw:
                            bytes_scanned += len(raw)
                            for pointer in pointers[start:stop]:
                                offset = pointer - span_start
                                if offset + 4 > len(raw):
                                    continue
                                length = struct.unpack_from("<I", raw, offset)[0]
                                if not 0 < length <= 256 or offset + 4 + length > len(raw):
                                    continue
                                try:
                                    value = raw[offset + 4:offset + 4 + length].decode("utf-8")
                                except UnicodeDecodeError:
                                    continue
                                if value and not any(ord(char) < 32 for char in value):
                                    names[pointer] = value
                        start = stop
            nation_ids: dict[int, int | None] = {}
            header_count = len(valid_headers) or 1
            for index, (address, team_raw, club_address, club_raw, name_pointer) in enumerate(valid_headers):
                if progress and (index % 500 == 0 or index + 1 == header_count):
                    progress(40 + int((index + 1) * 59 / header_count), "正在读取男足俱乐部资料")
                team_id = struct.unpack_from("<I", team_raw, 0x0C)[0]
                name = names.get(name_pointer)
                if not name:
                    continue
                competition_address = struct.unpack_from("<Q", team_raw, 0x50)[0]
                competition_row = reader.competition(competition_address) if competition_address else None
                competition = str((competition_row or {}).get("name") or "")
                if _looks_non_mens(name, competition):
                    continue
                nation_address = (
                    struct.unpack_from("<Q", club_raw, int(layout.club_nation_offset))[0]
                    if layout.club_nation_offset is not None else 0
                )
                if nation_address not in nation_ids:
                    nation_ids[nation_address] = reader.u32(nation_address + 0x0C) if nation_address else None
                continent, nation = _native_region_from_nation(
                    nation_ids[nation_address], competition,
                )
                _add_native_nation(nations, reader, nation_address, continent, nation)
                reputation = struct.unpack_from("<H", team_raw, int(layout.team_reputation_offset))[0]
                row = {
                    "id": team_id, "name": name, "short_name": None,
                    "address": hex(address), "club_address": hex(club_address),
                    "manager_address": hex(struct.unpack_from("<Q", team_raw, 0x80)[0]),
                    "team_type": "club",
                    "reputation": reputation if 1 <= reputation <= 10000 else None,
                    "continent": continent, "nation": nation,
                    "competition": competition or "未分类",
                    "competition_id": int((competition_row or {}).get("id") or 0) or None,
                    "competitions": [competition] if competition else [],
                    "recent_results": [], "manager": "待读取", "balance": None,
                    "source": "native_scan",
                }
                current = clubs.get(team_id)
                if current is None or int(row.get("reputation") or 0) > int(current.get("reputation") or 0):
                    clubs[team_id] = row
                elif competition and competition not in current["competitions"]:
                    current["competitions"].append(competition)
        else:
            scan_mode = "full_heap"
            for index, address in enumerate(sorted_addresses):
                if progress and (index % 250 == 0 or index + 1 == address_count):
                    progress(40 + int((index + 1) * 59 / address_count), "正在校验男足俱乐部资料")
                team = reader.team(address)
                if not team or team.get("team_type") != "club" or not team.get("name"):
                    continue
                competition = (team.get("competition") or {}).get("name") or ""
                competition_id = int((team.get("competition") or {}).get("id") or 0) or None
                if _looks_non_mens(str(team.get("name") or ""), str(competition)):
                    continue
                continent, nation = _native_region(reader, str(team.get("club_address") or "0"), str(competition))
                try:
                    club_address = int(str(team.get("club_address") or "0"), 16)
                except (TypeError, ValueError):
                    club_address = 0
                nation_address = (
                    int(reader.ptr(club_address + int(layout.club_nation_offset or 0)) or 0)
                    if club_address and layout.club_nation_offset is not None else 0
                )
                _add_native_nation(nations, reader, nation_address, continent, nation)
                row = {
                    **team, "continent": continent, "nation": nation,
                    "competition": competition or "未分类",
                    "competition_id": competition_id,
                    "competitions": [competition] if competition else [],
                    "recent_results": [], "manager": "待读取", "balance": None,
                    "source": "native_scan",
                }
                team_id = int(row.get("id") or 0)
                if team_id <= 0:
                    continue
                current = clubs.get(team_id)
                if current is None or int(row.get("reputation") or 0) > int(current.get("reputation") or 0):
                    clubs[team_id] = row
                elif competition and competition not in current["competitions"]:
                    current["competitions"].append(competition)
        if indexed_addresses:
            bytes_scanned = max(
                bytes_scanned,
                int(getattr(reader, "returned_bytes", 0) or 0) - index_returned_before,
            )
        identity = {
            "game_key": game_key,
            "game_version": str(layout.game_version or layout.display_name),
            "build_identity": str(layout.executable_sha256 or layout.display_name),
            "save_id": str(save_id or ""),
            "process_id": int(pid),
            "module_base": hex(module.base_address),
        }
        if progress:
            progress(100, "扫描完成")
        return {
            "schema_version": 5,
            **identity,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "bytes_scanned": bytes_scanned,
            "scan_mode": scan_mode,
            "candidate_addresses": len(addresses),
            "nations": sorted(
                nations.values(),
                key=lambda row: (-int(row.get("youth_rating") or 0), str(row.get("name") or "").casefold()),
            ),
            "clubs": sorted(
                clubs.values(),
                key=lambda row: (-int(row.get("reputation") or 0), str(row.get("name") or "").casefold()),
            ),
        }


def refresh_native_world_clubs(
    payload: dict[str, Any], save_id: str = "",
    progress: Callable[[int, str], None] | None = None,
) -> dict[str, Any]:
    """Refresh volatile club fields from validated cached Team addresses."""
    pid, _path, layout = select_process_layout()
    clubs = list(payload.get("clubs") or [])
    if not clubs:
        raise RuntimeError("俱乐部地址缓存为空")
    with borrow_game_reader((pid, _path, layout)) as reader:
        process = reader.process
        module = reader.module
        identity = {
            "game_key": str(layout.key),
            "game_version": str(layout.game_version or layout.display_name),
            "build_identity": str(layout.executable_sha256 or layout.display_name),
            "save_id": str(save_id or ""),
            "process_id": int(pid),
            "module_base": hex(module.base_address),
        }
        if any(
            str(payload.get(key) or "") != str(identity.get(key) or "")
            for key in (
                "game_key", "game_version", "build_identity", "save_id",
                "process_id", "module_base",
            )
        ):
            raise RuntimeError("俱乐部地址缓存已失效")

        addresses = []
        for club in clubs:
            try:
                address = int(str(club.get("address") or "0"), 16)
            except (TypeError, ValueError):
                address = 0
            if address:
                addresses.append(address)
        if not addresses:
            raise RuntimeError("俱乐部地址缓存不可用")

        reputation_offset = layout.team_reputation_offset
        header_size = max(
            0x88, int(reputation_offset) + 2 if reputation_offset is not None else 0,
        )
        sorted_addresses = sorted(set(addresses))
        spans: list[tuple[int, int, list[int]]] = []
        for address in sorted_addresses:
            end = address + header_size
            if (
                spans
                and address - spans[-1][1] <= 0x1000
                and end - spans[-1][0] <= 8 * 1024 * 1024
            ):
                start, previous_end, members = spans[-1]
                members.append(address)
                spans[-1] = (start, max(previous_end, end), members)
            else:
                spans.append((address, end, [address]))

        headers: dict[int, bytes] = {}
        bytes_scanned = 0
        for index, (start, end, members) in enumerate(spans):
            raw = read_process_memory(process, start, end - start)
            if raw and len(raw) == end - start:
                bytes_scanned += len(raw)
                for address in members:
                    offset = address - start
                    headers[address] = raw[offset:offset + header_size]
            else:
                for address in members:
                    header = read_process_memory(process, address, header_size)
                    if header and len(header) == header_size:
                        bytes_scanned += len(header)
                        headers[address] = header
            if progress:
                progress(
                    min(70, 5 + int((index + 1) * 65 / max(1, len(spans)))),
                    "正在更新俱乐部索引",
                )

        team_vtable = module.base_address + int(layout.team_vtable_rva)
        refreshed = []
        invalid = 0
        for index, club in enumerate(clubs):
            try:
                address = int(str(club.get("address") or "0"), 16)
            except (TypeError, ValueError):
                address = 0
            raw = headers.get(address)
            if not raw or len(raw) < header_size:
                invalid += 1
                continue
            cached_id = int(club.get("id") or 0)
            vtable = struct.unpack_from("<Q", raw, 0)[0]
            team_id = struct.unpack_from("<I", raw, ENTITY_UID)[0]
            club_address = struct.unpack_from("<Q", raw, TEAM_CLUB)[0]
            if vtable != team_vtable or team_id != cached_id or not club_address:
                invalid += 1
                continue
            competition_address = struct.unpack_from("<Q", raw, TEAM_COMP)[0]
            competition_row = reader.competition(competition_address) if competition_address else None
            competition = str((competition_row or {}).get("name") or "")
            reputation = (
                struct.unpack_from("<H", raw, int(reputation_offset))[0]
                if reputation_offset is not None else club.get("reputation")
            )
            updated = dict(club)
            updated.update({
                "club_address": hex(club_address),
                "manager_address": hex(struct.unpack_from("<Q", raw, TEAM_MANAGER)[0]),
                "reputation": (
                    int(reputation)
                    if reputation is not None and 1 <= int(reputation) <= 10000
                    else None
                ),
                "competition": competition or club.get("competition") or "未分类",
                "competition_id": int((competition_row or {}).get("id") or 0) or club.get("competition_id"),
            })
            if competition:
                competitions = list(updated.get("competitions") or [])
                if competition not in competitions:
                    competitions.append(competition)
                updated["competitions"] = competitions
            refreshed.append(updated)
            if progress and (index % 500 == 0 or index + 1 == len(clubs)):
                progress(
                    70 + int((index + 1) * 29 / max(1, len(clubs))),
                    "正在校验俱乐部资料",
                )
        minimum_valid = max(1, int(len(clubs) * 0.95))
        if len(refreshed) < minimum_valid:
            raise RuntimeError(f"俱乐部地址失效过多（{invalid}/{len(clubs)}）")
        if progress:
            progress(100, "刷新完成")
        return {
            **payload,
            **identity,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "bytes_scanned": bytes_scanned,
            "scan_mode": "cached_addresses",
            "candidate_addresses": len(headers),
            "clubs": sorted(
                refreshed,
                key=lambda row: (-int(row.get("reputation") or 0), str(row.get("name") or "").casefold()),
            ),
        }


def save_native_world_clubs(payload: dict[str, Any], save_id: str = "") -> None:
    game_key = str(payload.get("game_key") or select_process_layout()[2].key)
    path = _native_cache_path(game_key, save_id)
    with _CACHE_FILE_LOCK:
        _atomic_write_json(path, payload)


def load_native_world_clubs(save_id: str = "") -> dict[str, Any] | None:
    """Load the matching cache without ever starting a memory scan."""
    _pid, _path, layout = select_process_layout()
    path = _native_cache_path(str(layout.key), save_id)
    try:
        with _CACHE_FILE_LOCK:
            payload = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(payload, dict)
            or int(payload.get("schema_version") or 0) not in {4, 5}
            or not isinstance(payload.get("clubs"), list)
        ):
            return None
        validity = native_world_club_cache_validity(payload, save_id)
        if not validity["matches_database"]:
            return None
        payload["addresses_current"] = validity["addresses_current"]
        return payload
    except (OSError, json.JSONDecodeError):
        return None


def native_world_club_cache_validity(payload: dict[str, Any], save_id: str = "") -> dict[str, bool]:
    identity = _runtime_identity(save_id)
    matches_database = not any(
        str(payload.get(key) or "") != str(identity.get(key) or "")
        for key in ("game_key", "game_version", "build_identity", "save_id")
    )
    return {
        "matches_database": matches_database,
        "addresses_current": bool(
            matches_database
            and int(payload.get("process_id") or 0) == int(identity["process_id"])
            and str(payload.get("module_base") or "") == str(identity["module_base"])
        ),
    }


def native_world_club_cache_status(save_id: str = "") -> dict[str, Any]:
    payload = load_native_world_clubs(save_id)
    return {
        "ready": bool(payload),
        "addresses_current": bool(payload and payload.get("addresses_current")),
        "club_count": len(payload.get("clubs", [])) if payload else 0,
        "generated_at": payload.get("generated_at") if payload else None,
        "game_key": payload.get("game_key") if payload else select_process_layout()[2].key,
    }


def _empty_portfolio() -> dict[str, Any]:
    return {"schema_version": 1, "group_name": "我的集团", "clubs": []}


def load_acquired_clubs(scope_id: str = "") -> dict[str, Any]:
    payload = load_document(
        "acquired_clubs", None, scope_id or None,
        legacy_path=_portfolio_path(scope_id),
    )
    if not isinstance(payload, dict) or not isinstance(payload.get("clubs"), list):
        payload = _empty_portfolio()
    payload.setdefault("schema_version", 1)
    payload.setdefault("group_name", "我的集团")
    return payload


def acquired_clubs_for_game_date(
    clubs: list[dict[str, Any]], game_date: str,
) -> list[dict[str, Any]]:
    """Annotate holdings without deleting future-timeline acquisitions."""
    try:
        current = datetime.fromisoformat(str(game_date)).date()
    except (TypeError, ValueError):
        current = None
    rows: list[dict[str, Any]] = []
    for source in clubs or []:
        row = dict(source)
        try:
            acquired = datetime.fromisoformat(
                str(row.get("acquired_game_date") or ""),
            ).date()
        except (TypeError, ValueError):
            acquired = None
        active = not (current is not None and acquired is not None and current < acquired)
        row["ownership_active"] = active
        row["ownership_suspended_until"] = acquired.isoformat() if not active else None
        rows.append(row)
    return rows


def _normalize_portfolio(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or not isinstance(payload.get("clubs"), list):
        payload = _empty_portfolio()
    payload.setdefault("schema_version", 1)
    payload.setdefault("group_name", "我的集团")
    return payload


def _update_portfolio(
    mutator: Callable[[dict[str, Any]], Any], scope_id: str = "",
) -> Any:
    def update(payload: Any) -> Any:
        normalized = _normalize_portfolio(payload)
        if normalized is not payload:
            if isinstance(payload, dict):
                payload.clear()
                payload.update(normalized)
                normalized = payload
        return mutator(normalized)

    return update_document(
        "acquired_clubs", _empty_portfolio(), update, scope_id or None,
        legacy_path=_portfolio_path(scope_id),
    )


def set_portfolio_group_name(name: str, scope_id: str = "") -> str:
    normalized = str(name or "").strip()
    if not normalized:
        raise ValueError("集团名称不能为空")
    if len(normalized) > 60:
        raise ValueError("集团名称不能超过 60 个字符")
    _update_portfolio(
        lambda payload: payload.__setitem__("group_name", normalized), scope_id,
    )
    return normalized


def acquire_club(club: dict[str, Any], scope_id: str = "") -> dict[str, Any]:
    team_id = int(club.get("id") or 0)
    if team_id <= 0:
        raise ValueError("俱乐部 ID 无效")
    row = dict(club)
    row["id"] = team_id
    row["acquired_at"] = str(row.get("acquired_at") or datetime.now().isoformat(timespec="seconds"))
    def mutate(payload: dict[str, Any]) -> dict[str, Any]:
        rows = [
            item for item in payload["clubs"]
            if int(item.get("id") or 0) != team_id
        ]
        rows.append(row)
        payload["clubs"] = rows
        return row

    return _update_portfolio(mutate, scope_id)


def remove_acquired_club(team_id: int, scope_id: str = "") -> bool:
    def mutate(payload: dict[str, Any]) -> bool:
        before = len(payload["clubs"])
        payload["clubs"] = [
            item for item in payload["clubs"]
            if int(item.get("id") or 0) != int(team_id)
        ]
        return len(payload["clubs"]) != before

    return bool(_update_portfolio(mutate, scope_id))


def recover_acquired_clubs_from_transactions(
    transactions: list[dict[str, Any]], scope_id: str = "",
) -> list[dict[str, Any]]:
    """Restore holdings deleted by the V2.0.4 date-rewind regression.

    Acquisition and sale payments are durable account evidence. Fold those
    events in recorded order and recreate only clubs whose latest completed
    ownership event says they are still held. Existing portfolio rows always
    win because they contain the complete native rollback and valuation data.
    """
    ownership: dict[int, dict[str, Any]] = {}
    held: dict[int, bool] = {}
    refunded_transaction_ids = {
        str(transaction.get("refund_of") or "")
        for transaction in transactions or []
        if isinstance(transaction, dict)
        and str(transaction.get("refund_of") or "")
    }
    recruitment_floors: dict[int, int] = {}
    sold_recruitment_floors: dict[int, int] = {}
    for transaction in transactions or []:
        if not isinstance(transaction, dict):
            continue
        try:
            team_id = int(transaction.get("team_id") or 0)
        except (TypeError, ValueError):
            continue
        if team_id <= 0:
            continue
        kind = str(transaction.get("type") or "")
        if kind == "world_club_acquisition":
            try:
                price = max(0, int(round(abs(float(transaction.get("amount") or 0)))))
            except (TypeError, ValueError, OverflowError):
                price = 0
            ownership[team_id] = {
                "id": team_id,
                "name": str(transaction.get("team_name") or team_id),
                "acquired_at": str(transaction.get("at") or ""),
                "acquired_game_date": str(transaction.get("game_date") or ""),
                "acquisition_price": price,
                "acquisition_valuation": {"price": price},
                "acquisition_mode": "portfolio_recovered",
                "sale_restore": {"schema_version": 1, "modified_fields": []},
                "recovered_from_transaction": True,
            }
            held[team_id] = True
            recruitment_floors.pop(team_id, None)
        elif kind == "world_club_acquisition_rollback":
            held[team_id] = False
            recruitment_floors.pop(team_id, None)
        elif kind == "world_club_sale":
            held[team_id] = False
            sold_recruitment_floors[team_id] = recruitment_floors.pop(team_id, 0)
        elif kind == "world_club_sale_rollback" and team_id in ownership:
            held[team_id] = True
            previous_floor = sold_recruitment_floors.pop(team_id, 0)
            if previous_floor:
                recruitment_floors[team_id] = previous_floor
        elif (
            kind == "world_club_facility_upgrade"
            and str(transaction.get("facility") or "") == "recruitment"
            and str(transaction.get("facility_upgrade_mode") or "")
            != "scheduled_12_day"
            and str(transaction.get("id") or "")
            and str(transaction.get("id") or "") not in refunded_transaction_ids
        ):
            try:
                target_level = int(transaction.get("facility_level")) + 1
            except (TypeError, ValueError):
                continue
            if 1 <= target_level <= 20:
                recruitment_floors[team_id] = max(
                    recruitment_floors.get(team_id, 0), target_level,
                )

    def mutate(payload: dict[str, Any]) -> list[dict[str, Any]]:
        existing_ids = {
            int(row.get("id") or 0) for row in payload["clubs"]
            if isinstance(row, dict) and int(row.get("id") or 0) > 0
        }
        recovered = [
            dict(row) for team_id, row in ownership.items()
            if held.get(team_id) and team_id not in existing_ids
        ]
        payload["clubs"].extend(recovered)
        for row in payload["clubs"]:
            team_id = int(row.get("id") or 0)
            recovered_floor = recruitment_floors.get(team_id, 0)
            if not recovered_floor:
                continue
            saved = row.get("facility_level_floors")
            floors = dict(saved) if isinstance(saved, dict) else {}
            try:
                saved_floor = int(floors.get("recruitment") or 0)
            except (TypeError, ValueError):
                saved_floor = 0
            floors["recruitment"] = max(saved_floor, recovered_floor)
            row["facility_level_floors"] = floors
        return recovered

    return _update_portfolio(mutate, scope_id)


def update_acquired_club(
    team_id: int, updates: dict[str, Any], scope_id: str = "",
) -> dict[str, Any]:
    def mutate(payload: dict[str, Any]) -> dict[str, Any]:
        current = next(
            (
                row for row in payload["clubs"]
                if int(row.get("id") or 0) == int(team_id)
            ),
            None,
        )
        if not current:
            raise ValueError("该俱乐部尚未收购")
        current.update(dict(updates))
        return dict(current)

    return _update_portfolio(mutate, scope_id)


def update_acquired_club_map_entries(
    team_id: int,
    map_updates: dict[str, dict[str, Any]],
    scope_id: str = "",
    *,
    expected_absent: tuple[tuple[str, str], ...] = (),
) -> dict[str, Any]:
    """Atomically update selected nested-map entries without losing siblings."""
    def mutate(payload: dict[str, Any]) -> dict[str, Any]:
        current = next(
            (
                row for row in payload["clubs"]
                if int(row.get("id") or 0) == int(team_id)
            ),
            None,
        )
        if not current:
            raise ValueError("该俱乐部尚未收购")
        for field, key in expected_absent:
            saved = current.get(field)
            if isinstance(saved, dict) and key in saved:
                raise ValueError("该项设施已有进行中的升级计划")
        for field, entries in map_updates.items():
            saved = current.get(field)
            updated = dict(saved) if isinstance(saved, dict) else {}
            for key, value in entries.items():
                if value is None:
                    updated.pop(str(key), None)
                else:
                    updated[str(key)] = value
            current[field] = updated
        return dict(current)

    return _update_portfolio(mutate, scope_id)


def _club_result_key(row: dict[str, Any]) -> str:
    return json.dumps([
        str(row.get("date") or ""),
        str(row.get("competition") or ""),
        str(row.get("opponent") or ""),
        str(row.get("score") or ""),
        str(row.get("home_away") or ""),
    ], ensure_ascii=False, separators=(",", ":"))


def _record_summary(rows: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "wins": sum(str(row.get("result") or "") == "胜" for row in rows),
        "draws": sum(str(row.get("result") or "") == "平" for row in rows),
        "losses": sum(str(row.get("result") or "") == "负" for row in rows),
    }


def sync_acquired_club_manager_record(
    team_id: int, manager_id: int, results: list[dict[str, Any]], scope_id: str = "",
) -> dict[str, Any] | None:
    """Accumulate readable club results and reset the counter on a manager change."""
    readable = [
        {"key": _club_result_key(row), "date": str(row.get("date") or ""),
         "result": str(row.get("result") or "")}
        for row in results
        if str(row.get("result") or "") in {"胜", "平", "负"}
    ]
    current_keys = {row["key"] for row in readable}
    manager_id = max(0, int(manager_id or 0))

    def mutate(payload: dict[str, Any]) -> dict[str, Any]:
        club = next(
            (
                row for row in payload["clubs"]
                if int(row.get("id") or 0) == int(team_id)
            ),
            None,
        )
        if not club:
            raise ValueError("该俱乐部尚未收购")
        previous = club.get("manager_record_state")
        state = dict(previous) if isinstance(previous, dict) else {}
        previous_manager_id = int(state.get("manager_id") or 0)
        if not state:
            state = {
                "schema_version": 1,
                "manager_id": manager_id,
                "started_at": datetime.now().isoformat(timespec="seconds"),
                "ignored_result_keys": [],
                "counted_results": readable if manager_id > 0 else [],
            }
        elif previous_manager_id != manager_id:
            state = {
                "schema_version": 1,
                "manager_id": manager_id,
                "started_at": datetime.now().isoformat(timespec="seconds"),
                # Every currently readable result predates the detected change.
                "ignored_result_keys": sorted(current_keys),
                "counted_results": [],
            }
        elif manager_id > 0:
            counted = [
                row for row in state.get("counted_results") or []
                if isinstance(row, dict) and row.get("key")
            ]
            known = {
                str(key) for key in state.get("ignored_result_keys") or [] if key
            } | {str(row["key"]) for row in counted}
            counted.extend(row for row in readable if row["key"] not in known)
            state["counted_results"] = counted
        if state != previous:
            club["manager_record_state"] = state
        return state

    state = _update_portfolio(mutate, scope_id)
    if manager_id <= 0:
        return None

    counted_rows = [
        row for row in state.get("counted_results") or []
        if isinstance(row, dict) and str(row.get("result") or "") in {"胜", "平", "负"}
    ]
    season_rows = [row for row in counted_rows if str(row.get("key") or "") in current_keys]
    return {
        "manager_id": manager_id,
        "season": _record_summary(season_rows),
        "total": _record_summary(counted_rows),
        "started_at": state.get("started_at"),
        "source": "available_club_results",
    }


def _club_fixture_cache_key(
    pid: int, module_base: int, layout: Any, save_identity: str, team_id: int,
) -> tuple[Any, ...]:
    return (
        int(pid), int(module_base), str(layout.key), str(layout.distribution),
        str(layout.executable_sha256), str(getattr(layout, "game_version", "") or ""),
        str(save_identity or ""), int(team_id),
    )


def _target_club_fixture_snapshot(
    reader: Reader, addresses: list[int] | tuple[int, ...], team_address: int,
    game_date: Any, *, validate_cached: bool = False,
) -> tuple[set[tuple[str, int, int, int]], dict[tuple[str, int, int, int], int], list[int], bool]:
    required: set[tuple[str, int, int, int]] = set()
    hints: dict[tuple[str, int, int, int], int] = {}
    target_addresses: list[int] = []
    for address in addresses:
        fixture = parse_fixture(reader, int(address))
        if not fixture:
            if validate_cached:
                return set(), {}, [], False
            continue
        if team_address not in {int(fixture.home_team), int(fixture.away_team)}:
            if validate_cached:
                return set(), {}, [], False
            continue
        target_addresses.append(int(address))
        if game_date and fixture.match_date > game_date:
            continue
        competition = reader.competition(fixture.competition_season) or {}
        home = reader.team(fixture.home_team) or {}
        away = reader.team(fixture.away_team) or {}
        competition_id = int(competition.get("id") or 0)
        home_id = int(home.get("id") or 0)
        away_id = int(away.get("id") or 0)
        if competition_id <= 0 or home_id <= 0 or away_id <= 0:
            if validate_cached:
                return set(), {}, [], False
            continue
        key = (fixture.match_date.isoformat(), competition_id, home_id, away_id)
        required.add(key)
        hints[key] = int(address)
    return required, hints, target_addresses, True


def read_native_world_club_results(
    club: dict[str, Any], *, save_identity: str = "", force_refresh: bool = False,
) -> dict[str, Any]:
    """Read one club's completed matches independently of the odds scope."""
    from tools.preview_cup_odds import read_completed_results

    pid, _path, layout = select_process_layout()
    fixture_pool_bytes_scanned = 0
    fixture_cache_hit = False
    with open_process(pid) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} not loaded")
        reader = Reader(process, module.base_address, layout)
        team_address, team = _resolve_world_club_team(reader, club)
        team_id = int(team.get("id") or 0)
        game_date = (
            decode_date(reader.u32(module.base_address + int(layout.game_date_rva)) or 0)
            if layout.game_date_rva is not None else None
        )
        game_date_text = game_date.isoformat() if game_date else ""
        cache_key = _club_fixture_cache_key(
            pid, module.base_address, layout, save_identity, team_id,
        )
        with _CLUB_FIXTURE_CACHE_LOCK:
            cached = dict(_CLUB_FIXTURE_CACHE.get(cache_key) or {})
        required: set[tuple[str, int, int, int]] = set()
        hints: dict[tuple[str, int, int, int], int] = {}
        target_addresses: list[int] = []
        if (
            cached and not force_refresh
            and cached.get("game_date") == game_date_text
            and int(cached.get("team_address") or 0) == team_address
        ):
            required, hints, target_addresses, fixture_cache_hit = _target_club_fixture_snapshot(
                reader, tuple(cached.get("fixture_addresses") or ()), team_address,
                game_date, validate_cached=True,
            )
        if not fixture_cache_hit:
            fixture_addresses, fixture_pool_bytes_scanned = scan_fixture_addresses(reader)
            required, hints, target_addresses, _valid = _target_club_fixture_snapshot(
                reader, fixture_addresses, team_address, game_date,
            )
            with _CLUB_FIXTURE_CACHE_LOCK:
                for old_key in list(_CLUB_FIXTURE_CACHE):
                    if int(old_key[0]) != int(pid):
                        _CLUB_FIXTURE_CACHE.pop(old_key, None)
                _CLUB_FIXTURE_CACHE[cache_key] = {
                    "game_date": game_date_text,
                    "team_address": team_address,
                    "fixture_addresses": tuple(target_addresses),
                }
                while len(_CLUB_FIXTURE_CACHE) > 256:
                    _CLUB_FIXTURE_CACHE.pop(next(iter(_CLUB_FIXTURE_CACHE)))
        fixture_count = len(target_addresses)
    completed = read_completed_results(required, fixture_hints=hints)
    rows = []
    for item in completed:
        home = item.get("home") or item.get("home_team") or {}
        away = item.get("away") or item.get("away_team") or {}
        home_id = int(home.get("id") or 0)
        away_id = int(away.get("id") or 0)
        if team_id not in {home_id, away_id}:
            continue
        home_goals = item.get("home_goals")
        away_goals = item.get("away_goals")
        if home_goals is None or away_goals is None:
            continue
        is_home = home_id == team_id
        goals_for = int(home_goals if is_home else away_goals)
        goals_against = int(away_goals if is_home else home_goals)
        competition = item.get("competition") or {}
        rows.append({
            "date": str(item.get("date") or ""),
            "result": "胜" if goals_for > goals_against else "负" if goals_for < goals_against else "平",
            "score": f"{int(home_goals)}-{int(away_goals)}",
            "opponent": away.get("name") if is_home else home.get("name"),
            "competition": item.get("competition_name") or competition.get("short_name") or competition.get("name"),
            "home_away": "主" if is_home else "客",
        })
    rows.sort(key=lambda row: row["date"], reverse=True)
    return {
        "team_id": team_id,
        "fixture_count": fixture_count,
        "completed_count": len(rows),
        "results": rows,
        "recent_results": rows[:5],
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "source": "target_club_fixture_pool_and_persistent_results",
        "fixture_cache_hit": fixture_cache_hit,
        "fixture_cache_refreshed": not fixture_cache_hit,
        "fixture_pool_bytes_scanned": fixture_pool_bytes_scanned,
    }


def _result_form(output: dict[str, Any]) -> dict[int, list[dict[str, Any]]]:
    forms: dict[int, list[dict[str, Any]]] = {}
    rows = sorted(output.get("season_results") or [], key=lambda row: str(row.get("date") or ""))
    for row in rows:
        home = row.get("home") or {}
        away = row.get("away") or {}
        home_id, away_id = int(home.get("id") or 0), int(away.get("id") or 0)
        home_goals, away_goals = row.get("home_goals"), row.get("away_goals")
        if home_id <= 0 or away_id <= 0 or home_goals is None or away_goals is None:
            continue
        if int(home_goals) > int(away_goals):
            outcome = {home_id: "胜", away_id: "负"}
        elif int(home_goals) < int(away_goals):
            outcome = {home_id: "负", away_id: "胜"}
        else:
            outcome = {home_id: "平", away_id: "平"}
        for team_id, result in outcome.items():
            forms.setdefault(team_id, []).append({
                "date": row.get("date"), "result": result,
                "score": f"{int(home_goals)}-{int(away_goals)}",
                "opponent": away.get("name") if team_id == home_id else home.get("name"),
                "competition": row.get("competition_name"),
            })
    return {team_id: rows[-5:][::-1] for team_id, rows in forms.items()}


def build_world_clubs(output: dict[str, Any]) -> dict[str, Any]:
    clubs: dict[int, dict[str, Any]] = {}
    league_team_ids: set[int] = set()
    cup_rows: list[tuple[dict[str, Any], str, str, str]] = []
    for competition in output.get("competition_formats") or []:
        competition_id = int(competition.get("competition_id") or competition.get("id") or 0) or None
        competition_name = str(competition.get("competition_name") or "未知赛事")
        competition_kind = str(competition.get("competition_kind") or "")
        if competition_kind not in {"league", "cup"}:
            continue
        continent, nation = _COMPETITION_REGIONS.get(competition_name, ("世界", "未分类"))
        for stage in competition.get("stages") or []:
            for team in stage.get("teams") or []:
                team_id = int(team.get("id") or 0)
                if team_id <= 0 or _looks_non_mens(
                    str(team.get("name") or ""), competition_name,
                ):
                    continue
                if competition_kind == "cup":
                    cup_rows.append((team, competition_name, continent, nation))
                    continue
                league_team_ids.add(team_id)
                row = clubs.setdefault(team_id, {
                    "id": team_id,
                    "team_type": "club",
                    "name": team.get("name") or str(team_id),
                    "address": team.get("address"),
                    "reputation": int(team.get("reputation") or 0),
                    "continent": continent,
                    "nation": nation,
                    "competition": competition_name,
                    "competition_id": competition_id,
                    "competitions": [],
                })
                row["reputation"] = max(int(row.get("reputation") or 0), int(team.get("reputation") or 0))
                row["competitions"] = sorted(set(row["competitions"] + [competition_name]))
    for team, competition_name, continent, nation in cup_rows:
        team_id = int(team.get("id") or 0)
        if team_id not in league_team_ids:
            continue
        row = clubs[team_id]
        row["competitions"] = sorted(set(row["competitions"] + [competition_name]))
    forms = _result_form(output)
    for team_id, row in clubs.items():
        row["recent_results"] = forms.get(team_id, [])
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "clubs": sorted(clubs.values(), key=lambda row: (-int(row.get("reputation") or 0), str(row.get("name") or "").casefold())),
    }


def merge_native_world_clubs(directory: dict[str, Any], native: dict[str, Any] | None) -> dict[str, Any]:
    """Combine native coverage with live competition results and standings."""
    if not native or not isinstance(native.get("clubs"), list):
        return directory
    clubs: dict[int, dict[str, Any]] = {}
    for source_row in directory.get("clubs", []):
        team_id = int(source_row.get("id") or 0)
        if team_id <= 0:
            continue
        row = dict(source_row)
        row["nation"] = _world_club_nation_name(row.get("nation"))
        clubs[team_id] = row
    for source_row in native["clubs"]:
        native_row = dict(source_row)
        native_row["nation"] = _world_club_nation_name(native_row.get("nation"))
        team_id = int(native_row.get("id") or 0)
        if team_id <= 0 or _looks_non_mens(
            str(native_row.get("name") or ""),
            str(native_row.get("competition") or ""),
        ):
            continue
        current = clubs.get(team_id)
        if current is None:
            clubs[team_id] = dict(native_row)
            continue
        current["team_type"] = native_row.get("team_type") or current.get("team_type") or "club"
        current["squad_type_code"] = native_row.get("squad_type_code", current.get("squad_type_code"))
        current["name"] = native_row.get("name") or current.get("name")
        current["short_name"] = native_row.get("short_name") or current.get("short_name")
        current["address"] = native_row.get("address") or current.get("address")
        current["reputation"] = int(native_row.get("reputation") or current.get("reputation") or 0)
        current["manager"] = native_row.get("manager") or current.get("manager") or "待读取"
        current["balance"] = native_row.get("balance", current.get("balance"))
        current["source"] = "native_scan"
        current["competitions"] = sorted(set(
            list(current.get("competitions") or []) + list(native_row.get("competitions") or [])
        ))
        # The competition snapshot already knows which domestic league wins
        # over a cup. Keep that choice when it exists.
        if not current.get("competition") or current.get("competition") == "未分类":
            current["competition"] = native_row.get("competition") or current.get("competition")
            current["competition_id"] = native_row.get("competition_id") or current.get("competition_id")
        if current.get("nation") in {None, "未分类", "未知"}:
            current["nation"] = native_row.get("nation") or current.get("nation")
        if current.get("continent") in {None, "世界", "未分类"}:
            current["continent"] = native_row.get("continent") or current.get("continent")
    return {
        "generated_at": directory.get("generated_at") or datetime.now().isoformat(timespec="seconds"),
        "clubs": sorted(clubs.values(), key=lambda row: (-int(row.get("reputation") or 0), str(row.get("name") or "").casefold())),
    }


def apply_acquired_club_overrides(
    directory: dict[str, Any], acquired: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Apply distinct account-local full/short names to a copied directory."""
    overrides = {
        int(row.get("id") or 0): {
            "name": str(row.get("renamed_name") or "").strip(),
            "short_name": str(
                row.get("renamed_short_name") or row.get("renamed_name") or ""
            ).strip(),
        }
        for row in acquired or []
        if int(row.get("id") or 0) > 0
        and (
            str(row.get("renamed_name") or "").strip()
            or str(row.get("renamed_short_name") or "").strip()
        )
    }
    if not overrides:
        return directory
    rows = []
    for source_row in directory.get("clubs") or []:
        row = dict(source_row)
        renamed = overrides.get(int(row.get("id") or 0))
        if renamed:
            if renamed["name"]:
                row["name"] = renamed["name"]
            if renamed["short_name"]:
                row["short_name"] = renamed["short_name"]
            row["display_name"] = (
                renamed["short_name"] or renamed["name"]
                or row.get("short_name") or row.get("name")
            )
            row["name_overridden"] = True
        rows.append(row)
    return {**directory, "clubs": rows}


def enrich_world_club_standings(
    directory: dict[str, Any], standings: dict[str, Any] | None,
) -> dict[str, Any]:
    """Attach a rank only when the club's selected competition has a league table."""
    table_index: dict[tuple[int, int], dict[str, int]] = {}
    tables_by_team: dict[int, list[dict[str, Any]]] = {}
    for competition in (standings or {}).get("competitions") or []:
        competition_id = int(competition.get("competition_id") or 0)
        competition_name = str(competition.get("competition_name") or "")
        teams = list(competition.get("teams") or [])
        if competition_id <= 0 or not teams:
            continue
        team_count = len(teams)
        for team in teams:
            team_id = int(team.get("team_id") or 0)
            position = int(team.get("position") or 0)
            if team_id <= 0 or not 1 <= position <= team_count:
                continue
            standing = {
                "competition_id": competition_id,
                "competition_name": competition_name,
                "league_position": position,
                "league_team_count": team_count,
                "league_points": int(team.get("points") or 0),
            }
            table_index[(competition_id, team_id)] = standing
            tables_by_team.setdefault(team_id, []).append(standing)
    rows = []
    for club in directory.get("clubs") or []:
        row = dict(club)
        row.pop("league_position", None)
        row.pop("league_team_count", None)
        row.pop("league_points", None)
        key = (int(row.get("competition_id") or 0), int(row.get("id") or 0))
        standing = table_index.get(key)
        if standing is None:
            nation = str(row.get("nation") or "")
            domestic = [
                candidate for candidate in tables_by_team.get(key[1], [])
                if _COMPETITION_REGIONS.get(
                    str(candidate["competition_name"]), (None, None),
                )[1] == nation
            ]
            if len(domestic) == 1:
                standing = domestic[0]
                row["competition_id"] = int(standing["competition_id"])
                row["competition"] = str(standing["competition_name"])
        if standing is not None:
            row.update({
                "league_position": int(standing["league_position"]),
                "league_team_count": int(standing["league_team_count"]),
                "league_points": int(standing["league_points"]),
            })
        rows.append(row)
    return {**directory, "clubs": rows}


def enrich_world_club_reputation_ranks(directory: dict[str, Any]) -> dict[str, Any]:
    """Attach reputation ranks within each club's current league and nation."""
    rows = [dict(row) for row in directory.get("clubs") or []]
    league_groups: dict[tuple[str, Any], list[dict[str, Any]]] = {}
    nation_groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        reputation = int(row.get("reputation") or 0)
        if reputation <= 0:
            continue
        competition_id = int(row.get("competition_id") or 0)
        competition_name = str(row.get("competition") or "").strip()
        if competition_id > 0:
            league_key = ("id", competition_id)
        elif competition_name and competition_name not in {"未分类", "未知赛事"}:
            league_key = ("name", competition_name)
        else:
            league_key = None
        if league_key is not None:
            league_groups.setdefault(league_key, []).append(row)
        nation = _world_club_nation_name(row.get("nation"))
        if nation and nation not in {"未分类", "未知", "世界"}:
            nation_groups.setdefault(nation, []).append(row)

    def attach(
        groups: dict[Any, list[dict[str, Any]]],
        rank_key: str,
        total_key: str,
    ) -> None:
        for group in groups.values():
            ranked = sorted(
                group,
                key=lambda row: (
                    -int(row.get("reputation") or 0),
                    str(row.get("name") or "").casefold(),
                    int(row.get("id") or 0),
                ),
            )
            total = len(ranked)
            previous_reputation: int | None = None
            previous_rank = 0
            for index, row in enumerate(ranked, start=1):
                reputation = int(row.get("reputation") or 0)
                if reputation != previous_reputation:
                    previous_rank = index
                    previous_reputation = reputation
                row[rank_key] = previous_rank
                row[total_key] = total

    attach(
        league_groups,
        "competition_reputation_rank",
        "competition_reputation_total",
    )
    attach(nation_groups, "nation_reputation_rank", "nation_reputation_total")
    return {**directory, "clubs": rows}


def build_world_club_directory_index(
    directory: dict[str, Any],
) -> dict[str, Any]:
    """Prepare immutable lookup structures for repeated filtering and paging."""
    all_clubs: list[dict[str, Any]] = []
    assigned_clubs: list[dict[str, Any]] = []
    clubs_by_id: dict[int, dict[str, Any]] = {}
    clubs_by_continent: dict[str, list[dict[str, Any]]] = {}
    clubs_by_nation: dict[str, list[dict[str, Any]]] = {}
    clubs_by_region: dict[tuple[str, str], list[dict[str, Any]]] = {}
    collision_groups: dict[str, list[dict[str, Any]]] = {}
    facet_counts: dict[str, dict[str, int]] = {}
    reputation_ranks: dict[int, int] = {}
    unassigned = {"", "未分类", "未知", "世界"}

    for index, source_row in enumerate(directory.get("clubs") or [], start=1):
        row = dict(source_row)
        row["nation"] = _world_club_nation_name(row.get("nation"))
        all_clubs.append(row)
        team_id = int(row.get("id") or 0)
        if team_id > 0:
            clubs_by_id[team_id] = row
            reputation_ranks[team_id] = index
        name_key = str(row.get("name") or "").strip().casefold()
        if name_key:
            collision_groups.setdefault(name_key, []).append(row)
        continent_name = str(row.get("continent") or "").strip()
        nation_name = str(row.get("nation") or "").strip()
        if continent_name in unassigned or nation_name in unassigned:
            continue
        assigned_clubs.append(row)
        clubs_by_continent.setdefault(continent_name, []).append(row)
        clubs_by_nation.setdefault(nation_name, []).append(row)
        clubs_by_region.setdefault((continent_name, nation_name), []).append(row)
        nations = facet_counts.setdefault(continent_name, {})
        nations[nation_name] = nations.get(nation_name, 0) + 1

    facets = [
        {
            "name": continent_name,
            "count": sum(nations.values()),
            "nations": [
                {"name": nation_name, "count": count}
                for nation_name, count in sorted(nations.items())
            ],
        }
        for continent_name, nations in sorted(facet_counts.items())
    ]
    return {
        "clubs": all_clubs,
        "assigned_clubs": assigned_clubs,
        "clubs_by_id": clubs_by_id,
        "clubs_by_continent": clubs_by_continent,
        "clubs_by_nation": clubs_by_nation,
        "clubs_by_region": clubs_by_region,
        "collision_groups": collision_groups,
        "reputation_ranks": reputation_ranks,
        "facets": facets,
    }


def paginate_world_clubs(
    directory: dict[str, Any], *, search: str = "", continent: str = "",
    nation: str = "", page: int = 1, page_size: int = 15,
    directory_index: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Filter and paginate the native directory without sending it all to the browser."""
    prepared = directory_index or build_world_club_directory_index(directory)
    all_clubs = prepared["clubs"]
    nation = _world_club_nation_name(nation)
    search_terms = _world_club_search_terms(search)
    if search_terms:
        ranked: list[tuple[int, dict[str, Any]]] = []
        for club in all_clubs:
            if continent and str(club.get("continent") or "") != continent:
                continue
            if nation and str(club.get("nation") or "") != nation:
                continue
            rank = _world_club_search_rank(club, search_terms)
            if rank is not None:
                ranked.append((rank, club))
        ranked.sort(key=lambda item: (
            item[0], -int(item[1].get("reputation") or 0),
            str(item[1].get("name") or "").casefold(), int(item[1].get("id") or 0),
        ))
        filtered = [club for _rank, club in ranked]
    elif continent and nation:
        filtered = prepared["clubs_by_region"].get((continent, nation), [])
    elif continent:
        filtered = prepared["clubs_by_continent"].get(continent, [])
    elif nation:
        filtered = prepared["clubs_by_nation"].get(nation, [])
    else:
        filtered = prepared["assigned_clubs"]
    page_size = max(1, min(15, int(page_size or 15)))
    total = len(filtered)
    page_count = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(int(page or 1), page_count))
    start = (page - 1) * page_size
    collision_groups = prepared["collision_groups"]
    reputation_ranks = prepared["reputation_ranks"]
    page_clubs = [
        {
            **club,
            "display_name": _world_club_display_name(club, collision_groups),
            "reputation_rank": reputation_ranks.get(int(club.get("id") or 0)),
            "competition_count": len(club.get("competitions") or []),
        }
        for club in filtered[start:start + page_size]
    ]
    return {
        "clubs": page_clubs,
        "pagination": {
            "page": page, "page_size": page_size, "page_count": page_count,
            "total": total, "from": start + 1 if total else 0,
            "to": min(start + page_size, total),
        },
        "facets": [
            {
                "name": row["name"],
                "count": row["count"],
                "nations": [
                    dict(nation) for nation in row["nations"]
                ],
            }
            for row in prepared["facets"]
        ],
    }


def paginate_world_nations(
    nations: list[dict[str, Any]] | None, *, search: str = "", continent: str = "",
    page: int = 1, page_size: int = 15,
) -> dict[str, Any]:
    """Filter and paginate the scanned country directory."""
    rows = [dict(row) for row in nations or [] if int(row.get("id") or 0) > 0]
    query = str(search or "").strip().casefold()
    facets: dict[str, int] = {}
    for row in rows:
        region = str(row.get("continent") or "未分类")
        facets[region] = facets.get(region, 0) + 1
    filtered = [
        row for row in rows
        if (not continent or str(row.get("continent") or "") == continent)
        and (not query or any(
            query in str(value or "").casefold()
            for value in (row.get("name"), row.get("continent"), row.get("id"))
        ))
    ]
    page_size = max(1, min(15, int(page_size or 15)))
    total = len(filtered)
    page_count = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(int(page or 1), page_count))
    start = (page - 1) * page_size
    page_rows = [
        {**row, "youth_rating_max": 200}
        for row in filtered[start:start + page_size]
    ]
    return {
        "nations": page_rows,
        "pagination": {
            "page": page, "page_size": page_size, "page_count": page_count,
            "total": total, "from": start + 1 if total else 0,
            "to": min(start + page_size, total),
        },
        "facets": [
            {"name": name, "count": count}
            for name, count in sorted(facets.items())
        ],
    }


def _related_club_squads(
    reader: Reader, first_team_address: int,
) -> list[tuple[int, dict[str, Any]]]:
    """Return manageable native Team objects sharing the first team's Club."""
    type_offset = getattr(reader.layout, "team_type_offset", None)
    first_team = reader.team(first_team_address)
    if not first_team:
        return []
    if type_offset is None:
        return [(first_team_address, first_team)]
    club_address = int(reader.ptr(first_team_address + TEAM_CLUB) or 0)
    if not club_address:
        return [(first_team_address, first_team)]
    first_type_code = reader.u8(first_team_address + int(type_offset))
    if first_type_code is None or int(first_type_code) != 0:
        return [(first_team_address, first_team)]

    candidates: tuple[int, ...] = ()
    read_bytes = getattr(reader, "bytes", None)
    read_pointers = getattr(reader, "ptr_array", None)
    if callable(read_bytes) and callable(read_pointers):
        header = read_bytes(club_address + 0x18, 24)
        if header and len(header) == 24:
            begin, end, capacity = struct.unpack("<QQQ", header)
            count = (end - begin) // 8 if begin <= end else -1
            if (
                begin and begin <= end <= capacity and not (end - begin) % 8
                and 1 <= count <= 16
            ):
                linked = tuple(
                    int(address) for address in read_pointers(begin, count)
                    if address
                )
                if first_team_address in linked:
                    candidates = linked
    if not candidates:
        directory = database_index_for_reader(reader)
        if directory is not None:
            candidates = directory.addresses_for_vtable(
                "team", int(reader.layout.team_vtable_rva),
            )
    if not candidates:
        return [(first_team_address, first_team)]
    rows: list[tuple[int, dict[str, Any]]] = []
    seen: set[int] = set()
    for address in candidates:
        address = int(address)
        if address in seen or reader.ptr(address + TEAM_CLUB) != club_address:
            continue
        raw_type_code = reader.u8(address + int(type_offset))
        if raw_type_code is None:
            continue
        type_code = int(raw_type_code)
        if type_code not in MANAGEABLE_CLUB_TEAM_TYPES:
            continue
        team = reader.team(address)
        if not team:
            continue
        rows.append((address, team))
        seen.add(address)
    if first_team_address not in seen:
        rows.append((first_team_address, first_team))
    order = {code: index for index, code in enumerate(
        (0, 1, 2, 3, 16, 15, 17, 9, 10, 18, 11, 12, 30, 13, 14)
    )}
    return sorted(rows, key=lambda item: (
        order.get(int(item[1].get("squad_type_code") or 0), 99),
        int(item[1].get("id") or 0),
    ))


def _resolve_world_club_team(
    reader: Reader, club: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    """Resolve a world-club Team by UID, retaining the legacy address path.

    World-club rows are cached across reads, so their public address is only a
    session hint.  The database index is the preferred source of a current
    Team -> Club object pair; the old address is still tried when the index is
    unavailable or a reader does not expose the newer resolver primitives.
    The existing men's club type and Team UID checks remain mandatory for both
    paths.
    """
    team_id = int(club.get("id") or 0)
    try:
        hint = int(str(club.get("address") or "0"), 16)
    except (TypeError, ValueError):
        hint = 0
    candidates: list[int] = []
    resolver_error: Exception | None = None
    if team_id > 0:
        try:
            resolved = resolve_team_club(reader, hint, team_id)
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as error:
            # Keep the previous validated-address behavior available for old
            # readers and for transient directory failures.
            resolver_error = error
            resolved = None
        if resolved is not None:
            try:
                resolved_address = int(resolved.team_address)
            except (AttributeError, TypeError, ValueError):
                resolved_address = 0
            if resolved_address > 0:
                candidates.append(resolved_address)
    if hint > 0 and hint not in candidates:
        candidates.append(hint)
    layout = getattr(reader, "layout", None)
    pointer_reader = getattr(reader, "ptr", None)
    expected_team_rva = getattr(layout, "team_vtable_rva", None)
    expected_club_rva = getattr(layout, "club_vtable_rva", None)
    module_base = int(getattr(reader, "module_base", 0) or 0)
    for address in candidates:
        try:
            team = reader.team(address)
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
            continue
        if (
            team
            and int(team.get("id") or 0) == team_id
            # ``read_native_world_club_results`` historically received a
            # minimal Team projection without ``team_type`` in lightweight
            # readers.  A real Reader always supplies it; preserve that
            # compatibility while rejecting an explicit non-club object.
            and team.get("team_type") in (None, "club")
        ):
            # Full Readers expose both typed vtable layouts and the Team ->
            # Club edge.  Keep those gates on the fallback too; lightweight
            # test/readers that intentionally expose only a Team projection
            # do not claim a vtable and therefore retain the old behavior.
            if callable(pointer_reader) and expected_team_rva is not None:
                if int(pointer_reader(address) or 0) != module_base + int(expected_team_rva):
                    continue
            if callable(pointer_reader) and expected_club_rva is not None:
                club_address = int(pointer_reader(address + TEAM_CLUB) or 0)
                if (
                    not club_address
                    or int(pointer_reader(club_address) or 0)
                    != module_base + int(expected_club_rva)
                ):
                    continue
            return int(address), team
    raise RuntimeError("俱乐部地址已失效，请重新扫描俱乐部") from resolver_error


def read_native_world_club_detail(
    club: dict[str, Any], *, include_related_squads: bool = False,
) -> dict[str, Any]:
    """Read one club's roster, facilities and finances on demand."""
    from tools.club_reader import _club_information, read_roster_player_profile

    with borrow_game_reader() as reader:
        layout = reader.layout
        module = reader.module
        team_address, team = _resolve_world_club_team(reader, club)
        team_id = int(team.get("id") or 0)
        information = _club_information(reader, team_address)
        roster = reader.roster(team_address)
        manager_person = 0
        try:
            manager_address = int(str(team.get("manager_address") or "0"), 16)
        except (TypeError, ValueError):
            manager_address = 0
        staff_offset = getattr(
            layout, "team_manager_person_offset",
            getattr(layout, "staff_complete_object_offset", None),
        )
        if manager_address and staff_offset is not None:
            manager_person = manager_address + int(staff_offset)
            expected_staff_vtable = (
                module.base_address + int(layout.staff_person_vtable_rva)
                if layout.staff_person_vtable_rva is not None else 0
            )
            if expected_staff_vtable and reader.ptr(manager_person) != expected_staff_vtable:
                manager_person = 0
        game_date = (
            decode_date(reader.u32(module.base_address + int(layout.game_date_rva)) or 0)
            if layout.game_date_rva is not None else None
        )
        valuation = club_acquisition_valuation(
            information, roster, team.get("reputation"), club.get("competition"),
        )
        squad_teams = (
            _related_club_squads(reader, team_address)
            if include_related_squads else [(team_address, team)]
        )
        squads: list[dict[str, Any]] = []
        player_by_id: dict[int, dict[str, Any]] = {}
        first_team_players: list[dict[str, Any]] = []
        for squad_address, squad_team in squad_teams:
            squad_roster = reader.roster(squad_address)
            squad_players: list[dict[str, Any]] = []
            type_code = squad_team.get("squad_type_code")
            type_name = (
                squad_team.get("squad_type_name")
                or CLUB_TEAM_TYPE_NAMES.get(type_code)
                or ("一线队" if squad_address == team_address else f"球队 {squad_team.get('id')}")
            )
            for row in squad_roster:
                player = read_roster_player_profile(
                    reader, row, squad_address, game_date, manager_person,
                )
                if not player:
                    continue
                try:
                    player_address = int(str(player.get("address") or "0"), 0)
                except (TypeError, ValueError):
                    player_address = 0
                enriched = dict(player)
                if include_related_squads:
                    enriched.update({
                        "squad_team_id": int(squad_team.get("id") or 0),
                        "squad_team_address": hex(squad_address),
                        "squad_type_code": type_code,
                        "squad_type": type_name,
                        "squad_label": type_name,
                        "squad_is_current": bool(
                            player_address
                            and reader.ptr(player_address + 0x130) == squad_address
                        ),
                    })
                squad_players.append(enriched)
                player_id = int(enriched.get("id") or 0)
                previous = player_by_id.get(player_id)
                if player_id and (
                    previous is None
                    or enriched.get("squad_is_current") and not previous.get("squad_is_current")
                ):
                    player_by_id[player_id] = enriched
            if squad_address == team_address:
                first_team_players = squad_players
            squads.append({
                "team_id": int(squad_team.get("id") or 0),
                "team_address": hex(squad_address),
                "type_code": type_code,
                "type": type_name,
                "label": type_name,
                "player_count": len(squad_players),
                "players": squad_players,
            })
        players = list(player_by_id.values())
        ca_values = [int(row["ca"]) for row in roster if row.get("ca") is not None]
        portfolio_metrics = club_portfolio_metrics(
            first_team_players, information, valuation,
        )
        club_identity = {
            **club,
            "address": hex(team_address),
            "reputation": team.get("reputation"),
            "average_ca": round(sum(ca_values) / len(ca_values), 1) if ca_values else None,
        }
        information_nation = (information or {}).get("nation") or {}
        information_nation_id = int(information_nation.get("id") or 0)
        if information_nation_id > 0:
            club_identity["nation_id"] = information_nation_id
            club_identity["nation"] = (
                str(information_nation.get("name") or "").strip()
                or club_identity.get("nation")
            )
        result = {
            "club": club_identity,
            "manager": None,
            "players": players,
            "staff": None,
            "staff_loading": True,
            "club_information": information,
            "acquisition": valuation,
            "portfolio_metrics": portfolio_metrics,
        }
        if include_related_squads:
            result["squads"] = squads
        return result


def read_native_world_club_snapshot(club: dict[str, Any]) -> dict[str, Any]:
    """Read only the live club fields required to validate a write.

    Unlike the detail endpoint this deliberately skips roster expansion,
    per-player profiles, valuation and staff.  It also borrows the persistent
    read-only game session shared by FM24 and FM26.
    """
    from tools.club_reader import _club_information

    with borrow_game_reader() as reader:
        team_address, team = _resolve_world_club_team(reader, club)
        team_id = int(team.get("id") or 0)
        return {
            "club": {
                **club,
                "name": team.get("name") or club.get("name"),
                "short_name": team.get("short_name") or club.get("short_name"),
                "reputation": team.get("reputation"),
                "address": hex(team_address),
            },
            "club_information": _club_information(reader, team_address),
            "session": {
                "pid": int(reader.process.pid),
                "game_key": str(reader.layout.key),
                "module_base": int(reader.module_base),
            },
        }


def read_native_world_club_balances(
    clubs: list[dict[str, Any]],
) -> dict[int, dict[str, Any]]:
    """Read only current balances for a bounded set of validated clubs.

    Sponsorship pricing needs a league-wide average, but must not expand every
    club's roster or persist volatile addresses.  Each result is keyed by the
    stable Team UID and includes only the value required by the caller.
    """
    from tools.club_balance import read_club_balance

    results: dict[int, dict[str, Any]] = {}
    with borrow_game_operation() as operation:
        for club in clubs or []:
            team_id = int(club.get("id") or 0)
            if team_id <= 0:
                continue
            try:
                balance = read_club_balance(
                    club.get("address"), team_id, operation=operation,
                )
                results[team_id] = {
                    "team_id": team_id,
                    "team_name": str(club.get("name") or team_id),
                    "balance": int(balance["amount"]),
                }
            except Exception as error:
                results[team_id] = {
                    "team_id": team_id,
                    "team_name": str(club.get("name") or team_id),
                    "balance": None,
                    "error": str(error),
                }
    return results


def read_native_world_club_acquisition_quote(club: dict[str, Any]) -> dict[str, Any]:
    """Read only the live fields required to price an acquisition."""
    from tools.club_reader import _club_information

    with borrow_game_reader() as reader:
        team_address, team = _resolve_world_club_team(reader, club)
        team_id = int(team.get("id") or 0)
        information = _club_information(reader, team_address)
        roster = reader.roster_valuation(team_address)
        return {
            "club": {
                **club,
                "name": team.get("name") or club.get("name"),
                "short_name": team.get("short_name") or club.get("short_name"),
                "reputation": team.get("reputation"),
                "address": hex(team_address),
            },
            "acquisition": club_acquisition_valuation(
                information, roster, team.get("reputation"),
                club.get("competition"),
            ),
        }


def read_native_world_club_player_membership(
    club: dict[str, Any], player_id: int,
) -> dict[str, Any] | None:
    """Validate one player against club rosters without expanding profiles."""
    player_id = int(player_id)
    with borrow_game_reader() as reader:
        team_address, team = _resolve_world_club_team(reader, club)
        team_id = int(team.get("id") or 0)
        for squad_address, squad in _related_club_squads(reader, team_address):
            for row in reader.roster(squad_address):
                if int(row.get("id") or 0) != player_id:
                    continue
                return {
                    **dict(row),
                    "squad_team_id": int(squad.get("id") or 0),
                    "squad_team_address": hex(squad_address),
                }
    return None


def read_native_world_club_metrics(
    clubs: list[dict[str, Any]],
    progress: Callable[[int, str], None] | None = None,
) -> dict[int, dict[str, Any]]:
    """Read portfolio-card metrics for many clubs in one process session."""
    from tools.club_reader import _club_information

    results: dict[int, dict[str, Any]] = {}
    with borrow_game_reader() as reader:
        total = max(1, len(clubs))
        for index, club in enumerate(clubs):
            team_id = int(club.get("id") or 0)
            try:
                team_address, team = _resolve_world_club_team(reader, club)
                team_id = int(team.get("id") or 0)
                information = _club_information(reader, team_address)
                try:
                    roster = reader.roster_valuation(team_address)
                    valuation = club_acquisition_valuation(
                        information, roster, team.get("reputation"), club.get("competition"),
                    )
                except Exception as error:
                    metrics = club_portfolio_metrics([], information, None)
                    metrics.update({
                        "partial": True,
                        "errors": {"valuation": str(error)},
                    })
                    results[team_id] = metrics
                else:
                    results[team_id] = club_portfolio_metrics(
                        roster, information, valuation,
                    )
                results[team_id]["reputation"] = team.get("reputation")
            except Exception as error:
                results[team_id] = {"unavailable": True, "error": str(error)}
            if progress:
                progress(
                    int((index + 1) * 100 / total),
                    f"正在读取俱乐部指标（{index + 1}/{len(clubs)}）",
                )
    return results


def read_native_world_club_staff(club: dict[str, Any]) -> dict[str, Any]:
    """Read the expensive global staff pool separately from the club overview."""
    from tools.club_reader import _scan_staff

    with borrow_game_reader() as reader:
        team_address, team = _resolve_world_club_team(reader, club)
        staff = _scan_staff(reader, team_address, 0)
        return _world_club_staff_summary(reader, team, staff)


def _world_club_staff_summary(
    reader: Reader, team: dict[str, Any], staff: list[dict[str, Any]],
) -> dict[str, Any]:
    from tools.club_reader import _name

    layout = reader.layout
    module = reader.module
    manager = None
    try:
        manager_address = int(str(team.get("manager_address") or "0"), 16)
    except (TypeError, ValueError):
        manager_address = 0
    manager_person = (
        manager_address + int(getattr(
            layout, "team_manager_person_offset", layout.staff_complete_object_offset,
        ))
        if manager_address else 0
    )
    expected_staff_vtable = (
        module.base_address + int(layout.staff_person_vtable_rva)
        if layout.staff_person_vtable_rva is not None else 0
    )
    if (
        manager_person and expected_staff_vtable
        and reader.ptr(manager_person) == expected_staff_vtable
    ):
        manager_id = int(reader.u32(manager_person + ENTITY_UID) or 0)
        manager_name = _name(reader, manager_person)
        if manager_id and manager_name:
            manager = next(
                (row for row in staff if int(row.get("id") or 0) == manager_id),
                {
                    "id": manager_id, "name": manager_name,
                    "address": hex(manager_person),
                },
            )
    if manager is None:
        manager = next(
            (
                row for row in staff
                if row.get("role") == "主教练"
                or int(row.get("job_type") if row.get("job_type") is not None else -1) in {0, 1, 16}
            ),
            None,
        )
    director = next(
        (row for row in staff if row.get("role") == "足球总监" or int(row.get("job_type") or 0) == 10),
        None,
    )
    return {
        "manager": (
            {
                "id": int(manager.get("id") or 0),
                "name": manager.get("name"),
                "contract_start_date": manager.get("contract_start_date"),
                "contract_expiry_date": manager.get("contract_expiry_date"),
            }
            if manager else None
        ),
        "leadership": {
            "director_of_football": (
                {
                    "id": int(director.get("id") or 0),
                    "name": director.get("name"),
                    "role": director.get("role") or "足球总监",
                }
                if director else None
            ),
        },
        "staff": [
            {
                "id": int(row.get("id") or 0),
                "name": row.get("name"),
                "role": row.get("role"),
                "job_type": row.get("job_type"),
                "ca": row.get("ca"),
                "pa": row.get("pa"),
                "wage_display": row.get("wage_display"),
                "contract_start_date": row.get("contract_start_date"),
                "contract_expiry_date": row.get("contract_expiry_date"),
                "abilities": row.get("abilities") or {},
            }
            for row in staff
        ],
    }


def read_native_world_club_group_people(
    clubs: list[dict[str, Any]],
) -> dict[int, dict[str, Any]]:
    """Read group people in one session, without finances or valuations."""
    from tools.club_reader import _scan_staff_for_teams, read_roster_player_profile

    results: dict[int, dict[str, Any]] = {}
    with borrow_game_reader() as reader:
        resolved: dict[int, tuple[dict[str, Any], int, dict[str, Any]]] = {}
        for club in clubs:
            team_id = int(club.get("id") or 0)
            try:
                address, team = _resolve_world_club_team(reader, club)
                resolved[team_id] = (club, address, team)
            except (ValueError, RuntimeError) as error:
                results[team_id] = {"error": str(error)}
        if not resolved:
            return results
        try:
            staff_by_team = _scan_staff_for_teams(
                reader, {address for _club, address, _team in resolved.values()},
            )
        except (ValueError, RuntimeError) as error:
            results.update({team_id: {"error": str(error)} for team_id in resolved})
            return results
        layout = reader.layout
        game_date = (
            decode_date(reader.u32(reader.module.base_address + int(layout.game_date_rva)) or 0)
            if layout.game_date_rva is not None else None
        )
        for team_id, (club, address, team) in resolved.items():
            try:
                try:
                    manager_object = int(str(team.get("manager_address") or "0"), 16)
                except (TypeError, ValueError):
                    manager_object = 0
                person_offset = getattr(
                    layout, "team_manager_person_offset", layout.staff_complete_object_offset,
                )
                manager_person = manager_object + int(person_offset) if manager_object and person_offset is not None else 0
                expected_vtable = (
                    reader.module.base_address + int(layout.staff_person_vtable_rva)
                    if layout.staff_person_vtable_rva is not None else 0
                )
                if not expected_vtable or reader.ptr(manager_person) != expected_vtable:
                    manager_person = 0
                players = []
                for row in reader.roster(address):
                    player = read_roster_player_profile(reader, row, address, game_date, manager_person)
                    if player:
                        players.append(player)
                staff = _world_club_staff_summary(reader, team, staff_by_team.get(address, []))
                results[team_id] = {
                    "club": {**club, "id": team_id, "address": hex(address)},
                    "players": players, **staff,
                }
            except (ValueError, RuntimeError) as error:
                results[team_id] = {"error": str(error)}
    return results


__all__ = [
    "acquire_club", "acquisition_rebalance_quote", "build_world_clubs", "club_acquisition_valuation",
    "club_investment_performance", "club_portfolio_summary",
    "acquired_clubs_for_game_date", "enrich_world_club_standings",
    "load_acquired_clubs", "load_native_world_clubs",
    "merge_native_world_clubs", "nation_youth_investment_price", "nation_youth_investment_quote", "native_world_club_cache_status", "native_world_club_cache_validity", "paginate_world_clubs", "paginate_world_nations",
    "read_native_world_club_detail", "read_native_world_club_metrics",
    "read_native_world_club_group_people",
    "read_native_world_club_balances",
    "read_native_world_club_snapshot",
    "read_native_world_club_results", "read_native_world_club_staff", "read_native_world_nation_detail",
    "read_native_world_nation_summaries",
    "invest_native_world_nation_youth",
    "recover_acquired_clubs_from_transactions", "remove_acquired_club",
    "save_native_world_clubs", "set_portfolio_group_name", "sync_acquired_club_manager_record",
    "sync_acquired_club_metric_history",
    "invalidate_world_club_runtime_cache",
    "update_acquired_club",
    "refresh_native_world_clubs", "resolve_native_team_addresses", "scan_native_world_clubs",
]
