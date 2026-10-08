from pathlib import Path
import threading
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from fm_odds_web import LocalOddsState
from tools.club_economy import PRODUCTS
from tools import club_reader
from tools.club_reader import develop_player, plan_player_development


ROOT = Path(__file__).resolve().parents[1]
ENLIGHTENMENT_SKUS = (
    "enlightenment",
    "random_enlightenment",
    "extremely_unstable_enlightenment",
)
DESCRIPTIONS = {
    "enlightenment": "仅可对 CA 低于 PA 的球员使用，使 CA 提升1点并自行指定强化的一项可见属性，PA不变。",
    "random_enlightenment": "仅可对 CA 低于 PA 的球员使用，使 CA 提升1点并随机强化一项可见属性，PA不变。",
    "extremely_unstable_enlightenment": "仅可对 CA 低于 PA 的球员使用，使 CA 随机提升1点或降低1点，并随机强化或削弱一项可见属性，PA不变。",
}


def formula_plan(attribute_key: str, attribute_points: int, ca_change: int) -> dict:
    return {
        "attribute_key": attribute_key,
        "attribute_points": attribute_points,
        "attribute_before": 10,
        "attribute_after": 10 + attribute_points,
        "ca_change": ca_change,
        "model": "test-rca",
        "verified": True,
        "recommended_ca_before": 100,
        "recommended_ca_after": 100 + ca_change,
    }


@pytest.mark.parametrize("sku", ENLIGHTENMENT_SKUS)
def test_enlightenment_products_have_distinct_descriptions(sku: str) -> None:
    assert PRODUCTS[sku]["description"] == DESCRIPTIONS[sku]


def test_enlightenment_effect_allows_ca_above_180_and_uses_selected_attribute() -> None:
    raw = bytes([50] * 54)

    updated, ca, pa, audit = plan_player_development(
        raw, 190, 195, "身体:速度", 10, "enlightenment", 2,
        formula_plan=formula_plan("身体:速度", 1, 2),
    )

    assert updated != raw
    assert (ca, pa) == (192, 195)
    assert (audit["attribute_before"], audit["attribute_after"]) == (10, 11)
    assert (audit["ca_before"], audit["ca_after"]) == (190, 192)
    assert (audit["pa_before"], audit["pa_after"]) == (195, 195)


def test_enlightenment_caps_near_maximum_raw_attribute_at_100() -> None:
    raw = bytearray([50] * 54)
    raw_index = club_reader.VISIBLE_ATTRIBUTE_IDS["身体:速度"] - 0x0F
    raw[raw_index] = 98

    updated, ca, pa, audit = plan_player_development(
        bytes(raw), 150, 180, "身体:速度", 19, "enlightenment", 1,
        formula_plan={
            **formula_plan("身体:速度", 1, 1),
            "attribute_before": 19,
            "attribute_after": 20,
        },
    )

    assert updated[raw_index] == 100
    assert (ca, pa) == (151, 180)
    assert (audit["attribute_before"], audit["attribute_after"]) == (19, 20)


def test_formula_snapshot_survives_unused_sentinel_and_near_cap_attribute() -> None:
    raw = bytearray([50] * 54)
    raw[0x2E - 0x0F] = 255
    raw[club_reader.VISIBLE_ATTRIBUTE_IDS["身体:速度"] - 0x0F] = 98
    positions = bytearray(15)
    positions[12] = 20
    layout = SimpleNamespace(key="fm24", attribute_display_bias=0)

    snapshot = club_reader._player_training_ca_snapshot(
        layout,
        bytes(raw),
        bytes(positions),
        {"身体": {"速度": 19}, "定位球": {"角球": 10}, "精神": {"侵略性": 10}},
    )

    assert snapshot["verified"] is True
    assert snapshot["costs"]["身体:速度"]["milestones"]
    assert snapshot["costs"]["定位球:角球"]["milestones"]
    assert snapshot["costs"]["定位球:角球"]["ca_affected"] is True
    assert snapshot["costs"]["精神:侵略性"]["ca_affected"] is False
    fallback = snapshot["costs"]["精神:侵略性"]["milestones"][0]
    assert fallback["attribute_points"] == 1
    assert fallback["ca_change"] == 1
    assert fallback["minimum_ca_fallback"] == 1


def test_low_raw_attribute_snapshot_and_enlightenment_use_display_target() -> None:
    raw = bytes([0] * 54)
    positions = bytes([0] * 15)
    layout = SimpleNamespace(key="fm24", attribute_display_bias=0)
    attributes = {"身体": {"速度": 1}}

    snapshot = club_reader._player_training_ca_snapshot(
        layout, raw, positions, attributes,
    )
    plan = snapshot["costs"]["身体:速度"]["milestones"][0]
    updated, ca, pa, audit = plan_player_development(
        raw, 66, 167, "身体:速度", 1, "enlightenment", 1,
        formula_plan={**plan, "attribute_key": "身体:速度"},
    )

    raw_index = club_reader.VISIBLE_ATTRIBUTE_IDS["身体:速度"] - 0x0F
    assert snapshot["verified"] is True
    assert plan["attribute_after"] == 2
    assert updated[raw_index] == 10
    assert (ca, pa) == (67, 167)
    assert (audit["attribute_before"], audit["attribute_after"]) == (1, 2)


@pytest.mark.parametrize("sku", ("enlightenment", "random_enlightenment"))
def test_enlightenment_effect_rejects_ca_equal_to_pa_or_batch_over_pa(sku: str) -> None:
    raw = bytes([50] * 54)
    with pytest.raises(ValueError, match="仅可对 CA 低于 PA"):
        plan_player_development(raw, 195, 195, "身体:速度", 10, sku)
    with pytest.raises(ValueError, match="CA 必须处于1至PA"):
        plan_player_development(
            raw, 194, 195, "身体:速度", 10, sku, 2,
            formula_plan=formula_plan("身体:速度", 1, 2),
        )


def test_random_enlightenment_uses_formula_selected_attribute() -> None:
    raw = bytes([50] * 54)

    updated, ca, pa, audit = plan_player_development(
        raw, 190, 195, None, -1, "random_enlightenment", 2,
        visible_attribute_keys={"身体:速度", "技术:传球"},
        formula_plan=formula_plan("身体:速度", 1, 2),
    )

    speed_index = club_reader.VISIBLE_ATTRIBUTE_IDS["身体:速度"] - 0x0F
    assert updated[speed_index] == 55
    assert (ca, pa) == (192, 195)
    assert audit["attribute_key"] == "身体:速度"
    assert audit["attribute_changes"] == {
        "身体:速度": {"before": 10, "after": 11},
    }


def test_extremely_unstable_enlightenment_applies_one_formula_result() -> None:
    raw = bytes([50] * 54)

    updated, ca, pa, audit = plan_player_development(
        raw, 190, 195, None, -1, "extremely_unstable_enlightenment", 2,
        visible_attribute_keys={"身体:速度", "技术:传球"},
        formula_plan=formula_plan("技术:传球", -1, -2),
    )

    speed_index = club_reader.VISIBLE_ATTRIBUTE_IDS["身体:速度"] - 0x0F
    passing_index = club_reader.VISIBLE_ATTRIBUTE_IDS["技术:传球"] - 0x0F
    assert updated[speed_index] == 50
    assert updated[passing_index] == 45
    assert (ca, pa) == (188, 195)
    assert audit["attribute_key"] == "技术:传球"
    assert audit["attribute_changes"] == {
        "技术:传球": {"before": 10, "after": 9},
    }


def test_latent_dragon_does_not_validate_unused_attribute_sentinels() -> None:
    raw = bytes([255]) + bytes([50] * 53)

    updated, ca, pa, audit = plan_player_development(
        raw, 177, 180, None, -1, "latent_dragon_divine", 20,
    )

    assert updated == raw
    assert (ca, pa) == (177, 200)
    assert (audit["ca_before"], audit["ca_after"]) == (177, 177)
    assert (audit["pa_before"], audit["pa_after"]) == (180, 200)


def test_latent_dragon_writes_only_pa_and_leaves_attribute_block_untouched() -> None:
    player_address = 0x4200
    layout = SimpleNamespace(
        module_name="game_plugin.dll", player_attributes_offset=0x100,
        player_ca_offset=0x200, player_pa_offset=0x202,
        player_positions_offset=0x300, attribute_display_bias=0,
        module=lambda _process: SimpleNamespace(base_address=0x100000),
    )
    reader = SimpleNamespace(
        bytes=MagicMock(),
        u16=MagicMock(side_effect=[177, 180, 200]),
    )

    with (
        patch.object(club_reader, "select_process_layout", return_value=(1, "fm.exe", layout)),
        patch.object(club_reader, "open_process", return_value=nullcontext(object())),
        patch.object(club_reader, "Reader", return_value=reader),
        patch.object(club_reader, "_validated_player_person"),
        patch.object(
            club_reader, "_visible_player_attributes",
            return_value={"身体": {"速度": 10}},
        ),
        patch.object(club_reader, "write_process_memory") as write,
    ):
        result = develop_player(
            42, player_address, None, -1, "latent_dragon_divine", 20,
        )

    write.assert_called_once()
    assert write.call_args.args[1:] == (
        player_address + 0x202, (200).to_bytes(2, "little"),
    )
    assert result["_original_raw"] == b""
    assert result["attributes"] is None
    assert (result["ca"], result["pa"]) == (177, 200)
    reader.bytes.assert_not_called()


def test_breakthrough_writes_only_ca_and_pa() -> None:
    player_address = 0x4200
    layout = SimpleNamespace(
        module_name="game_plugin.dll", player_attributes_offset=0x100,
        player_ca_offset=0x200, player_pa_offset=0x202,
        player_positions_offset=0x300, attribute_display_bias=0,
        module=lambda _process: SimpleNamespace(base_address=0x100000),
    )
    reader = SimpleNamespace(
        bytes=MagicMock(),
        u16=MagicMock(side_effect=[170, 180, 190, 200]),
    )

    with (
        patch.object(club_reader, "select_process_layout", return_value=(1, "fm.exe", layout)),
        patch.object(club_reader, "open_process", return_value=nullcontext(object())),
        patch.object(club_reader, "Reader", return_value=reader),
        patch.object(club_reader, "_validated_player_person"),
        patch.object(club_reader, "write_process_memory") as write,
    ):
        result = develop_player(
            42, player_address, None, -1, "marrow_cleansing_divine", 20,
        )

    assert [call.args[1:] for call in write.call_args_list] == [
        (player_address + 0x200, (190).to_bytes(2, "little")),
        (player_address + 0x202, (200).to_bytes(2, "little")),
    ]
    assert result["attributes"] is None
    reader.bytes.assert_not_called()


def test_enlightenment_writes_only_selected_attribute_and_ca() -> None:
    player_address = 0x4200
    raw_index = club_reader.VISIBLE_ATTRIBUTE_IDS["身体:速度"] - 0x0F
    original = bytearray([50] * 54)
    original[0] = 255
    layout = SimpleNamespace(
        module_name="game_plugin.dll", player_attributes_offset=0x100,
        player_ca_offset=0x200, player_pa_offset=0x202,
        player_positions_offset=0x300, attribute_display_bias=0,
        module=lambda _process: SimpleNamespace(base_address=0x100000),
    )
    reader = SimpleNamespace(
        bytes=MagicMock(side_effect=[bytes(original), bytes([0] * 15)]),
        u8=MagicMock(return_value=55),
        u16=MagicMock(side_effect=[190, 195, 192]),
    )

    with (
        patch.object(club_reader, "select_process_layout", return_value=(1, "fm.exe", layout)),
        patch.object(club_reader, "open_process", return_value=nullcontext(object())),
        patch.object(club_reader, "Reader", return_value=reader),
        patch.object(club_reader, "_validated_player_person"),
        patch.object(
            club_reader, "_visible_player_attributes",
            return_value={"身体": {"速度": 10}},
        ),
        patch.object(
            club_reader, "_formula_development_plan",
            return_value=formula_plan("身体:速度", 1, 2),
        ),
        patch.object(club_reader, "_player_training_ca_snapshot", return_value={}),
        patch.object(club_reader, "write_process_memory") as write,
    ):
        result = develop_player(
            42, player_address, "身体:速度", 10, "enlightenment", 2,
        )

    assert [call.args[1:] for call in write.call_args_list] == [
        (player_address + 0x100 + raw_index, bytes([55])),
        (player_address + 0x200, (192).to_bytes(2, "little")),
    ]
    assert result["_restore_attribute_keys"] == ("身体:速度",)


def test_write_failure_reports_rollback_verification_failure() -> None:
    reader = SimpleNamespace(u8=MagicMock(return_value=0))

    with (
        patch.object(
            club_reader, "write_process_memory",
            side_effect=[OSError("write failed"), OSError("restore failed")],
        ),
        pytest.raises(RuntimeError, match="回滚校验异常"),
    ):
        club_reader._apply_verified_memory_changes(
            reader, object(), [{
                "address": 0x1000, "original": b"\x01", "updated": b"\x02",
            }],
            write_error="写入校验失败",
            rollback_error="写入失败且回滚校验异常",
        )


def test_ability_only_rollback_restores_ca_pa_without_writing_attributes() -> None:
    player_address = 0x4200
    layout = SimpleNamespace(
        module_name="game_plugin.dll", player_attributes_offset=0x100,
        player_ca_offset=0x200, player_pa_offset=0x202,
        module=lambda _process: SimpleNamespace(base_address=0x100000),
    )
    reader = SimpleNamespace(
        bytes=MagicMock(),
        u16=MagicMock(side_effect=[177, 180]),
    )

    with (
        patch.object(club_reader, "select_process_layout", return_value=(1, "fm.exe", layout)),
        patch.object(club_reader, "open_process", return_value=nullcontext(object())),
        patch.object(club_reader, "Reader", return_value=reader),
        patch.object(club_reader, "_validated_player_person"),
        patch.object(club_reader, "write_process_memory") as write,
    ):
        club_reader.restore_player_development(
            42, player_address, b"", 177, 180,
            attribute_keys=(),
        )

    assert [call.args[1] for call in write.call_args_list] == [
        player_address + 0x200,
        player_address + 0x202,
    ]
    reader.bytes.assert_not_called()


@pytest.mark.parametrize("sku", ("random_enlightenment", "extremely_unstable_enlightenment"))
def test_inventory_random_development_ignores_client_attribute_selection(sku: str) -> None:
    state = LocalOddsState.__new__(LocalOddsState)
    state.lock = threading.RLock()
    state.memory_lock = threading.RLock()
    state._bind_current_save = lambda: None
    player = {
        "id": 42,
        "name": "Player",
        "address": "0x4200",
        "ca": 190,
        "pa": 195,
        "attributes": {"身体": {"速度": 10}},
    }
    item_ids = ["pill-1", "pill-2"]
    inventory = [
        {"id": item_id, "sku": sku, "status": "available"}
        for item_id in item_ids
    ]
    effect = {
        "player_id": 42,
        "attribute_key": "身体:速度",
        "attributes": {"身体": {"速度": 12}},
        "attribute_before": 10,
        "attribute_after": 12,
        "ca": 192,
        "pa": 195,
        "ca_before": 190,
        "ca_after": 192,
        "pa_before": 195,
        "pa_after": 195,
        "_original_raw": bytes([50] * 54),
        "_original_ca": 190,
        "_original_pa": 195,
    }

    with (
        patch.object(state, "_live_player_profile", return_value=({"players": [player]}, player)),
        patch("fm_odds_web.public_economy", return_value={"inventory": inventory}),
        patch("fm_odds_web.develop_player", return_value=effect) as develop,
        patch("fm_odds_web.consume_instant_items", return_value={"inventory": []}),
    ):
        result = state.inventory_develop_player({
            "item_ids": item_ids,
            "player_id": 42,
            "team_id": 9,
            "attribute_key": "身体:速度",
            "expected_attribute": 10,
        })

    develop.assert_called_once_with(42, "0x4200", None, -1, sku, 2)
    assert result["player"]["ca"] == 192
    assert result["player"]["pa"] == 195


def test_reallocation_persistence_failure_restores_only_selected_attributes() -> None:
    state = LocalOddsState.__new__(LocalOddsState)
    state.lock = threading.RLock()
    state.memory_lock = threading.RLock()
    state._bind_current_save = lambda: None
    player = {
        "id": 42, "name": "Player", "address": "0x4200",
        "attributes": {"技术": {"传球": 10, "传中": 10}},
    }
    original = bytes([50] * 54)
    keys = ("技术:传球", "技术:传中")
    effect = {
        "player_id": 42, "attributes": player["attributes"],
        "ca": 100, "pa": 120,
        "before": {keys[0]: 10, keys[1]: 10},
        "after": {keys[0]: 9, keys[1]: 11},
        "_original_raw": original,
        "_original_attribute_keys": keys,
    }

    with (
        patch.object(state, "_live_player_effect_target", return_value=SimpleNamespace(
            player=player, address="0x4200",
        )),
        patch("fm_odds_web.public_economy", return_value={"inventory": [{
            "id": "pill-1", "sku": "martial_manual_fragment", "status": "available",
        }]}),
        patch("fm_odds_web.reallocate_player_attributes", return_value=effect),
        patch("fm_odds_web.consume_instant_items", side_effect=RuntimeError("save failed")),
        patch("fm_odds_web.restore_player_attribute_block") as restore,
    ):
        with pytest.raises(RuntimeError, match="save failed"):
            state.inventory_reallocate_attributes({
                "item_id": "pill-1", "player_id": 42, "team_id": 9,
                "changes": {keys[0]: -1, keys[1]: 1},
                "expected": {keys[0]: 10, keys[1]: 10},
            })

    restore.assert_called_once_with(42, "0x4200", original, keys)


def test_reallocation_does_not_block_formula_ca_arbitrage() -> None:
    player_address = 0x4200
    original = bytes([50] * 54)
    layout = SimpleNamespace(
        module_name="game_plugin.dll", player_attributes_offset=0x100,
        player_ca_offset=0x200, player_pa_offset=0x202,
        player_positions_offset=0x300, attribute_display_bias=0,
        module=lambda _process: SimpleNamespace(base_address=0x100000),
    )
    reader = SimpleNamespace(
        bytes=MagicMock(side_effect=[original, bytes([0] * 15)]),
        u16=MagicMock(side_effect=[100, 120, 100, 120]),
    )

    with (
        patch.object(club_reader, "select_process_layout", return_value=(1, "fm.exe", layout)),
        patch.object(club_reader, "open_process", return_value=nullcontext(object())),
        patch.object(club_reader, "Reader", return_value=reader),
        patch.object(club_reader, "_validated_player_person"),
        patch.object(club_reader, "_visible_player_attributes", return_value={
            "技术": {"传球": 10, "传中": 10},
        }),
        patch.object(club_reader, "_formula_ca_delta", return_value={
            "model": "test-rca", "recommended_ca_before": 100,
            "recommended_ca_after": 101, "ca_change": 1,
        }),
        patch.object(club_reader, "_apply_verified_memory_changes"),
        patch.object(club_reader, "write_process_memory") as write,
    ):
        result = club_reader.reallocate_player_attributes(
            42, player_address,
            {"技术:传球": -1, "技术:传中": 1},
            {"技术:传球": 10, "技术:传中": 10},
        )

    assert result["formula_ca_change"] == 1
    write.assert_not_called()


def test_reallocation_preview_reports_formula_change_without_writing() -> None:
    player_address = 0x4200
    original = bytes([50] * 54)
    layout = SimpleNamespace(
        module_name="game_plugin.dll", player_attributes_offset=0x100,
        player_positions_offset=0x300, attribute_display_bias=0,
        module=lambda _process: SimpleNamespace(base_address=0x100000),
    )
    reader = SimpleNamespace(
        bytes=MagicMock(side_effect=[original, bytes([0] * 15)]),
    )

    with (
        patch.object(club_reader, "select_process_layout", return_value=(1, "fm.exe", layout)),
        patch.object(club_reader, "open_process", return_value=nullcontext(object())),
        patch.object(club_reader, "Reader", return_value=reader),
        patch.object(club_reader, "_validated_player_person"),
        patch.object(club_reader, "_visible_player_attributes", return_value={
            "技术": {"传球": 10, "传中": 10},
        }),
        patch.object(club_reader, "_formula_ca_delta", return_value={
            "model": "test-rca", "recommended_ca_before": 100,
            "recommended_ca_after": 101, "ca_change": 1,
        }),
        patch.object(club_reader, "write_process_memory") as write,
    ):
        result = club_reader.preview_player_attribute_reallocation(
            42, player_address,
            {"技术:传球": -1, "技术:传中": 1},
            {"技术:传球": 10, "技术:传中": 10},
        )

    assert result["valid"] is False
    assert result["formula_ca_change"] == 1
    write.assert_not_called()


def test_reallocation_frontend_requires_formula_preview_before_confirming() -> None:
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    assert 'request("/api/inventory/reallocate-attributes/preview"' in script
    assert 'const formulaValid = complete;' in script
    assert '仍可确认重新分配' in script
    assert "公式CA预估变化" in script


def test_enlightenment_frontend_separates_manual_and_random_attribute_rules() -> None:
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    assert "ENLIGHTENMENT_CA_LIMIT" not in script
    assert 'return ca < pa;' in script
    assert "playerEligibleForDevelopment(player, item.sku) &&" not in script
    assert "allManagedPlayers().filter((player) => playerEligibleForDevelopment(player, item.sku))" in script
    assert 'item.sku === "random_enlightenment" ? "仅限CA＜PA。每颗提供1点CA额度：随机属性提升到公式CA恰好增加所选额度；公式始终为CA+0时，按属性+1、CA+1结算；部分结果需要2颗或更多。"' in script
    assert "function formulaMilestoneForBudget(player, row, budget, downgrade = false)" in script
    assert "公式始终为CA+0时，按属性+1、CA+1结算" in script
    assert "首次公式CA变化为" in script
    assert 'const RANDOM_GROWTH_SKUS = new Set(["random_enlightenment", "extremely_unstable_enlightenment"]);' in script
    assert 'if (!latent && !breakthrough && !randomized) Object.assign(payload' in script
    assert '$("#development-attribute-groups").hidden = latent || breakthrough || randomized;' in script


def test_enlightenment_frontend_loads_non_compact_formula_snapshots() -> None:
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    assert 'await loadClub({compact:false})' in script
    assert 'Object.hasOwn(player || {}, "training_ca")' in script
    assert 'async function ensurePlayerDevelopmentRosterLoaded()' in script
    assert 'CA公式数据读取未完成，请刷新后重试' in script
