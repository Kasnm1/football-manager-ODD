from __future__ import annotations

from pathlib import Path
import pytest
from unittest.mock import patch

from tools import club_economy
from tools.club_economy import (
    PRODUCTS, SHOP_SKUS, _account_statement_rows, _build_account_statement,
    account_statement_page,
    _shop_catalog, _shop_catalog_cached,
)


ROOT = (Path(__file__).resolve().parents[1] / "src")


EXPECTED_SHOP_PRICES = {
    "interview_perfume": 5_000_000.0,
    "red_bull": 20_000.0,
    "team_red_bull": 400_000.0,
    "doping": 200_000.0,
    "team_doping": 4_000_000.0,
    "bribed_goalkeeper": 1_000_000.0,
    "opponent_flu": 500_000.0,
    "referee": 50_000.0,
    "referee_level2": 500_000.0,
    "referee_level3": 5_000_000.0,
    "fake_marrow_pill": 20_000.0,
    "martial_manual_fragment": 20_000.0,
    "martial_manual_mid": 200_000.0,
    "martial_manual_high": 2_000_000.0,
    "martial_manual_immortal": 10_000_000.0,
    "extremely_unstable_enlightenment": 500_000.0,
    "enlightenment": 5_000_000.0,
    "rejuvenation_pill": 1_000_000.0,
    "age_reversal_pill": 88_000_000.0,
    "marrow_cleansing_basic": 50_000.0,
    "marrow_cleansing_mid": 500_000.0,
    "marrow_cleansing_high": 5_000_000.0,
    "marrow_cleansing_immortal": 50_000_000.0,
    "marrow_cleansing_divine": 500_000_000.0,
    "latent_dragon_basic": 10_000.0,
    "latent_dragon_mid": 100_000.0,
    "latent_dragon_high": 1_000_000.0,
    "latent_dragon_immortal": 10_000_000.0,
    "latent_dragon_divine": 100_000_000.0,
    "player_brochure": 20_000_000.0,
    "league_brochure": 40_000_000.0,
    "league_point_plus_one": 20_000_000.0,
}


def test_product_definitions_match_current_shop_catalog() -> None:
    assert set(PRODUCTS) - set(SHOP_SKUS) == {
        "club_brochure", "random_enlightenment",
    }
    assert set(SHOP_SKUS) <= set(PRODUCTS)
    assert PRODUCTS["league_point_plus_one"] == {
        "name": "联赛积分 +1", "price": 20_000_000.0, "category": "积分",
        "family": "league_points", "duration": "instant",
        "description": "为当前玩家执教俱乐部增加1点联赛积分，可批量使用。",
    }


def test_random_enlightenment_is_retired_but_existing_inventory_stays_supported() -> None:
    assert "random_enlightenment" not in SHOP_SKUS
    assert PRODUCTS["random_enlightenment"]["name"] == "不稳定培元丹"
    assert PRODUCTS["extremely_unstable_enlightenment"]["price"] == 500_000.0


def test_shop_prices_match_the_configured_catalog() -> None:
    assert {sku: PRODUCTS[sku]["price"] for sku in SHOP_SKUS} == EXPECTED_SHOP_PRICES


@pytest.mark.parametrize(
    ("currency", "rate", "display_scale"),
    (
        ("GBP", 1.0, 1.0),
        ("USD", 1.35082459495356, 1.0),
        ("EUR", 1.1571304778043, 1.0),
        ("CNY", 9.7068147703991, 10.0),
    ),
)
def test_shop_catalog_uses_fixed_currency_face_values(
    currency: str, rate: float, display_scale: float,
) -> None:
    catalog = _shop_catalog({"money_currency": currency, "money_rate": rate})

    for item in catalog:
        expected_display = EXPECTED_SHOP_PRICES[item["sku"]] * display_scale
        assert item["price"] * rate == pytest.approx(expected_display, abs=0.05)


def test_purchase_charges_the_same_currency_specific_price_as_the_catalog() -> None:
    rate = 9.7068147703991
    settings = {"money_currency": "CNY", "money_rate": rate}
    economy = club_economy._new_state()
    economy["general_balance_minor"] = 100_000_000_000_00
    with (
        patch.object(club_economy, "load_settings", return_value=settings),
        patch.object(club_economy, "load_economy", return_value=economy),
        patch.object(club_economy, "available_balance", return_value=0.0),
        patch.object(
            club_economy, "_save_economy_with_wallet_adjustments",
            return_value={"balance": 0.0},
        ),
    ):
        result = club_economy.buy_item("red_bull")

    assert result["payment"]["total"] * rate == pytest.approx(200_000.0, abs=0.05)
    assert result["inventory"][0]["price"] == result["payment"]["total"]


def test_currency_change_refreshes_currency_specific_shop_prices() -> None:
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    handler = script.split('$("#money-currency").addEventListener("change"', 1)[1]

    assert "await loadState({fresh:true});" in handler[:1_000]


def test_shop_catalog_cache_is_bounded_and_returns_isolated_rows() -> None:
    _shop_catalog_cached.cache_clear()
    settings = {
        "money_currency": "GBP", "money_rate": 1.0,
        "purchase_money_scale": 1.0,
    }
    first = _shop_catalog(settings)
    second = _shop_catalog(settings)

    assert _shop_catalog_cached.cache_info().hits == 1
    first[0]["price"] = -1
    assert second[0]["price"] == EXPECTED_SHOP_PRICES[second[0]["sku"]]
    assert _shop_catalog(settings)[0]["price"] == second[0]["price"]


def test_account_statement_cache_key_covers_dates_and_amounts() -> None:
    _account_statement_rows.cache_clear()
    transactions = [
        {"game_date": "2026-07-01", "amount_minor": 120_00},
        {"game_date": "2026-07-02", "amount_minor": -20_00},
    ]
    first = _build_account_statement(transactions)
    second = _build_account_statement(transactions)

    assert _account_statement_rows.cache_info().hits == 1
    assert first == second
    assert first["current"] == {
        "month": "2026-07", "income": 120.0, "expense": 20.0,
        "net": 100.0, "count": 2,
    }

    transactions[0]["game_date"] = "2026-08-01"
    transactions[0]["amount_minor"] = 50_00
    changed = _build_account_statement(transactions)
    assert changed["current_month"] == "2026-08"
    assert changed["current"]["income"] == 50.0
    assert changed != first


def test_unified_statement_includes_wallet_and_excludes_internal_transfer_from_net() -> None:
    statement = _build_account_statement(
        [
            {"id": "salary", "type": "manager_salary", "game_date": "2026-08-01", "amount_minor": 100_00},
            {"id": "transfer", "type": "bank_to_casino", "game_date": "2026-08-02", "amount_minor": -40_00},
            {"id": "shop", "type": "shop_purchase", "game_date": "2026-08-02", "amount_minor": -30_00, "wallet_used_minor": 10_00},
            {"id": "credit", "type": "credit_repaid", "game_date": "2026-08-02", "amount_minor": -30_00, "wallet_used_minor": 10_00},
        ],
        [
            {"id": "wallet-transfer", "type": "bank_recharge", "at": "2026-08-02T12:00:00", "amount_minor": 40_00},
            {"id": "wallet-shop", "type": "shop_purchase", "at": "2026-08-02T13:00:00", "amount_minor": -10_00},
            {"id": "wallet-credit", "type": "credit_repayment", "at": "2026-08-02T14:00:00", "amount_minor": -10_00},
            {"id": "stake", "type": "bet_placed", "at": "2026-08-03T12:00:00", "amount_minor": -25_00},
            {"id": "payout", "type": "bet_settlement", "at": "2026-08-04T12:00:00", "amount_minor": 60_00},
        ],
    )

    assert statement["current"] == {
        "month": "2026-08", "income": 160.0, "expense": 85.0,
        "net": 75.0, "count": 5,
    }
    assert statement["summary"]["transfers"] == 40.0
    assert statement["summary"]["count"] == 6


def test_account_statement_page_filters_and_paginates_server_side() -> None:
    economy = club_economy._new_state()
    economy["transactions"] = [
        {"id": "salary", "type": "manager_salary", "game_date": "2026-08-01", "amount_minor": 100_00},
        {"id": "shop", "type": "shop_purchase", "game_date": "2026-07-01", "amount_minor": -20_00, "sku": "red_bull"},
    ]
    wallet = {
        "balance_minor": 50_00,
        "transactions": [
            {"id": f"bet-{index}", "type": "bet_placed", "at": f"2026-08-{index + 1:02d}T12:00:00", "amount_minor": -1_00}
            for index in range(12)
        ],
    }
    with (
        patch.object(club_economy, "load_economy", return_value=economy),
        patch.object(club_economy, "load_wallet", return_value=wallet),
    ):
        result = account_statement_page(
            page=2, page_size=10, month="2026-08", account="wallet",
            category="betting", direction="expense", search="投注",
        )

    assert result["pagination"] == {
        "page": 2, "page_size": 10, "total": 12, "total_pages": 2,
    }
    assert len(result["entries"]) == 2
    assert result["summary"]["expense"] == 12.0
    assert {entry["account"] for entry in result["entries"]} == {"wallet"}
    assert result["months"] == [{
        "value": "2026-08", "count": 12, "income": 0.0,
        "expense": 12.0, "net": -12.0, "transfers": 0.0,
    }]
    assert result["categories"] == [
        {"value": "income", "label": "收入", "count": 1},
        {"value": "betting", "label": "投注", "count": 12},
        {"value": "shop", "label": "商店与道具", "count": 1},
    ]


def test_account_statement_exposes_item_sku_for_locale_aware_rendering() -> None:
    entry = club_economy._ledger_entry(
        {
            "id": "shop-red-bull",
            "type": "shop_purchase",
            "game_date": "2026-08-02",
            "amount_minor": -20_00,
            "sku": "red_bull",
        },
        "bank",
    )

    assert entry is not None
    assert entry["item_sku"] == "red_bull"
    assert entry["description"] == PRODUCTS["red_bull"]["name"]


def test_removed_non_catalog_products_are_not_defined() -> None:
    assert {
        "no_retirement",
        "world_work_permit",
        "green_card",
        "club_work_permit",
    }.isdisjoint(PRODUCTS)
