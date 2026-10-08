from contextlib import nullcontext
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from fm_odds_web import LocalOddsState
from tools.club_economy import PRODUCTS, SHOP_SKUS
from tools.club_reader import plan_fake_marrow_ca
from tools import club_reader


ROOT = (Path(__file__).resolve().parents[1] / "src")


def test_fake_marrow_product_precedes_common_marrow_pill() -> None:
    product = PRODUCTS["fake_marrow_pill"]
    assert product == {
        "name": "洗髓丹（赝品）",
        "price": 20_000.0,
        "category": "球员",
        "family": "reallocation",
        "tier": "赝品",
        "duration": "instant",
        "description": "使指定球员的CA减半，其他属性及PA不变。",
    }
    assert SHOP_SKUS.index("fake_marrow_pill") + 1 == SHOP_SKUS.index("martial_manual_fragment")


@pytest.mark.parametrize(("current", "expected"), ((100, 50), (101, 50), (2, 1)))
def test_fake_marrow_halves_ca_with_integer_floor(current: int, expected: int) -> None:
    assert plan_fake_marrow_ca(current) == expected


def test_fake_marrow_rejects_ca_below_two() -> None:
    with pytest.raises(ValueError, match="CA 不足2"):
        plan_fake_marrow_ca(1)


def test_fake_marrow_writes_only_ca_and_keeps_attributes_and_pa() -> None:
    player_address = 0x4200
    attribute_address = player_address + 0x100
    original = bytes(range(47, 101))
    ca_address = player_address + 0x200
    pa_address = player_address + 0x202
    memory = {
        attribute_address: original,
        ca_address: (100).to_bytes(2, "little"),
        pa_address: (120).to_bytes(2, "little"),
    }
    layout = SimpleNamespace(
        module_name="fm.exe", player_attributes_offset=0x100,
        player_ca_offset=0x200, player_pa_offset=0x202,
        attribute_display_bias=0,
        module=lambda _process: SimpleNamespace(base_address=0x100000),
    )

    class FakeReader:
        def __init__(self) -> None:
            self.layout = layout

        @staticmethod
        def bytes(address: int, size: int) -> bytes | None:
            value = memory.get(address)
            return value if value is not None and len(value) == size else None

        @staticmethod
        def u16(address: int) -> int | None:
            raw = memory.get(address)
            return int.from_bytes(raw, "little") if raw and len(raw) == 2 else None

    reader = FakeReader()

    def write(_process, address: int, raw: bytes) -> None:
        memory[address] = bytes(raw)

    with (
        patch.object(club_reader, "select_process_layout", return_value=(1, "fm.exe", layout)),
        patch.object(club_reader, "open_process", return_value=nullcontext(object())),
        patch.object(club_reader, "Reader", return_value=reader),
        patch.object(club_reader, "_validated_player_person"),
        patch.object(club_reader, "_visible_player_attributes", return_value={"身体": {"速度": 5}}),
        patch.object(club_reader, "write_process_memory", side_effect=write),
    ):
        result = club_reader.apply_fake_marrow_pill(42, player_address)

    assert memory[attribute_address] == original
    assert result["ca"] == 50
    assert int.from_bytes(memory[ca_address], "little") == 50
    assert result["pa"] == 120
    assert result["_original_raw"] == b""


def test_fake_marrow_consumption_failure_restores_attributes() -> None:
    state = LocalOddsState.__new__(LocalOddsState)
    state.lock = threading.RLock()
    state.memory_lock = threading.RLock()
    state._bind_current_save = lambda: None
    player = {"id": 42, "name": "Player", "address": "0x4200"}
    effect = {
        "player_id": 42, "attributes": {"身体": {"速度": 5}},
        "ca": 50, "pa": 120, "_original_raw": b"",
        "_original_ca": 100, "_original_pa": 120,
    }

    with (
        patch.object(state, "_live_player_profile", return_value=({"players": [player]}, player)),
        patch("fm_odds_web.public_economy", return_value={"inventory": [{
            "id": "fake-1", "sku": "fake_marrow_pill", "status": "available",
        }]}),
        patch("fm_odds_web.apply_fake_marrow_pill", return_value=effect),
        patch("fm_odds_web.consume_instant_item", side_effect=RuntimeError("save failed")),
        patch("fm_odds_web.restore_player_development") as restore,
    ):
        with pytest.raises(RuntimeError, match="save failed"):
            state.inventory_apply_fake_marrow({
                "item_id": "fake-1", "player_id": 42, "team_id": 9,
            })

    restore.assert_called_once_with(
        42, "0x4200", b"", 100, 120,
        attribute_keys=(), restore_ca=True, restore_pa=False,
    )


def test_fake_marrow_frontend_wires_dialog_and_api() -> None:
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    catalog = (ROOT / "web" / "i18n.static.js").read_text(encoding="utf-8")
    styles = (ROOT / "web" / "app.css").read_text(encoding="utf-8")
    assert 'fake_marrow_pill: "赝品"' in script
    assert 'const TIER_CLASS = {赝品:"counterfeit"' in script
    assert 'request("/api/inventory/fake-marrow"' in script
    assert 'id="fake-marrow-dialog"' in html
    assert 'data-i18n="dialog.fake_reallocation.subtitle"' in html
    assert '"dialog.fake_reallocation.subtitle": "This halves the player\'s CA; other attributes and PA remain unchanged"' in catalog
    assert ".tier-counterfeit" in styles
