from __future__ import annotations

import struct
import threading
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import club_reader
from tools.initial_data_audit import (
    CLUB_NAME_FULL, CLUB_NAME_SHORT, ENTITY_UID, TEAM_CLUB, TEAM_STADIUM,
)
from tools.world_clubs import merge_native_world_clubs


ROOT = (Path(__file__).resolve().parents[1] / "src")


class FakeReader:
    def __init__(self, layout):
        self.layout = layout
        self.module_base = 0x10000000
        self.string_cache = {}
        self.pointers = {
            0x1000 + TEAM_CLUB: 0x2000,
            0x2000: self.module_base + layout.club_vtable_rva,
            0x2000 + layout.club_detail2_offset: 0x3000,
            0x2000 + CLUB_NAME_FULL: 0x4000,
            0x2000 + CLUB_NAME_SHORT: 0x4100,
        }
        self.u32_values = {
            0x1000 + ENTITY_UID: 42,
            0x2000 + ENTITY_UID: 42,
        }
        self.u8_values = {
            0x3000 + layout.club_training_facilities_offset: 10,
            0x3000 + layout.club_youth_facilities_offset: 15,
            0x3000 + layout.club_junior_coaching_offset: 8,
            0x3000 + layout.club_youth_recruitment_offset: 12,
        }
        self.u16_values = {}
        self.strings = {0x4000: "旧俱乐部", 0x4100: "旧俱乐部"}

    def ptr(self, address):
        return self.pointers.get(address, 0)

    def u32(self, address):
        return self.u32_values.get(address)

    def u8(self, address):
        return self.u8_values.get(address)

    def u16(self, address):
        return self.u16_values.get(address)

    def team(self, address):
        return {"id": 42, "name": "旧俱乐部", "team_type": "club"} if address == 0x1000 else None

    def fm_string_at(self, address):
        return self.strings.get(self.ptr(address))

    def bytes(self, address, size):
        for pointer, value in self.strings.items():
            block = club_reader._fm_utf8_string_block(value.encode("utf-8"))
            block_start = pointer - 12
            if block_start <= address and address + size <= block_start + len(block):
                offset = address - block_start
                return block[offset:offset + size]
        return None


def runtime(monkeypatch):
    module = SimpleNamespace(base_address=0x10000000)
    layout = SimpleNamespace(
        key="fm26",
        module_name="game_plugin.dll",
        club_vtable_rva=0x1234,
        club_detail2_offset=0x100,
        club_training_facilities_offset=0x118,
        club_youth_facilities_offset=0x123,
        club_junior_coaching_offset=0x124,
        club_youth_recruitment_offset=0x125,
        club_loans_offset=0x48,
        club_debt_record_size=0x1C,
        team_reputation_offset=0x90,
        module=lambda _process: module,
    )
    reader = FakeReader(layout)
    process = SimpleNamespace(pid=77)

    @contextmanager
    def opened(_pid, **_kwargs):
        yield process

    monkeypatch.setattr(club_reader, "select_process_layout", lambda: (77, "fm.exe", layout))
    monkeypatch.setattr(club_reader, "open_process", opened)
    monkeypatch.setattr(club_reader, "Reader", lambda _process, _base, _layout: reader)
    monkeypatch.setattr(club_reader, "_resolve_team_address", lambda _reader, _team_id, _address: 0x1000)
    return reader


def test_facility_level_read_skips_unrelated_club_containers(monkeypatch):
    reader = runtime(monkeypatch)

    assert club_reader.read_native_club_facility_levels(
        42, "0x1000", reader=reader,
    ) == {
        "training": 10, "youth": 15,
        "junior": 8, "recruitment": 12,
    }


def test_brand_reputation_read_skips_unrelated_club_containers(monkeypatch):
    reader = runtime(monkeypatch)
    reader.u16_values[0x1000 + reader.layout.team_reputation_offset] = 4_100

    assert club_reader.read_native_club_reputation(
        42, "0x1000", reader=reader,
    ) == 4_100


def test_training_facility_upgrade_writes_one_level(monkeypatch):
    reader = runtime(monkeypatch)

    def write_memory(_process, address, data):
        reader.u8_values[address] = data[0]

    monkeypatch.setattr(club_reader, "write_process_memory", write_memory)
    monkeypatch.setattr(
        club_reader, "read_process_memory",
        lambda _process, address, size: bytes([reader.u8_values[address]]) if size == 1 else None,
    )

    result = club_reader.upgrade_native_club_facility(
        42, "0x1000", "training", expected_level=10,
    )

    assert result == {"team_id": 42, "facility": "training", "before": 10, "after": 11}
    assert reader.u8_values[0x3000 + reader.layout.club_training_facilities_offset] == 11


def test_facility_upgrade_matches_fmrte_single_write_without_delayed_rollback(monkeypatch):
    reader = runtime(monkeypatch)
    address = 0x3000 + reader.layout.club_training_facilities_offset

    def write_memory(_process, target, data):
        reader.u8_values[target] = data[0]

    monkeypatch.setattr(club_reader, "write_process_memory", write_memory)
    monkeypatch.setattr(
        club_reader, "read_process_memory",
        lambda _process, target, size: bytes([reader.u8_values[target]]) if size == 1 else None,
    )
    monkeypatch.setattr(
        club_reader, "sleep",
        lambda _seconds: pytest.fail("FMRTE 设施 setter 没有延迟二次回滚"),
    )

    result = club_reader.upgrade_native_club_facility(
        42, "0x1000", "training", expected_level=10,
    )

    assert result["after"] == 11
    assert reader.u8_values[address] == 11


def test_sugar_daddy_write_validates_finance_back_reference_and_reads_back(monkeypatch):
    reader = runtime(monkeypatch)
    reader.layout.finance_sugar_daddy_offset = 0x20
    reader.layout.club_finance_offset = 0x150
    reader.pointers[0x2000 + 0x150] = 0x7000
    reader.pointers[0x7000 + 0x08] = 0x2000
    address = 0x7000 + 0x20
    reader.u8_values[address] = 1
    operation = SimpleNamespace(
        reader=reader, process=SimpleNamespace(pid=77),
        layout=reader.layout, writable=True,
    )

    def write_memory(_process, target, data, **_kwargs):
        reader.u8_values[target] = data[0]

    monkeypatch.setattr(club_reader, "write_process_memory", write_memory)

    result = club_reader.update_native_club_sugar_daddy(
        42, "0x1000", 3, expected=1, operation=operation,
    )

    assert result == {
        "team_id": 42, "before": 1, "after": 3,
        "before_name": "疯狂烧钱型", "after_name": "填补亏空型",
    }
    assert reader.u8_values[address] == 3


def test_owned_club_debt_repayment_zeros_verified_amounts(monkeypatch):
    reader = runtime(monkeypatch)
    debt = struct.pack(
        "<IIIIII4B", 64_000_000, 359_550, 12_000, 8_000,
        0, 0, 15, 1, 0, 0,
    )
    memory = {
        0x3000 + reader.layout.club_loans_offset: struct.pack(
            "<QQQ", 0x5000, 0x5008, 0x5008,
        ),
        0x5000: struct.pack("<Q", 0x6000),
        0x6000: debt,
    }
    reader.bytes = lambda address, size: memory.get(address, b"")[:size]

    def write_memory(_process, address, data):
        memory[address] = bytes(data)

    monkeypatch.setattr(club_reader, "write_process_memory", write_memory)

    assert club_reader.read_native_club_debt_total(
        42, "0x1000", reader=reader,
    ) == 64_000_000

    result = club_reader.repay_native_club_debts(
        42, "0x1000", expected_total=64_000_000,
    )

    assert result == {
        "team_id": 42, "before": 64_000_000, "after": 0,
        "repaid_records": 1,
    }
    assert memory[0x6000][:0x10] == b"\0" * 0x10
    assert memory[0x6000][0x10:] == debt[0x10:]


def test_owned_club_debt_repayment_accepts_unknown_native_source(monkeypatch):
    """Unknown debt enum values remain writable after structural validation."""
    reader = runtime(monkeypatch)
    debt = struct.pack(
        "<IIIIII4B", 12_000_000, 100_000, 2_000, 0,
        0, 0, 17, 1, 0, 0,
    )
    memory = {
        0x3000 + reader.layout.club_loans_offset: struct.pack(
            "<QQQ", 0x5000, 0x5008, 0x5008,
        ),
        0x5000: struct.pack("<Q", 0x6000),
        0x6000: debt,
    }
    reader.bytes = lambda address, size: memory.get(address, b"")[:size]

    monkeypatch.setattr(
        club_reader, "write_process_memory",
        lambda _process, address, data: memory.__setitem__(address, bytes(data)),
    )

    result = club_reader.repay_native_club_debts(
        42, "0x1000", expected_total=12_000_000,
    )

    assert result["before"] == 12_000_000
    assert memory[0x6000][:0x10] == b"\0" * 0x10


def test_owned_club_debt_repayment_charges_and_refunds_on_native_failure(monkeypatch):
    import fm_odds_web

    state = fm_odds_web.LocalOddsState.__new__(fm_odds_web.LocalOddsState)
    state.memory_lock = threading.RLock()
    state._owned_world_club_target = lambda team_id, **_kwargs: (
        "scope-1", {},
        {"id": team_id, "name": "测试俱乐部", "address": "0x1000"},
    )
    monkeypatch.setattr(
        fm_odds_web, "read_native_club_debt_total",
        lambda _team_id, _address: 64_000_000,
    )
    payment = {"total": 64_000_000.0, "bank": 64_000_000.0, "wallet": 0.0}
    monkeypatch.setattr(
        fm_odds_web, "charge_combined_funds",
        lambda amount, event, **_metadata: payment
        if (amount, event) == (64_000_000, "world_club_debt_repayment")
        else pytest.fail("还债扣款参数错误"),
    )
    refunds = []
    monkeypatch.setattr(
        fm_odds_web, "refund_combined_funds",
        lambda paid, event, **metadata: refunds.append((paid, event, metadata)),
    )
    monkeypatch.setattr(
        fm_odds_web, "repay_native_club_debts",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("写入失败")),
    )

    with pytest.raises(RuntimeError, match="写入失败"):
        state.repay_owned_world_club_debt({
            "team_id": 42, "expected_debt": 64_000_000,
        })

    assert refunds[0][0] is payment
    assert refunds[0][1] == "world_club_debt_repayment_rollback"


def test_owned_brand_operations_use_targeted_reputation_read(monkeypatch):
    import fm_odds_web

    state = fm_odds_web.LocalOddsState.__new__(fm_odds_web.LocalOddsState)
    state.memory_lock = threading.RLock()
    state.lock = threading.RLock()
    state.world_club_cache = {"clubs": [{"id": 42, "reputation": 4_100}]}
    state._owned_world_club_target = lambda team_id, **_kwargs: (
        "scope-1", {},
        {"id": team_id, "name": "测试俱乐部", "address": "0x1000"},
    )
    reads = []
    monkeypatch.setattr(
        fm_odds_web, "read_native_club_reputation",
        lambda team_id, address: reads.append((team_id, address)) or 4_100,
    )
    monkeypatch.setattr(
        fm_odds_web, "read_native_world_club_snapshot",
        lambda *_args: pytest.fail("brand action must not read a full club snapshot"),
    )
    monkeypatch.setattr(
        fm_odds_web, "charge_combined_funds",
        lambda amount, event, **_metadata: {"total": amount, "event": event},
    )
    monkeypatch.setattr(
        fm_odds_web, "apply_team_reputation_delta",
        lambda *_args: {"before": 4_100, "after": 4_150, "applied": 50},
    )
    monkeypatch.setattr(
        fm_odds_web, "update_acquired_club",
        lambda team_id, updates, _scope: {"id": team_id, **updates},
    )
    monkeypatch.setattr(fm_odds_web, "public_economy", lambda: {"wallet": 1})

    quote = state.owned_world_club_brand_quote(42)
    result = state.promote_owned_world_club_brand({"team_id": 42, "delta": 50})

    assert quote["reputation"] == 4_100
    assert result["native"]["after"] == 4_150
    assert reads == [(42, "0x1000"), (42, "0x1000")]


@pytest.mark.parametrize(
    ("facility", "level", "offset_name"),
    (
        ("junior", 8, "club_junior_coaching_offset"),
        ("recruitment", 12, "club_youth_recruitment_offset"),
    ),
)
def test_youth_staff_facility_upgrades_write_one_level(
    monkeypatch, facility, level, offset_name,
):
    reader = runtime(monkeypatch)

    def write_memory(_process, address, data):
        reader.u8_values[address] = data[0]

    monkeypatch.setattr(club_reader, "write_process_memory", write_memory)
    monkeypatch.setattr(
        club_reader, "read_process_memory",
        lambda _process, address, size: bytes([reader.u8_values[address]]) if size == 1 else None,
    )

    result = club_reader.upgrade_native_club_facility(
        42, "0x1000", facility, expected_level=level,
    )

    assert result == {
        "team_id": 42, "facility": facility, "before": level, "after": level + 1,
    }
    assert reader.u8_values[0x3000 + getattr(reader.layout, offset_name)] == level + 1


def test_recruitment_floor_restore_can_write_multiple_levels(monkeypatch):
    reader = runtime(monkeypatch)
    address = 0x3000 + reader.layout.club_youth_recruitment_offset

    def write_memory(_process, target, data):
        reader.u8_values[target] = data[0]

    monkeypatch.setattr(club_reader, "write_process_memory", write_memory)
    monkeypatch.setattr(
        club_reader, "read_process_memory",
        lambda _process, target, size: (
            bytes([reader.u8_values[target]]) if size == 1 else None
        ),
    )

    result = club_reader.set_native_club_facility_level(
        42, "0x1000", "recruitment", 17, expected_level=12,
    )

    assert result == {
        "team_id": 42, "facility": "recruitment", "before": 12, "after": 17,
    }
    assert reader.u8_values[address] == 17


def test_facility_upgrade_rejects_stale_quote_and_level_twenty(monkeypatch):
    reader = runtime(monkeypatch)
    with pytest.raises(ValueError, match="已经变化"):
        club_reader.upgrade_native_club_facility(42, "0x1000", "youth", expected_level=14)

    reader.u8_values[0x3000 + reader.layout.club_youth_facilities_offset] = 20
    with pytest.raises(ValueError, match="达到 20 级"):
        club_reader.upgrade_native_club_facility(42, "0x1000", "youth", expected_level=20)


def test_rename_updates_full_and_short_names_through_separate_paths(monkeypatch):
    reader = runtime(monkeypatch)
    def allocate(_process, encoded):
        reader.strings[0x500C] = encoded.decode("utf-8")
        return 0x500C, 0x5000

    monkeypatch.setattr(club_reader, "_allocate_fm_utf8_string", allocate)
    monkeypatch.setattr(club_reader, "_free_fm_utf8_string", lambda *_args: None)
    monkeypatch.setattr(club_reader, "sleep", lambda _seconds: None)

    def write_memory(_process, address, data):
        if address in {0x2000 + CLUB_NAME_FULL, 0x2000 + CLUB_NAME_SHORT}:
            reader.pointers[address] = struct.unpack("<Q", data)[0]
            return
        if address == 0x4004:
            reader.strings[0x4000] = bytes(data).decode("utf-8")

    monkeypatch.setattr(club_reader, "write_process_memory", write_memory)

    result = club_reader.rename_native_club(
        42, "0x1000", "新俱乐部", new_short_name="新俱乐部",
    )

    assert result["before"] == {"name": "旧俱乐部", "short_name": "旧俱乐部"}
    assert result["after"] == {"name": "新俱乐部", "short_name": "新俱乐部"}
    assert reader.fm_string_at(0x2000 + CLUB_NAME_FULL) == "新俱乐部"
    assert reader.fm_string_at(0x2000 + CLUB_NAME_SHORT) == "新俱乐部"
    assert reader.ptr(0x2000 + CLUB_NAME_FULL) == 0x4000
    assert reader.ptr(0x2000 + CLUB_NAME_SHORT) == 0x500C


def test_fm_utf8_string_block_matches_fmrte_reference_layout():
    encoded = "曼彻斯特城".encode("utf-8")
    block = club_reader._fm_utf8_string_block(encoded)

    assert struct.unpack_from("<Q", block, 0)[0] == len(encoded) + 9
    assert struct.unpack_from("<I", block, 8)[0] == 1
    assert struct.unpack_from("<I", block, 12)[0] == len(encoded)
    assert block[16:-1] == encoded
    assert block[-1:] == b"\0"


def test_rename_rejects_different_full_name_shape_before_allocation(monkeypatch):
    runtime(monkeypatch)
    monkeypatch.setattr(
        club_reader, "_allocate_fm_utf8_string",
        lambda *_args: pytest.fail("容量校验失败前不应分配内存"),
    )

    with pytest.raises(ValueError, match="字符数和存储长度") as error:
        club_reader.rename_native_club(42, "0x1000", "过长的新俱乐部名称")
    assert "FMRTE" not in str(error.value)


def test_rename_rejects_different_character_count_even_when_bytes_fit(monkeypatch):
    runtime(monkeypatch)
    with pytest.raises(ValueError, match="字符数和存储长度") as error:
        club_reader.rename_native_club(42, "0x1000", "123456789012")
    assert "FMRTE" not in str(error.value)


def test_rename_short_name_only_leaves_full_name_untouched(monkeypatch):
    reader = runtime(monkeypatch)

    def allocate(_process, encoded):
        reader.strings[0x500C] = encoded.decode("utf-8")
        return 0x500C, 0x5000

    writes = []

    def write_memory(_process, address, data):
        writes.append(address)
        if address == 0x2000 + CLUB_NAME_SHORT:
            reader.pointers[address] = struct.unpack("<Q", data)[0]

    monkeypatch.setattr(club_reader, "_allocate_fm_utf8_string", allocate)
    monkeypatch.setattr(club_reader, "_free_fm_utf8_string", lambda *_args: None)
    monkeypatch.setattr(club_reader, "write_process_memory", write_memory)
    monkeypatch.setattr(club_reader, "sleep", lambda _seconds: None)

    result = club_reader.rename_native_club(
        42, "0x1000", new_short_name="新简称可以更长",
    )

    assert result["after"] == {
        "name": "旧俱乐部", "short_name": "新简称可以更长",
    }
    assert reader.fm_string_at(0x2000 + CLUB_NAME_FULL) == "旧俱乐部"
    assert reader.fm_string_at(0x2000 + CLUB_NAME_SHORT) == "新简称可以更长"
    assert writes == [0x2000 + CLUB_NAME_SHORT]


def test_rename_allows_fm26_managed_club_for_explicit_testing(monkeypatch):
    reader = runtime(monkeypatch)

    def allocate(_process, encoded):
        reader.strings[0x500C] = encoded.decode("utf-8")
        return 0x500C, 0x5000

    def write_memory(_process, address, data):
        if address == 0x2000 + CLUB_NAME_SHORT:
            reader.pointers[address] = struct.unpack("<Q", data)[0]

    monkeypatch.setattr(club_reader, "_allocate_fm_utf8_string", allocate)
    monkeypatch.setattr(club_reader, "_free_fm_utf8_string", lambda *_args: None)
    monkeypatch.setattr(club_reader, "write_process_memory", write_memory)
    monkeypatch.setattr(club_reader, "sleep", lambda _seconds: None)

    result = club_reader.rename_native_club(
        42, "0x1000", new_short_name="新简称", is_managed_club=True,
    )

    assert result["after"]["short_name"] == "新简称"


def test_rename_full_name_decouples_shared_short_pointer_first(monkeypatch):
    reader = runtime(monkeypatch)
    reader.pointers[0x2000 + CLUB_NAME_SHORT] = 0x4000
    writes = []

    def allocate(_process, encoded):
        assert encoded.decode("utf-8") == "旧俱乐部"
        reader.strings[0x500C] = encoded.decode("utf-8")
        return 0x500C, 0x5000

    def write_memory(_process, address, data):
        writes.append(address)
        if address == 0x2000 + CLUB_NAME_SHORT:
            reader.pointers[address] = struct.unpack("<Q", data)[0]
        elif address == 0x4004:
            reader.strings[0x4000] = bytes(data).decode("utf-8")

    monkeypatch.setattr(club_reader, "_allocate_fm_utf8_string", allocate)
    monkeypatch.setattr(club_reader, "_free_fm_utf8_string", lambda *_args: None)
    monkeypatch.setattr(club_reader, "write_process_memory", write_memory)
    monkeypatch.setattr(club_reader, "sleep", lambda _seconds: None)

    result = club_reader.rename_native_club(42, "0x1000", "新俱乐部")

    assert result["after"] == {"name": "新俱乐部", "short_name": "旧俱乐部"}
    assert reader.fm_string_at(0x2000 + CLUB_NAME_FULL) == "新俱乐部"
    assert reader.fm_string_at(0x2000 + CLUB_NAME_SHORT) == "旧俱乐部"
    assert writes[:2] == [0x2000 + CLUB_NAME_SHORT, 0x4004]


def test_rename_rejects_legacy_headerless_name_pointer(monkeypatch):
    reader = runtime(monkeypatch)
    original_bytes = reader.bytes
    reader.bytes = lambda address, size: (
        b"\0" * 16 if address == 0x4000 - 12 and size == 16
        else original_bytes(address, size)
    )

    with pytest.raises(RuntimeError, match="字符串头校验失败"):
        club_reader.rename_native_club(42, "0x1000", "新俱乐部")


def stadium_runtime(monkeypatch, *, fixed_vtable=True):
    module = SimpleNamespace(base_address=0x10000000, size=0x100000)
    layout = SimpleNamespace(
        module_name="game_plugin.dll",
        club_vtable_rva=0x1234,
        stadium_vtable_rva=0x5678 if fixed_vtable else None,
        club_training_ground_offset=0x140,
        stadium_nearby_offset=0x68,
        stadium_name_offset=0x40,
        stadium_id_offset=0x0C,
        stadium_id2_offset=0x10,
        stadium_capacity_offset=0x7C,
        stadium_seating_capacity_offset=0x80,
        stadium_used_capacity_offset=0x84,
        stadium_all_seater_capacity_offset=0x88,
        stadium_expansion_capacity_offset=0x8C,
        stadium_pitch_condition_offset=0x98,
        stadium_pitch_type_offset=0x9D,
        stadium_state_offset=0xAA,
        module=lambda _process: module,
    )

    class StadiumReader:
        def __init__(self):
            self.layout = layout
            self.module_base = module.base_address
            self.string_cache = {}
            self.pointers = {
                0x1000 + TEAM_CLUB: 0x2000,
                0x1000 + TEAM_STADIUM: 0x6000,
                0x2000: module.base_address + layout.club_vtable_rva,
                0x2000 + layout.club_training_ground_offset: 0x6100,
                0x6000: module.base_address + 0x5678,
                0x6100: module.base_address + 0x5678,
                0x6000 + layout.stadium_name_offset: 0x7000,
            }
            self.u32_values = {
                0x2000 + ENTITY_UID: 42,
                0x6000 + layout.stadium_id_offset: 9001,
                0x6000 + layout.stadium_id2_offset: 9001,
            }
            self.u8_values = {
                0x6000 + layout.stadium_state_offset: 16,
                0x6000 + layout.stadium_pitch_condition_offset: 96,
                0x6000 + layout.stadium_pitch_type_offset: 2,
            }
            self.capacities = [40_000, 39_000, 38_000, 39_000, 52_000]
            self.strings = {0x7000: "旧球场"}

        def ptr(self, address):
            return self.pointers.get(address, 0)

        def u32(self, address):
            if 0x6000 + layout.stadium_capacity_offset <= address <= 0x6000 + layout.stadium_expansion_capacity_offset:
                return self.capacities[(address - 0x6000 - layout.stadium_capacity_offset) // 4]
            return self.u32_values.get(address)

        def bytes(self, address, size):
            if address == 0x6000 + layout.stadium_capacity_offset and size == 20:
                return struct.pack("<5I", *self.capacities)
            for pointer, value in self.strings.items():
                block = club_reader._fm_utf8_string_block(value.encode("utf-8"))
                block_start = pointer - 12
                if block_start <= address and address + size <= block_start + len(block):
                    offset = address - block_start
                    return block[offset:offset + size]
            return None

        def u8(self, address):
            return self.u8_values.get(address)

        def fm_string_at(self, address):
            return self.strings.get(self.ptr(address))

    reader = StadiumReader()
    process = SimpleNamespace(pid=77)

    @contextmanager
    def opened(_pid, **_kwargs):
        yield process

    monkeypatch.setattr(club_reader, "select_process_layout", lambda: (77, "fm.exe", layout))
    monkeypatch.setattr(club_reader, "open_process", opened)
    monkeypatch.setattr(club_reader, "Reader", lambda _process, _base, _layout: reader)
    monkeypatch.setattr(club_reader, "_resolve_team_address", lambda *_args: 0x1000)
    monkeypatch.setattr(club_reader, "sleep", lambda _seconds: None)
    return reader, layout


def test_stadium_action_read_skips_unrelated_club_containers(monkeypatch):
    reader, _layout = stadium_runtime(monkeypatch)
    monkeypatch.setattr(
        club_reader, "read_native_club_facility_levels",
        lambda *_args, **_kwargs: {
            "training": 10, "youth": 11, "junior": 12, "recruitment": 13,
        },
    )

    result = club_reader.read_native_stadium_action_state(
        42, "0x1000", reader=reader,
    )

    assert result["stadium"] == {
        "name": "旧球场", "capacity": 40_000, "seating_capacity": 39_000,
        "used_capacity": 38_000, "all_seater_capacity": 39_000,
        "expansion_capacity": 52_000, "state_raw": 16,
        "pitch_condition": 96, "pitch_type": 2,
    }
    assert result["facilities"]["recruitment"] == 13


def test_remote_malloc_timeout_keeps_active_call_cave_allocated(monkeypatch):
    freed = []

    fake_kernel32 = SimpleNamespace(
        LoadLibraryW=lambda _name: 0x200000,
        GetProcAddress=lambda _module, name: 0x200100 if name == b"malloc" else 0x200200,
        VirtualAllocEx=lambda *_args: 0x300000,
        CreateRemoteThread=lambda *_args: 123,
        WaitForSingleObject=lambda _thread, timeout: (
            0x102 if timeout == club_reader.REMOTE_THREAD_WAIT_MS else pytest.fail("unbounded wait")
        ),
        CloseHandle=lambda _thread: 1,
        VirtualFreeEx=lambda *_args: freed.append(_args[1].value) or 1,
    )
    monkeypatch.setattr(club_reader, "kernel32", fake_kernel32)
    monkeypatch.setattr(
        club_reader, "find_module",
        lambda *_args: SimpleNamespace(base_address=0x100000),
    )
    monkeypatch.setattr(club_reader.ctypes, "string_at", lambda *_args: b"X" * 16)
    monkeypatch.setattr(
        club_reader, "read_process_memory", lambda *_args: b"X" * 16,
    )
    monkeypatch.setattr(club_reader, "write_process_memory", lambda *_args: None)

    with pytest.raises(RuntimeError, match="内存分配等待失败"):
        club_reader._remote_malloc_block(SimpleNamespace(handle=1), 64)

    assert freed == []


def test_stadium_update_writes_name_and_coherent_capacities(monkeypatch):
    reader, layout = stadium_runtime(monkeypatch)

    def write_memory(_process, address, data):
        if address == 0x6000 + layout.stadium_capacity_offset:
            reader.capacities = list(struct.unpack("<5I", data))
        elif address == 0x7004:
            reader.strings[0x7000] = bytes(data).decode("utf-8")

    monkeypatch.setattr(club_reader, "write_process_memory", write_memory)
    result = club_reader.update_native_stadium(
        42, "0x1000", name="新球场", current_capacity=60_000,
        expansion_capacity=75_000, expected_name="旧球场",
        expected_capacity=40_000, expected_expansion_capacity=52_000,
    )

    assert reader.capacities == [60_000, 60_000, 60_000, 60_000, 75_000]
    assert result["after"] == {
        "name": "新球场", "capacity": 60_000, "seating_capacity": 60_000,
        "used_capacity": 60_000, "all_seater_capacity": 60_000,
        "expansion_capacity": 75_000,
        "state_raw": 16, "pitch_condition": 96, "pitch_type": 2,
    }


def test_stadium_update_rejects_stale_capacity(monkeypatch):
    stadium_runtime(monkeypatch)
    with pytest.raises(ValueError, match="当前容量已经变化"):
        club_reader.update_native_stadium(
            42, "0x1000", name="旧球场", current_capacity=60_000,
            expansion_capacity=75_000, expected_capacity=39_999,
        )


def test_stadium_rename_rejects_legacy_headerless_name_pointer(monkeypatch):
    reader, _layout = stadium_runtime(monkeypatch)
    original_bytes = reader.bytes
    reader.bytes = lambda address, size: (
        b"\0" * 16 if address == 0x7000 - 12 and size == 16
        else original_bytes(address, size)
    )

    with pytest.raises(RuntimeError, match="存储状态不适合安全修改") as error:
        club_reader.update_native_stadium(42, "0x1000", name="新球场")
    assert "FMRTE" not in str(error.value)


def test_stadium_rename_rejects_shared_name_string(monkeypatch):
    reader, _layout = stadium_runtime(monkeypatch)
    original_bytes = reader.bytes

    def shared_header(address, size):
        raw = original_bytes(address, size)
        if address == 0x7000 - 12 and size == 16 and raw:
            raw = bytearray(raw)
            struct.pack_into("<I", raw, 8, 2)
            return bytes(raw)
        return raw

    reader.bytes = shared_header
    with pytest.raises(RuntimeError, match="存储状态不适合安全修改") as error:
        club_reader.update_native_stadium(42, "0x1000", name="新球场")
    assert "FMRTE" not in str(error.value)


def test_stadium_rename_replaces_managed_pointer_for_different_length(monkeypatch):
    reader, layout = stadium_runtime(monkeypatch)
    writes = []

    def allocate(_process, encoded):
        assert encoded == "全新八字体育中心".encode("utf-8")
        reader.strings[0x800C] = encoded.decode("utf-8")
        return 0x800C, 0x8000

    def write_memory(_process, address, data):
        writes.append(address)
        if address == 0x6000 + layout.stadium_name_offset:
            reader.pointers[address] = struct.unpack("<Q", data)[0]

    monkeypatch.setattr(club_reader, "_allocate_fm_utf8_string", allocate)
    monkeypatch.setattr(club_reader, "_free_fm_utf8_string", lambda *_args: None)
    monkeypatch.setattr(club_reader, "write_process_memory", write_memory)

    result = club_reader.update_native_stadium(
        42, "0x1000", name="全新八字体育中心",
    )

    assert result["after"]["name"] == "全新八字体育中心"
    assert reader.ptr(0x6000 + layout.stadium_name_offset) == 0x800C
    assert writes == [0x6000 + layout.stadium_name_offset]


def test_stadium_update_accepts_dynamic_peer_vtable(monkeypatch):
    reader, layout = stadium_runtime(monkeypatch, fixed_vtable=False)

    def write_memory(_process, address, data):
        if address == 0x6000 + layout.stadium_capacity_offset:
            reader.capacities = list(struct.unpack("<5I", data))

    monkeypatch.setattr(club_reader, "write_process_memory", write_memory)
    result = club_reader.update_native_stadium(
        42, "0x1000", name="旧球场", current_capacity=55_000,
        expansion_capacity=70_000,
    )

    assert result["after"]["capacity"] == 55_000
    assert reader.capacities == [55_000, 55_000, 55_000, 55_000, 70_000]


def test_stadium_state_upgrade_preserves_capacity_block(monkeypatch):
    reader, layout = stadium_runtime(monkeypatch)

    def write_memory(_process, address, data):
        if address == 0x6000 + layout.stadium_capacity_offset:
            reader.capacities = list(struct.unpack("<5I", data))
        elif address == 0x6000 + layout.stadium_state_offset:
            reader.u8_values[address] = data[0]

    monkeypatch.setattr(club_reader, "write_process_memory", write_memory)
    result = club_reader.update_native_stadium(
        42, "0x1000", state_raw=11, expected_state_raw=16,
    )

    assert result["after"]["state_raw"] == 11
    assert reader.capacities == [40_000, 39_000, 38_000, 39_000, 52_000]


def test_stadium_pitch_updates_and_clamps_condition_to_200(monkeypatch):
    reader, layout = stadium_runtime(monkeypatch)
    reader.u8_values[0x6000 + layout.stadium_pitch_condition_offset] = 196

    def write_memory(_process, address, data):
        if address in {
            0x6000 + layout.stadium_pitch_condition_offset,
            0x6000 + layout.stadium_pitch_type_offset,
        }:
            reader.u8_values[address] = data[0]

    monkeypatch.setattr(club_reader, "write_process_memory", write_memory)
    result = club_reader.update_native_stadium(
        42, "0x1000", pitch_condition=200, pitch_type=8,
        expected_pitch_condition=196, expected_pitch_type=2,
    )

    assert result["before"]["pitch_condition"] == 196
    assert result["before"]["pitch_type"] == 2
    assert result["after"]["pitch_condition"] == 200
    assert result["after"]["pitch_type"] == 8
    assert reader.u8_values[0x6000 + layout.stadium_pitch_condition_offset] == 200
    assert reader.u8_values[0x6000 + layout.stadium_pitch_type_offset] == 8


def test_stadium_update_rolls_back_when_name_readback_fails(monkeypatch):
    reader, layout = stadium_runtime(monkeypatch)
    name_writes = 0

    def write_memory(_process, address, data):
        nonlocal name_writes
        if address == 0x6000 + layout.stadium_capacity_offset:
            reader.capacities = list(struct.unpack("<5I", data))
        elif address == 0x7004:
            name_writes += 1
            if name_writes > 1:
                reader.strings[0x7000] = bytes(data).decode("utf-8")

    monkeypatch.setattr(club_reader, "write_process_memory", write_memory)
    with pytest.raises(RuntimeError, match="原值已恢复"):
        club_reader.update_native_stadium(
            42, "0x1000", name="新球场", current_capacity=60_000,
            expansion_capacity=75_000,
        )

    assert reader.capacities == [40_000, 39_000, 38_000, 39_000, 52_000]
    assert reader.fm_string_at(0x6000 + layout.stadium_name_offset) == "旧球场"


def test_stadium_name_trims_fm26_localization_marker():
    raw = "克拉文农场球场".encode("utf-8") + b"\x09"
    reader = SimpleNamespace(
        layout=SimpleNamespace(stadium_name_offset=0x40),
        fm_string_at=lambda _address: None,
        ptr=lambda address: 0x2000 if address == 0x1040 else 0,
        u32=lambda address: len(raw) if address == 0x2000 else None,
        bytes=lambda address, size: raw if address == 0x2004 and size == len(raw) else None,
    )
    assert club_reader._stadium_name(reader, 0x1000) == "克拉文农场球场"


def test_stadium_rename_preserves_fm26_localization_marker(monkeypatch):
    reader, layout = stadium_runtime(monkeypatch)
    pointer = 0x7000
    block_start = pointer - 12
    payload = "旧球场".encode("utf-8") + b"\x09"
    block = bytearray(club_reader._fm_utf8_string_block(payload))
    original_bytes = reader.bytes
    original_u32 = reader.u32

    reader.fm_string_at = lambda _address: None
    reader.u32 = lambda address: len(payload) if address == pointer else original_u32(address)

    def read_bytes(address, size):
        if block_start <= address and address + size <= block_start + len(block):
            offset = address - block_start
            return bytes(block[offset:offset + size])
        return original_bytes(address, size)

    def write_memory(_process, address, data):
        if address == pointer + 4:
            offset = address - block_start
            block[offset:offset + len(data)] = data

    reader.bytes = read_bytes
    monkeypatch.setattr(club_reader, "write_process_memory", write_memory)

    result = club_reader.update_native_stadium(42, "0x1000", name="新球场")

    assert result["after"]["name"] == "新球场"
    assert bytes(block[16:16 + len(payload)]) == "新球场".encode("utf-8") + b"\x09"


def test_owned_stadium_controls_are_inline_paid_actions():
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    assert '"编辑球场"' not in script
    assert '"当前可用"' not in script
    for action in (
        '"stadium-rename"', '"stadium-capacity"',
        '"stadium-expansion"', '"stadium-state"',
        '"stadium-pitch-type"', '"stadium-pitch_condition"',
    ):
        assert action in script
    assert "owned-stadium-capacity" not in html
    assert "owned-stadium-expansion-capacity" not in html
    assert "owned-stadium-rename-price" in html
    assert "owned-stadium-rename-footer" in html
    assert "owned-stadium-capacity-actions" in script
    assert "owned-stadium-pitch-dialog" in html


def test_group_control_deck_exposes_fifth_club_relations_entry():
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    styles = (ROOT / "web" / "app.css").read_text(encoding="utf-8")
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")

    deck = script.split("function portfolioControlDeck", 1)[1].split(
        "function openGroupRenameDialog", 1,
    )[0]
    assert '["relations", "network", "俱乐部关系"' in deck
    assert "repeat(5,minmax(0,1fr))" in styles
    assert ".portfolio-command-5" in styles
    assert 'relations:["CLUB NETWORK", "俱乐部关系"' in script
    assert 'if (kind === "relations") return loadPortfolioRelations' in script
    assert 'request("/api/world-clubs/relations",' in script
    assert 'operation:"create"' in script
    assert 'id="portfolio-relation-start"' in script
    assert 'id="portfolio-relation-end"' in script
    assert "default_start_date" in script
    assert "default_end_date" in script
    assert 'id="portfolio-relation-create-details"' in script
    assert 'id="portfolio-relation-payment"' in script
    assert 'id="portfolio-relation-is-parent"' in script
    assert "data-relation-main checked" in script
    assert "is_parent_club:" in script
    assert "请先填写开始日期和结束日期" in script
    assert '<form method="dialog" class="fmodd-confirm-actions">' in html
    assert 'dialog.addEventListener("close", closed)' in script
    assert "event.submitter === okButton" in script
    assert "const closed = () => finish(false)" in script
    assert "正在提交俱乐部关系…" in script
    assert "const clubOptions = (selected) => clubs.map" in script
    assert "app.portfolioRelationParentId === previousFeederId" in script
    assert "app.portfolioRelationFeederId === previousParentId" in script
    relation_submit = script.split(
        'relationForm?.addEventListener("submit"', 1,
    )[1].split(
        'content.querySelectorAll("[data-relation-save]")', 1,
    )[0]
    assert "fmoddConfirm(" not in relation_submit
    assert "再次点击确认建立" in relation_submit
    assert "dataset.confirmPayload" in relation_submit
    assert 'id="portfolio-relation-create-status"' in script
    assert ".portfolio-relation-create-status" in styles
    assert "详情默认收起；日期已按当前游戏年份预填" not in script
    assert '<details class="portfolio-relation-details"><summary>关系详情</summary>' in script
    assert '<details class="portfolio-relation-details" open>' not in script
    assert ".portfolio-relation-details" in styles
    assert ".portfolio-relation-actions select:hover" in styles
    assert "background-color: #277658" in styles
    assert 'annual_payment:Math.round(toInternalMoney' in script
    assert 'friendly_probability:optionalNumber("[data-relation-friendly]")' in script
    assert 'maximum_players_loaned:optionalNumber("[data-relation-loans]")' in script
    assert 'parent_wage_percentage:optionalNumber("[data-relation-wages]")' in script
    assert "const benefits = [...(createDetails?.querySelectorAll" in script
    assert 'operation:"update"' in script
    assert 'operation:"delete"' in script
    assert "syncPortfolioRelationBenefitDependencies" in script
    assert "data-relation-benefit-requires" in script
    assert "portfolioRelationOptionalNumber" in script
    assert "data-relation-optional-toggle" in script
    assert "data-relation-field-requires" in script
    assert ".portfolio-relation-benefits label[hidden]" in styles
    assert ".portfolio-relation-create-details" in styles
    assert ".portfolio-relation-optional" in styles
    assert ".portfolio-relation-optional[hidden]" in styles
    assert "安全写入完成验证后开放设置" not in script
    assert ".portfolio-relation-create" in styles


def test_owned_club_rename_ui_only_submits_short_name():
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")

    assert 'id="owned-club-rename-name"' not in html
    assert 'id="owned-club-rename-short-name"' in html
    assert "只修改游戏中的俱乐部简称，完整名称保持不变" in html
    assert "short_name:shortName" in script
    assert "name, short_name:shortName" not in script
    assert "result.native?.after?.short_name" in script
    assert "club.renamed_short_name || club.short_name" in script
    assert "display_name:detail.club.short_name" in script


def test_owned_club_rename_uses_quick_target_and_updates_name_caches(monkeypatch):
    import fm_odds_web

    output = {"save_instance_id": "save-1", "game_date": "2026-08-03"}
    native = {"clubs": [{
        "id": 42, "name": "完整名称", "short_name": "旧简称",
        "address": "0x1000",
    }]}
    state = fm_odds_web.LocalOddsState.__new__(fm_odds_web.LocalOddsState)
    state.lock = threading.RLock()
    state.memory_lock = threading.RLock()
    state.output = dict(output)
    state._bind_current_save = lambda: "scope-1"
    state._data_scope_id = lambda _output: "scope-1"
    state._world_club_native_cache = lambda _save_id: native
    targets = []
    state._owned_world_club_target = lambda team_id, **kwargs: (
        targets.append((team_id, kwargs))
        or ("scope-1", output, dict(native["clubs"][0]))
    )
    updated = {}

    monkeypatch.setattr(fm_odds_web, "managed_team_rows", lambda _output: [])
    monkeypatch.setattr(
        fm_odds_web, "charge_combined_funds",
        lambda *_args, **_kwargs: {"total": 4_000_000},
    )
    monkeypatch.setattr(
        fm_odds_web, "rename_native_club",
        lambda *_args, **_kwargs: {
            "after": {"name": "完整名称", "short_name": "新简称"},
        },
    )

    def persist(_team_id, changes, _scope_id):
        updated.update({"id": 42, **changes})
        return dict(updated)

    monkeypatch.setattr(fm_odds_web, "update_acquired_club", persist)
    monkeypatch.setattr(
        fm_odds_web, "account_acquired_clubs",
        lambda *_args, **_kwargs: [dict(updated)],
    )

    result = state.rename_world_club({"team_id": 42, "short_name": "新简称"})

    assert targets == [(42, {"quick_rebind": True})]
    assert result["club"]["name"] == "完整名称"
    assert result["club"]["short_name"] == "新简称"
    assert result["acquired"][0]["renamed_short_name"] == "新简称"
    assert native["clubs"][0]["short_name"] == "新简称"
    assert state.world_club_cache is native


def test_fm26_name_editing_and_transfer_controls_are_available():
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    assert "function selectedGameIsFm26()" in script
    assert 'ownedClubAction("暂未实装","pencil-line","rename-club","",true)' not in script
    assert 'ownedClubAction("暂未实装", "pencil-line", "stadium-rename", "", true)' not in script
    assert 'ownedClubAction("转会","arrow-right-left","player-transfer")' in script
    assert 'ownedClubAction("租借","calendar-arrow-down","player-loan")' in script
    assert 'const loanAction = ownedClubAction("租借","calendar-arrow-down","player-loan");' in script
    assert "const loanAction = Number(playerSquadType) === 0" not in script
    assert "$" + "{loanAction}" in script
    assert "fm26LoanUnavailable" not in script
    assert 'if (action === "player-loan" && selectedGameIsFm26()) return;' not in script
    assert 'toast(result.warning ? `已收购 ${clubName}；${result.warning}` : `已收购 ${clubName}`);' in script
    assert "client_submission_id:submissionId" in script
    assert "ownedClubOperationsInFlight: new Set()" in script
    assert 'submitButton.textContent = loan ? "正在确认租借" : "正在确认转会"' in script
    assert '? "租借成功"' in script
    assert 'ownedClubAction("下调","arrow-down-to-line","player-demote")' in script
    assert 'ownedClubAction("上调","arrow-up-to-line","player-promote")' in script
    assert 'request("/api/world-clubs/player-squad"' in script


def test_owned_staff_controls_protect_every_club_controller_role():
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    assert "function isClubControllerJobType(value)" in script
    assert "[4, 66, 70].includes(Number(value))" in script
    assert "isClubControllerJobType(row.job_type) ? \"\"" in script
    assert 'const ownerRole = club?.owner_role || info.chairman?.role || "主席"' in script
    assert "ownedClubMetric(ownerRole, ownerName)" in script


def test_native_name_overrides_stale_competition_snapshot():
    merged = merge_native_world_clubs(
        {"clubs": [{"id": 42, "name": "旧俱乐部", "competition": "测试联赛"}]},
        {"clubs": [{"id": 42, "name": "新俱乐部", "short_name": "新俱乐部", "address": "0x1000"}]},
    )
    assert merged["clubs"][0]["name"] == "新俱乐部"


@pytest.mark.parametrize("job_type", [4, 66, 70])
def test_club_controller_job_types_include_chairman_owner_and_president(job_type):
    assert club_reader.is_club_controller_job_type(job_type)


@pytest.mark.parametrize("job_type", [0, 6, 8, 10, 16, None, ""])
def test_club_controller_job_types_exclude_non_owners(job_type):
    assert not club_reader.is_club_controller_job_type(job_type)


def test_club_controller_selection_uses_stable_role_priority():
    rows = [
        {"id": 3, "name": "President", "job_type": 70},
        {"id": 2, "name": "Owner", "job_type": 66},
        {"id": 1, "name": "Chairman", "job_type": 4},
    ]
    assert club_reader._club_controller_staff(rows) == rows[2]


@pytest.mark.parametrize(
    ("game_key", "job_offset"),
    [("fm24", 0x1C), ("fm26", 0x26)],
)
def test_sparse_controller_contract_scan_finds_every_target_role(
    monkeypatch, game_key, job_offset,
):
    team_address = 0x5000
    contract_vtable = 0x10000400
    person_vtable = 0x10000200
    region_data = bytearray(0x100)
    for offset, person, job_type in (
        (0x10, 0x7000, 4),
        (0x50, 0x8000, 66),
    ):
        struct.pack_into("<Q", region_data, offset, contract_vtable)
        struct.pack_into("<Q", region_data, offset + 0x08, person)
        struct.pack_into("<Q", region_data, offset + 0x10, team_address)
        region_data[offset + job_offset] = job_type

    class SparseReader:
        module_base = 0x10000000
        process = SimpleNamespace(pid=77)
        layout = SimpleNamespace(
            key=game_key,
            staff_job_type_offset=job_offset,
            staff_person_vtable_rva=0x200,
        )

        def ptr(self, address):
            if address == 0x1010:
                return contract_vtable
            if address in {0x7000, 0x8000}:
                return person_vtable
            return 0

        def u8(self, address):
            if address == 0x1010 + job_offset:
                return 4
            return None

        def bytes(self, address, size):
            if address == 0x1000 and size == len(region_data):
                return bytes(region_data)
            return None

    monkeypatch.setattr(club_reader, "_scan_staff", lambda *_args: [])
    monkeypatch.setattr(
        club_reader, "_staff_scan_entries",
        lambda *_args: [(0x7000, 0x1010)],
    )
    monkeypatch.setattr(
        club_reader, "iter_readable_regions",
        lambda _process: [
            SimpleNamespace(
                base_address=0x1000,
                size=len(region_data),
                type=club_reader.MEM_PRIVATE,
            ),
        ],
    )
    monkeypatch.setattr(
        club_reader, "_staff_candidate",
        lambda _reader, person, _team, _manager: {
            "id": 1 if person == 0x7000 else 2,
            "name": "主席" if person == 0x7000 else "所有者",
            "job_type": 4 if person == 0x7000 else 66,
            "address": hex(person),
        },
    )

    rows = club_reader._scan_club_controllers(
        SparseReader(), team_address,
    )

    assert [(row["id"], row["job_type"]) for row in rows] == [(1, 4), (2, 66)]


@pytest.mark.parametrize("game_key", ["fm24", "fm26"])
def test_acquisition_sets_underwriter_and_rollback_restores_original(
    monkeypatch, game_key,
):
    module = SimpleNamespace(base_address=0x10000000)
    birth_offset = 0x44 if game_key == "fm24" else 0x88
    manager_birth_raw = (
        struct.pack("<HH", 100, 1990)
        if game_key == "fm24" else struct.pack("<I", (1990 << 16) | 100)
    )
    chairman_birth_raw = (
        struct.pack("<HH", 200, 1970)
        if game_key == "fm24" else struct.pack("<I", (1970 << 16) | 200)
    )
    layout = SimpleNamespace(
        key=game_key, module_name="fm.exe" if game_key == "fm24" else "game_plugin.dll",
        club_detail2_offset=0x100,
        club_chairman_status_offset=0xD6,
        chairman_patience_offset=0x21,
        chairman_interference_offset=0x1E,
        chairman_base_adjustment=0,
        staff_complete_object_offset=0,
        person_first_name_offset=0x10,
        person_last_name_offset=0x18,
        person_common_name_offset=0x20,
        person_full_name_offset=0x28,
        person_nationality_offset=0x68,
        person_date_of_birth_offset=birth_offset,
        person_date_of_birth_day_year=game_key == "fm24",
        manager_person_offset=0x450,
        club_finance_offset=0x150,
        finance_balance_offset=0x14,
        finance_sugar_daddy_offset=0x476,
        club_ownership_offset=0x70,
        module=lambda _process: module,
    )

    class AcquisitionReader:
        def __init__(self):
            self.layout = layout
            self.string_cache = {}
            self.pointers = {
                0x1000 + TEAM_CLUB: 0x2000,
                0x2000 + 0x100: 0x3000,
                0x2000 + 0x150: 0x4000,
                0x4000 + 0x08: 0x2000,
                0x6000 + 0x68: 0xB000,
            }
            self.u16_values = {0x3000 + 0xD6: 5000}
            self.u8_values = {
                0x4000 + 0x476: 2,
                0x5000 + 0x21: 10,
                0x5000 + 0x1E: 12,
                0x6000 + 0x21: 9,
                0x6000 + 0x1E: 6,
            }
            self.u32_values = {
                0x5000 + ENTITY_UID: 99,
                0x6000 + ENTITY_UID: 100,
                0xB000 + ENTITY_UID: 769,
            }
            self.balance = -500
            self.ownership = bytes.fromhex("785634122a030507021e3201")
            self.birth_dates = {
                0x8000 + birth_offset: manager_birth_raw,
                0x6000 + birth_offset: chairman_birth_raw,
            }

        def ptr(self, address):
            return self.pointers.get(address, 0)

        def u16(self, address):
            for birth_address, raw in self.birth_dates.items():
                if address == birth_address:
                    return struct.unpack_from("<H", raw, 0)[0]
                if address == birth_address + 2:
                    return struct.unpack_from("<H", raw, 2)[0]
            return self.u16_values.get(address)

        def u32(self, address):
            if address in self.birth_dates:
                return struct.unpack("<I", self.birth_dates[address])[0]
            return self.u32_values.get(address)

        def u8(self, address):
            return self.u8_values.get(address)

        def bytes(self, address, size):
            if address == 0x4000 + 0x14 and size == 4:
                return struct.pack("<i", self.balance)
            if address == 0x3000 + 0x70 + 0x0C and size == 12:
                return self.ownership
            if address in self.birth_dates and size == 4:
                return self.birth_dates[address]
            return None

    reader = AcquisitionReader()
    controller_names = {
        0x5000: "原所有者",
        0x6000: "原主席",
    }

    @contextmanager
    def opened(_pid, **_kwargs):
        yield SimpleNamespace(handle=1)

    def write_memory(_process, address, data):
        if address == 0x3000 + 0xD6:
            reader.u16_values[address] = struct.unpack("<H", data)[0]
        elif address == 0x4000 + 0x14:
            reader.balance = struct.unpack("<i", data)[0]
        elif address == 0x4000 + 0x476:
            reader.u8_values[address] = data[0]
        elif address == 0x6000 + 0x68:
            reader.pointers[address] = struct.unpack("<Q", data)[0]
        elif address == 0x3000 + 0x70 + 0x0C:
            reader.ownership = bytes(data)
        elif address in reader.birth_dates:
            reader.birth_dates[address] = bytes(data)
        elif address in {
            0x5000 + 0x10, 0x5000 + 0x18, 0x5000 + 0x20,
            0x6000 + 0x10, 0x6000 + 0x18, 0x6000 + 0x20,
        }:
            reader.pointers[address] = struct.unpack("<Q", data)[0]
        elif address in {
            0x5000 + 0x21, 0x5000 + 0x1E,
            0x6000 + 0x21, 0x6000 + 0x1E,
        }:
            reader.u8_values[address] = data[0]

    monkeypatch.setattr(club_reader, "select_process_layout", lambda: (77, "fm.exe", layout))
    monkeypatch.setattr(club_reader, "open_process", opened)
    monkeypatch.setattr(club_reader, "Reader", lambda *_args: reader)
    monkeypatch.setattr(club_reader, "_resolve_team_address", lambda *_args: 0x1000)
    monkeypatch.setattr(
        club_reader, "_manager_primary_nation",
        lambda *_args: (0xA000, 1651),
    )
    monkeypatch.setattr(
        club_reader, "_validated_manager_person",
        lambda *_args: 0x8000,
    )
    manager_name_references = (0xA100, 0xA200, 0)
    monkeypatch.setattr(
        club_reader, "_validated_person_name_references",
        lambda *_args: (
            (0x8010, 0x8018, 0x8020), manager_name_references,
        ),
    )
    monkeypatch.setattr(
        club_reader, "_validated_nationality_target",
        lambda _reader, _base, _nation_id, nation_address: int(nation_address),
    )
    monkeypatch.setattr(
        club_reader, "_nation_address_for_uid",
        lambda _reader, _base, nation_id: 0xB000 if nation_id == 769 else 0,
    )
    monkeypatch.setattr(
        club_reader, "_scan_staff",
        lambda *_args: [
            {"job_type": 66, "address": "0x5000", "id": 99},
            {"job_type": 4, "address": "0x6000", "id": 100},
        ],
    )
    monkeypatch.setattr(
        club_reader, "_name",
        lambda _reader, person: controller_names.get(person, "测试经理"),
    )
    monkeypatch.setattr(
        club_reader, "_write_person_display_name",
        lambda _process, fields, encoded: (
            controller_names.__setitem__(
                int(fields[0]) - 0x10, encoded.decode("utf-8"),
            )
            or (0x7000 + int(fields[0]), 0x9000 + int(fields[0]))
        ),
    )
    def write_name_references(_process, fields, references):
        person = int(fields[0]) - 0x10
        controller_names[person] = "测试经理"
        for field, reference in zip(fields, references, strict=True):
            write_memory(_process, field, struct.pack("<Q", reference))

    monkeypatch.setattr(
        club_reader, "_write_person_name_references", write_name_references,
    )
    monkeypatch.setattr(club_reader, "write_process_memory", write_memory)
    monkeypatch.setattr(club_reader, "_remote_free_block", lambda *_args: None)

    result = club_reader.acquire_native_club(
        42, "0x1000", "测试经理",
        manager_id=123, manager_address="0x8000",
    )
    assert result["sale_restore"] == {
        "schema_version": 1,
        "game_key": game_key,
        "modified_fields": [
            "chairman_birth_date", "chairman_interference", "chairman_name",
            "chairman_nationality", "chairman_patience", "chairman_status",
            "sugar_daddy",
        ],
        "chairman_id": 100,
        "controllers": [
            {
                "id": 100,
                "job_type": 4,
                "role": "主席",
                "modified_fields": ["interference", "name", "patience"],
                "name": "原主席",
                "patience": 9,
                "interference": 6,
            },
            {
                "id": 99,
                "job_type": 66,
                "role": "主席",
                "modified_fields": ["interference", "name", "patience"],
                "name": "原所有者",
                "patience": 10,
                "interference": 12,
            },
        ],
        "chairman_name": "原主席",
        "chairman_status": 5000,
        "chairman_patience": 9,
        "chairman_interference": 6,
        "chairman_nationality_id": 769,
        "chairman_birth_date_raw": chairman_birth_raw.hex(),
        "chairman_birth_date": "1970-07-19",
        "sugar_daddy": 2,
    }
    assert result["sugar_daddy_before"] == 2
    assert result["sugar_daddy_after"] == 3
    assert result["chairman_available"] is True
    assert result["controller_job_type"] == 4
    assert result["controller_count"] == 2
    assert result["controller_role"] == "主席"
    assert controller_names == {0x5000: "测试经理", 0x6000: "测试经理"}
    assert result["warning"] is None
    assert reader.u8_values[0x4000 + 0x476] == 3
    assert reader.u8_values[0x5000 + 0x21] == 20
    assert reader.u8_values[0x5000 + 0x1E] == 0
    assert reader.u8_values[0x6000 + 0x21] == 20
    assert reader.u8_values[0x6000 + 0x1E] == 0
    assert reader.pointers[0x6000 + 0x68] == 0xA000
    assert result["chairman_nationality_before"] == 769
    assert result["chairman_nationality_after"] == 1651
    assert result["chairman_nationality_updated"] is True
    assert result["chairman_birth_date_before"] == "1970-07-19"
    assert result["chairman_birth_date_after"] == "1990-04-10"
    assert result["chairman_birth_date_updated"] is True
    assert reader.birth_dates[0x6000 + birth_offset] == manager_birth_raw
    assert reader.balance == 0
    assert result["ownership_type"] is None
    assert result["ownership_updated"] is False
    assert reader.ownership == bytes.fromhex("785634122a030507021e3201")

    sale = club_reader.restore_native_club_after_sale(
        42, "0x1000", result["sale_restore"],
    )
    assert sale["restored"] is True
    assert reader.u16_values[0x3000 + 0xD6] == 5000
    assert reader.u8_values[0x4000 + 0x476] == 2
    assert reader.u8_values[0x5000 + 0x21] == 10
    assert reader.u8_values[0x5000 + 0x1E] == 12
    assert reader.u8_values[0x6000 + 0x21] == 9
    assert reader.u8_values[0x6000 + 0x1E] == 6
    assert reader.pointers[0x6000 + 0x68] == 0xB000
    assert reader.birth_dates[0x6000 + birth_offset] == chairman_birth_raw
    assert controller_names == {0x5000: "原所有者", 0x6000: "原主席"}
    assert reader.balance == 0

    club_reader.rollback_native_club_sale(sale["_rollback"])
    assert reader.u16_values[0x3000 + 0xD6] == 10000
    assert reader.u8_values[0x4000 + 0x476] == 3
    assert reader.u8_values[0x5000 + 0x21] == 20
    assert reader.u8_values[0x5000 + 0x1E] == 0
    assert reader.u8_values[0x6000 + 0x21] == 20
    assert reader.u8_values[0x6000 + 0x1E] == 0
    assert reader.pointers[0x6000 + 0x68] == 0xA000
    assert reader.birth_dates[0x6000 + birth_offset] == manager_birth_raw

    club_reader.rollback_native_club_acquisition(result["_rollback"])
    assert reader.u8_values[0x4000 + 0x476] == 2
    assert reader.u8_values[0x5000 + 0x21] == 10
    assert reader.u8_values[0x5000 + 0x1E] == 12
    assert reader.u8_values[0x6000 + 0x21] == 9
    assert reader.u8_values[0x6000 + 0x1E] == 6
    assert reader.pointers[0x6000 + 0x68] == 0xB000
    assert reader.birth_dates[0x6000 + birth_offset] == chairman_birth_raw
    assert reader.balance == -500
    assert reader.ownership == bytes.fromhex("785634122a030507021e3201")


def test_acquisition_degrades_native_failures_but_rolls_back_persistence_failures(monkeypatch):
    import fm_odds_web

    stored = []
    refunds = []
    club = {"id": 42, "name": "测试俱乐部", "address": "0x1000"}
    state = SimpleNamespace(
        lock=threading.RLock(), memory_lock=threading.RLock(),
        output={"save_instance_id": "save-1", "game_date": "2024-05-01"},
        acquired_status_enforced=set(),
        _bind_current_save=lambda: "scope-1",
        _data_scope_id=lambda _output: "scope-1",
        _world_club_native_cache=lambda _save_id: {"addresses_current": True},
        _sync_owned_club_board_hook=lambda _output: (_ for _ in ()).throw(
            RuntimeError("Hook unavailable")
        ),
    )

    monkeypatch.setattr(fm_odds_web, "build_world_clubs", lambda _output: {"clubs": []})
    monkeypatch.setattr(
        fm_odds_web, "merge_native_world_clubs",
        lambda _directory, _native: {"clubs": [club]},
    )
    monkeypatch.setattr(
        fm_odds_web, "load_acquired_clubs",
        lambda _scope: {"clubs": list(stored)},
    )
    monkeypatch.setattr(
        fm_odds_web, "read_native_world_club_acquisition_quote",
        lambda _club: {"acquisition": {"price": 100_000_000}},
    )
    monkeypatch.setattr(
        fm_odds_web, "charge_combined_funds",
        lambda *_args, **_kwargs: {
            "bank": 100_000_000, "wallet": 0, "total": 100_000_000,
            "transaction_id": "payment-1",
        },
    )
    monkeypatch.setattr(
        fm_odds_web, "refund_combined_funds",
        lambda *_args, **_kwargs: refunds.append((_args, _kwargs)),
    )
    monkeypatch.setattr(
        fm_odds_web, "acquire_native_club",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("ownership write rejected")
        ),
    )
    monkeypatch.setattr(fm_odds_web, "manager_display_name", lambda _output: "测试经理")

    def persist(row, _scope):
        stored.append(dict(row))
        return dict(row)

    monkeypatch.setattr(fm_odds_web, "acquire_club", persist)

    result = fm_odds_web.LocalOddsState.acquire_world_club(state, {"team_id": 42})

    assert result["club"]["id"] == 42
    assert len(stored) == 1
    assert refunds == []
    assert result["native"]["native_effects_verified"] is False
    assert result["native"]["sale_restore"]["modified_fields"] == []
    assert result["club"]["acquisition_mode"] == "portfolio_only"
    assert "附加效果未完成" in result["warning"]
    assert "董事会解锁暂未启用" in result["warning"]
    assert result["board_listens"]["active"] is False
    assert state.acquired_status_enforced == set()

    stored.clear()
    club["id"] = 43
    rollbacks = []
    monkeypatch.setattr(
        fm_odds_web, "acquire_native_club",
        lambda *_args, **_kwargs: {
            "requested_owner_name": "测试经理",
            "native_effects_verified": True,
            "native_effects_applied": True,
            "applied_fields": ["chairman_status"],
            "sale_restore": {
                "schema_version": 1,
                "modified_fields": ["chairman_status"],
                "chairman_status": 5000,
            },
            "warning": None,
            "_rollback": {"pid": 77, "modified_fields": ["chairman_status"]},
        },
    )
    monkeypatch.setattr(
        fm_odds_web, "rollback_native_club_acquisition",
        lambda snapshot: rollbacks.append(snapshot),
    )
    monkeypatch.setattr(
        fm_odds_web, "acquire_club",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(RuntimeError, match="俱乐部收购写入失败"):
        fm_odds_web.LocalOddsState.acquire_world_club(state, {"team_id": 43})

    assert len(refunds) == 1
    assert rollbacks == [{"pid": 77, "modified_fields": ["chairman_status"]}]


def test_concurrent_duplicate_acquisition_charges_once_and_replays_result(monkeypatch):
    import fm_odds_web

    club = {"id": 42, "name": "测试俱乐部", "address": "0x1000"}
    state = SimpleNamespace(
        lock=threading.RLock(), memory_lock=threading.RLock(),
        output={"save_instance_id": "save-1", "game_date": "2024-05-01"},
        acquired_status_enforced=set(),
        _bind_current_save=lambda: "scope-1",
        _data_scope_id=lambda _output: "scope-1",
        _world_club_native_cache=lambda _save_id: {"addresses_current": True},
        _sync_owned_club_board_hook=lambda _output: {
            "supported": True, "active": True, "team_ids": [42],
        },
    )
    stored = []
    charge_count = 0
    storage_lock = threading.Lock()

    def acquired(*_args, **_kwargs):
        with storage_lock:
            return [dict(row) for row in stored]

    def charge(*_args, **_kwargs):
        nonlocal charge_count
        with storage_lock:
            charge_count += 1
        return {
            "bank": 100_000_000, "wallet": 0, "total": 100_000_000,
            "transaction_id": "payment-1",
        }

    def persist(row, _scope):
        with storage_lock:
            stored.append(dict(row))
        return dict(row)

    monkeypatch.setattr(fm_odds_web, "build_world_clubs", lambda _output: {"clubs": []})
    monkeypatch.setattr(
        fm_odds_web, "merge_native_world_clubs",
        lambda _directory, _native: {"clubs": [club]},
    )
    monkeypatch.setattr(fm_odds_web, "account_acquired_clubs", acquired)
    monkeypatch.setattr(
        fm_odds_web, "read_native_world_club_acquisition_quote",
        lambda _club: {"acquisition": {"price": 100_000_000}},
    )
    monkeypatch.setattr(fm_odds_web, "charge_combined_funds", charge)
    monkeypatch.setattr(
        fm_odds_web, "acquire_native_club",
        lambda *_args, **_kwargs: {
            "requested_owner_name": "测试经理",
            "native_effects_verified": True,
            "native_effects_applied": True,
            "sale_restore": {"schema_version": 1, "modified_fields": []},
            "_rollback": {},
        },
    )
    monkeypatch.setattr(fm_odds_web, "acquire_club", persist)
    monkeypatch.setattr(fm_odds_web, "manager_display_name", lambda _output: "测试经理")

    barrier = threading.Barrier(2)
    results = []
    errors = []

    def run() -> None:
        try:
            barrier.wait(timeout=2)
            results.append(fm_odds_web.LocalOddsState.acquire_world_club(
                state,
                {"team_id": 42, "client_submission_id": "submission-42"},
            ))
        except Exception as error:  # pragma: no cover - assertion relay
            errors.append(error)

    threads = [threading.Thread(target=run) for _index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)

    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert charge_count == 1
    assert len(stored) == 1
    assert len(results) == 2
    assert results[0] == results[1]
    assert stored[0]["client_submission_id"] == "submission-42"


def test_empty_acquisition_snapshot_does_not_restore_unmodified_fields(monkeypatch):
    writes = []

    @contextmanager
    def opened(_pid, **_kwargs):
        yield SimpleNamespace(handle=1)

    monkeypatch.setattr(
        club_reader, "select_process_layout",
        lambda: (77, "fm.exe", SimpleNamespace()),
    )
    monkeypatch.setattr(club_reader, "open_process", opened)
    monkeypatch.setattr(
        club_reader, "write_process_memory",
        lambda *_args: writes.append(_args),
    )

    club_reader.rollback_native_club_acquisition({
        "pid": 77, "modified_fields": [],
    })

    assert writes == []


def test_chairman_name_write_keeps_nested_common_name_reference(monkeypatch):
    writes = {}
    monkeypatch.setattr(
        club_reader, "_remote_malloc_block", lambda *_args: (0x7000, 0x9000),
    )
    monkeypatch.setattr(club_reader, "_remote_free_block", lambda *_args: None)
    monkeypatch.setattr(
        club_reader, "write_process_memory",
        lambda _process, address, data: writes.__setitem__(address, bytes(data)),
    )

    fields = (0x5010, 0x5018, 0x5020, 0x5028)
    allocated, free_address = club_reader._write_person_display_name(
        SimpleNamespace(), fields, "测试经理".encode("utf-8"),
    )

    payload = writes[allocated]
    common_entry_offset = (4 + len("测试经理".encode("utf-8")) + 1 + 7) & ~7
    assert (allocated, free_address) == (0x7000, 0x9000)
    assert struct.unpack("<Q", writes[fields[0]])[0] == 0
    assert struct.unpack("<Q", writes[fields[1]])[0] == 0
    assert struct.unpack("<Q", writes[fields[2]])[0] == allocated + common_entry_offset
    assert struct.unpack("<Q", writes[fields[3]])[0] == allocated
    assert struct.unpack_from("<Q", payload, common_entry_offset)[0] == allocated


@pytest.mark.parametrize("game_key", ["fm24", "fm26"])
def test_existing_acquisition_repairs_name_without_writing_ownership(
    monkeypatch, game_key,
):
    module = SimpleNamespace(base_address=0x10000000)
    layout = SimpleNamespace(
        key=game_key,
        module_name="fm.exe" if game_key == "fm24" else "game_plugin.dll",
        club_detail2_offset=0x100,
        club_chairman_status_offset=0xD6,
        club_ownership_offset=0x70,
        person_first_name_offset=0x10, person_last_name_offset=0x18,
        person_full_name_offset=0x28, person_common_name_offset=0x20,
        person_nationality_offset=None,
        module=lambda _process: module,
    )

    class RepairReader:
        def __init__(self):
            self.layout = layout
            self.string_cache = {}
            self.pointers = {
                0x1000 + TEAM_CLUB: 0x2000,
                0x2000 + 0x100: 0x3000,
                0x5000 + 0x28: 0x6000,
            }
            self.status = 5000
            self.ownership = bytes.fromhex("785634122a030507021e3201")

        def ptr(self, address):
            return self.pointers.get(address, 0)

        def u16(self, address):
            return self.status if address == 0x3000 + 0xD6 else None

        def u32(self, address):
            return {0x5000 + ENTITY_UID: 99}.get(address)

        def bytes(self, address, size):
            if address == 0x3000 + 0x70 + 0x0C and size == 12:
                return self.ownership
            return None

        def fm_string_at(self, address):
            return "测试经理" if self.ptr(address) == 0x6000 else None

        def fm_nested_string_at(self, address):
            entry = self.ptr(address)
            return "测试经理" if entry and self.ptr(entry) == 0x6000 else None

    reader = RepairReader()

    @contextmanager
    def opened(_pid, **_kwargs):
        yield SimpleNamespace(handle=1)

    def write_memory(_process, address, data):
        if address == 0x3000 + 0xD6:
            reader.status = struct.unpack("<H", data)[0]
        elif address == 0x3000 + 0x70 + 0x0C:
            reader.ownership = bytes(data)
        elif len(data) == 8:
            reader.pointers[address] = struct.unpack("<Q", data)[0]

    monkeypatch.setattr(club_reader, "select_process_layout", lambda: (77, "fm.exe", layout))
    monkeypatch.setattr(club_reader, "open_process", opened)
    monkeypatch.setattr(club_reader, "Reader", lambda *_args: reader)
    monkeypatch.setattr(club_reader, "_resolve_team_address", lambda *_args: 0x1000)
    monkeypatch.setattr(
        club_reader, "_scan_staff",
        lambda *_args: [{"job_type": 70, "address": "0x5000", "id": 99}],
    )
    monkeypatch.setattr(club_reader, "_validated_manager_person", lambda *_args: 0x8000)
    monkeypatch.setattr(club_reader, "_manager_primary_nation", lambda *_args: (0, 0))
    monkeypatch.setattr(
        club_reader, "_validated_nationality_target", lambda *_args: 0,
    )
    monkeypatch.setattr(
        club_reader, "_validated_person_name_references",
        lambda *_args: ((0x8010, 0x8018, 0x8020), (0x7100, 0x7200, 0)),
    )
    monkeypatch.setattr(club_reader, "write_process_memory", write_memory)

    assert club_reader.enforce_acquired_club_chairman_status(
        42, "0x1000", "测试经理", manager_id=123, manager_address="0x8000",
    )
    assert reader.status == 10000
    assert reader.ownership == bytes.fromhex("785634122a030507021e3201")
    assert tuple(
        reader.pointers.get(0x5000 + offset, 0)
        for offset in (0x10, 0x18, 0x20)
    ) == (0x7100, 0x7200, 0)
    assert reader.pointers[0x5000 + 0x28] == 0x6000


@pytest.mark.parametrize("game_key", ["fm24", "fm26"])
def test_existing_acquisition_rewrites_changed_chairman_name(
    monkeypatch, game_key,
):
    module = SimpleNamespace(base_address=0x10000000)
    layout = SimpleNamespace(
        key=game_key,
        module_name="fm.exe" if game_key == "fm24" else "game_plugin.dll",
        club_detail2_offset=0x100,
        club_chairman_status_offset=0xD6,
        person_first_name_offset=0x10,
        person_last_name_offset=0x18,
        person_common_name_offset=0x20,
        person_full_name_offset=0x28,
        person_nationality_offset=0x68,
        module=lambda _process: module,
    )
    pointers = {
        0x1000 + TEAM_CLUB: 0x2000,
        0x2000 + 0x100: 0x3000,
    }

    class Reader:
        string_cache = {}

        @staticmethod
        def ptr(address):
            return pointers.get(address, 0)

        @staticmethod
        def u16(address):
            return 10000 if address == 0x3000 + 0xD6 else None

        @staticmethod
        def u32(address):
            return 99 if address == 0x5000 + ENTITY_UID else None

    @contextmanager
    def opened(_pid, **_kwargs):
        yield SimpleNamespace(handle=1)

    writes = []

    def write_memory(_process, address, data):
        writes.append((address, bytes(data)))
        if len(data) == 8:
            pointers[address] = struct.unpack("<Q", data)[0]

    monkeypatch.setattr(club_reader, "select_process_layout", lambda: (77, "fm.exe", layout))
    monkeypatch.setattr(club_reader, "open_process", opened)
    monkeypatch.setattr(club_reader, "Reader", lambda *_args: Reader())
    monkeypatch.setattr(club_reader, "_resolve_team_address", lambda *_args: 0x1000)
    monkeypatch.setattr(
        club_reader, "_scan_club_controllers",
        lambda *_args: [{"job_type": 4, "address": "0x5000", "id": 99}],
    )
    monkeypatch.setattr(club_reader, "_validated_manager_person", lambda *_args: 0x8000)
    monkeypatch.setattr(club_reader, "_manager_primary_nation", lambda *_args: (0, 0))
    monkeypatch.setattr(
        club_reader, "_validated_nationality_target", lambda *_args: 0,
    )
    monkeypatch.setattr(
        club_reader, "_validated_person_name_references",
        lambda *_args: ((0x8010, 0x8018, 0x8020), (0x7100, 0x7200, 0)),
    )
    monkeypatch.setattr(club_reader, "write_process_memory", write_memory)

    assert club_reader.enforce_acquired_club_chairman_status(
        42, "0x1000", "测试经理", manager_id=123, manager_address="0x8000",
    )
    assert tuple(pointers.get(field, 0) for field in (0x5010, 0x5018, 0x5020)) == (
        0x7100, 0x7200, 0,
    )
    assert 0x5028 not in pointers


def _takeover_layout(game_key):
    adjustment = 0x08 if game_key == "fm26" else 0
    module = SimpleNamespace(base_address=0x10000000)
    layout = SimpleNamespace(
        key=game_key,
        module_name="fm.exe" if game_key == "fm24" else "game_plugin.dll",
        club_detail2_offset=0x100,
        club_chairman_status_offset=0xD6,
        person_first_name_offset=0x10,
        person_last_name_offset=0x18,
        person_common_name_offset=0x20,
        person_full_name_offset=0x28,
        person_nationality_offset=0x68,
        staff_complete_object_offset=0x100,
        chairman_base_adjustment=adjustment,
        chairman_patience_offset=0x19,
        chairman_interference_offset=0x16,
        module=lambda _process: module,
    )
    bases = {
        0x5000: 0x5000 - 0x100 + adjustment,
        0x7000: 0x7000 - 0x100 + adjustment,
    }
    return layout, bases


def _install_takeover_reader(monkeypatch, layout, bases, *, freeze_interference=False):
    pointers = {
        0x1000 + TEAM_CLUB: 0x2000,
        0x2000 + 0x100: 0x3000,
    }
    attributes = {}
    frozen = set()
    for base in bases.values():
        attributes[base + 0x19] = 3
        attributes[base + 0x16] = 9
        if freeze_interference:
            frozen.add(base + 0x16)

    class Reader:
        string_cache = {}

        @staticmethod
        def ptr(address):
            return pointers.get(address, 0)

        @staticmethod
        def u16(address):
            return 10000 if address == 0x3000 + 0xD6 else None

        @staticmethod
        def u32(address):
            return {
                0x5000 + ENTITY_UID: 250,
                0x7000 + ENTITY_UID: 66,
            }.get(address)

        @staticmethod
        def u8(address):
            return attributes.get(address)

    @contextmanager
    def opened(_pid, **_kwargs):
        yield SimpleNamespace(handle=1)

    def write_memory(_process, address, data):
        if len(data) == 8:
            pointers[address] = struct.unpack("<Q", data)[0]
        elif len(data) == 1 and address not in frozen:
            attributes[address] = data[0]

    monkeypatch.setattr(
        club_reader, "select_process_layout", lambda: (77, "fm.exe", layout),
    )
    monkeypatch.setattr(club_reader, "open_process", opened)
    monkeypatch.setattr(club_reader, "Reader", lambda *_args: Reader())
    monkeypatch.setattr(club_reader, "_resolve_team_address", lambda *_args: 0x1000)
    monkeypatch.setattr(
        club_reader, "_scan_club_controllers",
        lambda *_args: [
            {"job_type": 4, "address": "0x5000", "id": 250},
            {"job_type": 66, "address": "0x7000", "id": 66},
        ],
    )
    monkeypatch.setattr(club_reader, "_validated_manager_person", lambda *_args: 0x8000)
    monkeypatch.setattr(club_reader, "_manager_primary_nation", lambda *_args: (0, 0))
    monkeypatch.setattr(
        club_reader, "_validated_nationality_target", lambda *_args: 0,
    )
    monkeypatch.setattr(
        club_reader, "_validated_person_name_references",
        lambda *_args: ((0x8010, 0x8018, 0x8020), (0x7100, 0x7200, 0)),
    )
    monkeypatch.setattr(club_reader, "write_process_memory", write_memory)
    return pointers, attributes


@pytest.mark.parametrize("game_key", ["fm24", "fm26"])
def test_existing_acquisition_rewrites_controller_attributes_after_takeover(
    monkeypatch, game_key,
):
    layout, bases = _takeover_layout(game_key)
    pointers, attributes = _install_takeover_reader(monkeypatch, layout, bases)

    assert club_reader.enforce_acquired_club_chairman_status(
        42, "0x1000", "测试经理", manager_id=123, manager_address="0x8000",
    )
    for base in bases.values():
        assert attributes[base + 0x19] == 20
        assert attributes[base + 0x16] == 0
    for person in (0x5000, 0x7000):
        assert tuple(
            pointers.get(person + offset, 0) for offset in (0x10, 0x18, 0x20)
        ) == (0x7100, 0x7200, 0)


def test_controller_attribute_failure_rolls_back_previous_writes(monkeypatch):
    layout, bases = _takeover_layout("fm24")
    pointers, attributes = _install_takeover_reader(
        monkeypatch, layout, bases, freeze_interference=True,
    )

    with pytest.raises(RuntimeError):
        club_reader.enforce_acquired_club_chairman_status(
            42, "0x1000", "测试经理", manager_id=123, manager_address="0x8000",
        )
    for base in bases.values():
        assert attributes[base + 0x19] == 3
        assert attributes[base + 0x16] == 9
    for person in (0x5000, 0x7000):
        assert tuple(
            pointers.get(person + offset, 0) for offset in (0x10, 0x18, 0x20)
        ) == (0, 0, 0)


@pytest.mark.parametrize("game_key", ["fm24", "fm26"])
def test_sale_does_not_restore_legacy_ownership_snapshot(monkeypatch, game_key):
    monkeypatch.setattr(
        club_reader, "select_process_layout",
        lambda: (77, "fm.exe", SimpleNamespace(key=game_key)),
    )

    result = club_reader.restore_native_club_after_sale(
        42, "0x1000", {
            "schema_version": 1,
            "game_key": game_key,
            "modified_fields": ["ownership"],
            "ownership": "785634122a030507021e3201",
        },
    )

    assert result == {
        "restored": False, "legacy": False, "skipped": True,
        "modified_fields": [], "_rollback": {},
    }


@pytest.mark.parametrize(
    "rollback",
    [
        club_reader.rollback_native_club_acquisition,
        club_reader.rollback_native_club_sale,
    ],
)
def test_rollback_does_not_write_legacy_ownership_snapshot(monkeypatch, rollback):
    monkeypatch.setattr(
        club_reader, "select_process_layout",
        lambda: (77, "fm.exe", SimpleNamespace()),
    )
    monkeypatch.setattr(
        club_reader, "open_process",
        lambda *_args, **_kwargs: pytest.fail("ownership-only rollback opened FM"),
    )

    rollback({
        "pid": 77,
        "modified_fields": ["ownership"],
        "ownership_address": 0x3000,
        "ownership_before": bytes.fromhex("785634122a030507021e3201"),
        "ownership": bytes.fromhex("000000002a01000000000000"),
    })


@pytest.mark.parametrize("game_key", ["fm24", "fm26"])
def test_existing_acquisition_maintains_status_without_ownership_write(
    monkeypatch, game_key,
):
    module = SimpleNamespace(base_address=0x10000000)
    layout = SimpleNamespace(
        key=game_key,
        module_name="fm.exe" if game_key == "fm24" else "game_plugin.dll",
        club_detail2_offset=0x100,
        person_nationality_offset=0x68,
        club_chairman_status_offset=0xD6, club_ownership_offset=0x70,
        module=lambda _process: module,
    )

    class FailingOwnershipReader:
        def __init__(self):
            self.pointers = {
                0x1000 + TEAM_CLUB: 0x2000,
                0x2000 + 0x100: 0x3000,
                0x5000 + 0x68: 0xB000,
            }
            self.status = 5000
            self.ownership = bytes.fromhex("785634122a030507021e3201")

        def ptr(self, address):
            return self.pointers.get(address, 0)

        def u16(self, address):
            return self.status if address == 0x3000 + 0xD6 else None

        def u32(self, address):
            return {
                0x5000 + ENTITY_UID: 99,
                0xB000 + ENTITY_UID: 769,
            }.get(address)

        def bytes(self, address, size):
            if address == 0x3000 + 0x70 + 0x0C and size == 12:
                return self.ownership
            return None

    reader = FailingOwnershipReader()

    @contextmanager
    def opened(_pid, **_kwargs):
        yield SimpleNamespace(handle=1)

    def write_memory(_process, address, data):
        if address == 0x3000 + 0xD6:
            reader.status = struct.unpack("<H", data)[0]
        elif address == 0x5000 + 0x68:
            reader.pointers[address] = struct.unpack("<Q", data)[0]
        # Ownership writes are intentionally ignored for both generations.

    monkeypatch.setattr(club_reader, "select_process_layout", lambda: (77, "fm.exe", layout))
    monkeypatch.setattr(club_reader, "open_process", opened)
    monkeypatch.setattr(club_reader, "Reader", lambda *_args: reader)
    monkeypatch.setattr(club_reader, "_resolve_team_address", lambda *_args: 0x1000)
    monkeypatch.setattr(
        club_reader, "_scan_club_controllers",
        lambda *_args: [
            {"job_type": 4, "address": "0x5000", "id": 99, "name": "主席"},
        ],
    )
    monkeypatch.setattr(
        club_reader, "_manager_primary_nation",
        lambda *_args: (0xA000, 1651),
    )
    monkeypatch.setattr(
        club_reader, "_validated_nationality_target",
        lambda _reader, _base, _nation_id, nation_address: int(nation_address),
    )
    monkeypatch.setattr(club_reader, "write_process_memory", write_memory)

    assert club_reader.enforce_acquired_club_chairman_status(
        42, "0x1000", manager_id=123, manager_address="0x8000",
    )
    assert reader.status == 10000
    assert reader.pointers[0x5000 + 0x68] == 0xA000
    assert reader.ownership == bytes.fromhex("785634122a030507021e3201")


def test_owned_club_temporary_prices():
    from fm_odds_web import (
        owned_club_action_quotes, owned_club_facility_upgrade_price,
        owned_club_facility_upgrade_quote,
        owned_stadium_action_quotes,
    )

    assert owned_club_facility_upgrade_price(10) == 3_750_000
    assert owned_club_facility_upgrade_quote(10, 3) == {
        "before": 10, "after": 13, "levels": 3,
        "price": 12_000_000, "days": 36,
    }
    assert owned_club_facility_upgrade_price(20) is None
    quotes = owned_club_action_quotes({
        "club_information": {"facilities": {
            "training": 10, "youth": 15,
            "junior_coaching": 8, "youth_recruitment": 12,
        }},
    })
    assert quotes["rename_price"] == 4_000_000
    assert quotes["manager_replace_price"] == 1_000_000
    assert quotes["facility_upgrade_prices"] == {
        "training": 3_750_000,
        "youth": 5_000_000,
        "junior": 3_250_000,
        "recruitment": 4_250_000,
    }
    assert quotes["facility_upgrade_quotes"]["training"]["options"][2] == {
        "before": 10, "after": 13, "levels": 3,
        "price": 12_000_000, "days": 36,
    }
    assert quotes["facility_upgrade_cooldowns"] == {
        key: {
            "active": False, "status": "idle", "until": None,
            "next_due_on": None, "remaining_days": 0,
        }
        for key in ("training", "youth", "junior", "recruitment")
    }
    stadium = owned_stadium_action_quotes({
        "capacity": 75_000, "expansion_capacity": 80_000, "state_raw": 16,
    })
    assert stadium["rename_price"] == 1_000_000
    assert stadium["capacity"] == {
        "before": 75_000, "after": 80_000, "step": 5_000, "price": 480_000_000,
    }
    assert stadium["expansion"] == {
        "before": 80_000, "after": 85_000, "step": 5_000, "price": 880_000_000,
    }
    assert stadium["state"] == {
        "before": 16, "after": 11, "price": 120_000_000, "after_name": "一般",
    }
    partial = owned_stadium_action_quotes({
        "capacity": 75_842, "expansion_capacity": 80_000, "state_raw": 16,
        "pitch_condition": 196, "pitch_type": 2,
    })
    assert partial["capacity"] == {
        "before": 75_842, "after": 80_000, "step": 4_158,
        "price": 480_000_000,
    }
    assert partial["pitch_condition"] == {
        "before": 196, "after": 200, "step": 4, "price": 20_000_000,
    }
    assert partial["pitch_type_price"] == 40_000_000
    assert partial["pitch_types"][1] == {"value": 2, "name": "人造草皮（软）"}


def test_owned_club_facility_upgrade_creates_paid_multi_level_plan(monkeypatch):
    import fm_odds_web
    from contextlib import nullcontext

    detail = {"club_information": {"facilities": {
        "training": 10, "youth": 10,
        "junior_coaching": 10, "youth_recruitment": 10,
    }}}
    state = fm_odds_web.LocalOddsState.__new__(fm_odds_web.LocalOddsState)
    state.lock = threading.RLock()
    state.memory_lock = threading.RLock()
    foreground_operations = []
    state._timed_user_memory_operation = lambda name: (
        foreground_operations.append(name) or nullcontext({})
    )
    state._bind_current_save = lambda: "scope-1"
    state._data_scope_id = lambda _output: "scope-1"
    state.output = {"save_instance_id": "save-1", "game_date": "2026-07-28"}
    state._owned_world_club_target = lambda team_id: (
        "scope-1", {"game_date": "2026-07-28"},
        {"id": team_id, "name": "测试俱乐部", "address": "0x1000"},
    )
    persisted = {"id": 42}
    monkeypatch.setattr(
        fm_odds_web, "load_acquired_clubs", lambda _scope: {"clubs": [persisted]},
    )
    monkeypatch.setattr(
        fm_odds_web, "update_acquired_club",
        lambda _team_id, updates, _scope: persisted.update(updates) or dict(persisted),
    )
    def update_maps(_team_id, map_updates, _scope, **_kwargs):
        for field, entries in map_updates.items():
            saved = dict(persisted.get(field) or {})
            for key, value in entries.items():
                saved.pop(key, None) if value is None else saved.__setitem__(key, value)
            persisted[field] = saved
        return dict(persisted)
    monkeypatch.setattr(
        fm_odds_web, "update_acquired_club_map_entries", update_maps,
    )
    session = SimpleNamespace(reader=object())
    monkeypatch.setattr(
        fm_odds_web, "borrow_game_operation", lambda **_kwargs: nullcontext(session),
    )
    monkeypatch.setattr(
        fm_odds_web, "read_native_club_facility_levels",
        lambda team_id, address, *, reader: {
            "training": 10, "youth": 10, "junior": 10, "recruitment": 10,
        },
    )
    monkeypatch.setattr(
        fm_odds_web, "read_native_world_club_snapshot",
        lambda _club: pytest.fail("facility plan must not read the full club snapshot"),
    )
    monkeypatch.setattr(
        fm_odds_web, "charge_combined_funds",
        lambda price, _event, **_metadata: {"total": price, "bank": price, "wallet": 0},
    )
    native_writes = []
    monkeypatch.setattr(
        fm_odds_web, "upgrade_native_club_facility",
        lambda *_args, **_kwargs: native_writes.append(True),
    )

    result = state.upgrade_world_club_facility({
        "team_id": 42, "facility": "training", "levels": 3,
    })

    assert native_writes == []
    assert result["native"] is None
    assert result["payment"]["total"] == 12_000_000
    assert persisted["facility_upgrade_cooldowns"] == {}
    assert persisted["facility_upgrade_plans"]["training"] == {
        "status": "active", "facility": "training",
        "start_level": 10, "target_level": 13,
        "total_levels": 3, "completed_levels": 0,
        "started_on": "2026-07-28", "next_due_on": "2026-08-09",
        "quoted_price": 12_000_000, "paid_total": 12_000_000,
        "payment_transaction_id": "",
    }
    plan = result["owned_actions_patch"]["facility_upgrade_plans"]["training"]
    assert plan["active"] is True
    assert plan["remaining_days"] == 12
    assert plan["remaining_levels"] == 3
    assert foreground_operations == ["owned_club_facility_plan"]
    with pytest.raises(ValueError, match="进行中的升级计划"):
        state.upgrade_world_club_facility({"team_id": 42, "facility": "training"})


@pytest.mark.parametrize(
    ("facility", "field"),
    [
        ("training", "training"),
        ("youth", "youth"),
        ("junior", "junior_coaching"),
        ("recruitment", "youth_recruitment"),
    ],
)
def test_owned_club_facility_plan_advances_one_level_per_twelve_days(
    monkeypatch, facility, field,
):
    import fm_odds_web

    detail = {"club_information": {"facilities": {
        "training": 10, "youth": 10,
        "junior_coaching": 10, "youth_recruitment": 10,
    }}}
    persisted = {"id": 42, "facility_upgrade_plans": {facility: {
        "status": "active", "facility": facility,
        "start_level": 10, "target_level": 13,
        "total_levels": 3, "completed_levels": 0,
        "started_on": "2026-07-28", "next_due_on": "2026-08-09",
        "quoted_price": 12_000_000, "paid_total": 12_000_000,
    }}}
    state = fm_odds_web.LocalOddsState.__new__(fm_odds_web.LocalOddsState)
    state.lock = threading.RLock()
    state.memory_lock = threading.RLock()
    state._bind_current_save = lambda: "scope-1"
    state._data_scope_id = lambda _output: "scope-1"
    state.output = {"save_instance_id": "save-1", "game_date": "2026-07-28"}
    state._owned_world_club_target = lambda team_id: (
        "scope-1", {"game_date": "2026-07-28"},
        {"id": team_id, "name": "测试俱乐部", "address": "0x1000"},
    )
    monkeypatch.setattr(
        fm_odds_web, "account_acquired_clubs", lambda *_args, **_kwargs: [persisted],
    )
    monkeypatch.setattr(
        fm_odds_web, "update_acquired_club",
        lambda _team_id, updates, _scope: persisted.update(updates) or dict(persisted),
    )
    def update_maps(_team_id, map_updates, _scope, **_kwargs):
        for map_field, entries in map_updates.items():
            saved = dict(persisted.get(map_field) or {})
            for key, value in entries.items():
                saved.pop(key, None) if value is None else saved.__setitem__(key, value)
            persisted[map_field] = saved
        return dict(persisted)
    monkeypatch.setattr(
        fm_odds_web, "update_acquired_club_map_entries", update_maps,
    )
    monkeypatch.setattr(
        fm_odds_web, "read_native_club_facility_levels",
        lambda _team_id, _address: {
            "training": live.get("training", 10),
            "youth": live.get("youth", 10),
            "junior": live.get("junior_coaching", 10),
            "recruitment": live.get("youth_recruitment", 10),
        },
    )
    live = {field: 10}
    detail["club_information"]["facilities"] = live
    writes = []
    monkeypatch.setattr(
        fm_odds_web, "upgrade_native_club_facility",
        lambda _team_id, _address, written_facility, expected_level: (
            writes.append((written_facility, expected_level)),
            live.__setitem__(field, expected_level + 1),
            {"before": expected_level, "after": expected_level + 1},
        )[-1],
    )

    before_due = state._advance_owned_club_facility_plans("2026-08-08", state.output)
    assert before_due["advanced"] == 0
    assert writes == []

    first_due = state._advance_owned_club_facility_plans("2026-08-09", state.output)
    assert first_due["advanced"] == 1
    assert live[field] == 11
    assert persisted["facility_upgrade_plans"][facility]["completed_levels"] == 1
    assert persisted["facility_upgrade_plans"][facility]["next_due_on"] == "2026-08-21"

    catch_up = state._advance_owned_club_facility_plans("2026-09-02", state.output)
    assert catch_up["advanced"] == 2
    assert catch_up["completed"] == 1
    assert live[field] == 13
    assert facility not in persisted["facility_upgrade_plans"]
    if facility == "recruitment":
        assert persisted["facility_level_floors"] == {"recruitment": 13}


def test_owned_club_facility_plan_rolls_back_native_write_when_progress_save_fails(
    monkeypatch,
):
    import fm_odds_web

    plan = {
        "status": "active", "facility": "training",
        "start_level": 10, "target_level": 11,
        "total_levels": 1, "completed_levels": 0,
        "started_on": "2026-07-28", "next_due_on": "2026-08-09",
    }
    persisted = {"id": 42, "name": "测试俱乐部", "facility_upgrade_plans": {
        "training": dict(plan),
    }}
    state = fm_odds_web.LocalOddsState.__new__(fm_odds_web.LocalOddsState)
    state.lock = threading.RLock()
    state.memory_lock = threading.RLock()
    state._bind_current_save = lambda: "scope-1"
    state._data_scope_id = lambda _output: "scope-1"
    state.output = {"save_instance_id": "save-1", "game_date": "2026-08-09"}
    state._owned_world_club_target = lambda team_id: (
        "scope-1", state.output,
        {"id": team_id, "name": "测试俱乐部", "address": "0x1000"},
    )
    live = {"training": 10}
    monkeypatch.setattr(
        fm_odds_web, "account_acquired_clubs", lambda *_args, **_kwargs: [persisted],
    )
    monkeypatch.setattr(
        fm_odds_web, "read_native_club_facility_levels",
        lambda _team_id, _address: {
            "training": live.get("training", 10),
            "youth": live.get("youth", 10),
            "junior": live.get("junior_coaching", 10),
            "recruitment": live.get("youth_recruitment", 10),
        },
    )
    monkeypatch.setattr(
        fm_odds_web, "upgrade_native_club_facility",
        lambda *_args, expected_level, **_kwargs: (
            live.__setitem__("training", expected_level + 1),
            {"before": expected_level, "after": expected_level + 1},
        )[-1],
    )
    monkeypatch.setattr(
        fm_odds_web, "set_native_club_facility_level",
        lambda _team_id, _address, _facility, target, **_kwargs: (
            live.__setitem__("training", target), {"after": target},
        )[-1],
    )
    failed = False

    def update(_team_id, updates, _scope):
        nonlocal failed
        saved = updates.get("facility_upgrade_plans", {}).get("training")
        if not failed and saved is None:
            failed = True
            raise OSError("disk full")
        persisted.update(updates)
        return dict(persisted)

    monkeypatch.setattr(fm_odds_web, "update_acquired_club", update)

    def update_maps(_team_id, map_updates, _scope, **_kwargs):
        flattened = {}
        for map_field, entries in map_updates.items():
            saved = dict(persisted.get(map_field) or {})
            for key, value in entries.items():
                saved.pop(key, None) if value is None else saved.__setitem__(key, value)
            flattened[map_field] = saved
        return update(_team_id, flattened, _scope)

    monkeypatch.setattr(
        fm_odds_web, "update_acquired_club_map_entries", update_maps,
    )

    result = state._advance_owned_club_facility_plans("2026-08-09", state.output)

    assert result["advanced"] == 0
    assert result["errors"]
    assert live["training"] == 10
    assert persisted["facility_upgrade_plans"]["training"]["completed_levels"] == 0
    assert persisted["facility_upgrade_plans"]["training"]["last_error"]


def test_owned_managed_club_staff_uses_player_manager(monkeypatch):
    import fm_odds_web

    output = {
        "selected_manager_id": 77,
        "manager": {"id": 77, "name": "玩家经理"},
        "manager_options": [{"id": 77, "name": "玩家本人"}],
        "managed_teams": [{"id": 42, "name": "测试俱乐部", "team_type": "club"}],
        "save_instance_id": "save-1",
    }
    state = fm_odds_web.LocalOddsState.__new__(fm_odds_web.LocalOddsState)
    state.lock = threading.RLock()
    state.output = output
    state._bind_current_save = lambda: "scope-1"
    state._data_scope_id = lambda _output: "scope-1"
    state._owned_world_club_target = lambda team_id: (
        "scope-1", output, {"id": team_id, "name": "测试俱乐部", "address": "0x1000"},
    )
    monkeypatch.setattr(
        fm_odds_web, "load_acquired_clubs",
        lambda _scope: {"clubs": [{"id": 42, "club_results": [{"result": "胜"}]}]},
    )
    monkeypatch.setattr(
        fm_odds_web, "read_native_world_club_staff",
        lambda _club: {
            "manager": {"id": 88, "name": "原生扫描结果"},
            "leadership": {}, "staff": [],
        },
    )
    synced = []
    monkeypatch.setattr(
        fm_odds_web, "sync_acquired_club_manager_record",
        lambda team_id, manager_id, results, scope: (
            synced.append((team_id, manager_id, results, scope))
            or {"season": {"wins": 1, "draws": 0, "losses": 0}}
        ),
    )

    result = state.world_club_staff(42)

    assert result["manager"] == {
        "id": 77, "name": "玩家本人", "contract_start_date": None,
    }
    assert result["manager_is_player"] is True
    assert synced == [(42, 77, [{"result": "胜"}], "scope-1")]


def test_owned_managed_club_rejects_dismissing_player_manager(monkeypatch):
    import fm_odds_web

    output = {
        "selected_manager_id": 77,
        "manager": {"id": 77, "name": "玩家本人"},
        "managed_team": {"id": 42, "team_type": "club"},
    }
    state = fm_odds_web.LocalOddsState.__new__(fm_odds_web.LocalOddsState)
    state.memory_lock = threading.RLock()
    state._owned_world_club_target = lambda team_id: (
        "scope-1", output, {"id": team_id, "name": "测试俱乐部", "address": "0x1000"},
    )
    monkeypatch.setattr(
        fm_odds_web, "release_owned_club_staff",
        lambda **_kwargs: pytest.fail("不应调用内存解雇事务"),
    )

    with pytest.raises(ValueError, match="不能解雇玩家主教练"):
        state.release_owned_world_club_staff({"source_team_id": 42, "staff_id": 77})


def test_connection_reconciles_persisted_owned_club_customizations(monkeypatch):
    import fm_odds_web

    output = {
        "save_instance_id": "save-1",
        "game_date": "2026-08-03",
        "game_layout": "fm26",
        "manager": {"id": 77, "manager_address": "0x9000"},
        "managed_team": {"id": 42, "team_type": "club"},
    }
    ownership = {
        "id": 42,
        "name": "新俱乐部",
        "short_name": "新简称",
        "renamed_name": "新俱乐部",
        "renamed_short_name": "新简称",
        "renamed_stadium_name": "新球场",
        "owner_name": "测试经理",
        "address": "0x1000",
    }
    state = fm_odds_web.LocalOddsState.__new__(fm_odds_web.LocalOddsState)
    state.lock = threading.RLock()
    state.output = dict(output)
    state.acquired_status_enforced = set()
    state.owned_world_club_addresses = {}
    state.owned_world_club_address_save_id = None
    state.world_club_cache = None
    state.world_club_cache_save_id = None
    state._data_scope_id = lambda _output: "scope-1"
    state._world_club_native_cache = lambda _save_id: None

    monkeypatch.setattr(fm_odds_web, "set_active_save_id", lambda _scope: None)
    monkeypatch.setattr(fm_odds_web, "account_acquired_clubs", lambda *_args: [ownership])
    monkeypatch.setattr(fm_odds_web, "world_club_team_address_hints", lambda _output: set())
    monkeypatch.setattr(
        fm_odds_web, "resolve_native_team_addresses",
        lambda *_args, **_kwargs: {42: "0x5000"},
    )
    monkeypatch.setattr(fm_odds_web, "manager_memory_identity", lambda _output: (77, "0x9000"))
    monkeypatch.setattr(fm_odds_web, "managed_team_rows", lambda _output: [output["managed_team"]])
    chairman_calls = []
    monkeypatch.setattr(
        fm_odds_web, "enforce_acquired_club_chairman_status",
        lambda *args, **kwargs: chairman_calls.append((args, kwargs)) or True,
    )
    monkeypatch.setattr(
        fm_odds_web, "read_native_world_club_snapshot",
        lambda _club: {
            "club": {"name": "旧俱乐部", "short_name": "旧简称"},
            "club_information": {"stadium": {"name": "旧球场"}},
        },
    )
    club_writes = []
    stadium_writes = []
    monkeypatch.setattr(
        fm_odds_web, "rename_native_club",
        lambda *args, **kwargs: club_writes.append((args, kwargs)) or {},
    )
    monkeypatch.setattr(
        fm_odds_web, "update_native_stadium",
        lambda *args, **kwargs: stadium_writes.append((args, kwargs)) or {},
    )

    result = state._reconcile_owned_club_customizations(output)

    assert result == {
        "checked": 1,
        "chairmen_checked": 1,
        "club_names_repaired": 1,
        "stadium_names_repaired": 1,
        "missing": [],
        "errors": [],
    }
    assert chairman_calls[0][0] == (42, "0x5000", "测试经理")
    assert club_writes == [((42, "0x5000", "新俱乐部"), {
        "new_short_name": "新简称", "is_managed_club": True,
    })]
    assert stadium_writes == [((42, "0x5000"), {
        "name": "新球场", "expected_name": "旧球场",
    })]


def test_connection_skips_owned_club_name_writes_when_values_match(monkeypatch):
    import fm_odds_web

    output = {
        "save_instance_id": "save-1", "game_date": "2026-08-03",
        "game_layout": "fm26", "manager": {"id": 77},
    }
    ownership = {
        "id": 42, "renamed_name": "俱乐部", "renamed_short_name": "简称",
        "renamed_stadium_name": "球场", "owner_name": "经理",
    }
    state = fm_odds_web.LocalOddsState.__new__(fm_odds_web.LocalOddsState)
    state.lock = threading.RLock()
    state.output = dict(output)
    state.acquired_status_enforced = set()
    state._data_scope_id = lambda _output: "scope-1"
    state._world_club_native_cache = lambda _save_id: None
    monkeypatch.setattr(fm_odds_web, "set_active_save_id", lambda _scope: None)
    monkeypatch.setattr(fm_odds_web, "account_acquired_clubs", lambda *_args: [ownership])
    monkeypatch.setattr(fm_odds_web, "world_club_team_address_hints", lambda _output: set())
    monkeypatch.setattr(fm_odds_web, "resolve_native_team_addresses", lambda *_args, **_kwargs: {42: "0x5000"})
    monkeypatch.setattr(fm_odds_web, "manager_memory_identity", lambda _output: (77, 0))
    monkeypatch.setattr(fm_odds_web, "managed_team_rows", lambda _output: [])
    monkeypatch.setattr(fm_odds_web, "enforce_acquired_club_chairman_status", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        fm_odds_web, "read_native_world_club_snapshot",
        lambda _club: {
            "club": {"name": "俱乐部", "short_name": "简称"},
            "club_information": {"stadium": {"name": "球场"}},
        },
    )
    monkeypatch.setattr(
        fm_odds_web, "rename_native_club",
        lambda *_args, **_kwargs: pytest.fail("名称一致时不应写俱乐部名称"),
    )
    monkeypatch.setattr(
        fm_odds_web, "update_native_stadium",
        lambda *_args, **_kwargs: pytest.fail("名称一致时不应写球场名称"),
    )

    result = state._reconcile_owned_club_customizations(output)

    assert result["club_names_repaired"] == 0
    assert result["stadium_names_repaired"] == 0
    assert result["errors"] == []


def test_connection_restores_persisted_recruitment_floor_without_downgrade(monkeypatch):
    import fm_odds_web

    output = {"save_instance_id": "save-1", "game_date": "2026-08-03"}
    ownership = {
        "id": 42, "name": "测试俱乐部",
        "facility_level_floors": {"recruitment": 16},
    }
    state = fm_odds_web.LocalOddsState.__new__(fm_odds_web.LocalOddsState)
    state.lock = threading.RLock()
    state.output = dict(output)
    state.owned_world_club_addresses = {42: "0x5000"}
    state.owned_world_club_address_save_id = "save-1"
    state._data_scope_id = lambda _output: "scope-1"
    monkeypatch.setattr(fm_odds_web, "set_active_save_id", lambda _scope: None)
    monkeypatch.setattr(fm_odds_web, "account_acquired_clubs", lambda *_args: [ownership])
    live_level = {"value": 10}
    monkeypatch.setattr(
        fm_odds_web, "read_native_world_club_snapshot",
        lambda _club: {"club_information": {
            "facilities": {"youth_recruitment": live_level["value"]},
        }},
    )
    writes = []

    def restore(team_id, address, facility, target, **kwargs):
        writes.append((team_id, address, facility, target, kwargs))
        before = live_level["value"]
        live_level["value"] = target
        return {
            "team_id": team_id, "facility": facility,
            "before": before, "after": target,
        }

    monkeypatch.setattr(fm_odds_web, "set_native_club_facility_level", restore)

    repaired = state._reconcile_owned_club_native_overrides(output)
    assert repaired == {
        "checked": 1, "recruitment_repaired": 1,
        "sugar_daddy_repaired": 0, "vision_repaired": 0,
        "missing": [], "errors": [],
    }
    assert writes == [(
        42, "0x5000", "recruitment", 16, {"expected_level": 10},
    )]

    live_level["value"] = 18
    writes.clear()
    unchanged = state._reconcile_owned_club_native_overrides(output)
    assert unchanged["recruitment_repaired"] == 0
    assert writes == []


def test_connection_replays_sugar_daddy_for_three_clubs_including_managed(monkeypatch):
    import fm_odds_web

    output = {
        "save_instance_id": "save-1", "game_date": "2026-09-09",
        "managed_team": {"id": 42, "name": "执教俱乐部"},
    }
    ownerships = [
        {"id": 42, "name": "执教俱乐部", "sugar_daddy_override": 1},
        {"id": 43, "name": "集团俱乐部甲", "sugar_daddy_override": 2},
        {"id": 44, "name": "集团俱乐部乙", "sugar_daddy_override": 3},
    ]
    state = fm_odds_web.LocalOddsState.__new__(fm_odds_web.LocalOddsState)
    state.lock = threading.RLock()
    state.output = dict(output)
    state.owned_world_club_addresses = {
        42: "0x5000", 43: "0x6000", 44: "0x7000",
    }
    state.owned_world_club_address_save_id = "save-1"
    state._data_scope_id = lambda _output: "scope-1"
    monkeypatch.setattr(fm_odds_web, "set_active_save_id", lambda _scope: None)
    monkeypatch.setattr(
        fm_odds_web, "account_acquired_clubs", lambda *_args: ownerships,
    )
    monkeypatch.setattr(
        fm_odds_web, "read_native_world_club_snapshot",
        lambda _club: {"club_information": {
            "finances": {"sugar_daddy": 0},
        }},
    )
    operation = SimpleNamespace(writable=True)
    monkeypatch.setattr(
        fm_odds_web, "borrow_game_operation",
        lambda **_kwargs: nullcontext(operation),
    )
    writes = []
    monkeypatch.setattr(
        fm_odds_web, "update_native_club_sugar_daddy",
        lambda team_id, address, target, **kwargs: writes.append(
            (team_id, address, target, kwargs)
        ) or {"team_id": team_id, "before": 0, "after": target},
    )

    result = state._reconcile_owned_club_native_overrides(output)

    assert result == {
        "checked": 3, "recruitment_repaired": 0,
        "sugar_daddy_repaired": 3, "vision_repaired": 0,
        "missing": [], "errors": [],
    }
    assert writes == [
        (42, "0x5000", 1, {"expected": 0, "operation": operation}),
        (43, "0x6000", 2, {"expected": 0, "operation": operation}),
        (44, "0x7000", 3, {"expected": 0, "operation": operation}),
    ]


def test_owned_stadium_rename_persists_name_for_connection_repair(monkeypatch):
    import fm_odds_web

    stadium = {
        "name": "旧球场", "capacity": 75_000,
        "expansion_capacity": 80_000, "state_raw": 16,
        "pitch_condition": 96, "pitch_type": 2,
    }
    detail = {"club_information": {"stadium": stadium, "facilities": {}}}
    persisted = {"id": 42}
    state = fm_odds_web.LocalOddsState.__new__(fm_odds_web.LocalOddsState)
    state.memory_lock = threading.RLock()
    state._owned_world_club_target = lambda team_id: (
        "scope-1", {"game_date": "2026-08-03"},
        {"id": team_id, "name": "测试俱乐部", "address": "0x1000"},
    )
    monkeypatch.setattr(
        fm_odds_web, "load_acquired_clubs", lambda _scope: {"clubs": [persisted]},
    )
    monkeypatch.setattr(
        fm_odds_web, "update_acquired_club",
        lambda _team_id, updates, _scope: persisted.update(updates) or dict(persisted),
    )
    monkeypatch.setattr(
        fm_odds_web, "read_native_stadium_action_state",
        lambda _team_id, _address: detail["club_information"],
    )
    payments = []
    monkeypatch.setattr(
        fm_odds_web, "charge_combined_funds",
        lambda price, event, **metadata: payments.append((price, event, metadata)) or {
            "total": price, "bank": price, "wallet": 0,
        },
    )
    monkeypatch.setattr(
        fm_odds_web, "update_native_stadium",
        lambda _team_id, _address, **kwargs: {
            "before": dict(stadium),
            "after": {**stadium, "name": kwargs["name"]},
        },
    )

    result = state.update_world_club_stadium({
        "team_id": 42, "action": "rename", "name": "新球场",
    })

    assert persisted["renamed_stadium_name"] == "新球场"
    assert result["stadium"]["name"] == "新球场"
    assert result["warning"] is None
    assert payments[0][2]["new_stadium_name"] == "新球场"


def test_owned_stadium_capacity_upgrade_charges_authoritative_quote(monkeypatch):
    import fm_odds_web

    stadium = {
        "name": "测试球场", "capacity": 75_000, "seating_capacity": 75_000,
        "used_capacity": 75_000, "all_seater_capacity": 75_000,
        "expansion_capacity": 80_000, "state_raw": 16, "state_name": "较差",
    }
    detail = {"club_information": {"stadium": stadium, "facilities": {}}}
    state = fm_odds_web.LocalOddsState.__new__(fm_odds_web.LocalOddsState)
    state.memory_lock = threading.RLock()
    state._owned_world_club_target = lambda team_id: (
        "scope-1", {"game_date": "2026-07-28"},
        {"id": team_id, "name": "测试俱乐部", "address": "0x1000"},
    )
    charges = []
    persisted = {"id": 42}
    monkeypatch.setattr(
        fm_odds_web, "load_acquired_clubs", lambda _scope: {"clubs": [persisted]},
    )
    monkeypatch.setattr(
        fm_odds_web, "update_acquired_club",
        lambda _team_id, updates, _scope: persisted.update(updates) or dict(persisted),
    )
    monkeypatch.setattr(
        fm_odds_web, "read_native_stadium_action_state",
        lambda _team_id, _address: detail["club_information"],
    )
    monkeypatch.setattr(
        fm_odds_web, "charge_combined_funds",
        lambda price, event, **metadata: charges.append((price, event, metadata)) or {
            "total": float(price), "bank": float(price), "wallet": 0.0,
        },
    )

    def update(_team_id, _address, **kwargs):
        assert kwargs == {
            "current_capacity": 80_000,
            "expected_capacity": 75_000,
            "expected_expansion_capacity": 80_000,
        }
        return {
            "before": dict(stadium),
            "after": {
                **stadium, "capacity": 80_000, "seating_capacity": 80_000,
                "used_capacity": 80_000, "all_seater_capacity": 80_000,
            },
        }

    monkeypatch.setattr(fm_odds_web, "update_native_stadium", update)
    result = state.update_world_club_stadium({"team_id": 42, "action": "capacity"})

    assert charges[0][0:2] == (480_000_000, "world_club_stadium_capacity")
    assert result["stadium"]["capacity"] == 80_000
    assert result["owned_actions"]["stadium"]["capacity"] is None
    assert result["owned_actions"]["stadium"]["cooldowns"]["capacity"] == {
        "active": True, "until": "2026-09-16", "remaining_days": 50,
    }


def test_owned_stadium_pitch_condition_upgrade_from_196(monkeypatch):
    import fm_odds_web

    stadium = {
        "name": "测试球场", "capacity": 75_000,
        "expansion_capacity": 80_000, "state_raw": 16,
        "pitch_condition": 196, "pitch_type": 2,
    }
    detail = {"club_information": {"stadium": stadium, "facilities": {}}}
    state = fm_odds_web.LocalOddsState.__new__(fm_odds_web.LocalOddsState)
    state.memory_lock = threading.RLock()
    state._owned_world_club_target = lambda team_id: (
        "scope-1", {}, {"id": team_id, "name": "测试俱乐部", "address": "0x1000"},
    )
    charges = []
    monkeypatch.setattr(
        fm_odds_web, "read_native_stadium_action_state",
        lambda _team_id, _address: detail["club_information"],
    )
    monkeypatch.setattr(
        fm_odds_web, "charge_combined_funds",
        lambda price, event, **metadata: charges.append((price, event, metadata)) or {
            "total": float(price), "bank": float(price), "wallet": 0.0,
        },
    )

    def update(_team_id, _address, **kwargs):
        assert kwargs == {
            "pitch_condition": 200, "expected_pitch_condition": 196,
        }
        return {"before": dict(stadium), "after": {**stadium, "pitch_condition": 200}}

    monkeypatch.setattr(fm_odds_web, "update_native_stadium", update)
    result = state.update_world_club_stadium({"team_id": 42, "action": "pitch_condition"})

    assert charges[0][0:2] == (20_000_000, "world_club_stadium_pitch_condition")
    assert result["stadium"]["pitch_condition"] == 200
    assert result["owned_actions"]["stadium"]["pitch_condition"] is None


def test_owned_stadium_pitch_type_change_charges_selected_type(monkeypatch):
    import fm_odds_web

    stadium = {
        "name": "测试球场", "capacity": 75_000,
        "expansion_capacity": 80_000, "state_raw": 16,
        "pitch_condition": 96, "pitch_type": 2,
    }
    detail = {"club_information": {"stadium": stadium, "facilities": {}}}
    state = fm_odds_web.LocalOddsState.__new__(fm_odds_web.LocalOddsState)
    state.memory_lock = threading.RLock()
    state._owned_world_club_target = lambda team_id: (
        "scope-1", {}, {"id": team_id, "name": "测试俱乐部", "address": "0x1000"},
    )
    charges = []
    monkeypatch.setattr(
        fm_odds_web, "read_native_stadium_action_state",
        lambda _team_id, _address: detail["club_information"],
    )
    monkeypatch.setattr(
        fm_odds_web, "charge_combined_funds",
        lambda price, event, **metadata: charges.append((price, event, metadata)) or {
            "total": float(price), "bank": float(price), "wallet": 0.0,
        },
    )

    def update(_team_id, _address, **kwargs):
        assert kwargs == {"pitch_type": 8, "expected_pitch_type": 2}
        return {"before": dict(stadium), "after": {**stadium, "pitch_type": 8}}

    monkeypatch.setattr(fm_odds_web, "update_native_stadium", update)
    result = state.update_world_club_stadium({
        "team_id": 42, "action": "pitch_type", "pitch_type": 8,
    })

    assert charges[0][0:2] == (40_000_000, "world_club_stadium_pitch_type")
    assert result["stadium"]["pitch_type_name"] == "天然与人造混合草皮"


def test_owned_stadium_write_failure_refunds_payment(monkeypatch):
    import fm_odds_web

    detail = {"club_information": {"stadium": {
        "name": "测试球场", "capacity": 75_000, "expansion_capacity": 80_000,
        "state_raw": 16,
    }, "facilities": {}}}
    state = fm_odds_web.LocalOddsState.__new__(fm_odds_web.LocalOddsState)
    state.memory_lock = threading.RLock()
    state._owned_world_club_target = lambda team_id: (
        "scope-1", {"game_date": "2026-07-28"},
        {"id": team_id, "name": "测试俱乐部", "address": "0x1000"},
    )
    payment = {"total": 220_000_000.0, "bank": 220_000_000.0, "wallet": 0.0}
    refunds = []
    persisted = {"id": 42}
    monkeypatch.setattr(
        fm_odds_web, "load_acquired_clubs", lambda _scope: {"clubs": [persisted]},
    )
    monkeypatch.setattr(
        fm_odds_web, "update_acquired_club",
        lambda _team_id, updates, _scope: persisted.update(updates) or dict(persisted),
    )
    monkeypatch.setattr(
        fm_odds_web, "read_native_stadium_action_state",
        lambda _team_id, _address: detail["club_information"],
    )
    monkeypatch.setattr(fm_odds_web, "charge_combined_funds", lambda *_args, **_kwargs: payment)
    monkeypatch.setattr(
        fm_odds_web, "refund_combined_funds",
        lambda paid, event, **metadata: refunds.append((paid, event, metadata)),
    )
    monkeypatch.setattr(
        fm_odds_web, "update_native_stadium",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("写入失败")),
    )

    with pytest.raises(RuntimeError, match="写入失败"):
        state.update_world_club_stadium({"team_id": 42, "action": "expansion"})
    assert refunds[0][0] is payment
    assert refunds[0][1] == "world_club_stadium_expansion_rollback"
    assert persisted["stadium_upgrade_cooldowns"] == {}


def test_owned_stadium_capacity_cooldown_blocks_charge_and_write(monkeypatch):
    import fm_odds_web

    stadium = {
        "name": "测试球场", "capacity": 75_000,
        "expansion_capacity": 80_000, "state_raw": 16,
    }
    detail = {"club_information": {"stadium": stadium, "facilities": {}}}
    ownership = {
        "id": 42,
        "stadium_upgrade_cooldowns": {"capacity": "2026-09-16"},
    }
    state = fm_odds_web.LocalOddsState.__new__(fm_odds_web.LocalOddsState)
    state.memory_lock = threading.RLock()
    state._owned_world_club_target = lambda team_id: (
        "scope-1", {"game_date": "2026-08-27"},
        {"id": team_id, "name": "测试俱乐部", "address": "0x1000"},
    )
    monkeypatch.setattr(
        fm_odds_web, "read_native_stadium_action_state",
        lambda _team_id, _address: detail["club_information"],
    )
    monkeypatch.setattr(
        fm_odds_web, "load_acquired_clubs", lambda _scope: {"clubs": [ownership]},
    )
    monkeypatch.setattr(
        fm_odds_web, "charge_combined_funds",
        lambda *_args, **_kwargs: pytest.fail("冷却期间不应扣款"),
    )
    monkeypatch.setattr(
        fm_odds_web, "update_native_stadium",
        lambda *_args, **_kwargs: pytest.fail("冷却期间不应写入球场"),
    )

    with pytest.raises(ValueError, match=r"还需 20 天.*2026-09-16"):
        state.update_world_club_stadium({"team_id": 42, "action": "capacity"})


def owned_finance_state(monkeypatch):
    import fm_odds_web

    state = fm_odds_web.LocalOddsState.__new__(fm_odds_web.LocalOddsState)
    state.memory_lock = threading.RLock()
    state._owned_world_club_target = lambda team_id: (
        "scope-1", {}, {"id": team_id, "name": "测试俱乐部", "address": "0x1000"},
    )
    state._economy_patch_response = lambda response, transient=(): response
    operation = SimpleNamespace(reader=object(), writable=True)
    state._test_finance_operation = operation
    monkeypatch.setattr(
        fm_odds_web, "borrow_game_operation",
        lambda **_kwargs: nullcontext(operation),
    )
    monkeypatch.setattr(
        fm_odds_web, "read_game_clock_from_reader",
        lambda _reader: {"date": "2026-07-27", "minutes": 600, "time": "10:00"},
    )
    monkeypatch.setattr(
        fm_odds_web, "read_game_clock",
        lambda: {"date": "2026-07-27", "minutes": 600, "time": "10:00"},
    )
    monkeypatch.setattr(
        fm_odds_web, "public_economy",
        lambda: {"bank_balance": 1_000_000},
    )
    monkeypatch.setattr(
        fm_odds_web, "public_league_standings",
        lambda _output: {"competitions": [{
            "competition_id": 7, "competition_name": "测试联赛",
            "teams": [{"team_id": 42, "position": 3, "points": 20}],
        }]},
    )
    return fm_odds_web, state


def test_owned_club_target_uses_single_target_full_rebind(monkeypatch):
    import fm_odds_web

    state = fm_odds_web.LocalOddsState.__new__(fm_odds_web.LocalOddsState)
    state.lock = threading.RLock()
    state.output = {"save_instance_id": "save-1"}
    state._bind_current_save = lambda: "scope-1"
    state._data_scope_id = lambda _output: "scope-1"
    state._world_club_native_cache = lambda _save_id: {
        "clubs": [{"id": 42, "name": "测试俱乐部", "address": "0x1000"}],
    }
    state.owned_world_club_addresses = {}
    state.owned_world_club_address_save_id = "save-1"
    monkeypatch.setattr(
        fm_odds_web, "account_acquired_clubs",
        lambda *_args, **_kwargs: [{"id": 42, "name": "测试俱乐部"}],
    )
    calls = []
    monkeypatch.setattr(
        fm_odds_web, "resolve_native_team_addresses",
        lambda team_ids, hints, **kwargs: (
            calls.append((team_ids, hints, kwargs)) or {42: "0x2000"}
        ),
    )
    monkeypatch.setattr(
        fm_odds_web, "world_club_team_address_hints", lambda _output: {0x3000},
    )
    monkeypatch.setattr(
        fm_odds_web, "scan_native_world_clubs",
        lambda *_args, **_kwargs: pytest.fail(
            "单目标全区回绑不应重建完整世界俱乐部缓存"
        ),
    )

    _scope, _output, club = state._owned_world_club_target(42)

    assert club["address"] == "0x2000"
    assert calls == [({42}, {0x3000}, {
        "scan_all_if_unresolved": True,
        "known_addresses": {42: "0x1000"},
    })]


def test_owned_club_balance_withdrawal_updates_memory_and_bank(monkeypatch):
    fm_odds_web, state = owned_finance_state(monkeypatch)
    writes = []
    bank = []
    monkeypatch.setattr(
        fm_odds_web, "read_club_balance",
        lambda *_args, **_kwargs: {"amount": 500_000},
    )
    monkeypatch.setattr(
        fm_odds_web, "write_club_balance",
        lambda _address, amount, **kwargs: (
            writes.append((amount, kwargs)) or {"amount": amount}
        ),
    )
    monkeypatch.setattr(
        fm_odds_web, "adjust_bank_balance",
        lambda delta, kind, **details: (
            bank.append((delta, kind, details)) or {"bank_balance": 1_100_000}
        ),
    )

    result = state.transfer_owned_world_club_finance({
        "team_id": 42, "field": "balance", "direction": "out", "amount": 100_000,
    })

    assert writes == [(400_000, {
        "team_id": 42, "expected": 500_000,
        "operation": state._test_finance_operation,
    })]
    assert bank[0][0:2] == (100_000, "club_balance_to_bank")
    assert bank[0][2]["source"] == "owned_club_club_balance"
    assert result["owned_club_finance"] == {
        "team_id": 42, "team_name": "测试俱乐部", "field": "balance",
        "direction": "out", "before": 500_000, "amount": 400_000,
    }


def test_owned_club_sugar_daddy_update_returns_new_type(monkeypatch):
    fm_odds_web, state = owned_finance_state(monkeypatch)
    persisted = []
    monkeypatch.setattr(
        fm_odds_web, "update_native_club_sugar_daddy",
        lambda team_id, address, target, **kwargs: {
            "team_id": team_id, "before": 1, "after": target,
            "before_name": "疯狂烧钱型", "after_name": "填补亏空型",
        },
    )
    monkeypatch.setattr(
        fm_odds_web, "update_acquired_club",
        lambda team_id, patch, scope_id: (
            persisted.append((team_id, patch, scope_id)) or {"id": team_id}
        ),
    )

    result = state.update_owned_world_club_sugar_daddy({
        "team_id": 42, "sugar_daddy": 3, "expected": 1,
    })

    assert result["owned_club_finance"] == {
        "team_id": 42, "field": "sugar_daddy", "before": 1, "after": 3,
        "before_name": "疯狂烧钱型", "after_name": "填补亏空型",
    }
    assert persisted == [(42, {"sugar_daddy_override": 3}, "scope-1")]


def test_owned_club_fund_quote_reads_live_amount(monkeypatch):
    fm_odds_web, state = owned_finance_state(monkeypatch)
    reads = []
    monkeypatch.setattr(
        fm_odds_web, "read_transfer_budget",
        lambda address, team_id, **kwargs: (
            reads.append((address, team_id, kwargs)) or {"amount": 375_000}
        ),
    )

    result = state.quote_club_fund_transfer({
        "team_id": 42, "field": "transfer_budget",
    })

    assert reads == [("0x1000", 42, {"force_scan": True})]
    assert result == {
        "available": True, "amount": 375_000,
        "field": "transfer_budget", "team_id": 42,
        "team_name": "测试俱乐部",
    }


def test_owned_club_finance_is_blocked_when_league_is_not_loaded(monkeypatch):
    fm_odds_web, state = owned_finance_state(monkeypatch)
    monkeypatch.setattr(
        fm_odds_web, "public_league_standings",
        lambda _output: {"competitions": []},
    )
    monkeypatch.setattr(
        fm_odds_web, "read_club_balance",
        lambda *_args, **_kwargs: {"amount": 0},
    )
    monkeypatch.setattr(
        fm_odds_web, "read_transfer_budget",
        lambda *_args, **_kwargs: {"amount": 0},
    )
    monkeypatch.setattr(
        fm_odds_web, "write_club_balance",
        lambda *_args, **_kwargs: pytest.fail("未开启联赛时不应写入俱乐部资金"),
    )

    with pytest.raises(ValueError, match="请开启此俱乐部联赛"):
        state.quote_club_fund_transfer({"team_id": 42, "field": "balance"})
    with pytest.raises(ValueError, match="请开启此俱乐部联赛"):
        state.transfer_owned_world_club_finance({
            "team_id": 42, "field": "balance", "direction": "in",
            "amount": 100_000,
        })


def test_owned_club_league_standing_requires_a_current_rank(monkeypatch):
    fm_odds_web, _state = owned_finance_state(monkeypatch)
    output = {}

    standing = fm_odds_web.owned_club_league_standing(output, 42)

    assert standing == {
        "competition_id": 7, "competition_name": "测试联赛",
        "league_position": 3, "league_team_count": 1,
        "league_points": 20,
    }
    monkeypatch.setattr(
        fm_odds_web, "public_league_standings",
        lambda _output: {"competitions": [{
            "competition_id": 7,
            "teams": [{"team_id": 42, "position": 0}],
        }]},
    )
    assert fm_odds_web.owned_club_league_standing(output, 42) is None
    assert fm_odds_web.owned_club_finance_available(None, 0, 0) is False
    assert fm_odds_web.owned_club_finance_available(None, 1, 0) is True
    assert fm_odds_web.owned_club_finance_available(None, 0, 1) is True
    assert fm_odds_web.owned_club_finance_available(None, None, 0) is True


def test_missing_rank_does_not_block_nonzero_owned_club_funds(monkeypatch):
    fm_odds_web, state = owned_finance_state(monkeypatch)
    monkeypatch.setattr(
        fm_odds_web, "public_league_standings",
        lambda _output: {"competitions": []},
    )
    monkeypatch.setattr(
        fm_odds_web, "read_club_balance",
        lambda *_args, **_kwargs: {"amount": 25_000},
    )
    monkeypatch.setattr(
        fm_odds_web, "read_transfer_budget",
        lambda *_args, **_kwargs: pytest.fail(
            "非零结余已证明俱乐部可用时不应扫描无关的转会预算"
        ),
    )

    result = state.quote_club_fund_transfer({
        "team_id": 42, "field": "balance",
    })

    assert result["available"] is True
    assert result["amount"] == 25_000


def test_missing_rank_budget_quote_skips_unrelated_balance_read(monkeypatch):
    fm_odds_web, state = owned_finance_state(monkeypatch)
    monkeypatch.setattr(
        fm_odds_web, "public_league_standings",
        lambda _output: {"competitions": []},
    )
    monkeypatch.setattr(
        fm_odds_web, "read_transfer_budget",
        lambda *_args, **_kwargs: {"amount": 30_000},
    )
    monkeypatch.setattr(
        fm_odds_web, "read_club_balance",
        lambda *_args, **_kwargs: pytest.fail(
            "非零转会预算已证明俱乐部可用时不应读取无关的结余"
        ),
    )

    result = state.quote_club_fund_transfer({
        "team_id": 42, "field": "transfer_budget",
    })

    assert result["available"] is True
    assert result["amount"] == 30_000


def test_owned_transfer_budget_rolls_back_when_bank_update_fails(monkeypatch):
    fm_odds_web, state = owned_finance_state(monkeypatch)
    writes = []
    monkeypatch.setattr(
        fm_odds_web, "read_transfer_budget",
        lambda *_args, **_kwargs: {"amount": 500_000},
    )
    monkeypatch.setattr(
        fm_odds_web, "write_transfer_budget",
        lambda _address, amount, **kwargs: (
            writes.append((amount, kwargs)) or {"amount": amount}
        ),
    )
    monkeypatch.setattr(
        fm_odds_web, "adjust_bank_balance",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("bank failed")),
    )

    with pytest.raises(RuntimeError, match="bank failed"):
        state.transfer_owned_world_club_finance({
            "team_id": 42, "field": "transfer_budget",
            "direction": "in", "amount": 100_000,
        })

    assert writes == [
        (600_000, {
            "team_id": 42, "expected": 500_000,
            "operation": state._test_finance_operation,
        }),
        (500_000, {
            "team_id": 42, "expected": 600_000,
            "operation": state._test_finance_operation,
        }),
    ]


def test_owned_player_alias_requires_current_roster_membership(monkeypatch):
    fm_odds_web, state = owned_finance_state(monkeypatch)
    monkeypatch.setattr(
        fm_odds_web, "read_native_world_club_player_membership",
        lambda _club, player_id: (
            {"id": 7, "name": "游戏原名"} if player_id == 7 else None
        ),
    )
    saved = []
    monkeypatch.setattr(
        fm_odds_web, "set_player_alias",
        lambda scope, player_id, name: saved.append((scope, player_id, name)) or name,
    )

    result = state.update_owned_world_club_player_name({
        "team_id": 42, "player_id": 7, "name": "新名字",
    })
    assert result["name"] == "新名字"
    assert saved == [("scope-1", 7, "新名字")]

    with pytest.raises(ValueError, match="不属于该已收购俱乐部"):
        state.update_owned_world_club_player_name({
            "team_id": 42, "player_id": 8, "name": "错误目标",
        })


def test_clock_rewind_checks_managed_and_all_owned_clubs(monkeypatch):
    fm_odds_web, state = owned_finance_state(monkeypatch)
    state.lock = threading.RLock()
    state.output = {"save_instance_id": "save-1"}
    state.status = ""
    state._bind_current_save = lambda: "scope-1"
    state._data_scope_id = lambda _output: "scope-1"
    state._managed_club_team = lambda: {"id": 42}
    monkeypatch.setattr(
        fm_odds_web, "load_acquired_clubs",
        lambda _scope: {"clubs": [{"id": 42}, {"id": 84}]},
    )
    checked = []
    monkeypatch.setattr(
        fm_odds_web, "rewound_transfer_budget_withdrawals",
        lambda team_id, *_args: checked.append(team_id) or [{"id": str(team_id)}],
    )
    monkeypatch.setattr(
        fm_odds_web, "clawback_rewound_transfer_budget",
        lambda rows, *_args: {
            "clawed_back": len(rows), "amount": 200, "bank": 200,
            "wallet": 0, "unrecovered": 0,
        },
    )

    result = state._reapply_rewound_transfer_budget(
        ("2026-07-27", 600), ("2026-07-27", 500),
    )

    assert checked == [42, 84]
    assert result == {"reapplied": 2, "amount": 200.0}


def test_rewound_game_clock_does_not_delete_owned_clubs(monkeypatch):
    fm_odds_web, state = owned_finance_state(monkeypatch)
    state.lock = threading.RLock()
    state.output = {"save_instance_id": "save-1", "game_date": "2026-07-18"}
    state._data_scope_id = lambda _output: "scope-1"
    monkeypatch.setattr(fm_odds_web, "set_active_save_id", lambda _scope: None)
    monkeypatch.setattr(fm_odds_web, "load_economy", lambda: {"transactions": []})
    recovered = []
    monkeypatch.setattr(
        fm_odds_web, "recover_acquired_clubs_from_transactions",
        lambda transactions, scope: recovered.append((transactions, scope)) or [],
    )
    monkeypatch.setattr(
        fm_odds_web, "load_acquired_clubs",
        lambda scope: {"clubs": [{"id": 42, "acquired_game_date": "2026-07-20"}]},
    )
    state.board_listens_hook = type("Hook", (), {
        "sync": staticmethod(lambda active: {"active": active}),
    })()

    result = state._sync_owned_club_board_hook()

    assert recovered == [([], "scope-1")]
    assert result["team_ids"] == []
