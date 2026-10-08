from __future__ import annotations

import struct
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tools import club_vision
from tools.game_layout import (
    FM24_EPIC_LAYOUT, FM24_LAYOUT, FM24_XGP_LAYOUT, FM26_LAYOUT,
    FM26_XGP_TEMPLATE,
)


ROOT = (Path(__file__).resolve().parents[1] / "src")


@pytest.fixture(autouse=True)
def _stub_native_culture_calls(monkeypatch):
    # Unit tests exercise the vector transaction only; native constructor ABI
    # is covered separately without touching a real FM process.
    monkeypatch.setattr(
        club_vision, "_native_culture_layout_ready",
        lambda _reader: (0, 0, None),
    )
    monkeypatch.setattr(club_vision, "_native_unary_call", lambda *_args: None)


class FakeProcessContext:
    def __enter__(self):
        return object()

    def __exit__(self, *_args):
        return None


class FakeReader:
    def __init__(self):
        self.module_base = 0x500000
        self.layout = SimpleNamespace(
            team_details_offset=0xA0,
            team_details_club_vision_offset=0xC0,
            club_vision_culture_offset=0x78,
            club_culture_record_size=0x40,
            club_vtable_rva=0x1000,
        )
        self.team_address = 0x100000
        self.club_address = 0x200000
        self.details_address = 0x300000
        self.vision_address = 0x400000
        self.root_address = 0x410000
        self.vector_address = 0x420000
        self.record_address = 0x430000
        raw = bytearray(0x40)
        struct.pack_into("<h", raw, 0x08, -1)
        struct.pack_into("<b", raw, 0x0B, -1)
        struct.pack_into("<i", raw, 0x28, -1)
        raw[0x2C] = 150
        raw[0x2D] = 9
        raw[0x30] = 1
        struct.pack_into("<I", raw, 0x3C, 0x200)
        self.record = bytes(raw)

    def team(self, address):
        if address != self.team_address:
            return None
        return {"id": 679, "team_type": "club"}

    def u32(self, address):
        return 679 if address == self.team_address + club_vision.ENTITY_UID else 0

    def ptr(self, address):
        return {
            self.team_address + 0x30: self.club_address,
            self.club_address: self.module_base + self.layout.club_vtable_rva,
            self.team_address + self.layout.team_details_offset: self.details_address,
            self.details_address + self.layout.team_details_club_vision_offset: self.vision_address,
            self.vision_address + self.layout.club_vision_culture_offset: self.root_address,
            self.root_address + 0x30: self.club_address,
        }.get(address, 0)

    def bytes(self, address, size):
        if address == self.root_address and size == 24:
            return struct.pack(
                "<QQQ", self.vector_address, self.vector_address + 8,
                self.vector_address + 8,
            )
        if address == self.vector_address and size == 8:
            return struct.pack("<Q", self.record_address)
        if address == self.record_address and size == 0x40:
            return self.record
        return None


class MutableFakeReader(FakeReader):
    def __init__(self):
        super().__init__()
        self.regions = {
            self.root_address: bytearray(struct.pack(
                "<QQQ", self.vector_address, self.vector_address + 8,
                self.vector_address + 8,
            )),
            self.vector_address: bytearray(struct.pack("<Q", self.record_address)),
            self.record_address: bytearray(self.record),
        }

    def bytes(self, address, size):
        if size == 0:
            return b""
        for base, raw in self.regions.items():
            offset = address - base
            if 0 <= offset and offset + size <= len(raw):
                return bytes(raw[offset:offset + size])
        return None

    def write(self, _process, address, data):
        for base, raw in self.regions.items():
            offset = address - base
            if 0 <= offset and offset + len(data) <= len(raw):
                raw[offset:offset + len(data)] = data
                return
        # Native construction writes the object in verified slices rather
        # than copying one full template block.
        base = address & ~0x3F
        raw = self.regions.setdefault(base, bytearray(0x40))
        raw[address - base:address - base + len(data)] = data


class InlineVisionFakeReader(FakeReader):
    """FM24 acquired-club shape: inline node with header at Vision+0x08."""

    def ptr(self, address):
        if address == self.vision_address + self.layout.club_vision_culture_offset:
            return 0
        if address == self.vision_address + 0x40:
            return self.team_address
        return super().ptr(address)

    def bytes(self, address, size):
        if address == self.vision_address + 0x08 and size == 24:
            return struct.pack(
                "<QQQ", self.vector_address, self.vector_address + 8,
                self.vector_address + 8,
            )
        return super().bytes(address, size)


class MixedImportanceVisionFakeReader(FakeReader):
    """FM24 vector containing one inactive and one active native objective."""

    def __init__(self):
        super().__init__()
        self.inactive_record_address = 0x431000
        inactive = bytearray(self.record)
        inactive[0x2C] = 16
        inactive[0x2D] = 0
        self.inactive_record = bytes(inactive)

    def bytes(self, address, size):
        if address == self.root_address and size == 24:
            return struct.pack(
                "<QQQ", self.vector_address, self.vector_address + 16,
                self.vector_address + 16,
            )
        if address == self.vector_address and size == 16:
            return struct.pack(
                "<QQ", self.inactive_record_address, self.record_address,
            )
        if address == self.inactive_record_address and size == 0x40:
            return self.inactive_record
        return super().bytes(address, size)


def expected_record() -> dict[str, int]:
    return {
        "type": 150,
        "importance": 9,
        "source_type": 1,
        "value_raw": -1,
        "reference_id": -1,
        "reference_type": -1,
    }


def test_inline_fm24_vision_node_uses_embedded_vector_header() -> None:
    reader = InlineVisionFakeReader()
    club, root, begin, end, capacity, pointers, records = club_vision._locate_vector(
        reader, reader.team_address, 679,
    )
    assert club == reader.club_address
    assert root == reader.vision_address + 0x08
    assert (begin, end, capacity) == (
        reader.vector_address, reader.vector_address + 8,
        reader.vector_address + 8,
    )
    assert pointers == [reader.record_address]
    assert reader.record_address in records


def test_vision_vector_preserves_but_does_not_edit_inactive_records() -> None:
    reader = MixedImportanceVisionFakeReader()

    _club, _root, _begin, _end, _capacity, pointers, records = (
        club_vision._locate_vector(reader, reader.team_address, 679)
    )

    assert pointers == [reader.inactive_record_address, reader.record_address]
    assert reader.inactive_record_address not in records
    assert reader.record_address in records


def test_vision_vector_relocates_team_by_stable_uid_before_using_stale_hint() -> None:
    reader = FakeReader()
    stale_team_address = reader.team_address
    reader.team_address = 0x110000
    directory = SimpleNamespace(
        addresses_for_uid=lambda table, uid: (
            (reader.team_address,) if table == "team" and uid == 679 else ()
        ),
    )
    reader.module = SimpleNamespace(
        base_address=reader.module_base, size=0x1000, path="",
    )
    with patch.object(club_vision, "DatabaseIndex", return_value=directory) as build:
        club, _root, _begin, _end, _capacity, _pointers, records = (
            club_vision._locate_vector(reader, stale_team_address, 679)
        )

    build.assert_called_once_with(reader)
    assert club == reader.club_address
    assert reader.record_address in records


def test_native_culture_layouts_are_build_specific_and_fail_closed() -> None:
    assert (
        FM26_LAYOUT.club_culture_vtable_rva,
        FM26_LAYOUT.club_culture_constructor_rva,
        FM26_LAYOUT.club_culture_initialize_rva,
    ) == (0x4342788, 0x193D240, None)
    assert (
        FM24_LAYOUT.club_culture_vtable_rva,
        FM24_LAYOUT.club_culture_constructor_rva,
        FM24_LAYOUT.club_culture_initialize_rva,
    ) == (0x574AA78, 0x2E9A6D0, 0x294D110)
    assert (
        FM24_EPIC_LAYOUT.club_culture_vtable_rva,
        FM24_EPIC_LAYOUT.club_culture_constructor_rva,
        FM24_EPIC_LAYOUT.club_culture_initialize_rva,
    ) == (0x574AA78, 0x2E9A6D0, 0x294D110)
    for layout in (FM24_XGP_LAYOUT, FM26_XGP_TEMPLATE):
        assert layout.club_culture_vtable_rva is None
        assert layout.club_culture_constructor_rva is None
        assert layout.club_culture_initialize_rva is None


def test_custom_nation_target_accepts_only_exact_fm24_epic_identity() -> None:
    assert club_vision._custom_nation_target_layout_supported(FM24_EPIC_LAYOUT)
    wrong = SimpleNamespace(
        key="fm24", distribution="epic", executable_sha256="00" * 32,
    )
    assert not club_vision._custom_nation_target_layout_supported(wrong)


def test_native_unary_stub_preserves_win64_call_frame_and_restores_context() -> None:
    function = 0x1111222233334444
    argument = 0x5555666677778888
    result = 0x9999AAAABBBBCCCC
    context = 0xDDDDEEEEFFFF0000
    restore = 0x123456789ABCDEF0
    code = club_vision._build_native_unary_stub(
        function, argument, result, context, restore,
    )
    assert code.startswith(b"\x48\x83\xec\x28")
    assert code.count(function.to_bytes(8, "little")) == 1
    assert code.count(argument.to_bytes(8, "little")) == 1
    assert result.to_bytes(8, "little") in code
    assert context.to_bytes(8, "little") in code
    assert restore.to_bytes(8, "little") in code
    assert code.endswith(b"\xff\xe0")


def test_creation_template_uses_board_country_reference_not_supporter_simple() -> None:
    pointers = [0x1000, 0x2000, 0x3000]
    states = {
        0x1000: {
            "type": 155, "source_type": 2, "reference_type": -1,
            "value_raw": -1,
        },
        0x2000: {
            "type": 121, "source_type": 1, "reference_type": 9,
            "value_raw": -1,
        },
        0x3000: {
            "type": 44, "source_type": 1, "reference_type": -1,
            "value_raw": -1,
        },
    }

    assert club_vision._creation_template_address(
        pointers, states, 88, 10,
    ) == 0x2000


def test_creation_template_rejects_supporter_only_record() -> None:
    states = {
        0x1000: {
            "type": 155, "source_type": 2, "reference_type": -1,
            "value_raw": -1,
        },
    }

    assert club_vision._creation_template_address(
        [0x1000], states, 88, 10,
    ) == 0


def test_creation_template_falls_back_to_board_reference_record() -> None:
    states = {
        0x1000: {
            "type": 1, "source_type": 1, "reference_type": 25,
            "value_raw": -1,
        },
    }

    assert club_vision._creation_template_address(
        [0x1000], states, 150, -1,
    ) == 0x1000


def test_public_payload_only_offers_shape_compatible_replacements() -> None:
    payload = club_vision.public_club_vision([{
        "address": "0x430000", "type": 150, "importance": 9,
        "source_type": 1, "value_raw": -1, "reference_type": -1,
    }])

    offered = {row["value"] for row in payload["items"][0]["type_options"]}
    assert payload["available"] is True
    assert 151 in offered
    assert 1 not in offered
    assert 104 not in offered
    assert payload["mode"] == "manage_board_targets"
    assert payload["items"][0]["deletable"] is True
    assert {row["value"] for row in payload["create_options"]} == set(
        (club_vision.CREATABLE_SIMPLE_TYPES | club_vision.CREATABLE_VALUE_TYPES).intersection(
            club_vision.CLUB_VISION_TYPE_NAMES,
        ) - club_vision.HIDDEN_TARGET_TYPES
    )
    assert 86 not in {row["value"] for row in payload["create_options"]}
    assert 91 not in {row["value"] for row in payload["create_options"]}
    assert len(payload["catalog_options"]) == len(club_vision.CLUB_VISION_TYPE_NAMES) - 3 == 66
    catalog = {row["value"]: row for row in payload["catalog_options"]}
    assert catalog[151]["available"] is True
    assert catalog[1]["available"] is False
    assert catalog[100]["available"] is False
    assert catalog[81]["available"] is True
    assert catalog[81]["parameter"] == "age"
    assert catalog[84]["parameter"] == "years"
    assert payload["importance_options"] == [
        {"value": 2, "label": "锦上添花"},
        {"value": 6, "label": "有点在意"},
        {"value": 8, "label": "十分渴望"},
        {"value": 10, "label": "不容有失"},
    ]


def test_public_payload_exposes_nationality_target_and_resolves_reference_name() -> None:
    nations = [{"uid": 110, "item_id": 55, "name": "中国"}]
    payload = club_vision.public_club_vision([
        {
            "address": "0x430000", "type": 100, "importance": 8,
            "source_type": 1, "value_raw": -1,
            "reference_id": 55, "reference_type": 9,
        },
        {
            "address": "0x430040", "type": 88, "importance": 10,
            "source_type": 1, "value_raw": -1,
            "reference_id": 55, "reference_type": 10,
        },
    ], nations)

    assert {row["value"] for row in payload["create_options"]} >= {88, 100}
    assert payload["nation_options"] == nations
    assert next(row for row in payload["catalog_options"] if row["value"] == 100)["available"] is True
    assert next(row for row in payload["catalog_options"] if row["value"] == 88)["parameter"] == "nation"
    assert payload["items"][0]["type_name"] == "签下中国籍球员"
    assert payload["items"][0]["reference_name"] == "中国"
    assert payload["items"][1]["type_name"] == "不签下非中国国籍的球员"
    assert payload["items"][1]["reference_name"] == "中国"


def test_public_payload_accepts_native_nationality_reference_for_country_targets() -> None:
    nations = [{"uid": 771, "item_id": 145, "name": "德国"}]
    payload = club_vision.public_club_vision([
        {
            "address": "0x430000", "type": 100, "importance": 5,
            "source_type": 1, "value_raw": -1,
            "reference_id": 145, "reference_type": 10,
        },
        {
            "address": "0x430040", "type": 101, "importance": 2,
            "source_type": 1, "value_raw": -1,
            "reference_id": 145, "reference_type": 10,
        },
    ], nations)

    assert payload["items"][0]["type_name"] == "签下德国籍球员"
    assert payload["items"][0]["reference_name"] == "德国"
    assert payload["items"][1]["type_name"] == "签下效力于德国的球员"
    assert payload["items"][1]["reference_name"] == "德国"


def test_public_payload_embeds_referenced_competition_name_in_target_title() -> None:
    payload = club_vision.public_club_vision([{
        "address": "0x430000", "type": 2, "importance": 10,
        "source_type": 1, "value_raw": -1,
        "reference_id": 68, "reference_type": 25,
    }], reference_names={"25:68": "墨西哥甲级联赛上半程"})
    assert payload["items"][0]["type_name"] == "获得墨西哥甲级联赛上半程资格"
    assert payload["items"][0]["reference_name"] == "墨西哥甲级联赛上半程"


def test_cup_target_keeps_cup_name_and_excludes_league_only_replacements() -> None:
    payload = club_vision.public_club_vision([{
        "address": "0x430000", "type": 10, "importance": 6,
        "source_type": 1, "value_raw": 1,
        "reference_id": 77, "reference_type": 25,
    }], reference_names={"25:77": "西班牙国王杯"})
    row = payload["items"][0]
    offered = {option["value"] for option in row["type_options"]}
    assert row["type_name"] == "进入西班牙国王杯后期阶段"
    assert row["editable"] is True
    assert 10 in offered
    assert 7 not in offered


def test_supporter_target_is_hidden_from_board_target_payload() -> None:
    payload = club_vision.public_club_vision([{
        "address": "0x430000", "type": 150, "importance": 9,
        "source_type": 2, "value_raw": -1, "reference_type": -1,
    }])
    assert payload["items"] == []
    assert payload["ignored_non_board_items"] == 1


def test_supporter_reputation_targets_are_not_board_catalog_options() -> None:
    payload = club_vision.public_club_vision([])
    assert {63, 64}.isdisjoint(
        {row["value"] for row in payload["catalog_options"]}
    )
    assert {63, 64}.isdisjoint(
        {row["value"] for row in payload["create_options"]}
    )


def test_unknown_type_30_is_hidden_from_board_target_payload() -> None:
    payload = club_vision.public_club_vision([{
        "address": "0x430000", "type": 30, "importance": 10,
        "source_type": 1, "value_raw": 1,
        "reference_id": 68, "reference_type": 25,
    }])
    assert payload["items"] == []
    assert payload["ignored_non_board_items"] == 1


def test_unrecognized_numeric_type_is_hidden_from_board_target_payload() -> None:
    payload = club_vision.public_club_vision([{
        "address": "0x430000", "type": 183, "importance": 10,
        "source_type": 1, "value_raw": -1,
        "reference_id": -1, "reference_type": -1,
    }])
    assert payload["items"] == []
    assert payload["ignored_non_board_items"] == 1


def test_rival_finish_target_is_hidden_even_when_source_byte_says_board() -> None:
    payload = club_vision.public_club_vision([{
        "address": "0x430000", "type": 184, "importance": 10,
        "source_type": 1, "value_raw": -1,
        "reference_id": 1904, "reference_type": 3,
    }])
    assert payload["items"] == []
    assert payload["ignored_non_board_items"] == 1
    assert 184 not in {row["value"] for row in payload["catalog_options"]}


def test_unknown_source_board_record_remains_visible_but_read_only() -> None:
    payload = club_vision.public_club_vision([{
        "address": "0x430000", "type": 33, "importance": 10,
        "source_type": 19, "value_raw": 10, "reference_type": 25,
    }])
    assert payload["items"][0]["type_name"] == "在该项赛事保持竞争力"
    assert payload["items"][0]["editable"] is False
    assert payload["items"][0]["deletable"] is True


def test_fm26_bottom_finish_target_has_confirmed_board_label() -> None:
    payload = club_vision.public_club_vision([{
        "address": "0x430000", "type": 37, "importance": 10,
        "source_type": 1, "value_raw": 16, "reference_type": 25,
    }])
    assert payload["items"][0]["type_name"] == "避免在赛事中垫底"
    assert payload["items"][0]["editable"] is True
    assert payload["items"][0]["deletable"] is True


def test_existing_competition_target_can_be_updated_in_place() -> None:
    reader = FakeReader()
    raw = bytearray(reader.record)
    raw[0x2C] = 1
    struct.pack_into("<h", raw, 0x08, 68)
    struct.pack_into("<b", raw, 0x0B, 25)
    struct.pack_into("<i", raw, 0x28, 1)
    reader.record = bytes(raw)
    layout = SimpleNamespace(
        module=lambda _process: SimpleNamespace(base_address=reader.module_base),
        module_name="fm.exe",
    )
    expected = {
        "type": 1, "importance": 9, "source_type": 1,
        "value_raw": 1, "reference_id": 68, "reference_type": 25,
    }
    def write(_process, address, data):
        assert address == reader.record_address + 0x28
        updated = bytearray(reader.record)
        updated[0x28:0x31] = data
        reader.record = bytes(updated)
    with (
        patch.object(club_vision, "select_process_layout", return_value=(1, "fm.exe", layout)),
        patch.object(club_vision, "open_process", return_value=FakeProcessContext()),
        patch.object(club_vision, "Reader", return_value=reader),
        patch.object(club_vision, "write_process_memory", side_effect=write),
    ):
        result = club_vision.update_club_vision_record(
            reader.team_address, 679, reader.record_address,
            expected=expected, updates={"importance": 8},
        )
    assert result["changed"] is True
    assert result["importance"] == 8


def test_fm26_top_finish_target_uses_zero_based_position_value() -> None:
    payload = club_vision.public_club_vision([{
        "address": "0x430000", "type": 38, "importance": 10,
        "source_type": 1, "value_raw": 3, "reference_type": 25,
    }])
    assert payload["items"][0]["type_name"] == "取得赛事前 4 名"


def test_write_revalidates_and_updates_only_the_existing_record() -> None:
    reader = FakeReader()
    layout = SimpleNamespace(
        module=lambda _process: SimpleNamespace(base_address=reader.module_base),
        module_name="fm.exe",
    )

    def write(_process, address, data):
        assert address == reader.record_address + 0x28
        assert len(data) == 9
        updated = bytearray(reader.record)
        updated[0x28:0x31] = data
        reader.record = bytes(updated)

    with (
        patch.object(club_vision, "select_process_layout", return_value=(1, "fm.exe", layout)),
        patch.object(club_vision, "open_process", return_value=FakeProcessContext()),
        patch.object(club_vision, "Reader", return_value=reader),
        patch.object(club_vision, "write_process_memory", side_effect=write),
    ):
        result = club_vision.update_club_vision_record(
            reader.team_address, 679, reader.record_address,
            expected=expected_record(),
            updates={"type": 151, "importance": 8, "source_type": 1},
        )

    assert result["changed"] is True
    assert result["type"] == 151
    assert result["importance"] == 8
    assert result["source_type"] == 1
    assert reader.record[0x2C] == 151
    assert reader.record[0x2D] == 8
    assert reader.record[0x30] == 1


def test_write_rejects_stale_expected_value_before_mutation() -> None:
    reader = FakeReader()
    layout = SimpleNamespace(
        module=lambda _process: SimpleNamespace(base_address=reader.module_base),
        module_name="fm.exe",
    )
    expected = expected_record()
    expected["importance"] = 7

    with (
        patch.object(club_vision, "select_process_layout", return_value=(1, "fm.exe", layout)),
        patch.object(club_vision, "open_process", return_value=FakeProcessContext()),
        patch.object(club_vision, "Reader", return_value=reader),
        patch.object(club_vision, "write_process_memory") as write,
        pytest.raises(RuntimeError, match="刚刚发生变化"),
    ):
        club_vision.update_club_vision_record(
            reader.team_address, 679, reader.record_address,
            expected=expected,
            updates={"importance": 8},
        )

    write.assert_not_called()


def test_write_rejects_cross_shape_target() -> None:
    reader = FakeReader()
    layout = SimpleNamespace(
        module=lambda _process: SimpleNamespace(base_address=reader.module_base),
        module_name="fm.exe",
    )

    with (
        patch.object(club_vision, "select_process_layout", return_value=(1, "fm.exe", layout)),
        patch.object(club_vision, "open_process", return_value=FakeProcessContext()),
        patch.object(club_vision, "Reader", return_value=reader),
        pytest.raises(ValueError, match="结构不兼容"),
    ):
        club_vision.update_club_vision_record(
            reader.team_address, 679, reader.record_address,
            expected=expected_record(),
            updates={"type": 1, "importance": 8},
        )


def test_write_rejects_non_product_importance_value() -> None:
    reader = FakeReader()
    layout = SimpleNamespace(
        module=lambda _process: SimpleNamespace(base_address=reader.module_base),
        module_name="fm.exe",
    )
    with (
        patch.object(club_vision, "select_process_layout", return_value=(1, "fm.exe", layout)),
        patch.object(club_vision, "open_process", return_value=FakeProcessContext()),
        patch.object(club_vision, "Reader", return_value=reader),
        patch.object(club_vision, "write_process_memory") as write,
        pytest.raises(ValueError, match="锦上添花"),
    ):
        club_vision.update_club_vision_record(
            reader.team_address, 679, reader.record_address,
            expected=expected_record(), updates={"importance": 5},
        )
    write.assert_not_called()


def test_write_readback_failure_restores_touched_field_block() -> None:
    reader = FakeReader()
    original = reader.record
    layout = SimpleNamespace(
        module=lambda _process: SimpleNamespace(base_address=reader.module_base),
        module_name="fm.exe",
    )
    writes = 0

    def write(_process, address, data):
        nonlocal writes
        assert address == reader.record_address + 0x28
        writes += 1
        updated = bytearray(reader.record)
        updated[0x28:0x31] = data
        if writes == 1:
            updated[0x2D] = 1
        reader.record = bytes(updated)

    with (
        patch.object(club_vision, "select_process_layout", return_value=(1, "fm.exe", layout)),
        patch.object(club_vision, "open_process", return_value=FakeProcessContext()),
        patch.object(club_vision, "Reader", return_value=reader),
        patch.object(club_vision, "write_process_memory", side_effect=write),
        pytest.raises(RuntimeError, match="写入后校验失败"),
    ):
        club_vision.update_club_vision_record(
            reader.team_address, 679, reader.record_address,
            expected=expected_record(),
            updates={"importance": 8},
        )

    assert writes == 2
    assert reader.record == original


def test_create_board_target_allocates_record_and_full_vector() -> None:
    reader = MutableFakeReader()
    layout = SimpleNamespace(
        module=lambda _process: SimpleNamespace(base_address=reader.module_base),
        module_name="fm.exe", club_culture_record_size=0x40,
    )
    allocations = iter(((0x440000, 0x700000), (0x450000, 0x700000)))

    with (
        patch.object(club_vision, "select_process_layout", return_value=(1, "fm.exe", layout)),
        patch.object(club_vision, "open_process", return_value=FakeProcessContext()),
        patch.object(club_vision, "Reader", return_value=reader),
        patch.object(club_vision, "write_process_memory", side_effect=reader.write),
        patch.object(club_vision, "_allocate_native_block", side_effect=lambda *_args: next(allocations)),
    ):
        result = club_vision.create_club_vision_record(
            reader.team_address, 679, culture_type=151, importance=8,
        )

    assert result["operation"] == "create"
    assert result["source_type"] == 1
    assert result["type"] == 151
    begin, end, capacity = struct.unpack("<QQQ", reader.bytes(reader.root_address, 24))
    assert (begin, end, capacity) == (0x450000, 0x450010, 0x450010)


def test_create_age_target_writes_validated_value() -> None:
    reader = MutableFakeReader()
    layout = SimpleNamespace(
        module=lambda _process: SimpleNamespace(base_address=reader.module_base),
        module_name="fm.exe", club_culture_record_size=0x40,
    )
    allocations = iter(((0x440000, 0x700000), (0x450000, 0x700000)))
    with (
        patch.object(club_vision, "select_process_layout", return_value=(1, "fm.exe", layout)),
        patch.object(club_vision, "open_process", return_value=FakeProcessContext()),
        patch.object(club_vision, "Reader", return_value=reader),
        patch.object(club_vision, "write_process_memory", side_effect=reader.write),
        patch.object(club_vision, "_allocate_native_block", side_effect=lambda *_args: next(allocations)),
    ):
        result = club_vision.create_club_vision_record(
            reader.team_address, 679, culture_type=81,
            importance=6, value=32,
        )

    assert result["type"] == 81
    assert result["value_raw"] == 32
    assert result["reference_type"] == -1
    assert result["reference_id"] == -1


def test_create_nationality_target_writes_validated_nation_reference() -> None:
    reader = MutableFakeReader()
    layout = SimpleNamespace(
        module=lambda _process: SimpleNamespace(base_address=reader.module_base),
        module_name="fm.exe", club_culture_record_size=0x40,
    )
    allocations = iter(((0x440000, 0x700000), (0x450000, 0x700000)))
    with (
        patch.object(club_vision, "select_process_layout", return_value=(1, "fm.exe", layout)),
        patch.object(club_vision, "open_process", return_value=FakeProcessContext()),
        patch.object(club_vision, "Reader", return_value=reader),
        patch.object(club_vision, "write_process_memory", side_effect=reader.write),
        patch.object(club_vision, "_allocate_native_block", side_effect=lambda *_args: next(allocations)),
        patch.object(club_vision, "_resolve_nation_reference", return_value={
            "uid": 110, "item_id": 55, "name": "中国", "address": 0x500000,
        }),
    ):
        result = club_vision.create_club_vision_record(
            reader.team_address, 679, culture_type=100,
            importance=8, nation_id=110,
        )

    assert result["type"] == 100
    assert result["reference_type"] == 9
    assert result["reference_id"] == 55
    assert result["reference_uid"] == 110
    assert result["type_name"] == "签下中国籍球员"


def test_create_non_national_target_uses_verified_club_nation() -> None:
    reader = MutableFakeReader()
    layout = SimpleNamespace(
        module=lambda _process: SimpleNamespace(base_address=reader.module_base),
        module_name="fm.exe", club_culture_record_size=0x40,
    )
    allocations = iter(((0x440000, 0x700000), (0x450000, 0x700000)))
    with (
        patch.object(club_vision, "select_process_layout", return_value=(1, "fm.exe", layout)),
        patch.object(club_vision, "open_process", return_value=FakeProcessContext()),
        patch.object(club_vision, "Reader", return_value=reader),
        patch.object(club_vision, "write_process_memory", side_effect=reader.write),
        patch.object(club_vision, "_allocate_native_block", side_effect=lambda *_args: next(allocations)),
        patch.object(club_vision, "_resolve_club_nation_reference", return_value={
            "uid": 765, "item_id": 139, "name": "英格兰", "address": 0x500000,
        }),
        patch.object(club_vision, "_resolve_nation_reference") as selected_nation,
    ):
        result = club_vision.create_club_vision_record(
            reader.team_address, 679, culture_type=88,
            importance=10,
        )

    assert result["type"] == 88
    assert result["reference_type"] == 10
    assert result["reference_id"] == 139
    assert result["reference_uid"] == 765
    assert result["type_name"] == "不签下非英格兰国籍的球员"
    selected_nation.assert_not_called()


def test_create_non_national_target_accepts_custom_nation_on_verified_fm24_steam() -> None:
    reader = MutableFakeReader()
    layout = SimpleNamespace(
        key="fm24", distribution="steam",
        module=lambda _process: SimpleNamespace(base_address=reader.module_base),
        module_name="fm.exe", club_culture_record_size=0x40,
    )
    allocations = iter(((0x440000, 0x700000), (0x450000, 0x700000)))
    with (
        patch.object(club_vision, "select_process_layout", return_value=(1, "fm.exe", layout)),
        patch.object(club_vision, "open_process", return_value=FakeProcessContext()),
        patch.object(club_vision, "Reader", return_value=reader),
        patch.object(club_vision, "write_process_memory", side_effect=reader.write),
        patch.object(club_vision, "_allocate_native_block", side_effect=lambda *_args: next(allocations)),
        patch.object(club_vision, "_resolve_nation_reference", return_value={
            "uid": 110, "item_id": 55, "name": "中国", "address": 0x500000,
        }) as selected_nation,
    ):
        result = club_vision.create_club_vision_record(
            reader.team_address, 679, culture_type=88,
            importance=10, nation_id=110,
        )

    selected_nation.assert_called_once()
    assert result["type"] == 88
    assert result["reference_type"] == 10
    assert result["reference_id"] == 55
    assert result["reference_uid"] == 110
    assert result["type_name"] == "不签下非中国国籍的球员"


def test_create_target_rejects_non_product_importance_before_opening_process() -> None:
    with (
        patch.object(club_vision, "open_process") as opened,
        pytest.raises(ValueError, match="锦上添花"),
    ):
        club_vision.create_club_vision_record(
            0x100000, 679, culture_type=151, importance=5,
        )
    opened.assert_not_called()


def test_create_nationality_target_rejects_same_country_tuple_before_allocation() -> None:
    reader = MutableFakeReader()
    existing = reader.regions[reader.record_address]
    existing[0x2C] = 100
    struct.pack_into("<h", existing, 0x08, 55)
    struct.pack_into("<b", existing, 0x0B, 9)
    layout = SimpleNamespace(
        module=lambda _process: SimpleNamespace(base_address=reader.module_base),
        module_name="fm.exe", club_culture_record_size=0x40,
    )
    with (
        patch.object(club_vision, "select_process_layout", return_value=(1, "fm.exe", layout)),
        patch.object(club_vision, "open_process", return_value=FakeProcessContext()),
        patch.object(club_vision, "Reader", return_value=reader),
        patch.object(club_vision, "_resolve_nation_reference", return_value={
            "uid": 110, "item_id": 55, "name": "中国", "address": 0x500000,
        }),
        patch.object(club_vision, "_allocate_native_block") as allocate,
        pytest.raises(ValueError, match="相同的董事会目标"),
    ):
        club_vision.create_club_vision_record(
            reader.team_address, 679, culture_type=100,
            importance=8, nation_id=110,
        )
    allocate.assert_not_called()


def test_delete_board_target_compacts_vector_and_rejects_stale_state() -> None:
    reader = MutableFakeReader()
    layout = SimpleNamespace(
        module=lambda _process: SimpleNamespace(base_address=reader.module_base),
        module_name="fm.exe",
    )
    with (
        patch.object(club_vision, "select_process_layout", return_value=(1, "fm.exe", layout)),
        patch.object(club_vision, "open_process", return_value=FakeProcessContext()),
        patch.object(club_vision, "Reader", return_value=reader),
        patch.object(club_vision, "write_process_memory", side_effect=reader.write),
    ):
        result = club_vision.delete_club_vision_record(
            reader.team_address, 679, reader.record_address,
            expected=expected_record(),
        )
    assert result["operation"] == "delete"
    begin, end, capacity = struct.unpack("<QQQ", reader.bytes(reader.root_address, 24))
    assert begin == end == reader.vector_address
    assert capacity == reader.vector_address + 8


@pytest.mark.parametrize("fail_readback", [False, True])
def test_create_reuses_operation_directory_and_retains_rollback(fail_readback) -> None:
    reader = MutableFakeReader()
    directory = SimpleNamespace(addresses_for_uid=lambda *_args: [reader.team_address])
    reader.database_index_provider = lambda: directory
    operation = SimpleNamespace(
        reader=reader, process=object(),
        module=SimpleNamespace(base_address=reader.module_base),
    )
    original_header = reader.bytes(reader.root_address, 24)
    allocations = iter(((0x440000, 0x700000), (0x450000, 0x700000)))
    locate = club_vision._locate_vector
    reads = 0

    def checked_locate(*args):
        nonlocal reads
        reads += 1
        if fail_readback and reads == 2:
            raise RuntimeError("simulated readback failure")
        return locate(*args)

    with (
        patch.object(club_vision, "select_process_layout", side_effect=AssertionError("rediscovery")),
        patch.object(club_vision, "open_process", side_effect=AssertionError("reopen")),
        patch.object(club_vision, "Reader", side_effect=AssertionError("new standalone reader")),
        patch.object(club_vision, "DatabaseIndex", side_effect=AssertionError("rebuild directory")),
        patch.object(club_vision, "write_process_memory", side_effect=reader.write),
        patch.object(club_vision, "_allocate_native_block", side_effect=lambda *_args: next(allocations)),
        patch.object(club_vision, "_free_native_block") as free,
        patch.object(club_vision, "_locate_vector", side_effect=checked_locate),
    ):
        if fail_readback:
            with pytest.raises(RuntimeError, match="simulated readback failure"):
                club_vision.create_club_vision_record(
                    reader.team_address, 679, culture_type=151, importance=8, operation=operation,
                )
            assert reader.bytes(reader.root_address, 24) == original_header
            assert free.call_count == 2
        else:
            result = club_vision.create_club_vision_record(
                reader.team_address, 679, culture_type=151, importance=8, operation=operation,
            )
            assert result["type"] == 151
            assert result["record_address"] == "0x440000"
            assert club_vision._record_state(reader.bytes(0x440000, 0x40))["importance"] == 8
            free.assert_not_called()
    assert reads == 2


def test_update_reuses_operation_directory_without_process_rediscovery() -> None:
    reader = MutableFakeReader()
    directory = SimpleNamespace(addresses_for_uid=lambda *_args: [reader.team_address])
    reader.database_index_provider = lambda: directory
    operation = SimpleNamespace(
        reader=reader, process=object(),
        module=SimpleNamespace(base_address=reader.module_base),
    )

    with (
        patch.object(club_vision, "select_process_layout", side_effect=AssertionError("rediscovery")),
        patch.object(club_vision, "open_process", side_effect=AssertionError("reopen")),
        patch.object(club_vision, "Reader", side_effect=AssertionError("new standalone reader")),
        patch.object(club_vision, "DatabaseIndex", side_effect=AssertionError("rebuild directory")),
        patch.object(club_vision, "write_process_memory", side_effect=reader.write),
    ):
        result = club_vision.update_club_vision_record(
            reader.team_address, 679, reader.record_address,
            expected=expected_record(), updates={"importance": 8}, operation=operation,
        )

    assert result["changed"] is True
    assert result["importance"] == 8
    assert club_vision._record_state(reader.bytes(reader.record_address, 0x40))["importance"] == 8


@pytest.mark.parametrize("nation", [False, True])
def test_create_endpoint_returns_verified_item_without_post_commit_scans(monkeypatch, nation) -> None:
    from contextlib import nullcontext
    import threading
    import fm_odds_web as server

    session = object()
    receipt = {
        "changed": True, "operation": "create", "team_id": 679,
        "record_address": "0x440000", "type": 100 if nation else 151,
        "importance": 8, "source_type": 1, "value_raw": -1,
        "reference_type": club_vision.COUNTRY_REFERENCE_TYPES[100] if nation else -1,
        "reference_id": 73 if nation else -1,
    }
    if nation:
        receipt.update(reference_uid=769, reference_name="葡萄牙")
    state = SimpleNamespace(
        memory_lock=threading.Lock(),
        _timed_user_memory_operation=lambda _name: nullcontext({}),
        _owned_world_club_target=lambda _id: ("isolated-test", {}, {"address": "0x100000"}),
    )

    def borrow(**kwargs):
        assert kwargs == {"write_memory": True, "create_thread": True}
        return nullcontext(session)

    def create(*args, **kwargs):
        assert kwargs["operation"] is session
        return receipt

    def forbidden(*args, **kwargs):
        raise AssertionError("unnecessary post-commit full snapshot/catalog scan")

    monkeypatch.setattr(server, "borrow_game_operation", borrow)
    monkeypatch.setattr(server, "create_club_vision_record", create)
    for name in ("read_native_world_club_snapshot", "club_vision_nation_options", "club_vision_reference_names"):
        monkeypatch.setattr(server, name, forbidden)
    response = server.LocalOddsState.update_owned_world_club_vision(
        state, {"team_id": 679, "operation": "create", "type": receipt["type"], "importance": 8},
    )
    item = response["created_item"]
    assert response["result"] is receipt
    assert item["address"] == receipt["record_address"]
    assert item["editable"] and item["deletable"]
    assert item["importance"] == 8
    if nation:
        assert item["type_name"] == "签下葡萄牙籍球员"


def test_update_endpoint_returns_readback_without_post_commit_scans(monkeypatch) -> None:
    from contextlib import nullcontext
    import threading
    import fm_odds_web as server

    session = object()
    receipt = {
        "changed": True, "team_id": 679, "record_address": "0x430000",
        "type": 150, "importance": 8, "source_type": 1, "value_raw": -1,
        "reference_type": -1, "reference_id": -1, "type_name": "进攻足球",
    }
    foreground_operations = []
    state = SimpleNamespace(
        memory_lock=threading.Lock(),
        _timed_user_memory_operation=lambda name: (
            foreground_operations.append(name) or nullcontext({})
        ),
        _owned_world_club_target=lambda _id: ("isolated-test", {}, {"address": "0x100000"}),
    )

    monkeypatch.setattr(server, "borrow_game_operation", lambda **kwargs: nullcontext(session))
    monkeypatch.setattr(
        server, "update_club_vision_record",
        lambda *args, **kwargs: receipt if kwargs.get("operation") is session else None,
    )
    monkeypatch.setattr(server, "load_acquired_clubs", lambda _scope: {"clubs": [{"id": 679}]})
    monkeypatch.setattr(server, "update_acquired_club", lambda *_args: None)
    for name in ("read_native_world_club_snapshot", "club_vision_nation_options", "club_vision_reference_names"):
        monkeypatch.setattr(server, name, lambda *_args, _name=name: (_ for _ in ()).throw(
            AssertionError(f"unnecessary post-commit scan: {_name}")
        ))

    response = server.LocalOddsState.update_owned_world_club_vision(state, {
        "team_id": 679, "record_address": "0x430000",
        "expected": expected_record(), "updates": {"importance": 8},
    })
    assert response == {"result": receipt, "updated_item": receipt}
    assert foreground_operations == ["owned_club_vision"]


def test_delete_endpoint_returns_receipt_without_post_commit_scans(monkeypatch) -> None:
    from contextlib import nullcontext
    import fm_odds_web as server

    receipt = {
        "changed": True, "operation": "delete", "team_id": 679,
        "record_address": "0x430000", "type": 150,
    }
    state = SimpleNamespace(
        _timed_user_memory_operation=lambda _name: nullcontext({}),
        _owned_world_club_target=lambda _id: (
            "isolated-test", {}, {"address": "0x100000"},
        ),
    )
    monkeypatch.setattr(server, "delete_club_vision_record", lambda *_args, **_kwargs: receipt)
    for name in (
        "read_native_world_club_snapshot", "club_vision_nation_options",
        "club_vision_reference_names",
    ):
        monkeypatch.setattr(
            server, name,
            lambda *_args, _name=name: (_ for _ in ()).throw(
                AssertionError(f"unnecessary post-commit scan: {_name}")
            ),
        )

    response = server.LocalOddsState.update_owned_world_club_vision(state, {
        "team_id": 679, "operation": "delete", "record_address": "0x430000",
        "expected": expected_record(),
    })

    assert response == {
        "result": receipt, "deleted_record_address": "0x430000",
    }


def test_deleted_vision_receipt_removes_only_matching_ui_row() -> None:
    import subprocess
    from frontend_source import read_frontend_source

    source = read_frontend_source(ROOT / "web" / "app.js", mode="raw")
    apply = source.split("function applyDeletedOwnedClubVision(detail, result) {", 1)[1].split(
        "async function deleteOwnedClubVision", 1,
    )[0]
    script = '''
const assert = require('node:assert/strict');
const uiText = key => key;
''' + "function applyDeletedOwnedClubVision(detail, result) {" + apply + '''
const first = {address:'0x430000',editable:true};
const second = {address:'0x440000',editable:false};
const detail = {club_vision:{available:true,items:[first,second],nation_options:[{uid:769}]}};
applyDeletedOwnedClubVision(detail, {deleted_record_address:'0x430000'});
assert.deepEqual(detail.club_vision.items, [second]);
assert.equal(detail.club_vision.editable, false);
assert.equal(detail.club_vision.nation_options[0].uid, 769);
const full = {available:true,items:[]};
applyDeletedOwnedClubVision(detail, {club_vision:full});
assert.equal(detail.club_vision, full);
assert.throws(() => applyDeletedOwnedClubVision(detail, {}));
'''
    subprocess.run(
        ["node", "-"], input=script, text=True, encoding="utf-8",
        check=True, capture_output=True,
    )


def test_updated_vision_receipt_replaces_only_matching_ui_row() -> None:
    import subprocess
    from frontend_source import read_frontend_source

    source = read_frontend_source(ROOT / "web" / "app.js", mode="raw")
    apply = source.split("function applyUpdatedOwnedClubVision(detail, result) {", 1)[1].split("async function saveOwnedClubVision", 1)[0]
    script = '''
const assert = require('node:assert/strict');
const uiText = key => key;
''' + "function applyUpdatedOwnedClubVision(detail, result) {" + apply + '''
const first = {address:'0x430000',importance:9,type_options:[{value:150}],reference_name:'Kept'};
const second = {address:'0x440000',importance:6};
const detail = {club_vision:{available:true,items:[first,second],nation_options:[{uid:769}]}};
applyUpdatedOwnedClubVision(detail, {updated_item:{record_address:'0x430000',importance:8,type:151}});
assert.equal(detail.club_vision.items[0].address, '0x430000');
assert.equal(detail.club_vision.items[0].importance, 8);
assert.equal(detail.club_vision.items[0].type, 151);
assert.equal(detail.club_vision.items[0].reference_name, 'Kept');
assert.equal(detail.club_vision.items[0].type_options, first.type_options);
assert.equal(detail.club_vision.items[1], second);
assert.equal(detail.club_vision.nation_options[0].uid, 769);
const full = {available:true,items:[]};
applyUpdatedOwnedClubVision(detail, {club_vision:full});
assert.equal(detail.club_vision, full);
assert.throws(() => applyUpdatedOwnedClubVision(detail, {}));
'''
    subprocess.run(["node", "-"], input=script, text=True, encoding="utf-8", check=True, capture_output=True)


def test_created_vision_receipt_updates_ui_without_losing_existing_targets() -> None:
    import subprocess
    from frontend_source import read_frontend_source

    source = read_frontend_source(ROOT / "web" / "app.js", mode="raw")
    render = source.split("function renderOwnedClubVision(detail) {", 1)[1].split("async function saveOwnedClubVision", 1)[0]
    apply = source.split("function applyCreatedOwnedClubVision(detail, result) {", 1)[1].split("async function createOwnedClubVision", 1)[0]
    script = '''
const assert = require('node:assert/strict');
const uiText = key => key;
const escapeHtml = value => String(value);
const localizedVisionCategory = () => 'Other';
const localizedVisionImportance = value => String(value);
const localizedVisionTarget = item => item?.label || item?.type_name || String(item?.value ?? item?.type ?? '');
const localizedNationName = nation => String(nation?.name || nation || '');
''' + "function renderOwnedClubVision(detail) {" + render + "function applyCreatedOwnedClubVision(detail, result) {" + apply + '''
const existing = {address:'0x430000', type_name:'Existing competition', editable:true};
const nations = [{uid:769,name:'Portugal'}];
const catalog = [{value:151,label:'Attack',available:true}];
const detail = {club_vision:{available:true,items:[existing],nation_options:nations,catalog_options:catalog}};
const created = {address:'0x440000',type_name:'Attack',editable:true,deletable:true};
applyCreatedOwnedClubVision(detail, {created_item:created});
assert.equal(detail.club_vision.items.length, 2);
assert.equal(detail.club_vision.items[0], existing);
assert.equal(detail.club_vision.nation_options, nations);
assert.equal(detail.club_vision.catalog_options, catalog);
applyCreatedOwnedClubVision(detail, {created_item:created});
assert.equal(detail.club_vision.items.length, 2);
const html = renderOwnedClubVision(detail);
assert(!html.includes('owned.vision.catalog_summary'));
assert(html.includes('Existing competition'));
assert(html.includes('Attack'));
assert(html.includes('data-owned-vision-save'));
assert(html.includes('data-owned-vision-delete'));
assert.throws(() => applyCreatedOwnedClubVision(detail, {}));
const full = {available:true,items:[]};
applyCreatedOwnedClubVision(detail, {club_vision:full});
assert.equal(detail.club_vision, full);
'''
    subprocess.run(["node", "-"], input=script, text=True, encoding="utf-8", check=True, capture_output=True)


def test_owned_club_frontend_exposes_internal_vision_tab_and_endpoint() -> None:
    from frontend_source import read_frontend_source

    script = read_frontend_source(ROOT / "web" / "app.js", mode="raw")
    styles = (ROOT / "web" / "app.css").read_text(encoding="utf-8")
    server = (ROOT / "fm_odds_web.py").read_text(encoding="utf-8")
    assert '"owned.detail.vision"' in script
    assert 'data-world-vision="${Number(club.id)}"' not in script
    assert 'activeTab = "overview"' in script
    assert 'request("/api/world-clubs/vision"' in script
    assert 'data-owned-vision-save' in script
    assert 'data-owned-vision-create' in script
    assert 'data-owned-vision-delete' in script
    assert 'operation:"create"' in script
    assert 'operation:"delete"' in script
    assert "新增仅使用已验证的无引用目标类型" not in script
    assert "提出方固定为董事会" not in script
    assert "data-owned-vision-source" not in script
    assert 'row.start_date, row.end_date' not in script
    assert 'data-owned-vision-create-nation' in script
    assert 'data-owned-vision-target-dialog' in script
    assert 'data-owned-vision-nation-dialog' in script
    assert 'data-owned-vision-target-choice' in script
    assert 'data-owned-vision-nation-choice' in script
    assert 'data-owned-vision-picker-search' in script
    assert '<details class="owned-vision-row"' in script
    assert '<summary><h4>' in script
    assert 'referenceLabel' not in script
    assert '不关联具体对象' not in script
    assert 'data-owned-vision-create-value' in script
    assert 'parameter === "age"' in script
    assert 'parameter === "years"' in script
    assert 'type === 88' in script
    assert 'localizedNationName(detail.club_information?.nation || club?.nation || "")' in script
    assert '"owned.vision.nation_exclude"' in script
    assert '{nation:homeNationName}' in script
    assert 'afterChoice:syncVisionTargetLabel' in script
    assert '暂不可新增' not in script
    assert '"owned.vision.addable"' in script
    assert '.owned-vision-create-picker[hidden]' in styles
    assert '.owned-vision-picker-dialog [hidden]' in styles
    assert '[2, 6, 8, 10].includes(importance)' in script
    assert 'Array.from({length:10}' not in script
    assert 'if (value <= 2) return 2;' in script
    assert 'if (value <= 6) return 6;' in script
    assert 'if (value <= 8) return 8;' in script
    assert '原始值 ${selected}，请选择' not in script
    assert '${escapeHtml(option.label)}（${Number(option.value)}）' not in script
    from fm_odds_web import API_ROUTES
    assert API_ROUTES.resolve("POST", "/api/world-clubs/vision") is not None
    assert "update_owned_world_club_vision" in server
