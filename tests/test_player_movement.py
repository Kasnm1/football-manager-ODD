from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
import struct
from types import SimpleNamespace
import threading

import pytest

import fm_odds_web
from fm_odds_web import LocalOddsState
import tools.player_movement as player_movement
from tools.game_layout import (
    FM24_EPIC_LAYOUT, FM24_LAYOUT, FM26_LAYOUT, FM26_XGP_TEMPLATE,
)
from tools.player_movement import (
    FM24_TRANSFER_EXE_SHA256,
    _build_fm24_direct_transfer_stub, _build_fm24_loan_stub,
    _build_fm24_transfer_stub, _build_stub, _contract_template,
    _fm24_loan_contract_data, _fm24_native_stack_rsp,
    _fm24_other_contracts_data,
    _fm26_player_move_entry_failures, _pointer_matches, _remove_unique_pointer, _required_fm24_layout,
    _required_layout,
)

ROOT = (Path(__file__).resolve().parents[1] / "src")


class _EntryReader:
    def __init__(self, pointers, *, pool_size=player_movement.CONTRACT_SIZE, prefix=None):
        self.pointers = pointers
        self.pool_size = pool_size
        self.prefix = prefix if prefix is not None else player_movement.TERMINATE_CONTRACT_THUNK_PREFIX

    def ptr(self, address):
        return self.pointers.get(address)

    def u32(self, _address):
        return self.pool_size

    def bytes(self, _address, _size):
        return self.prefix


def test_owned_source_team_accepts_verified_youth_squad_from_same_club(monkeypatch):
    assert FM24_LAYOUT.team_type_offset == 0x28
    assert FM24_EPIC_LAYOUT.team_type_offset == 0x28
    assert FM26_LAYOUT.team_type_offset == 0x28
    first, youth, club = 0x1000, 0x2000, 0x9000
    layout = SimpleNamespace(club_vtable_rva=0x400, team_type_offset=0x28)
    module = SimpleNamespace(base_address=0x100000)

    class FakeReader:
        def __init__(self):
            self.layout = layout

        @staticmethod
        def ptr(address):
            return {
                first + 0x30: club,
                youth + 0x30: club,
                club: module.base_address + layout.club_vtable_rva,
            }.get(address, 0)

        @staticmethod
        def u32(address):
            return 679 if address == club + 0x0C else 0

        @staticmethod
        def u8(address):
            return 10 if address == youth + 0x28 else 0

    monkeypatch.setattr(
        player_movement, "_resolve_team_address",
        lambda _reader, uid, _address: {679: first, 2000339955: youth}[int(uid)],
    )

    resolved, resolved_club = player_movement._resolve_owned_source_team(
        FakeReader(), module, layout,
        source_team_id=679, source_team_address="0x1000",
        source_squad_team_id=2000339955,
        source_squad_team_address="0x2000",
    )

    assert (resolved, resolved_club) == (youth, club)


def test_reassign_owned_club_player_squad_preserves_contract_and_registration(monkeypatch):
    module_base = 0x10000000
    first_team, youth_team, club = 0x2000, 0x3000, 0x4000
    player, other_player, person, contract = 0x5000, 0x5100, 0x6000, 0x7000
    source_storage, target_storage = 0x8000, 0x9000
    memory: dict[int, int] = {}

    def store(address, raw):
        for index, value in enumerate(raw):
            memory[int(address) + index] = int(value)

    def read(address, size):
        return bytes(memory.get(int(address) + index, 0) for index in range(int(size)))

    def put_qword(address, value):
        store(address, struct.pack("<Q", int(value)))

    def put_dword(address, value):
        store(address, struct.pack("<I", int(value)))

    layout = SimpleNamespace(
        key="fm24", module_name="fm.exe", club_vtable_rva=0x1234,
        team_type_offset=0x28,
    )
    module = SimpleNamespace(base_address=module_base)
    layout.module = lambda _process: module
    process = SimpleNamespace(handle=1)

    put_qword(first_team + player_movement.TEAM_CLUB, club)
    put_qword(youth_team + player_movement.TEAM_CLUB, club)
    put_qword(club, module_base + layout.club_vtable_rva)
    put_dword(club + player_movement.ENTITY_UID, 679)
    store(first_team + layout.team_type_offset, b"\x00")
    store(youth_team + layout.team_type_offset, b"\x0a")
    put_qword(first_team + player_movement.TEAM_ROSTER_BEGIN, source_storage)
    put_qword(first_team + player_movement.TEAM_ROSTER_END, source_storage + 8)
    put_qword(first_team + player_movement.TEAM_ROSTER_CAPACITY, source_storage + 32)
    put_qword(source_storage, player)
    put_qword(youth_team + player_movement.TEAM_ROSTER_BEGIN, target_storage)
    put_qword(youth_team + player_movement.TEAM_ROSTER_END, target_storage + 8)
    put_qword(youth_team + player_movement.TEAM_ROSTER_CAPACITY, target_storage + 32)
    put_qword(target_storage, other_player)
    put_qword(player + player_movement.PLAYER_CURRENT_TEAM, first_team)
    put_qword(player + player_movement.PLAYER_REGISTERED_TEAM, first_team)
    put_qword(person + player_movement.FM24_PERSON_CONTRACT, contract)
    put_qword(person + player_movement.FM24_PERSON_CONTRACT_COLLECTION, 0)
    put_qword(contract + 0x08, person)
    put_qword(contract + 0x10, first_team)

    player_ids = {player: 30, other_player: 31}

    class FakeReader:
        _page_cache = None

        @staticmethod
        def ptr(address):
            return struct.unpack("<Q", read(address, 8))[0]

        @staticmethod
        def u32(address):
            return struct.unpack("<I", read(address, 4))[0]

        @staticmethod
        def u8(address):
            return read(address, 1)[0]

        @staticmethod
        def bytes(address, size):
            return read(address, size)

        def roster(self, team):
            begin = self.ptr(team + player_movement.TEAM_ROSTER_BEGIN)
            end = self.ptr(team + player_movement.TEAM_ROSTER_END)
            return [
                {"id": player_ids[pointer], "address": hex(pointer)}
                for pointer in (
                    struct.unpack(f"<{(end - begin) // 8}Q", read(begin, end - begin))
                    if end > begin else ()
                )
            ]

        @staticmethod
        def invalidate_prefetch():
            return None

    reader = FakeReader()
    monkeypatch.setattr(player_movement, "_configure_thread_api", lambda: None)
    monkeypatch.setattr(
        player_movement, "select_process_layout", lambda: (24, "fm.exe", layout),
    )
    monkeypatch.setattr(
        player_movement, "open_process", lambda *_args, **_kwargs: nullcontext(process),
    )
    monkeypatch.setattr(
        player_movement, "_writable_player_movement_reader", lambda *_args: reader,
    )
    monkeypatch.setattr(
        player_movement, "_resolve_team_address",
        lambda _reader, uid, _address: {679: first_team, 2001: youth_team}[int(uid)],
    )
    monkeypatch.setattr(
        player_movement, "_validated_player_person", lambda *_args: person,
    )
    monkeypatch.setattr(
        player_movement, "write_process_memory",
        lambda _process, address, raw: store(address, raw),
    )
    monkeypatch.setattr(
        player_movement, "read_process_memory",
        lambda _process, address, size: read(address, size),
    )
    idle = SimpleNamespace(Rip=1, Rsp=2)
    monkeypatch.setattr(player_movement, "_logic_thread", lambda _process: (7, idle))
    monkeypatch.setattr(player_movement, "_open_thread", lambda _thread_id: 8)
    monkeypatch.setattr(player_movement, "_get_context", lambda _handle: idle)
    monkeypatch.setattr(
        player_movement, "kernel32",
        SimpleNamespace(
            SuspendThread=lambda _handle: 0,
            ResumeThread=lambda _handle: 0,
            CloseHandle=lambda _handle: 1,
            VirtualAllocEx=lambda *_args: 0,
            VirtualFreeEx=lambda *_args: 1,
        ),
    )

    result = player_movement.reassign_owned_club_player_squad(
        club_team_id=679, club_team_address=hex(first_team),
        source_squad_team_id=679, source_squad_team_address=hex(first_team),
        target_squad_team_id=2001, target_squad_team_address=hex(youth_team),
        player_id=30,
    )

    assert result["verified"] is True
    assert result["source_squad_type"] == 0
    assert result["target_squad_type"] == 10
    assert reader.roster(first_team) == []
    assert [row["id"] for row in reader.roster(youth_team)] == [31, 30]
    assert reader.ptr(player + player_movement.PLAYER_CURRENT_TEAM) == youth_team
    assert reader.ptr(player + player_movement.PLAYER_REGISTERED_TEAM) == first_team
    assert reader.ptr(person + player_movement.FM24_PERSON_CONTRACT) == contract
    assert reader.ptr(contract + 0x10) == first_team

def _entry_validation_reader(*, prepare_matches=True, player_staff=False):
    base = 0x10000000
    offsets = {
        "player_move_contract_pool_rva": 0x1000,
        "player_move_loan_contract_pool_rva": 0x1100,
        "player_move_loan_contract_constructor_rva": 0x1200,
        "player_move_contract_factory_global_rva": 0x2000,
        "player_move_contract_factory_target_rva": 0x3000,
        "player_move_main_contract_vtable_rva": 0x4000,
        "player_move_club_method_rva": 0x5000,
        "player_move_prepare_loan_rva": 0x6000,
        "player_move_terminate_contract_rva": 0x7000,
    }
    person = 0x20000000
    person_vtable_rva = 0x4785848 if player_staff else 0x4509D68
    interface_rvas = (
        (0x47857F8, 0x47852DC)
        if player_staff else (0x4509D18, 0x4509804)
    )
    person_vtable = base + person_vtable_rva
    primary_contract = 0x22000000
    factory_object = 0x23000000
    factory_vtable = 0x24000000
    club_vtable = base + 0x8000
    pointers = {
        base + offsets["player_move_contract_factory_global_rva"]: factory_object,
        factory_object: factory_vtable,
        factory_vtable + 0x18: base + offsets["player_move_contract_factory_target_rva"],
        club_vtable + 0x1C0: base + offsets["player_move_club_method_rva"],
        primary_contract: base + offsets["player_move_main_contract_vtable_rva"],
        person: person_vtable,
        person_vtable + 0x320: (
            base + offsets["player_move_prepare_loan_rva"] if prepare_matches else 0
        ),
    }
    for index, rva in enumerate(interface_rvas):
        pointers[person + player_movement.CONTRACT_OWNER_INTERFACE_OFFSET + index * 8] = base + rva
    return _EntryReader(pointers), base, offsets, club_vtable, person, primary_contract


def test_fm26_transfer_does_not_require_unused_loan_entry():
    reader, base, offsets, club_vtable, person, contract = _entry_validation_reader(
        prepare_matches=False,
    )
    assert _fm26_player_move_entry_failures(
        reader, mode="transfer", module_base=base,
        offsets=offsets, expected_club_vtable=club_vtable,
        person=person, primary_contract=contract,
    ) == ()


def test_fm26_loan_uses_the_distinct_0xf8_contract_pool():
    reader, base, offsets, club_vtable, person, contract = _entry_validation_reader()
    reader.pool_size = player_movement.FM26_LOAN_CONTRACT_SIZE
    reader.prefix = player_movement.FM26_LOAN_CONSTRUCTOR_THUNK_PREFIX
    assert _fm26_player_move_entry_failures(
        reader, mode="loan", module_base=base,
        offsets=offsets, expected_club_vtable=club_vtable,
        person=person, primary_contract=contract,
    ) == ()


def test_fm26_loan_rejects_an_unmatched_contract_constructor():
    reader, base, offsets, club_vtable, person, contract = _entry_validation_reader()
    reader.pool_size = player_movement.FM26_LOAN_CONTRACT_SIZE
    reader.prefix = b"invalid"

    assert _fm26_player_move_entry_failures(
        reader, mode="loan", module_base=base,
        offsets=offsets, expected_club_vtable=club_vtable,
        person=person, primary_contract=contract,
    ) == ("租借合同构造器",)


def test_fm26_player_staff_uses_its_own_contract_owner_interface():
    reader, base, offsets, club_vtable, person, contract = _entry_validation_reader(
        player_staff=True,
    )
    assert _fm26_player_move_entry_failures(
        reader, mode="transfer", module_base=base,
        offsets=offsets, expected_club_vtable=club_vtable,
        person=person, primary_contract=contract,
    ) == ()


def test_fm26_contract_owner_interface_must_match_person_type():
    reader, base, offsets, club_vtable, person, contract = _entry_validation_reader(
        player_staff=True,
    )
    reader.pointers[person + player_movement.CONTRACT_OWNER_INTERFACE_OFFSET] = (
        base + 0x4509D18
    )
    assert _fm26_player_move_entry_failures(
        reader, mode="transfer", module_base=base,
        offsets=offsets, expected_club_vtable=club_vtable,
        person=person, primary_contract=contract,
    ) == ("合同所有者接口",)


def test_contract_template_matches_native_pre_factory_defaults():
    raw = _contract_template(0x1122334455667788)

    assert len(raw) == 0xC8
    assert int.from_bytes(raw[0:8], "little") == 0x1122334455667788
    assert int.from_bytes(raw[0x44:0x48], "little") == 0x076C0001
    assert int.from_bytes(raw[0x48:0x4C], "little") == 0x076C0001
    assert int.from_bytes(raw[0x3C:0x44], "little") == 0xFFFFFFFFFFFFFFFF


def test_fm26_fmrte_contract_clone_updates_all_transfer_dates_and_references():
    template = bytearray(range(player_movement.CONTRACT_SIZE))
    template[player_movement.FM26_TRANSFER_STATUS] = 0x7F

    raw = player_movement._fm26_fmrte_contract_data(
        bytes(template), main_vtable=0x1111222233334444,
        person=0x5555666677778888, target_team=0x9999AAAABBBBCCCC,
        start_code=0x07E91B53, expiry_code=0x07EC0153,
    )

    assert len(raw) == player_movement.CONTRACT_SIZE
    assert int.from_bytes(raw[0x00:0x08], "little") == 0x1111222233334444
    assert int.from_bytes(raw[0x08:0x10], "little") == 0x5555666677778888
    assert int.from_bytes(raw[0x10:0x18], "little") == 0x9999AAAABBBBCCCC
    assert int.from_bytes(
        raw[player_movement.FM26_CONTRACT_STARTED:player_movement.FM26_CONTRACT_STARTED + 4],
        "little",
    ) == 0x07E91B53
    assert int.from_bytes(
        raw[player_movement.FM26_CONTRACT_EXPIRY:player_movement.FM26_CONTRACT_EXPIRY + 4],
        "little",
    ) == 0x07EC0153
    assert int.from_bytes(
        raw[player_movement.FM26_CONTRACT_SIGNED:player_movement.FM26_CONTRACT_SIGNED + 4],
        "little",
    ) == 0x07E91B53
    assert raw[player_movement.FM26_TRANSFER_STATUS] == 0


def test_fm26_contract_slot_plan_uses_free_head_when_available():
    assert player_movement._fm26_contract_slot_plan(
        pool_head=0x1000,
        pool_available=2,
        pool_item_size=player_movement.CONTRACT_SIZE,
        next_free=0x2000,
        primary_contract=0x3000,
        template_contract=0x4000,
    ) == (0x1000, False)


def test_fm26_contract_slot_plan_reuses_primary_when_pool_is_exhausted():
    assert player_movement._fm26_contract_slot_plan(
        pool_head=0,
        pool_available=0,
        pool_item_size=player_movement.CONTRACT_SIZE,
        next_free=0,
        primary_contract=0x3000,
        template_contract=0x4000,
    ) == (0x3000, True)


def test_fm26_exhausted_pool_treats_reader_null_head_as_native_zero():
    reader = SimpleNamespace(ptr=lambda _address: None)
    assert player_movement._pointer_matches(reader, 0x1000, 0)


@pytest.mark.parametrize(
    ("pool_head", "pool_available", "next_free"),
    [(0x1000, 0, 0), (0, 1, 0), (0x1000, 2, 0)],
)
def test_fm26_contract_slot_plan_rejects_inconsistent_pool_state(
    pool_head, pool_available, next_free,
):
    with pytest.raises(RuntimeError, match="空闲链校验失败"):
        player_movement._fm26_contract_slot_plan(
            pool_head=pool_head,
            pool_available=pool_available,
            pool_item_size=player_movement.CONTRACT_SIZE,
            next_free=next_free,
            primary_contract=0x3000,
            template_contract=0x4000,
        )


def test_fm26_fmrte_layout_requires_exact_verified_executable():
    values = player_movement._required_fm26_fmrte_layout(FM26_LAYOUT)
    assert values == {
        "player_move_contract_pool_rva": 0x4E373A0,
        "player_move_main_contract_vtable_rva": 0x4334DF8,
        "game_date_rva": FM26_LAYOUT.game_date_rva,
    }
    unsupported = SimpleNamespace(**{
        **FM26_LAYOUT.__dict__, "executable_sha256": "00" * 32,
    })
    with pytest.raises(RuntimeError, match="球员转会目前仅支持已验证的 FM26 Steam 26.3.2") as error:
        player_movement._required_fm26_fmrte_layout(unsupported)
    assert "FMRTE" not in str(error.value)


def test_team_belongs_to_same_club_accepts_reserve_team():
    club = 0x1000
    team = 0x2000
    reader = SimpleNamespace(ptr=lambda address: club if address == team + player_movement.TEAM_CLUB else None)
    assert player_movement._team_belongs_to_club(reader, team, club)
    assert not player_movement._team_belongs_to_club(reader, team, 0x3000)


def test_transfer_listing_status_preserves_unrelated_flags():
    assert player_movement._transfer_listing_status(0x52, True) == 0x43
    assert player_movement._transfer_listing_status(0x43, False) == 0x42


def test_pointer_match_treats_reader_null_as_native_zero():
    null_reader = SimpleNamespace(ptr=lambda _address: None)
    pointer_reader = SimpleNamespace(ptr=lambda _address: 0x1234)

    assert _pointer_matches(null_reader, 0x1000, 0)
    assert _pointer_matches(pointer_reader, 0x1000, 0x1234)
    assert not _pointer_matches(pointer_reader, 0x1000, 0)


def test_player_move_invalidates_prefetched_pages_before_post_write_checks():
    calls = []
    reader = SimpleNamespace(
        _page_cache={0x1000: b"stale"},
        invalidate_prefetch=lambda: calls.append("invalidated"),
    )

    player_movement._invalidate_reader_after_write(reader)

    assert calls == ["invalidated"]


def test_player_move_cache_invalidation_supports_legacy_test_readers():
    reader = SimpleNamespace(_page_cache={0x1000: b"stale"})

    player_movement._invalidate_reader_after_write(reader)

    assert reader._page_cache == {}


def test_writable_player_movement_reader_disables_prefetch_cache(monkeypatch):
    reader = SimpleNamespace(_page_cache={0x1000: b"stale"})
    monkeypatch.setattr(player_movement, "Reader", lambda *_args: reader)

    resolved = player_movement._writable_player_movement_reader(
        object(), 0x100000, SimpleNamespace(key="fm26"),
    )

    assert resolved is reader
    assert reader._page_cache is None


def test_player_move_layout_is_fail_closed():
    supported = SimpleNamespace(
        key="fm26", distribution="steam", game_version="26.3.2",
        player_move_contract_pool_rva=1,
        player_move_loan_contract_pool_rva=12,
        player_move_loan_contract_constructor_rva=13,
        player_move_contract_allocate_rva=2,
        player_move_contract_release_rva=3,
        player_move_contract_factory_global_rva=4,
        player_move_contract_factory_target_rva=5,
        player_move_main_contract_vtable_rva=6,
        player_move_terminate_contract_rva=7,
        player_move_prepare_loan_rva=8,
        player_move_club_method_rva=9,
        loan_contract_vtable_rva=10,
        game_date_rva=11,
    )
    assert _required_layout(supported)["player_move_club_method_rva"] == 9

    unsupported = SimpleNamespace(**{**supported.__dict__, "distribution": "xgp"})
    with pytest.raises(RuntimeError, match="FM26 Steam 26.3.2"):
        _required_layout(unsupported)


def test_fm26_transfer_is_fail_closed_after_schedule_crash(monkeypatch):
    calls = []
    monkeypatch.setattr(
        player_movement, "select_process_layout",
        lambda: (26, "fm.exe", FM26_LAYOUT),
    )
    monkeypatch.setattr(player_movement, "_configure_thread_api", lambda: None)
    monkeypatch.setattr(
        player_movement, "_move_fm26_owned_club_player_fmrte",
        lambda **kwargs: calls.append(kwargs) or {
            "verified": True,
            "compatibility_mode": "fm26_verified_contract_pool",
        },
    )

    monkeypatch.setattr(player_movement, "FM26_TRANSFER_RUNTIME_ENABLED", False)

    with pytest.raises(RuntimeError, match="打开日程表时已两次触发游戏严重错误"):
        player_movement.move_owned_club_player(
            source_team_id=741, source_team_address="0x1000",
            target_team_id=708, target_team_address="0x2000",
            player_id=2000194005, mode="transfer",
        )

    assert calls == []


def test_fm26_transfer_runtime_is_enabled_for_explicit_testing():
    assert player_movement.FM26_TRANSFER_RUNTIME_ENABLED is True


def test_fm26_transfer_and_loan_publish_independent_capabilities():
    capabilities = player_movement.player_movement_capabilities(FM26_LAYOUT)

    assert capabilities["transfer"]["enabled"] is True
    assert capabilities["transfer"]["implementation"] == "fm26_main_contract_pool"
    assert capabilities["loan"]["enabled"] is False
    assert capabilities["loan"]["implementation"] == "fm26_native_loan_constructor"
    assert "实机仍会导致游戏闪退" in capabilities["loan"]["reason"]
    assert capabilities["transfer"]["implementation"] != capabilities["loan"]["implementation"]


@pytest.mark.parametrize(
    ("error", "code", "phase", "retryable"),
    [
        (
            player_movement.PlayerMovementContextChangedError("球队地址已变化"),
            "stale_context", "resolve_context", True,
        ),
        (
            player_movement.PlayerMovementContextChangedError(
                "球员状态校验失败：注册球队",
                retryable=False, error_code="player_state_changed",
                error_phase="preflight",
            ),
            "player_state_changed", "preflight", False,
        ),
        (player_movement.PlayerMovementOperationError("layout", code="layout_mismatch", phase="validate_layout"), "layout_mismatch", "validate_layout", False),
        (player_movement.PlayerMovementOperationError("verify", code="post_write_mismatch", phase="verify"), "post_write_mismatch", "verify", False),
        (player_movement.PlayerMovementOperationError("readback", code="post_write_mismatch", phase="verify"), "post_write_mismatch", "verify", False),
        (player_movement.PlayerMovementOperationError("rollback", code="rollback_incomplete", phase="rollback"), "rollback_incomplete", "rollback", False),
    ],
)
def test_player_movement_errors_have_stable_diagnostic_codes(
    error, code, phase, retryable,
):
    detail = player_movement.describe_player_movement_error(error, mode="transfer")

    assert detail["error_code"] == code
    assert detail["error_phase"] == phase
    assert detail["retryable"] is retryable
    assert detail["operation"] == "transfer"


def test_fm26_loan_is_rejected_before_native_access(monkeypatch):
    monkeypatch.setattr(
        player_movement, "select_process_layout",
        lambda: (26, "fm.exe", FM26_LAYOUT),
    )
    monkeypatch.setattr(player_movement, "_configure_thread_api", lambda: None)

    monkeypatch.setattr(
        player_movement, "open_process",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("native process access must not be reached")
        ),
    )
    with pytest.raises(
        player_movement.PlayerMovementCapabilityError,
        match="新构造器路径实机仍会导致游戏闪退",
    ) as raised:
        player_movement.move_owned_club_player(
            source_team_id=680, source_team_address="0x1000",
            target_team_id=679, target_team_address="0x2000",
            player_id=37058363, mode="loan",
        )
    assert raised.value.error_code == "capability_disabled"


def test_fm26_loan_accepts_nonzero_empty_other_contracts_container(monkeypatch):
    source_team, target_team = 0x1000, 0x2000
    source_club, target_club = 0x3000, 0x4000
    player, person, primary_contract = 0x5000, 0x6000, 0x7000
    roster_begin = 0x8000
    module = SimpleNamespace(base_address=0x10000000)
    layout = SimpleNamespace(
        key="fm26", club_vtable_rva=0x123400,
        module=lambda _process: module,
    )
    pointers = {
        source_team + player_movement.TEAM_ROSTER_BEGIN: roster_begin,
        source_team + player_movement.TEAM_ROSTER_END: roster_begin + 8,
        source_team + player_movement.TEAM_CLUB: source_club,
        roster_begin: player,
        player + player_movement.PLAYER_CURRENT_TEAM: source_team,
        player + player_movement.PLAYER_REGISTERED_TEAM: source_team,
        person + player_movement.PERSON_CONTRACT: primary_contract,
        person + player_movement.PERSON_CONTRACT_COLLECTION: 0x6800,
        0x6800: 0,
        primary_contract + 8: person,
        primary_contract + 0x10: source_team,
    }
    reader = SimpleNamespace(
        ptr=lambda address: pointers.get(address, 0),
        bytes=lambda _address, size: bytes(size),
        team=lambda address: {"id": 679, "team_type": "club"} if address == source_team else None,
    )
    offsets = {
        "player_move_contract_pool_rva": 1,
        "player_move_loan_contract_pool_rva": 12,
        "player_move_loan_contract_constructor_rva": 13,
        "player_move_contract_allocate_rva": 2,
        "player_move_contract_release_rva": 3,
        "player_move_contract_factory_global_rva": 4,
        "player_move_contract_factory_target_rva": 5,
        "player_move_main_contract_vtable_rva": 6,
        "loan_contract_vtable_rva": 7,
        "player_move_club_method_rva": 8,
        "player_move_prepare_loan_rva": 9,
        "player_move_terminate_contract_rva": 10,
        "game_date_rva": 11,
    }
    captured = {}

    def validate_entries(_reader, **kwargs):
        captured.update(kwargs)
        raise RuntimeError("entry validation reached")

    monkeypatch.setattr(player_movement, "_configure_thread_api", lambda: None)
    monkeypatch.setattr(
        player_movement, "select_process_layout", lambda: (26, "fm.exe", layout),
    )
    monkeypatch.setattr(player_movement, "player_movement_capabilities", lambda _layout: {
        "game_key": "fm26", "loan": {"enabled": True},
    })
    monkeypatch.setattr(player_movement, "_required_layout", lambda _layout: offsets)
    monkeypatch.setattr(
        player_movement, "open_process", lambda *_args, **_kwargs: nullcontext(object()),
    )
    monkeypatch.setattr(
        player_movement, "_writable_player_movement_reader",
        lambda *_args: reader,
    )
    monkeypatch.setattr(
        player_movement, "_resolve_player_movement_context",
        lambda *_args, **_kwargs: player_movement.PlayerMovementContext(
            source_team=source_team, target_team=target_team,
            source_club=source_club, target_club=target_club,
            player=player, person=person, source_rows=(),
            target_rows=(),
        ),
    )
    monkeypatch.setattr(player_movement, "_team_belongs_to_club", lambda *_args: True)
    monkeypatch.setattr(player_movement, "_fm26_player_move_entry_failures", validate_entries)

    with pytest.raises(RuntimeError, match="entry validation reached"):
        player_movement.move_owned_club_player(
            source_team_id=679, source_team_address="0x1000",
            target_team_id=915, target_team_address="0x2000",
            player_id=2002053720, mode="loan",
        )

    assert captured["expected_club_vtable"] == 0x10123400


def test_fm26_loan_keeps_registered_team_unchanged():
    source = (ROOT / "tools" / "player_movement.py").read_text(encoding="utf-8")

    assert "registration_team=contract_team" not in source
    assert "PLAYER_REGISTERED_TEAM) == registered_team" in source


def test_low_level_loan_dispatches_youth_team_as_transaction_source(monkeypatch):
    captured = {}
    layout = SimpleNamespace(key="fm24")
    monkeypatch.setattr(
        player_movement, "select_process_layout", lambda: (24, "fm.exe", layout),
    )
    monkeypatch.setattr(player_movement, "_configure_thread_api", lambda: None)
    monkeypatch.setattr(
        player_movement, "player_movement_capabilities",
        lambda *_args, **_kwargs: {
            "transfer": {"enabled": True}, "loan": {"enabled": True},
        },
    )

    def fake_loan(**kwargs):
        captured.update(kwargs)
        return {"verified": True}

    monkeypatch.setattr(
        player_movement, "_move_fm24_owned_club_player_loan", fake_loan,
    )

    result = player_movement.move_owned_club_player(
        source_team_id=679, source_team_address="0x1000",
        source_squad_team_id=2000779120,
        source_squad_team_address="0x1100",
        target_team_id=915, target_team_address="0x2000",
        player_id=2002053720, mode="loan",
    )

    assert result["verified"] is True
    assert captured["source_team_id"] == 679
    assert captured["source_squad_team_id"] == 2000779120
    assert captured["source_squad_team_address"] == "0x1100"


def test_fm26_disabled_loan_rejects_other_identity_before_layout_validation(monkeypatch):
    wrong_layout = SimpleNamespace(
        key="fm26", distribution="xgp", game_version="26.3.2",
        executable_sha256=FM26_LAYOUT.executable_sha256,
    )
    monkeypatch.setattr(
        player_movement, "select_process_layout",
        lambda: (26, "fm.exe", wrong_layout),
    )
    monkeypatch.setattr(player_movement, "_configure_thread_api", lambda: None)

    with pytest.raises(
        player_movement.PlayerMovementCapabilityError,
        match="新构造器路径实机仍会导致游戏闪退",
    ) as raised:
        player_movement.move_owned_club_player(
            source_team_id=680, source_team_address="0x1000",
            target_team_id=679, target_team_address="0x2000",
            player_id=37058363, mode="loan",
        )
    assert raised.value.error_code == "capability_disabled"


def test_fm26_layout_separates_full_and_loan_contract_pools():
    assert FM26_LAYOUT.player_move_contract_pool_rva == 0x4E373A0
    assert FM26_LAYOUT.player_move_loan_contract_pool_rva == 0x4E45C98
    assert FM26_LAYOUT.player_move_loan_contract_constructor_rva == 0x1AA7700
    assert FM26_XGP_TEMPLATE.player_move_loan_contract_constructor_rva is None
    assert player_movement.CONTRACT_SIZE == 0xC8
    assert player_movement.FM26_LOAN_CONTRACT_SIZE == 0xF8


def test_fm24_transfer_layout_accepts_exact_steam_and_epic_executables():
    assert FM24_LAYOUT.player_move_contract_pool_rva == 0x6378C80
    assert FM24_EPIC_LAYOUT.player_move_contract_pool_rva == 0x6378C80
    assert FM24_EPIC_LAYOUT.player_move_main_contract_vtable_rva == 0x5667C88
    supported = SimpleNamespace(
        key="fm24", distribution="steam",
        executable_sha256=FM24_TRANSFER_EXE_SHA256,
        player_move_main_contract_vtable_rva=1,
        player_move_contract_pool_rva=19,
        player_move_fm24_source_cleanup_rva=2,
        player_move_fm24_transfer_rva=3,
        player_move_fm24_contract_factory_rva=4,
        player_move_fm24_loan_constructor_rva=5,
        loan_contract_vtable_rva=6,
        game_date_rva=7,
        player_move_fm24_transaction_allocator_rva=8,
        player_move_fm24_transaction_constructor_rva=9,
        player_move_fm24_transaction_initializer_rva=10,
        player_move_fm24_transaction_prepare_rva=11,
        player_move_fm24_transaction_commit_rva=12,
        player_move_fm24_transaction_transition_rva=13,
        player_move_fm24_transaction_outer_submit_rva=14,
        player_move_fm24_transaction_entry_rva=15,
        player_move_fm24_transaction_vtable_rva=16,
        player_move_fm24_context_root_rva=17,
        player_move_fm24_context_vtable_rva=18,
    )
    assert _required_fm24_layout(supported) == {
        "player_move_contract_pool_rva": 19,
        "player_move_main_contract_vtable_rva": 1,
        "game_date_rva": 7,
    }

    unsupported = SimpleNamespace(**{
        **supported.__dict__, "executable_sha256": "00" * 32,
    })
    with pytest.raises(RuntimeError, match="FM24 Steam/Epic 24.4.2"):
        _required_fm24_layout(unsupported)

    assert _required_fm24_layout(FM24_EPIC_LAYOUT) == {
        "player_move_contract_pool_rva": 0x6378C80,
        "player_move_main_contract_vtable_rva": 0x5667C88,
        "game_date_rva": 0x631D5BC,
    }
    with pytest.raises(RuntimeError, match="原生高层球员事务仅支持"):
        player_movement._required_fm24_legacy_layout(FM24_EPIC_LAYOUT)


def test_fm24_transfer_stub_builds_native_high_level_transaction_chain():
    code = _build_fm24_transfer_stub(
        allocator=0x1000, constructor=0x2000, initializer=0x3000,
        transaction_prepare=0x4000, transaction_commit=0x5000,
        transaction_transition=0x5100, transaction_outer_submit=0x5200,
        transaction_vtable=0x6000,
        transaction_context=0x6000, person=0x7000,
        source_team=0x8000, target_team=0x9000,
        transfer_fee=12_500_000, wrapper_address=0xA000,
        result_address=0xB000,
    )

    assert code.startswith(b"\x48\x83\xec\x68")
    calls = [code.index(value.to_bytes(8, "little")) for value in (0x1000, 0x2000, 0x3000, 0x4000, 0x5000, 0x5100, 0x5200)]
    assert calls == sorted(calls)
    for value in (0x6000, 0x7000, 0x8000, 0x9000, 0xA000):
        assert value.to_bytes(8, "little") in code
    assert code.count(b"\xf0\xff\x47\x08") == 3
    assert b"\xc7\x87\x0c\x00\x00\x00\x01\x00\x00\x00" in code
    assert b"\xc7\x87\x7c\x00\x00\x00\x00\x00\x00\x00" in code
    assert b"\xc7\x87\x8c\x00\x00\x00\x00\x01\x00\x00" in code
    prepare_call = code.index((0x4000).to_bytes(8, "little"))
    assert b"\x48\x89\xf9" in code[prepare_call - 20:prepare_call]
    assert code.count(b"\xf0\xff\x4f\x08") == 3
    assert (
        b"\x48\xb8" + (0x8000).to_bytes(8, "little")
        + b"\x48\x89\x44\x24\x20"
    ) in code
    assert (12_500_000).to_bytes(4, "little") in code
    assert b"\x81\xbf\x50\x00\x00\x00" in code
    assert code.endswith(b"\xeb\xfe")


def test_fm24_transfer_uses_aligned_native_thread_stack():
    assert _fm24_native_stack_rsp(0x12345000) == 0x12344FF8
    assert _fm24_native_stack_rsp(0x12345008) == 0x12344FF8


def test_fm24_direct_transfer_uses_main_contract_factory_and_native_frame():
    scalars = bytes(range(0x40))
    code = _build_fm24_direct_transfer_stub(
        contract_factory=0x1000, transfer_main=0x2000,
        main_vtable=0x3000, target_club=0x4000, person=0x5000,
        source_team=0x6000, target_team=0x7000,
        game_date_address=0x8000, contract_scalars=scalars,
        start_code=0x07E71B05, expiry_code=0x07EA1B05,
        result_address=0x9000, context_address=0xA000,
        rtl_restore=0xB000,
    )

    assert code.startswith(b"\x48\x83\xec\x68")
    assert code.index((0x1000).to_bytes(8, "little")) < code.index((0x2000).to_bytes(8, "little"))
    assert b"\x48\xb8" + bytes(8) + b"\x48\x89\x44\x24\x20" in code
    assert b"\x48\x89\x7c\x24\x40" in code
    for value in (0x3000, 0x4000, 0x5000, 0x6000, 0x7000, 0x8000):
        assert value.to_bytes(8, "little") in code
    assert (0x07E71B05).to_bytes(4, "little") in code
    assert (0x07EA1B05).to_bytes(4, "little") in code
    assert b"\x48\x83\xc4\x68\x48\xb9" in code


def test_fm24_loan_stub_uses_native_factory_and_captured_club_frame():
    code = _build_fm24_loan_stub(
        contract_factory=0x1000, transfer_main=0x2000,
        target_club=0x3000, person=0x4000, target_team=0x5000,
        start_code=0x07E71B05, expiry_code=0x07E81B05,
        result_address=0x6000, context_address=0x7000,
        rtl_restore=0x8000,
    )

    assert code.startswith(b"\x48\x83\xec\x68")
    assert code.index((0x1000).to_bytes(8, "little")) < code.index((0x2000).to_bytes(8, "little"))
    assert b"\x48\x89\x7c\x24\x40" in code
    assert (0x07E71B05).to_bytes(4, "little") in code
    assert (0x07E81B05).to_bytes(4, "little") in code
    assert (0x48640000).to_bytes(4, "little") in code
    for value in (0x3000, 0x4000, 0x5000):
        assert value.to_bytes(8, "little") in code
    assert b"\x48\x83\xc4\x68\x48\xb9" in code


def test_fm24_loan_expiry_keeps_native_date_slot_bits():
    current_code = 0x07E71B05
    current = player_movement.decode_date(current_code)
    expiry_code = (
        player_movement._encode_date(current + player_movement.timedelta(days=365))
        | (current_code & 0xFE00)
    )

    assert expiry_code == 0x07E81B05


def test_fm24_fmrte_loan_templates_keep_separate_object_sizes():
    template = bytes([0xA5]) * player_movement.FM24_LOAN_CONTRACT_SIZE
    raw = _fm24_loan_contract_data(
        template, loan_vtable=0x1000, person=0x2000,
        target_team=0x3000, start_code=0x07E71B05,
        expiry_code=0x07E81B05,
    )

    assert len(raw) == 0xE8
    assert int.from_bytes(raw[0x00:0x08], "little") == 0x1000
    assert int.from_bytes(raw[0x08:0x10], "little") == 0x2000
    assert int.from_bytes(raw[0x10:0x18], "little") == 0x3000
    assert int.from_bytes(raw[0x18:0x1C], "little") == 0
    assert int.from_bytes(raw[0x3C:0x40], "little") == 0x07E71B05
    assert int.from_bytes(raw[0x40:0x44], "little") == 0x07E81B05
    assert int.from_bytes(raw[0x44:0x48], "little") == 0x07E71B05

    other = _fm24_other_contracts_data(0x4000)
    assert len(other) == 0x20
    assert int.from_bytes(other[0:8], "little") == 0x4000
    assert other[8:] == bytes(0x18)


def test_player_move_accepts_valid_stale_registration_team_from_another_club():
    registered_team = 0x2000
    registered_club = 0x3000

    class FakeReader:
        @staticmethod
        def team(address):
            if address == registered_team:
                return {"id": 677, "team_type": "club"}
            return None

        @staticmethod
        def ptr(address):
            if address == registered_team + player_movement.TEAM_CLUB:
                return registered_club
            return 0

    assert player_movement._valid_registered_club_team(
        FakeReader(), registered_team,
    ) is True


def test_player_move_rejects_invalid_registration_team_pointer():
    class FakeReader:
        @staticmethod
        def team(_address):
            return None

        @staticmethod
        def ptr(_address):
            return 0

    assert player_movement._valid_registered_club_team(
        FakeReader(), 0x2000,
    ) is False


def test_fm26_transfer_preflight_accepts_valid_stale_registration_team(monkeypatch):
    source_team, target_team = 0x1000, 0x2000
    source_club, target_club = 0x3000, 0x4000
    stale_registered_team, stale_registered_club = 0x4500, 0x4600
    player, person, primary_contract = 0x5000, 0x6000, 0x7000
    module = SimpleNamespace(base_address=0x10000000)
    main_contract_vtable_rva = 0x1234
    layout = SimpleNamespace(module=lambda _process: module)
    pointers = {
        person + player_movement.PERSON_CONTRACT: primary_contract,
        person + player_movement.PERSON_CONTRACT_COLLECTION: 0,
        player + player_movement.PLAYER_CURRENT_TEAM: source_team,
        player + player_movement.PLAYER_REGISTERED_TEAM: stale_registered_team,
        primary_contract: module.base_address + main_contract_vtable_rva,
        primary_contract + 0x08: person,
        primary_contract + 0x10: source_team,
        stale_registered_team + player_movement.TEAM_CLUB: stale_registered_club,
    }

    class FakeReader:
        @staticmethod
        def ptr(address):
            return pointers.get(address, 0)

        @staticmethod
        def team(address):
            if address == stale_registered_team:
                return {"id": 677, "team_type": "club"}
            return None

    monkeypatch.setattr(
        player_movement, "_required_fm26_fmrte_layout",
        lambda _layout: {
            "player_move_main_contract_vtable_rva": main_contract_vtable_rva,
        },
    )
    monkeypatch.setattr(
        player_movement, "open_process",
        lambda *_args, **_kwargs: nullcontext(object()),
    )
    monkeypatch.setattr(
        player_movement, "_writable_player_movement_reader",
        lambda *_args: FakeReader(),
    )
    monkeypatch.setattr(
        player_movement, "_resolve_player_movement_context",
        lambda *_args, **_kwargs: player_movement.PlayerMovementContext(
            source_team=source_team, target_team=target_team,
            source_club=source_club, target_club=target_club,
            player=player, person=person, source_rows=(), target_rows=(),
        ),
    )
    monkeypatch.setattr(player_movement, "_team_belongs_to_club", lambda *_args: True)
    monkeypatch.setattr(
        player_movement, "has_confirmed_future_transfer",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("future check reached")),
    )

    with pytest.raises(RuntimeError, match="future check reached"):
        player_movement._move_fm26_owned_club_player_fmrte(
            pid=26, layout=layout,
            source_team_id=679, source_team_address=hex(source_team),
            target_team_id=915, target_team_address=hex(target_team),
            player_id=2002053720, mode="transfer",
        )


def test_move_dispatches_fm24_transfer(monkeypatch):
    layout = SimpleNamespace(key="fm24")
    monkeypatch.setattr(player_movement, "FM24_TRANSFER_RUNTIME_ENABLED", True)
    monkeypatch.setattr(player_movement, "FM24_LOAN_RUNTIME_ENABLED", False)
    monkeypatch.setattr(player_movement, "_configure_thread_api", lambda: None)
    monkeypatch.setattr(
        player_movement, "select_process_layout", lambda: (24, "fm.exe", layout),
    )
    monkeypatch.setattr(
        player_movement, "player_movement_capabilities", lambda _layout: {
            "transfer": {
                "enabled": True,
                "implementation": "fm24_main_contract_pool",
                "reason": "",
            },
            "loan": {
                "enabled": False,
                "implementation": "fm24_loan_contract_pool",
                "reason": "FM24 租借尚未开放",
            },
        },
    )
    calls = []
    monkeypatch.setattr(
        player_movement, "_move_fm24_owned_club_player",
        lambda **kwargs: calls.append(kwargs) or {"verified": True},
    )

    result = player_movement.move_owned_club_player(
        source_team_id=602, source_team_address="0x1000",
        target_team_id=679, target_team_address="0x2000",
        player_id=55012984, mode="transfer",
    )
    assert result["verified"] is True
    assert result["capability"]["implementation"] == "fm24_main_contract_pool"
    assert calls == [{
        "pid": 24, "layout": layout,
        "source_team_id": 602, "source_team_address": "0x1000",
        "target_team_id": 679, "target_team_address": "0x2000",
        "player_id": 55012984, "mode": "transfer",
    }]

    with pytest.raises(RuntimeError, match="FM24 租借尚未开放"):
        player_movement.move_owned_club_player(
            source_team_id=602, source_team_address="0x1000",
            target_team_id=679, target_team_address="0x2000",
            player_id=55012984, mode="loan",
        )


def test_move_dispatches_fm24_loan_to_separate_fmrte_pool_transaction(monkeypatch):
    layout = SimpleNamespace(key="fm24")
    monkeypatch.setattr(player_movement, "FM24_LOAN_RUNTIME_ENABLED", True)
    monkeypatch.setattr(player_movement, "_configure_thread_api", lambda: None)
    monkeypatch.setattr(
        player_movement, "select_process_layout", lambda: (24, "fm.exe", layout),
    )
    monkeypatch.setattr(
        player_movement, "player_movement_capabilities", lambda _layout: {
            "transfer": {
                "enabled": False,
                "implementation": "fm24_main_contract_pool",
                "reason": "FM24 转会尚未开放",
            },
            "loan": {
                "enabled": True,
                "implementation": "fm24_loan_contract_pool",
                "reason": "",
            },
        },
    )
    calls = []
    monkeypatch.setattr(
        player_movement, "_move_fm24_owned_club_player_loan",
        lambda **kwargs: calls.append(kwargs) or {
            "verified": True, "compatibility_mode": "fm24_verified_loan_pool",
        },
    )
    monkeypatch.setattr(
        player_movement, "_move_fm24_owned_club_player",
        lambda **_kwargs: pytest.fail("loan must not use the permanent-move path"),
    )

    result = player_movement.move_owned_club_player(
        source_team_id=679, source_team_address="0x1000",
        target_team_id=1736, target_team_address="0x2000",
        player_id=5109668, mode="loan",
    )

    assert result["compatibility_mode"] == "fm24_verified_loan_pool"
    assert result["capability"]["implementation"] == "fm24_loan_contract_pool"
    assert calls == [{
        "pid": 24, "layout": layout,
        "source_team_id": 679, "source_team_address": "0x1000",
        "target_team_id": 1736, "target_team_address": "0x2000",
        "player_id": 5109668,
    }]


def test_player_release_dispatches_to_verified_generations(monkeypatch):
    layout = SimpleNamespace(key="fm24")
    monkeypatch.setattr(player_movement, "_configure_thread_api", lambda: None)
    monkeypatch.setattr(
        player_movement, "select_process_layout", lambda: (24, "fm.exe", layout),
    )
    calls = []
    monkeypatch.setattr(
        player_movement, "_release_fm24_owned_club_player",
        lambda **kwargs: calls.append(kwargs) or {
            "verified": True, "mode": "release",
        },
    )

    result = player_movement.release_owned_club_player(
        source_team_id=602, source_team_address="0x1000", player_id=55012984,
    )

    assert result == {"verified": True, "mode": "release"}
    assert calls == [{
        "pid": 24, "layout": layout,
        "source_team_id": 602, "source_team_address": "0x1000",
        "player_id": 55012984,
    }]

    fm26_layout = SimpleNamespace(key="fm26")
    fm26_calls = []
    monkeypatch.setattr(
        player_movement, "_release_fm26_owned_club_player",
        lambda **kwargs: fm26_calls.append(kwargs) or {
            "verified": True, "mode": "release",
        },
    )
    monkeypatch.setattr(
        player_movement, "select_process_layout",
        lambda: (26, "fm.exe", fm26_layout),
    )
    result = player_movement.release_owned_club_player(
        source_team_id=654, source_team_address="0x2000", player_id=18074558,
    )
    assert result["verified"] is True
    assert fm26_calls == [{
        "pid": 26, "layout": fm26_layout,
        "source_team_id": 654, "source_team_address": "0x2000",
        "player_id": 18074558,
    }]

    monkeypatch.setattr(
        player_movement, "select_process_layout",
        lambda: (27, "fm.exe", SimpleNamespace(key="fm27")),
    )
    with pytest.raises(RuntimeError, match="FM24 Steam 24.4.2 与 FM26 Steam 26.3.2"):
        player_movement.release_owned_club_player(
            source_team_id=654, source_team_address="0x2000", player_id=18074558,
        )


def test_staff_release_dispatches_to_verified_generations(monkeypatch):
    layout = SimpleNamespace(key="fm24")
    monkeypatch.setattr(player_movement, "_configure_thread_api", lambda: None)
    monkeypatch.setattr(
        player_movement, "select_process_layout", lambda: (24, "fm.exe", layout),
    )
    calls = []
    monkeypatch.setattr(
        player_movement, "_release_fm24_owned_club_staff",
        lambda **kwargs: calls.append(kwargs) or {
            "verified": True, "mode": "staff_release",
        },
    )

    result = player_movement.release_owned_club_staff(
        source_team_id=602, source_team_address="0x1000", staff_id=828925,
    )

    assert result == {"verified": True, "mode": "staff_release"}
    assert calls == [{
        "pid": 24, "layout": layout,
        "source_team_id": 602, "source_team_address": "0x1000",
        "staff_id": 828925,
    }]

    fm26_layout = SimpleNamespace(key="fm26")
    fm26_calls = []
    monkeypatch.setattr(
        player_movement, "_release_fm26_owned_club_staff",
        lambda **kwargs: fm26_calls.append(kwargs) or {
            "verified": True, "mode": "staff_release",
        },
    )
    monkeypatch.setattr(
        player_movement, "select_process_layout",
        lambda: (26, "fm.exe", fm26_layout),
    )
    result = player_movement.release_owned_club_staff(
        source_team_id=654, source_team_address="0x2000", staff_id=2000018080,
    )
    assert result["verified"] is True
    assert fm26_calls == [{
        "pid": 26, "layout": fm26_layout,
        "source_team_id": 654, "source_team_address": "0x2000",
        "staff_id": 2000018080,
    }]

    monkeypatch.setattr(
        player_movement, "select_process_layout",
        lambda: (27, "fm.exe", SimpleNamespace(key="fm27")),
    )
    with pytest.raises(RuntimeError, match="FM24 Steam 24.4.2 与 FM26 Steam 26.3.2"):
        player_movement.release_owned_club_staff(
            source_team_id=654, source_team_address="0x2000", staff_id=2000018080,
        )


def test_staff_release_candidate_uses_manager_slot_when_display_scan_omits_coach(monkeypatch):
    team, person, contract = 0x7000, 0x3100, 0x5000
    manager_base = person - 0xF8
    layout = SimpleNamespace(
        key="fm26", team_manager_person_offset=0xF8,
        staff_complete_object_offset=0x100, staff_job_type_offset=0x26,
    )

    class Reader:
        @staticmethod
        def ptr(address):
            return {
                team + player_movement.TEAM_MANAGER: manager_base,
                contract + 0x08: person,
                contract + 0x10: team,
                person + player_movement.PERSON_CONTRACT: contract,
            }.get(address)

        @staticmethod
        def u32(address):
            return 77 if address == person + player_movement.ENTITY_UID else 0

        @staticmethod
        def u8(address):
            return 16 if address == contract + layout.staff_job_type_offset else None

    Reader.layout = layout
    monkeypatch.setattr(player_movement, "_staff_scan_entries", lambda *_args: [])
    monkeypatch.setattr(player_movement, "_name", lambda *_args: "主教练")

    result = player_movement._staff_release_candidate(
        Reader(), team, 77, person_contract_offset=player_movement.PERSON_CONTRACT,
    )

    assert result == {
        "id": 77, "name": "主教练", "role": "主教练", "job_type": 16,
        "address": hex(person), "contract_address": hex(contract),
    }


def test_fm26_staff_release_layout_requires_exact_executable():
    supported = SimpleNamespace(
        key="fm26", distribution="steam", game_version="26.3.2",
        executable_sha256=player_movement.FM26_STAFF_RELEASE_EXE_SHA256,
        player_move_contract_pool_rva=1,
        player_move_main_contract_vtable_rva=2,
    )
    assert player_movement._required_staff_release_layout(supported) == {
        "player_move_contract_pool_rva": 1,
        "player_move_main_contract_vtable_rva": 2,
        "person_contract_offset": player_movement.PERSON_CONTRACT,
        "person_contract_collection_offset": player_movement.PERSON_CONTRACT_COLLECTION,
        "contract_size": player_movement.CONTRACT_SIZE,
    }

    unsupported = SimpleNamespace(
        **{**supported.__dict__, "executable_sha256": "00" * 32},
    )
    with pytest.raises(RuntimeError, match="FM26 Steam 26.3.2"):
        player_movement._required_staff_release_layout(unsupported)


def test_fm24_staff_release_layout_is_independent_of_player_move_date_gate():
    layout = SimpleNamespace(
        key="fm24", distribution="steam", game_date_rva=None,
        executable_sha256="00" * 32,
        player_move_contract_pool_rva=0x1000,
        player_move_main_contract_vtable_rva=0x2000,
    )
    assert player_movement._required_staff_release_layout(layout) == {
        "player_move_contract_pool_rva": 0x1000,
        "player_move_main_contract_vtable_rva": 0x2000,
        "person_contract_offset": player_movement.FM24_PERSON_CONTRACT,
        "person_contract_collection_offset": player_movement.FM24_PERSON_CONTRACT_COLLECTION,
        "contract_size": player_movement.FM24_CONTRACT_SIZE,
    }

    assert player_movement._required_staff_release_layout(FM24_EPIC_LAYOUT) == {
        "player_move_contract_pool_rva": 0x6378C80,
        "player_move_main_contract_vtable_rva": 0x5667C88,
        "person_contract_offset": player_movement.FM24_PERSON_CONTRACT,
        "person_contract_collection_offset": player_movement.FM24_PERSON_CONTRACT_COLLECTION,
        "contract_size": player_movement.FM24_CONTRACT_SIZE,
    }


def test_fm24_transfer_bridge_can_still_be_disabled(monkeypatch):
    monkeypatch.setattr(player_movement, "_configure_thread_api", lambda: None)
    monkeypatch.setattr(
        player_movement, "select_process_layout",
        lambda: (24, "fm.exe", SimpleNamespace(key="fm24")),
    )
    monkeypatch.setattr(player_movement, "FM24_TRANSFER_RUNTIME_ENABLED", False)

    with pytest.raises(RuntimeError, match="兼容转会桥梁已停用"):
        player_movement.move_owned_club_player(
            source_team_id=602, source_team_address="0x1000",
            target_team_id=679, target_team_address="0x2000",
            player_id=55012984, mode="transfer",
        )


def test_fm24_roster_removal_requires_one_exact_player_pointer():
    assert _remove_unique_pointer([0x10, 0x20, 0x30], 0x20) == [0x10, 0x30]
    with pytest.raises(RuntimeError, match="不是唯一值"):
        _remove_unique_pointer([0x10, 0x20, 0x20], 0x20)
    with pytest.raises(RuntimeError, match="不是唯一值"):
        _remove_unique_pointer([0x10, 0x30], 0x20)


def test_generated_stub_restores_stack_before_rtl_restore():
    code = _build_stub(
        mode="loan", pool=0x1000, allocate=0x2000, release=0x3000,
        factory_object=0x4000, factory_target=0x5000,
        main_vtable=0x6000, loan_vtable=0x7000, loan_constructor=0x7800,
        terminate_contract=0x8000, prepare_loan=0x8800, club_method=0x9000,
        source_club=0xA000, target_club=0xB000,
        contract_owner_interface=0xC000, person=0xD000, player=0xD800,
        source_team=0xE000, target_team=0xF000,
        source_roster_slot=0x14008, source_roster_end=0x14020,
        game_date_address=0x10000, start_code=0x07EA0010,
        expiry_code=0x07EB0010, source_wage=1000,
        result_address=0x11000, context_address=0x12000,
        rtl_restore=0x13000,
    )

    assert code.startswith(b"\x48\x83\xec\x68")
    assert b"\x48\x83\xc4\x68\x48\xb9" in code
    assert (0x7000).to_bytes(8, "little") in code
    assert (0x9000).to_bytes(8, "little") in code
    assert (0xC000).to_bytes(8, "little") in code
    assert code.index((0x8800).to_bytes(8, "little")) < code.index((0x9000).to_bytes(8, "little"))
    assert (0x07EA0010).to_bytes(4, "little") in code
    assert (0x07EB0010).to_bytes(4, "little") not in code
    assert (0xE040).to_bytes(8, "little") not in code
    assert (0x14008).to_bytes(8, "little") not in code


def test_loan_stub_uses_native_constructor_and_keeps_registered_team():
    common = dict(
        pool=0x1000, allocate=0x2000, release=0x3000,
        factory_object=0x4000, factory_target=0x5000,
        main_vtable=0x6000, loan_vtable=0x7000, loan_constructor=0x7800,
        terminate_contract=0x8000, prepare_loan=0x8800, club_method=0x9000,
        source_club=0xA000, target_club=0xB000,
        contract_owner_interface=0xC000, person=0xD000, player=0xD800,
        source_team=0xE000, target_team=0xF000,
        source_roster_slot=0x14008, source_roster_end=0x14020,
        game_date_address=0x10000, start_code=0x07EA0010,
        expiry_code=0x07EB0010, source_wage=1000,
        result_address=0x11000, context_address=0x12000,
        rtl_restore=0x13000,
    )

    loan_code = _build_stub(mode="loan", **common)
    transfer_code = _build_stub(mode="transfer", **common)
    registered_team_write = (
        b"\x48\xb8" + (0xE100).to_bytes(8, "little")
        + b"\x48\xa3" + (0xD938).to_bytes(8, "little")
    )

    assert registered_team_write not in loan_code
    assert registered_team_write not in transfer_code
    assert b"\xba\xf8\x00\x00\x00" in loan_code
    assert b"\xba\xc8\x00\x00\x00" in transfer_code
    constructor_call = loan_code.index((0x7800).to_bytes(8, "little"))
    factory_call = loan_code.index((0x5000).to_bytes(8, "little"))
    assert constructor_call < factory_call
    assert b"\x48\x89\xf9" in loan_code[:factory_call]
    prepare_call = (
        b"\x48\xb9" + (0xD000).to_bytes(8, "little")
        + b"\x48\xba" + (0xE000).to_bytes(8, "little")
        + b"\x49\xb8" + (0xFF).to_bytes(8, "little")
        + b"\x48\xb8" + (0x8800).to_bytes(8, "little")
        + b"\xff\xd0"
    )
    assert prepare_call in loan_code
    assert (0x07EB0010).to_bytes(4, "little") not in loan_code
    assert (0x00030003).to_bytes(4, "little") not in loan_code
    assert (0x48640000).to_bytes(4, "little") not in loan_code


def test_loan_stub_matches_captured_factory_and_club_call_frames():
    code = _build_stub(
        mode="loan", pool=0x1000, allocate=0x2000, release=0x3000,
        factory_object=0x4000, factory_target=0x5000,
        main_vtable=0x6000, loan_vtable=0x7000, loan_constructor=0x7800,
        terminate_contract=0x8000, prepare_loan=0x8800, club_method=0x9000,
        source_club=0xA000, target_club=0xB000,
        contract_owner_interface=0xC000, person=0xD000, player=0xD800,
        source_team=0xE000, target_team=0xF000,
        source_roster_slot=0x14008, source_roster_end=0x14020,
        game_date_address=0x10000, start_code=0x07EA0010,
        expiry_code=0, source_wage=1000,
        result_address=0x11000, context_address=0x12000,
        rtl_restore=0x13000,
    )

    def store_stack(offset, value):
        return (
            b"\x48\xb8" + int(value).to_bytes(8, "little")
            + b"\x48\x89\x44\x24" + bytes([offset])
        )

    factory_frame = b"".join(
        store_stack(offset, value)
        for offset, value in (
            (0x20, 1), (0x28, 0), (0x30, 0), (0x38, 0xFFFFFFFF),
            (0x40, 0), (0x48, 0), (0x50, 1), (0x58, 0),
        )
    )
    factory_call = (
        b"\x48\xb9" + (0x4000).to_bytes(8, "little")
        + b"\x48\x89\xfa"
        + b"\x49\xb8" + (0xC000).to_bytes(8, "little")
        + b"\x49\xb9" + (0xB000).to_bytes(8, "little")
        + b"\x48\xb8" + (0x5000).to_bytes(8, "little")
        + b"\xff\xd0"
    )
    club_frame = (
        store_stack(0x20, 0) + store_stack(0x28, 0)
        + store_stack(0x30, 0xF000) + store_stack(0x38, 0)
        + b"\x48\x89\x7c\x24\x40"
        + store_stack(0x48, 0) + store_stack(0x50, 0)
        + store_stack(0x58, 1)
    )
    club_call = (
        b"\x48\xb9" + (0xB000).to_bytes(8, "little")
        + b"\x48\xba" + (0xD000).to_bytes(8, "little")
        + b"\x41\xb8\x01\x00\x00\x00"
        + b"\x49\xb9" + (0xF000).to_bytes(8, "little")
        + b"\x48\xb8" + (0x9000).to_bytes(8, "little")
        + b"\xff\xd0"
    )

    assert factory_frame + factory_call in code
    assert club_frame + club_call in code
    assert code.index(factory_frame) < code.index(club_frame)


def test_transfer_stub_terminates_old_contract_before_club_transaction():
    code = _build_stub(
        mode="transfer", pool=0x1000, allocate=0x2000, release=0x3000,
        factory_object=0x4000, factory_target=0x5000,
        main_vtable=0x6000, loan_vtable=0x7000, loan_constructor=0x7800,
        terminate_contract=0x8000, prepare_loan=0x8800, club_method=0x9000,
        source_club=0xA000, target_club=0xB000,
        contract_owner_interface=0xC000, person=0xD000, player=0xD800,
        source_team=0xE000, target_team=0xF000,
        source_roster_slot=0x14008, source_roster_end=0x14020,
        game_date_address=0x10000, start_code=0x07EA0010,
        expiry_code=0x07EB0010, source_wage=1000,
        result_address=0x11000, context_address=0x12000,
        rtl_restore=0x13000,
    )

    terminate_call = code.index((0x8000).to_bytes(8, "little"))
    club_call = code.index((0x9000).to_bytes(8, "little"))
    assert terminate_call < club_call
    assert (0xA000).to_bytes(8, "little") in code
    assert (0x10000).to_bytes(8, "little") in code


def test_transfer_stub_cleans_source_roster_after_club_transaction():
    code = _build_stub(
        mode="transfer", pool=0x1000, allocate=0x2000, release=0x3000,
        factory_object=0x4000, factory_target=0x5000,
        main_vtable=0x6000, loan_vtable=0x7000, loan_constructor=0x7800,
        terminate_contract=0x8000, prepare_loan=0x8800, club_method=0x9000,
        source_club=0xA000, target_club=0xB000,
        contract_owner_interface=0xC000, person=0xD000, player=0xD800,
        source_team=0xE000, target_team=0xF000,
        source_roster_slot=0x14008, source_roster_end=0x14020,
        game_date_address=0x10000, start_code=0x07EA0010,
        expiry_code=0x07EB0010, source_wage=1000,
        result_address=0x11000, context_address=0x12000,
        rtl_restore=0x13000,
    )

    club_call = code.index((0x9000).to_bytes(8, "little"))
    end_pointer_check = code.index((0xE040).to_bytes(8, "little"))
    first_slot = code.index((0x14008).to_bytes(8, "little"))
    assert club_call < end_pointer_check < first_slot
    assert (0x14010).to_bytes(8, "little") in code
    assert (0x14018).to_bytes(8, "little") in code
    assert (0xD800).to_bytes(8, "little") in code


def test_owned_player_move_requires_two_acquired_clubs(monkeypatch):
    monkeypatch.setattr(fm_odds_web, "OWNED_PLAYER_MOVE_RUNTIME_ENABLED", True)
    state = LocalOddsState.__new__(LocalOddsState)
    state.memory_lock = threading.RLock()
    clubs = {
        10: ("scope-a", {"save_instance_id": "save-a"}, {"id": 10, "name": "来源", "address": "0x1000"}),
        20: ("scope-a", {"save_instance_id": "save-a"}, {"id": 20, "name": "目标", "address": "0x2000"}),
    }
    state._owned_world_club_target = lambda team_id: clubs[int(team_id)]
    state._rebind_player_movement_targets = lambda team_ids: [
        {"id": int(team_id), "address": f"0x{int(team_id) + 0x3000:x}"}
        for team_id in team_ids
    ]
    calls = []
    monkeypatch.setattr(
        fm_odds_web, "move_owned_club_player",
        lambda **kwargs: calls.append(kwargs) or {"verified": True},
    )

    result = state.move_owned_world_club_player({
        "source_team_id": 10, "target_team_id": 20,
        "player_id": 30, "mode": "transfer",
    })

    assert result["verified"] is True
    assert result["source_team_name"] == "来源"
    assert result["target_team_name"] == "目标"
    assert result["auto_refreshed"] is False
    assert result["affected_team_ids"] == [10, 20]
    assert [row["id"] for row in result["refreshed_targets"]] == [10, 20]
    assert calls == [{
        "source_team_id": 10, "source_team_address": "0x1000",
        "target_team_id": 20, "target_team_address": "0x2000",
        "player_id": 30, "mode": "transfer",
    }]


def test_owned_player_move_forwards_actual_youth_squad(monkeypatch):
    monkeypatch.setattr(fm_odds_web, "OWNED_PLAYER_MOVE_RUNTIME_ENABLED", True)
    state = LocalOddsState.__new__(LocalOddsState)
    state.memory_lock = threading.RLock()
    clubs = {
        679: ("scope-a", {"save_instance_id": "save-a"}, {"id": 679, "name": "曼城", "address": "0x1000"}),
        602: ("scope-a", {"save_instance_id": "save-a"}, {"id": 602, "name": "阿森纳", "address": "0x2000"}),
    }
    state._owned_world_club_target = lambda team_id: clubs[int(team_id)]
    state._rebind_player_movement_targets = lambda _team_ids: []
    calls = []
    monkeypatch.setattr(
        fm_odds_web, "move_owned_club_player",
        lambda **kwargs: calls.append(kwargs) or {"verified": True},
    )

    state.move_owned_world_club_player({
        "source_team_id": 679,
        "source_squad_team_id": 2000339955,
        "source_squad_team_address": "0x3000",
        "target_team_id": 602,
        "player_id": 28126241,
        "mode": "transfer",
    })

    assert calls[0]["source_team_id"] == 679
    assert calls[0]["source_squad_team_id"] == 2000339955
    assert calls[0]["source_squad_team_address"] == "0x3000"


def test_owned_youth_loan_is_forwarded_to_low_level_gate_without_reassignment(monkeypatch):
    monkeypatch.setattr(fm_odds_web, "OWNED_PLAYER_MOVE_RUNTIME_ENABLED", True)
    state = LocalOddsState.__new__(LocalOddsState)
    state.memory_lock = threading.RLock()
    clubs = {
        679: ("scope-a", {"save_instance_id": "save-a"}, {
            "id": 679, "name": "曼城", "address": "0x1000",
        }),
        602: ("scope-a", {"save_instance_id": "save-a"}, {
            "id": 602, "name": "阿森纳", "address": "0x2000",
        }),
    }
    state._owned_world_club_target = lambda team_id: clubs[int(team_id)]
    state._rebind_player_movement_targets = lambda _team_ids: []
    movements = []
    monkeypatch.setattr(
        fm_odds_web, "reassign_owned_club_player_squad",
        lambda **_kwargs: pytest.fail("青年队租借不得先修改阵容"),
    )
    monkeypatch.setattr(
        fm_odds_web, "move_owned_club_player",
        lambda **kwargs: movements.append(kwargs) or {"verified": True},
    )

    result = state.move_owned_world_club_player({
        "source_team_id": 679,
        "source_squad_team_id": 2000339955,
        "source_squad_team_address": "0x3000",
        "target_team_id": 602,
        "player_id": 28126241,
        "mode": "loan",
    })

    assert movements == [{
        "source_team_id": 679, "source_team_address": "0x1000",
        "source_squad_team_id": 2000339955,
        "source_squad_team_address": "0x3000",
        "target_team_id": 602, "target_team_address": "0x2000",
        "player_id": 28126241, "mode": "loan",
    }]
    assert result["verified"] is True


def test_owned_player_squad_reassignment_requires_acquired_club(monkeypatch):
    state = LocalOddsState.__new__(LocalOddsState)
    state.memory_lock = threading.RLock()
    state._owned_world_club_target = lambda team_id: (
        "scope-a", {"save_instance_id": "save-a"},
        {"id": int(team_id), "name": "曼城", "address": "0x1000"},
    )
    state._rebind_player_movement_targets = lambda team_ids: [
        {"id": int(team_id), "address": "0x1100"} for team_id in team_ids
    ]
    calls = []
    monkeypatch.setattr(
        fm_odds_web, "reassign_owned_club_player_squad",
        lambda **kwargs: calls.append(kwargs) or {"verified": True},
    )

    result = state.reassign_owned_world_club_player_squad({
        "team_id": 679, "player_id": 30,
        "source_squad_team_id": 679,
        "source_squad_team_address": "0x1000",
        "target_squad_team_id": 2001,
        "target_squad_team_address": "0x2000",
    })

    assert result["verified"] is True
    assert result["club_name"] == "曼城"
    assert result["affected_team_ids"] == [679]
    assert result["refreshed_targets"] == [{"id": 679, "address": "0x1100"}]
    assert calls == [{
        "club_team_id": 679, "club_team_address": "0x1000",
        "source_squad_team_id": 679,
        "source_squad_team_address": "0x1000",
        "target_squad_team_id": 2001,
        "target_squad_team_address": "0x2000",
        "player_id": 30,
    }]


def test_owned_player_move_rebinds_and_retries_stale_context(monkeypatch):
    monkeypatch.setattr(fm_odds_web, "OWNED_PLAYER_MOVE_RUNTIME_ENABLED", True)
    state = LocalOddsState.__new__(LocalOddsState)
    state.memory_lock = threading.RLock()
    calls = []

    def target(team_id, *, quick_rebind=False):
        address = f"0x{int(team_id) + (0x2000 if quick_rebind else 0x1000):x}"
        return "scope-a", {"save_instance_id": "save-a"}, {
            "id": int(team_id), "name": str(team_id), "address": address,
        }

    state._owned_world_club_target = target

    def move(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise player_movement.PlayerMovementContextChangedError("球队地址已变化")
        return {"verified": True}

    monkeypatch.setattr(fm_odds_web, "move_owned_club_player", move)
    result = state.move_owned_world_club_player({
        "source_team_id": 10, "target_team_id": 20,
        "player_id": 30, "mode": "transfer",
    })

    assert result["verified"] is True
    assert result["auto_refreshed"] is True
    assert calls[0]["source_team_address"] == "0x100a"
    assert calls[1]["source_team_address"] == "0x200a"
    assert calls[1]["target_team_address"] == "0x2014"


def test_owned_player_move_does_not_retry_live_state_mismatch(monkeypatch):
    monkeypatch.setattr(fm_odds_web, "OWNED_PLAYER_MOVE_RUNTIME_ENABLED", True)
    state = LocalOddsState.__new__(LocalOddsState)
    state.memory_lock = threading.RLock()
    state._owned_world_club_target = lambda team_id, **_kwargs: (
        "scope-a", {"save_instance_id": "save-a"},
        {"id": int(team_id), "name": str(team_id), "address": hex(int(team_id))},
    )
    calls = []

    def move(**kwargs):
        calls.append(kwargs)
        raise player_movement.PlayerMovementContextChangedError(
            "球员状态校验失败：注册球队",
            retryable=False, error_code="player_state_changed",
            error_phase="preflight",
        )

    monkeypatch.setattr(fm_odds_web, "move_owned_club_player", move)
    with pytest.raises(player_movement.PlayerMovementContextChangedError):
        state.move_owned_world_club_player({
            "source_team_id": 10, "target_team_id": 20,
            "player_id": 30, "mode": "transfer",
        })

    assert len(calls) == 1


def test_owned_club_target_batch_resolves_both_uids_once(monkeypatch):
    state = LocalOddsState.__new__(LocalOddsState)
    state.lock = threading.RLock()
    state.output = {"save_instance_id": "save-a"}
    state._bind_current_save = lambda: "scope-a"
    state._world_club_native_cache = lambda _save_id: {"clubs": []}
    address_calls = []

    monkeypatch.setattr(
        fm_odds_web, "account_acquired_clubs",
        lambda _scope, _output, include_suspended=False: [
            {"id": 10, "name": "来源"}, {"id": 20, "name": "目标"},
        ],
    )
    monkeypatch.setattr(fm_odds_web, "world_club_team_address_hints", lambda _output: set())
    monkeypatch.setattr(
        fm_odds_web, "resolve_native_team_addresses",
        lambda ids, _hints, **_kwargs: address_calls.append(set(ids)) or {
            10: "0x1000", 20: "0x2000",
        },
    )

    resolved = state._owned_world_club_targets((10, 20))

    assert address_calls == [{10, 20}]
    assert resolved[10][2]["address"] == "0x1000"
    assert resolved[20][2]["address"] == "0x2000"


def test_owned_player_move_records_portable_failure_diagnostics(monkeypatch):
    state = LocalOddsState.__new__(LocalOddsState)
    monkeypatch.setattr(
        state, "_execute_owned_world_club_player",
        lambda _payload: (_ for _ in ()).throw(
            OSError(5, "OpenThread failed"),
        ),
    )

    with pytest.raises(OSError, match="OpenThread failed"):
        state.move_owned_world_club_player({"mode": "loan"})

    assert state.last_operation_error_type == "OSError"
    assert state.last_operation_error_stage == "player_loan"
    assert state.last_operation_error_code == "native_access"
    assert state.last_operation_error_phase == "native_access"
    assert state.last_operation_retryable is True
    assert "OpenThread failed" in state.last_operation_error
    assert "move_owned_world_club_player" in state.last_operation_traceback
    assert "OpenThread failed" in state.last_operation_traceback


def test_owned_player_move_reuses_completed_submission(monkeypatch):
    monkeypatch.setattr(fm_odds_web, "OWNED_PLAYER_MOVE_RUNTIME_ENABLED", True)
    state = LocalOddsState.__new__(LocalOddsState)
    state.memory_lock = threading.RLock()
    state._owned_world_club_target = lambda team_id: (
        "scope-a", {"save_instance_id": "save-a"},
        {"id": int(team_id), "name": str(team_id), "address": hex(int(team_id))},
    )
    calls = []
    monkeypatch.setattr(
        fm_odds_web, "move_owned_club_player",
        lambda **kwargs: calls.append(kwargs) or {"verified": True},
    )
    payload = {
        "source_team_id": 10, "target_team_id": 20,
        "player_id": 30, "mode": "loan",
        "client_submission_id": "same-click",
    }

    first = state.move_owned_world_club_player(payload)
    first["verified"] = False
    second = state.move_owned_world_club_player(payload)

    assert second["verified"] is True
    assert len(calls) == 1


def test_owned_player_move_rejects_duplicate_in_flight_submission(monkeypatch):
    monkeypatch.setattr(fm_odds_web, "OWNED_PLAYER_MOVE_RUNTIME_ENABLED", True)
    state = LocalOddsState.__new__(LocalOddsState)
    state._player_move_submission_lock = threading.RLock()
    state._player_move_submissions_in_flight = {"double-click"}
    state._player_move_submission_results = {}

    with pytest.raises(RuntimeError, match="正在处理中"):
        state.move_owned_world_club_player({
            "source_team_id": 10, "target_team_id": 20,
            "player_id": 30, "mode": "loan",
            "client_submission_id": "double-click",
        })


def test_owned_player_move_rejects_same_club(monkeypatch):
    monkeypatch.setattr(fm_odds_web, "OWNED_PLAYER_MOVE_RUNTIME_ENABLED", True)
    state = LocalOddsState.__new__(LocalOddsState)
    with pytest.raises(ValueError, match="另一家已收购俱乐部"):
        state.move_owned_world_club_player({
            "source_team_id": 10, "target_team_id": 10,
            "player_id": 30, "mode": "loan",
        })


def test_owned_player_release_requires_acquired_source_club(monkeypatch):
    monkeypatch.setattr(fm_odds_web, "OWNED_PLAYER_RELEASE_RUNTIME_ENABLED", True)
    state = LocalOddsState.__new__(LocalOddsState)
    state.memory_lock = threading.RLock()
    state._owned_world_club_target = lambda team_id: (
        "scope-a", {"save_instance_id": "save-a"},
        {"id": int(team_id), "name": "阿森纳", "address": "0x1000"},
    )
    calls = []
    monkeypatch.setattr(
        fm_odds_web, "release_owned_club_player",
        lambda **kwargs: calls.append(kwargs) or {
            "verified": True, "mode": "release",
        },
    )

    result = state.release_owned_world_club_player({
        "source_team_id": 602, "player_id": 55012984,
    })

    assert result["verified"] is True
    assert result["source_team_name"] == "阿森纳"
    assert calls == [{
        "source_team_id": 602, "source_team_address": "0x1000",
        "player_id": 55012984,
    }]


def test_owned_staff_release_requires_acquired_source_club(monkeypatch):
    monkeypatch.setattr(fm_odds_web, "OWNED_STAFF_RELEASE_RUNTIME_ENABLED", True)
    state = LocalOddsState.__new__(LocalOddsState)
    state.memory_lock = threading.RLock()
    state._owned_world_club_target = lambda team_id: (
        "scope-a", {"save_instance_id": "save-a"},
        {"id": int(team_id), "name": "阿森纳", "address": "0x1000"},
    )
    calls = []
    monkeypatch.setattr(
        fm_odds_web, "release_owned_club_staff",
        lambda **kwargs: calls.append(kwargs) or {
            "verified": True, "mode": "staff_release",
        },
    )

    result = state.release_owned_world_club_staff({
        "source_team_id": 602, "staff_id": 828925,
    })

    assert result["verified"] is True
    assert result["source_team_name"] == "阿森纳"
    assert calls == [{
        "source_team_id": 602, "source_team_address": "0x1000",
        "staff_id": 828925,
    }]


def test_owned_player_listing_requires_acquired_source_club(monkeypatch):
    state = LocalOddsState.__new__(LocalOddsState)
    state.memory_lock = threading.RLock()
    state._owned_world_club_target = lambda team_id: (
        "scope-a", {"save_instance_id": "save-a"},
        {"id": int(team_id), "name": "富勒姆", "address": "0x2000"},
    )
    calls = []
    monkeypatch.setattr(
        fm_odds_web, "set_owned_club_player_transfer_listed",
        lambda **kwargs: calls.append(kwargs) or {
            "verified": True, "listed": True,
        },
    )

    result = state.set_owned_world_club_player_listed({
        "source_team_id": 654, "player_id": 18074558, "listed": True,
    })

    assert result["verified"] is True
    assert result["source_team_name"] == "富勒姆"
    assert calls == [{
        "source_team_id": 654, "source_team_address": "0x2000",
        "source_squad_team_id": 0, "source_squad_team_address": None,
        "player_id": 18074558, "listed": True,
    }]


def test_fm24_epic_player_listing_uses_contract_collection_offset(monkeypatch):
    team, club, player, person, contract = 0x2000, 0x3000, 0x4000, 0x5000, 0x6000
    module = SimpleNamespace(base_address=0x10000000)
    layout = SimpleNamespace(
        key="fm24", distribution="epic", module_name="fm.exe",
        module=lambda _process: module, club_vtable_rva=0x100,
        player_move_contract_pool_rva=0x6378C80,
        player_move_main_contract_vtable_rva=0x5667C88,
    )
    transfer_status = {contract + 0x4F: 0x52}
    pointers = {
        team + player_movement.TEAM_CLUB: club,
        club: module.base_address + layout.club_vtable_rva,
        person + player_movement.FM24_PERSON_CONTRACT: contract,
        person + player_movement.FM24_PERSON_CONTRACT_COLLECTION: 0,
        contract: module.base_address + layout.player_move_main_contract_vtable_rva,
        contract + 0x08: person,
        contract + 0x10: team,
        player + player_movement.PLAYER_CURRENT_TEAM: team,
    }

    class FakeReader:
        def ptr(self, address):
            return pointers.get(address)

        def u32(self, address):
            return 654 if address == club + player_movement.ENTITY_UID else 0

        def u8(self, address):
            return transfer_status.get(address)

        def roster(self, _team):
            return [{"id": 18074558, "address": hex(player)}]

    reader = FakeReader()
    monkeypatch.setattr(
        player_movement, "select_process_layout", lambda: (24, "fm.exe", layout),
    )
    monkeypatch.setattr(
        player_movement, "open_process", lambda *_args, **_kwargs: nullcontext(object()),
    )
    monkeypatch.setattr(player_movement, "Reader", lambda *_args: reader)
    monkeypatch.setattr(player_movement, "_resolve_team_address", lambda *_args: team)
    monkeypatch.setattr(player_movement, "_validated_player_person", lambda *_args: person)
    monkeypatch.setattr(
        player_movement, "write_process_memory",
        lambda _process, address, raw: transfer_status.__setitem__(address, raw[0]),
    )

    result = player_movement.set_owned_club_player_transfer_listed(
        source_team_id=654, source_team_address=hex(team),
        player_id=18074558, listed=True,
    )

    assert result["verified"] is True
    assert result["transfer_status_before"] == 0x52
    assert result["transfer_status_after"] == 0x43


def test_owned_staff_move_requires_two_acquired_clubs(monkeypatch):
    state = LocalOddsState.__new__(LocalOddsState)
    state.memory_lock = threading.RLock()
    state._owned_world_club_target = lambda team_id: (
        "scope-a", {"save_instance_id": "save-a"},
        {"id": int(team_id), "name": f"球队{team_id}", "address": hex(team_id)},
    )
    calls = []
    monkeypatch.setattr(
        fm_odds_web, "move_owned_club_staff",
        lambda **kwargs: calls.append(kwargs) or {"verified": True},
    )

    result = state.move_owned_world_club_staff({
        "source_team_id": 654,
        "target_team_id": 34039025,
        "staff_id": 2000404461,
    })

    assert result["verified"] is True
    assert result["source_team_name"] == "球队654"
    assert result["target_team_name"] == "球队34039025"
    assert calls == [{
        "source_team_id": 654, "source_team_address": hex(654),
        "target_team_id": 34039025, "target_team_address": hex(34039025),
        "staff_id": 2000404461,
    }]
