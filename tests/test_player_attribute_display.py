from types import SimpleNamespace

from tools.club_reader import (
    _display_attribute,
    _visible_player_attributes,
    plan_attribute_reallocation,
)
from tools.game_layout import FM24_LAYOUT, FM26_LAYOUT


def test_supported_layouts_discard_partial_attribute_points():
    assert FM24_LAYOUT.attribute_display_bias == 0
    assert FM26_LAYOUT.attribute_display_bias == 0


def test_attribute_display_discards_raw_remainder():
    expected = {
        1: 1,
        4: 1,
        5: 1,
        9: 1,
        10: 2,
        99: 19,
        100: 20,
    }

    assert {raw: _display_attribute(raw) for raw in expected} == expected


def test_visible_attributes_use_the_same_discard_rule():
    raw = bytearray([50] * 54)
    raw[0x16 - 0x0F] = 54  # Passing: 10 after discarding the remainder.
    reader = SimpleNamespace(
        layout=SimpleNamespace(
            player_attributes_offset=0x200,
            attribute_display_bias=0,
        ),
    )

    attributes = _visible_player_attributes(
        reader, 0x1000, bytes(raw), goalkeeper=False,
    )

    assert attributes["技术"]["传球"] == 10


def test_attribute_reallocation_uses_discarded_display_value_by_default():
    raw = bytearray([50] * 54)
    raw[0x16 - 0x0F] = 54
    raw[0x0F - 0x0F] = 54

    updated, audit = plan_attribute_reallocation(
        bytes(raw),
        {"技术:传球": -1, "技术:传中": 1},
        {"技术:传球": 10, "技术:传中": 10},
    )

    assert updated[0x16 - 0x0F] == 49
    assert updated[0x0F - 0x0F] == 59
    assert audit == {
        "before": {"技术:传球": 10, "技术:传中": 10},
        "after": {"技术:传球": 9, "技术:传中": 11},
    }


def test_attribute_reallocation_ignores_unselected_sentinel_fields():
    raw = bytearray([50] * 54)
    raw[-1] = 255

    updated, _audit = plan_attribute_reallocation(
        bytes(raw),
        {"技术:传球": -1, "技术:传中": 1},
        {"技术:传球": 10, "技术:传中": 10},
    )

    assert updated[-1] == 255
