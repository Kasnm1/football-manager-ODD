from __future__ import annotations

import struct
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from tools import future_transfers as future
from tools import game_layout as game_layout_module
from tools.game_layout import FM24_EXE_SHA256, FM26_EXE_SHA256


class FakeReader:
    def __init__(self, game_layout, module_base: int = 0x100000):
        self.layout = game_layout
        self.module_base = module_base
        self.process = SimpleNamespace(pid=77)
        self.memory: dict[int, int] = {}
        self.teams: dict[int, dict] = {}
        self.names: dict[int, str] = {}

    def write(self, address: int, data: bytes) -> None:
        for index, value in enumerate(data):
            self.memory[address + index] = value

    def bytes(self, address: int, size: int):
        values = [self.memory.get(address + index) for index in range(size)]
        if any(value is None for value in values):
            return None
        return bytes(values)

    def ptr(self, address: int):
        raw = self.bytes(address, 8)
        return struct.unpack("<Q", raw)[0] if raw else None

    def u32(self, address: int):
        raw = self.bytes(address, 4)
        return struct.unpack("<I", raw)[0] if raw else None

    def team(self, address: int):
        return self.teams.get(address)

    def fm_nested_string_at(self, address: int):
        return self.names.get(address)

    def fm_string_at(self, address: int):
        return self.names.get(address)


def game_layout(key: str = "fm26"):
    return SimpleNamespace(
        key=key,
        distribution="steam",
        executable_sha256=FM26_EXE_SHA256 if key == "fm26" else FM24_EXE_SHA256,
        game_version="26.3.2" if key == "fm26" else "24.4.2",
        display_name=key,
        module_name="game_plugin.dll" if key == "fm26" else "fm.exe",
        person_common_name_offset=0x60,
        person_first_name_offset=0x50,
        person_last_name_offset=0x58,
        person_full_name_offset=0x40,
    )


def write_vector(reader: FakeReader, header: int, storage: int, pointers: list[int]) -> None:
    reader.write(header, struct.pack("<QQQ", storage, storage + len(pointers) * 8, storage + len(pointers) * 8))
    if pointers:
        reader.write(storage, struct.pack(f"<{len(pointers)}Q", *pointers))


def write_offer(
    reader: FakeReader, address: int, layout: future.FutureTransferLayout,
    *, vtable: int, person: int, buyer: int, seller: int, value: int,
    transfer_id: int, status: int = future.CONFIRMED_STATUS,
) -> None:
    raw = bytearray(layout.offer_read_size)
    struct.pack_into("<Q", raw, 0, vtable)
    struct.pack_into("<Q", raw, layout.person_offset, person)
    struct.pack_into("<Q", raw, layout.buyer_offset, buyer)
    struct.pack_into("<Q", raw, layout.seller_offset, seller)
    struct.pack_into("<I", raw, layout.value_offset, value)
    struct.pack_into("<I", raw, layout.transfer_id_offset, transfer_id)
    raw[layout.status_offset] = status
    reader.write(address, raw)


def build_transfer_reader():
    reader = FakeReader(game_layout())
    layout = future.FM26_FUTURE_TRANSFERS
    manager = 0x200000
    target = 0x300000
    seller = 0x310000
    buyer = 0x320000
    reader.teams = {
        target: {"id": 10, "name": "目标俱乐部"},
        seller: {"id": 11, "name": "卖方俱乐部"},
        buyer: {"id": 12, "name": "买方俱乐部"},
    }
    people = [0x400000, 0x401000, 0x402000, 0x403000]
    for index, person in enumerate(people, 1):
        reader.write(person + 0x0C, struct.pack("<I", 1000 + index))
        reader.names[person + reader.layout.person_common_name_offset] = f"球员{index}"

    dates = [0x500000 + index * 0x10 for index in range(4)]
    for index, (record, person) in enumerate(zip(dates, people), 1):
        reader.write(record, struct.pack("<QII", person, (2029 << 16) | index, index))
    write_vector(reader, manager + layout.dates_vector_offset, 0x510000, dates)

    offers = [0x600000 + index * 0x200 for index in range(4)]
    write_offer(
        reader, offers[0], layout,
        vtable=reader.module_base + layout.full_offer_vtable_rva,
        person=people[0], buyer=target, seller=seller, value=1_250_000, transfer_id=1,
    )
    write_offer(
        reader, offers[1], layout,
        vtable=reader.module_base + layout.loan_offer_vtable_rva,
        person=people[1], buyer=buyer, seller=target, value=25_000, transfer_id=2,
    )
    write_offer(
        reader, offers[2], layout,
        vtable=reader.module_base + layout.full_offer_vtable_rva,
        person=people[2], buyer=target, seller=0, value=0, transfer_id=3,
        status=future.CANCELLED_STATUS,
    )
    write_offer(
        reader, offers[3], layout,
        vtable=reader.module_base + layout.full_offer_vtable_rva,
        person=people[3], buyer=buyer, seller=seller, value=50_000, transfer_id=4,
    )
    write_vector(reader, manager + layout.offers_vector_offset, 0x610000, offers)
    return reader, manager, target, offers


def test_layouts_keep_generation_specific_offsets_and_reject_unknown_builds():
    fm24 = future.future_transfer_layout_for(game_layout("fm24"))
    fm26 = future.future_transfer_layout_for(game_layout("fm26"))

    assert fm24.status_offset == 0x7B
    assert fm24.person_offset == 0x30
    assert fm26.status_offset == 0x8D
    assert fm26.person_offset == 0x38
    unsupported = game_layout("fm26")
    unsupported.executable_sha256 = "UNVERIFIED"
    with pytest.raises(RuntimeError, match="尚未验证"):
        future.future_transfer_layout_for(unsupported)


def test_confirmed_future_transfer_guard_ignores_cancelled_offer(monkeypatch):
    reader, manager, _target, _offers = build_transfer_reader()
    monkeypatch.setattr(
        future, "locate_transfer_manager",
        lambda _reader, _save_identity="": (manager, True),
    )

    assert future.has_confirmed_future_transfer(reader, 0x400000)
    assert future.has_confirmed_future_transfer(reader, 0x401000)
    assert not future.has_confirmed_future_transfer(reader, 0x402000)
    assert future.has_confirmed_future_transfer(reader, 0x403000)


def test_xgp_future_transfer_layout_uses_runtime_rtti_vtables():
    for key in ("fm24", "fm26"):
        layout = game_layout(key)
        layout.distribution = "xgp"
        layout.executable_sha256 = ""
        layout.future_transfer_manager_vtable_rva = 0x100
        layout.future_transfer_full_offer_vtable_rva = 0x200
        layout.future_transfer_loan_offer_vtable_rva = 0x300
        resolved = future.future_transfer_layout_for(layout)
        assert resolved.manager_vtable_rva == 0x100
        assert resolved.full_offer_vtable_rva == 0x200
        assert resolved.loan_offer_vtable_rva == 0x300


def test_fm26_xgp_template_does_not_inherit_steam_future_transfer_vtables():
    template = game_layout_module.FM26_XGP_TEMPLATE
    assert template.future_transfer_manager_vtable_rva is None
    assert template.future_transfer_full_offer_vtable_rva is None
    assert template.future_transfer_loan_offer_vtable_rva is None


def test_fm26_xgp_optional_future_transfer_rtti_does_not_block_core_layout():
    resolved = {
        key: [index]
        for index, key in enumerate((
            "fixture", "fixture_result", "season_result", "team", "national_team",
            "nation", "club", "competition",
        ), start=1)
    }
    resolved.update({
        "future_transfer_manager": [],
        "future_transfer_full_offer": [0x100, 0x200],
        "future_transfer_loan_offer": [],
    })

    game_layout_module._validate_fm26_xgp_core_rtti(resolved)


def test_xgp_layouts_do_not_inherit_unverified_steam_native_rvas():
    fm24_names = (
        "player_move_contract_pool_rva",
        "player_move_main_contract_vtable_rva",
        "player_move_fm24_source_cleanup_rva",
        "player_move_fm24_transfer_rva",
        "player_move_fm24_contract_factory_rva",
        "player_move_fm24_loan_constructor_rva",
    )
    assert all(
        getattr(game_layout_module.FM24_XGP_LAYOUT, name) is None
        for name in fm24_names
    )

    fm26_names = (
        "match_engine_active_state_rva",
        "play_fixture_manager_pointer_rva",
        "play_fixture_manager_vtable_rva",
        "player_move_contract_pool_rva",
        "player_move_loan_contract_pool_rva",
        "player_move_loan_contract_constructor_rva",
        "player_move_contract_allocate_rva",
        "player_move_contract_release_rva",
        "player_move_contract_factory_global_rva",
        "player_move_contract_factory_target_rva",
        "player_move_main_contract_vtable_rva",
        "player_move_terminate_contract_rva",
        "player_move_prepare_loan_rva",
        "player_move_club_method_rva",
    )
    assert all(
        getattr(game_layout_module.FM26_XGP_TEMPLATE, name) is None
        for name in fm26_names
    )


def test_epic_inherits_only_verified_contract_pool_rvas():
    assert game_layout_module.FM24_EPIC_LAYOUT.player_move_contract_pool_rva == 0x6378C80
    assert game_layout_module.FM24_EPIC_LAYOUT.player_move_main_contract_vtable_rva == 0x5667C88
    native_names = (
        "player_move_fm24_source_cleanup_rva",
        "player_move_fm24_transfer_rva",
        "player_move_fm24_contract_factory_rva",
        "player_move_fm24_loan_constructor_rva",
    )
    assert all(
        getattr(game_layout_module.FM24_EPIC_LAYOUT, name) is None
        for name in native_names
    )


def test_other_unverified_fm24_builds_do_not_inherit_player_movement_rvas():
    names = (
        "player_move_contract_pool_rva",
        "player_move_main_contract_vtable_rva",
        "player_move_fm24_source_cleanup_rva",
        "player_move_fm24_transfer_rva",
        "player_move_fm24_contract_factory_rva",
        "player_move_fm24_loan_constructor_rva",
    )
    layouts = (
        game_layout_module.FM24_240_LAYOUT,
        game_layout_module.FM24_241_LAYOUT,
        game_layout_module.FM24_XGP_LAYOUT,
    )
    for layout in layouts:
        assert all(getattr(layout, name) is None for name in names)


def test_confirmed_rows_pair_date_by_transfer_id_and_person_and_filter_by_club():
    reader, manager, target, _offers = build_transfer_reader()

    rows = future._confirmed_transfer_rows(
        reader, manager, future.FM26_FUTURE_TRANSFERS,
        team_id=10, team_address=target,
    )

    assert [(row["player_name"], row["direction"]) for row in rows] == [
        ("球员1", "incoming"),
        ("球员2", "outgoing"),
    ]
    assert rows[0]["fee"] == 1_250_000
    assert rows[0]["transfer_type"] == "full"
    assert rows[1]["transfer_type"] == "loan"


def test_target_team_address_prefers_uid_resolver_and_allows_missing_hint(monkeypatch):
    reader, _manager, _target, _offers = build_transfer_reader()
    monkeypatch.setattr(
        future, "resolve_team_club",
        lambda _reader, _hint, team_id: SimpleNamespace(
            team_address=0x330000, club_address=0x340000, team_id=team_id,
        ),
    )

    assert future._resolve_target_team_address(reader, 10, 0) == 0x330000


def test_target_team_address_legacy_fallback_still_checks_team_uid(monkeypatch):
    reader, _manager, target, _offers = build_transfer_reader()
    monkeypatch.setattr(
        future, "resolve_team_club",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("目录暂不可用")),
    )

    assert future._resolve_target_team_address(reader, 10, hex(target)) == target
    with pytest.raises(RuntimeError, match="地址已失效"):
        future._resolve_target_team_address(reader, 99, hex(target))


def test_date_record_person_must_match_offer_person():
    reader, manager, target, offers = build_transfer_reader()
    layout = future.FM26_FUTURE_TRANSFERS
    reader.write(0x500000, struct.pack("<QII", 0x499999, (2029 << 16) | 1, 1))

    rows = future._confirmed_transfer_rows(
        reader, manager, layout, team_id=10, team_address=target,
    )

    assert [row["offer_address"] for row in rows] == [hex(offers[1])]


def test_zero_transfer_id_is_valid():
    reader, manager, target, offers = build_transfer_reader()
    layout = future.FM26_FUTURE_TRANSFERS
    person = 0x400000
    reader.write(0x500000, struct.pack("<QII", person, (2029 << 16) | 1, 0))
    write_offer(
        reader, offers[0], layout,
        vtable=reader.module_base + layout.full_offer_vtable_rva,
        person=person, buyer=target, seller=0x310000, value=1_250_000, transfer_id=0,
    )

    rows = future._confirmed_transfer_rows(
        reader, manager, layout, team_id=10, team_address=target,
    )

    assert rows[0]["transfer_id"] == 0


def install_cancel_runtime(monkeypatch, reader, manager):
    module = SimpleNamespace(base_address=reader.module_base)
    reader.layout.module = lambda _process: module

    @contextmanager
    def opened(_pid, **_kwargs):
        yield reader.process

    monkeypatch.setattr(future, "select_process_layout", lambda: (77, "fm.exe", reader.layout))
    monkeypatch.setattr(future, "open_process", opened)
    monkeypatch.setattr(future, "Reader", lambda _process, _base, _layout: reader)
    monkeypatch.setattr(future, "locate_transfer_manager", lambda _reader, _save: (manager, True))


def test_cancel_writes_only_status_byte_after_identity_checks(monkeypatch):
    reader, manager, target, offers = build_transfer_reader()
    install_cancel_runtime(monkeypatch, reader, manager)

    def write_memory(_process, address, data):
        reader.write(address, data)

    monkeypatch.setattr(future, "write_process_memory", write_memory)
    monkeypatch.setattr(future, "read_process_memory", lambda _process, address, size: reader.bytes(address, size))

    result = future.cancel_future_transfer(10, hex(target), 1, hex(offers[0]), save_identity="save-a")

    assert result["status_after"] == future.CANCELLED_STATUS
    assert reader.bytes(offers[0] + future.FM26_FUTURE_TRANSFERS.status_offset, 1) == b"\x03"


def test_cancel_readback_failure_restores_confirmed_status(monkeypatch):
    reader, manager, target, offers = build_transfer_reader()
    install_cancel_runtime(monkeypatch, reader, manager)
    status_address = offers[0] + future.FM26_FUTURE_TRANSFERS.status_offset
    fail_first_readback = {"value": False}

    def write_memory(_process, address, data):
        reader.write(address, data)
        if address == status_address and data == b"\x03":
            fail_first_readback["value"] = True

    def read_memory(_process, address, size):
        if address == status_address and fail_first_readback["value"]:
            fail_first_readback["value"] = False
            return b"\x13"
        return reader.bytes(address, size)

    monkeypatch.setattr(future, "write_process_memory", write_memory)
    monkeypatch.setattr(future, "read_process_memory", read_memory)

    with pytest.raises(RuntimeError, match="已恢复原状态"):
        future.cancel_future_transfer(10, hex(target), 1, hex(offers[0]), save_identity="save-a")

    assert reader.bytes(status_address, 1) == b"\x13"


def test_cancel_write_exception_after_mutation_restores_confirmed_status(monkeypatch):
    reader, manager, target, offers = build_transfer_reader()
    install_cancel_runtime(monkeypatch, reader, manager)
    status_address = offers[0] + future.FM26_FUTURE_TRANSFERS.status_offset
    fail_first_write = {"value": True}

    def write_memory(_process, address, data):
        reader.write(address, data)
        if address == status_address and data == b"\x03" and fail_first_write["value"]:
            fail_first_write["value"] = False
            raise OSError("simulated ambiguous write")

    monkeypatch.setattr(future, "write_process_memory", write_memory)
    monkeypatch.setattr(future, "read_process_memory", lambda _process, address, size: reader.bytes(address, size))

    with pytest.raises(RuntimeError, match="原状态已确认"):
        future.cancel_future_transfer(10, hex(target), 1, hex(offers[0]), save_identity="save-a")

    assert reader.bytes(status_address, 1) == b"\x13"
