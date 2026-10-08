from __future__ import annotations

import threading
import struct
from contextlib import nullcontext
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import fm_odds_web
import pytest
from tools import club_reader, player_movement
from tools.game_layout import FM24_LAYOUT, FM26_LAYOUT
from tools.initial_data_audit import ENTITY_UID, TEAM_MANAGER


ROOT = (Path(__file__).resolve().parents[1] / "src")


def test_two_year_contract_extension_handles_leap_day() -> None:
    assert player_movement._add_years(date(2028, 2, 29), 2) == date(2030, 2, 28)


def test_team_manager_person_offsets_are_generation_specific() -> None:
    assert FM24_LAYOUT.team_manager_person_offset == 0xF8
    assert FM26_LAYOUT.team_manager_person_offset == 0x100


def test_coaching_license_offsets_are_generation_specific() -> None:
    assert FM24_LAYOUT.staff_coaching_license_offset == 0x16A
    assert FM26_LAYOUT.staff_coaching_license_offset == 0x13C


def test_manager_appointment_forces_full_time_contract_for_both_generations() -> None:
    for layout, contract_size, expected_offset in (
        (FM24_LAYOUT, player_movement.FM24_CONTRACT_SIZE, 0xB3),
        (FM26_LAYOUT, player_movement.CONTRACT_SIZE, 0xC3),
    ):
        contract = bytearray(contract_size)
        contract[expected_offset] = 0

        actual_offset = player_movement._set_full_time_staff_contract_type(
            contract, layout,
        )

        assert actual_offset == expected_offset
        assert contract[expected_offset] == player_movement.FULL_TIME_STAFF_CONTRACT_TYPE


@pytest.mark.parametrize(
    ("layout", "contract_size"),
    [
        (FM24_LAYOUT, player_movement.FM24_CONTRACT_SIZE),
        (FM26_LAYOUT, player_movement.CONTRACT_SIZE),
    ],
)
def test_manager_appointment_rebuilds_full_contract_for_both_generations(
    layout, contract_size,
) -> None:
    candidate_person = 0x1234567890
    target_team = 0x2345678900
    game_date = date(2023, 8, 21)
    game_date_code = player_movement._encode_date(game_date) | 0x200
    template = bytes([0x5A]) * contract_size

    rebuilt, contract_type_offset, start, expiry = (
        player_movement._manager_appointment_contract_data(
            template,
            layout=layout,
            candidate_person=candidate_person,
            target_team=target_team,
            game_date_code=game_date_code,
        )
    )

    assert template == bytes([0x5A]) * contract_size
    assert len(rebuilt) == contract_size
    assert struct.unpack_from("<Q", rebuilt, 0x08)[0] == candidate_person
    assert struct.unpack_from("<Q", rebuilt, 0x10)[0] == target_team
    assert rebuilt[contract_type_offset] == player_movement.FULL_TIME_STAFF_CONTRACT_TYPE
    assert rebuilt[int(layout.staff_job_type_offset)] == 16
    assert start == game_date
    assert expiry == date(2026, 8, 21)
    assert (
        struct.unpack_from("<I", rebuilt, int(layout.staff_contract_start_offset))[0]
        == game_date_code
    )
    assert player_movement.decode_date(
        struct.unpack_from("<I", rebuilt, int(layout.staff_contract_expiry_offset))[0]
    ) == expiry


@pytest.mark.parametrize(
    ("key", "license_offset", "contract_offset", "job_offset"),
    [("fm24", 0x16A, 0xC8, 0x1C), ("fm26", 0x13C, 0xA8, 0x26)],
)
def test_staff_coaching_license_upgrade_validates_and_reads_back(
    monkeypatch, key, license_offset, contract_offset, job_offset,
) -> None:
    module_base, person, contract, team = 0x100000, 0x3000, 0x5000, 0x7000
    memory = {person + license_offset: 2}
    layout = SimpleNamespace(
        key=key, game_version="test", module_name="fm.exe",
        staff_person_vtable_rva=0x200,
        staff_coaching_license_offset=license_offset,
        staff_job_type_offset=job_offset,
    )
    layout.module = lambda _process: SimpleNamespace(base_address=module_base)

    class FakeReader:
        def __init__(self, *_args):
            pass

        @staticmethod
        def ptr(address):
            return {
                person: module_base + 0x200,
                person + contract_offset: contract,
                contract + 0x08: person,
                contract + 0x10: team,
            }.get(address)

        @staticmethod
        def u32(address):
            return {person + ENTITY_UID: 88, team + ENTITY_UID: 679}.get(address)

        @staticmethod
        def u8(address):
            if address == contract + job_offset:
                return 2
            return memory.get(address)

    monkeypatch.setattr(club_reader, "select_process_layout", lambda: (24, "fm.exe", layout))
    monkeypatch.setattr(
        club_reader, "open_process",
        lambda *_args, **_kwargs: nullcontext(SimpleNamespace(handle=1)),
    )
    monkeypatch.setattr(club_reader, "Reader", FakeReader)
    monkeypatch.setattr(
        club_reader, "write_process_memory",
        lambda _process, address, raw: memory.__setitem__(address, int(bytes(raw)[0])),
    )

    result = club_reader.upgrade_staff_coaching_license(
        88, hex(person), 679, hex(team), 2, 1,
    )

    assert result["before"] == 2
    assert result["after"] == 1
    assert result["license"] == "洲际职业级"
    assert memory[person + license_offset] == 1


@pytest.mark.parametrize(
    ("key", "license_offset"),
    [("fm24", 0x16A), ("fm26", 0x13C)],
)
def test_player_manager_coaching_license_upgrade_validates_team_and_reads_back(
    monkeypatch, key, license_offset,
) -> None:
    module_base, manager, person, team = 0x100000, 0x2000, 0x3000, 0x7000
    memory = {person + license_offset: 2}
    layout = SimpleNamespace(
        key=key, game_version="test", module_name="fm.exe",
        manager_person_offset=0x100,
        staff_coaching_license_offset=license_offset,
    )
    layout.module = lambda _process: SimpleNamespace(base_address=module_base)

    class FakeReader:
        def __init__(self, *_args):
            pass

        @staticmethod
        def ptr(address):
            return {team + TEAM_MANAGER: manager}.get(address)

        @staticmethod
        def u8(address):
            return memory.get(address)

        @staticmethod
        def team(address):
            return {
                "id": 679, "team_type": "club", "address": hex(team),
            } if address == team else None

    monkeypatch.setattr(club_reader, "select_process_layout", lambda: (24, "fm.exe", layout))
    monkeypatch.setattr(
        club_reader, "open_process",
        lambda *_args, **_kwargs: nullcontext(SimpleNamespace(handle=1)),
    )
    monkeypatch.setattr(club_reader, "Reader", FakeReader)
    monkeypatch.setattr(
        club_reader, "_context_addresses",
        lambda *_args, **_kwargs: (manager, person, team),
    )
    monkeypatch.setattr(
        club_reader, "write_process_memory",
        lambda _process, address, raw: memory.__setitem__(address, int(bytes(raw)[0])),
    )

    result = club_reader.upgrade_human_manager_coaching_license(
        77, hex(manager), 679, hex(team), 2, 1,
    )

    assert result["before"] == 2
    assert result["after"] == 1
    assert result["license"] == "洲际职业级"
    assert memory[person + license_offset] == 1


def test_staff_contract_validation_keeps_internal_details_out_of_player_message() -> None:
    layout = SimpleNamespace(
        staff_person_vtable_rva=0x100,
        player_move_main_contract_vtable_rva=0x200,
        staff_job_type_offset=0x26,
    )

    class EmptyReader:
        @staticmethod
        def ptr(_address):
            return None

        @staticmethod
        def u32(_address):
            return None

        @staticmethod
        def u8(_address):
            return None

    with pytest.raises(ValueError) as caught:
        player_movement._validated_staff_main_contract(
            EmptyReader(), SimpleNamespace(base_address=0x100000), layout,
            person=0x3000, staff_id=77, team=0x4000,
            person_contract_offset=player_movement.FM24_PERSON_CONTRACT,
            label="主教练",
        )

    assert str(caught.value) == "主教练合同状态已变化，请刷新后重试"
    assert "vtable" not in str(caught.value)
    assert getattr(caught.value, "validation_failures")


def test_manager_contract_extension_preserves_date_flags_and_reads_back(monkeypatch) -> None:
    team = 0x4000
    manager = 0x6000
    person = 0x6100
    contract = 0x8000
    module_base = 0x100000
    before_date = date(2028, 2, 29)
    before_raw = player_movement._encode_date(before_date) | 0xA600
    expected_raw = player_movement._encode_date(date(2030, 2, 28)) | 0xA600
    memory = {contract + 0x48: struct.pack("<I", before_raw)}
    layout = SimpleNamespace(
        key="fm26", module_name="game_plugin.dll",
        staff_contract_expiry_offset=0x48, staff_job_type_offset=0x26,
        staff_complete_object_offset=0x100, staff_person_vtable_rva=0x200,
        player_move_main_contract_vtable_rva=0x300,
    )
    layout.module = lambda _process: SimpleNamespace(base_address=module_base)

    class FakeReader:
        def __init__(self, *_args):
            pass

        @staticmethod
        def ptr(address):
            return {
                team + TEAM_MANAGER: manager,
                person: module_base + 0x200,
                person + player_movement.PERSON_CONTRACT: contract,
                person + player_movement.PERSON_CONTRACT_COLLECTION: 0xA000,
                contract: module_base + 0x300,
                contract + 0x08: person,
                contract + 0x10: team,
            }.get(address)

        @staticmethod
        def u32(address):
            if address == person + ENTITY_UID:
                return 77
            if address == contract + 0x48:
                return before_raw
            return None

        @staticmethod
        def u8(address):
            return 16 if address == contract + 0x26 else None

    context = SimpleNamespace(Rip=0x10, Rsp=0x20)
    monkeypatch.setattr(player_movement, "_configure_thread_api", lambda: None)
    monkeypatch.setattr(player_movement, "select_process_layout", lambda: (26, "fm.exe", layout))
    monkeypatch.setattr(
        player_movement, "open_process",
        lambda *_args, **_kwargs: nullcontext(SimpleNamespace(handle=1)),
    )
    monkeypatch.setattr(player_movement, "Reader", FakeReader)
    monkeypatch.setattr(player_movement, "_validated_club_team", lambda *_args: team)
    monkeypatch.setattr(player_movement, "_logic_thread", lambda _process: (9, context))
    monkeypatch.setattr(player_movement, "_open_thread", lambda _thread_id: 10)
    monkeypatch.setattr(player_movement, "_get_context", lambda _handle: context)
    monkeypatch.setattr(player_movement.kernel32, "SuspendThread", lambda _handle: 0)
    monkeypatch.setattr(player_movement.kernel32, "ResumeThread", lambda _handle: 0)
    monkeypatch.setattr(player_movement.kernel32, "CloseHandle", lambda _handle: 1)
    monkeypatch.setattr(
        player_movement, "read_process_memory",
        lambda _process, address, size: memory.get(address, b"")[:size],
    )
    monkeypatch.setattr(
        player_movement, "write_process_memory",
        lambda _process, address, raw: memory.__setitem__(address, bytes(raw)),
    )

    result = player_movement.extend_owned_club_manager_contract(
        team_id=42, team_address=hex(team), manager_id=77,
    )

    assert result["before_expiry_date"] == "2028-02-29"
    assert result["contract_expiry_date"] == "2030-02-28"
    assert memory[contract + 0x48] == struct.pack("<I", expected_raw)


def test_manager_replacement_swaps_both_contracts_and_team_slots(monkeypatch) -> None:
    target_team, source_team = 0x4000, 0x5000
    current_manager, candidate_manager = 0x6000, 0x7000
    current_person, candidate_person = 0x6100, 0x7100
    current_contract, candidate_contract = 0x8000, 0x9000
    module_base = 0x100000
    main_vtable = module_base + 0x300
    person_vtable = module_base + 0x200
    layout = SimpleNamespace(
        key="fm26", module_name="game_plugin.dll",
        staff_job_type_offset=0x26, staff_complete_object_offset=0x100,
        staff_person_vtable_rva=0x200, player_move_main_contract_vtable_rva=0x300,
    )
    layout.module = lambda _process: SimpleNamespace(base_address=module_base)
    pointers = {
        target_team + TEAM_MANAGER: current_manager,
        source_team + TEAM_MANAGER: candidate_manager,
        current_person: person_vtable,
        candidate_person: person_vtable,
        current_person + player_movement.PERSON_CONTRACT: current_contract,
        candidate_person + player_movement.PERSON_CONTRACT: candidate_contract,
        current_person + player_movement.PERSON_CONTRACT_COLLECTION: 0xA000,
        candidate_person + player_movement.PERSON_CONTRACT_COLLECTION: 0xB000,
        current_contract: main_vtable,
        candidate_contract: main_vtable,
        current_contract + 0x08: current_person,
        candidate_contract + 0x08: candidate_person,
        current_contract + 0x10: target_team,
        candidate_contract + 0x10: source_team,
    }
    memory = {
        current_contract + 0x10: struct.pack("<Q", target_team),
        candidate_contract + 0x10: struct.pack("<Q", source_team),
        target_team + TEAM_MANAGER: struct.pack("<Q", current_manager),
        source_team + TEAM_MANAGER: struct.pack("<Q", candidate_manager),
    }

    class FakeReader:
        def __init__(self, *_args):
            pass

        @staticmethod
        def ptr(address):
            return pointers.get(address)

        @staticmethod
        def u32(address):
            return {
                current_person + ENTITY_UID: 77,
                candidate_person + ENTITY_UID: 88,
            }.get(address)

        @staticmethod
        def u8(address):
            return 16 if address in {current_contract + 0x26, candidate_contract + 0x26} else None

    context = SimpleNamespace(Rip=0x10, Rsp=0x20)
    monkeypatch.setattr(player_movement, "_configure_thread_api", lambda: None)
    monkeypatch.setattr(player_movement, "select_process_layout", lambda: (26, "fm.exe", layout))
    monkeypatch.setattr(
        player_movement, "open_process",
        lambda *_args, **_kwargs: nullcontext(SimpleNamespace(handle=1)),
    )
    monkeypatch.setattr(player_movement, "Reader", FakeReader)
    monkeypatch.setattr(
        player_movement, "_validated_club_team",
        lambda _reader, _module, team_id, _address: target_team if team_id == 42 else source_team,
    )
    monkeypatch.setattr(player_movement, "_scan_head_coaches", lambda *_args: [{
        "id": 88, "name": "候选教练", "team_id": 99,
        "address": hex(candidate_person), "contract_address": hex(candidate_contract),
        "team_address": hex(source_team), "contract_expiry_date": "2029-06-30",
    }])
    monkeypatch.setattr(player_movement, "_logic_thread", lambda _process: (9, context))
    monkeypatch.setattr(player_movement, "_open_thread", lambda _thread_id: 10)
    monkeypatch.setattr(player_movement, "_get_context", lambda _handle: context)
    monkeypatch.setattr(player_movement.kernel32, "SuspendThread", lambda _handle: 0)
    monkeypatch.setattr(player_movement.kernel32, "ResumeThread", lambda _handle: 0)
    monkeypatch.setattr(player_movement.kernel32, "CloseHandle", lambda _handle: 1)
    monkeypatch.setattr(
        player_movement, "read_process_memory",
        lambda _process, address, size: memory.get(address, b"")[:size],
    )
    monkeypatch.setattr(
        player_movement, "write_process_memory",
        lambda _process, address, raw: memory.__setitem__(address, bytes(raw)),
    )

    result = player_movement.replace_owned_club_manager(
        target_team_id=42, target_team_address=hex(target_team),
        current_manager_id=77, candidate_manager_id=88,
        candidate_team_id=99, scan_manager_id=123,
    )

    assert result["manager_id"] == 88
    assert memory[candidate_contract + 0x10] == struct.pack("<Q", target_team)
    assert memory[current_contract + 0x10] == struct.pack("<Q", source_team)
    assert memory[target_team + TEAM_MANAGER] == struct.pack("<Q", candidate_manager)
    assert memory[source_team + TEAM_MANAGER] == struct.pack("<Q", current_manager)


def test_head_coach_scan_only_returns_verified_club_manager_slot(monkeypatch) -> None:
    person = 0x3100
    contract = 0x5000
    team = 0x7000
    manager_object = person - 0xF8
    layout = SimpleNamespace(
        staff_job_type_offset=0x26,
        staff_complete_object_offset=0x100,
        team_manager_person_offset=0xF8,
        staff_contract_expiry_offset=0x48,
    )

    class FakeReader:
        def __init__(self):
            self.layout = layout

        @staticmethod
        def ptr(address):
            return {
                contract + 0x08: person,
                contract + 0x10: team,
                team + TEAM_MANAGER: manager_object,
            }.get(address)

        @staticmethod
        def u8(address):
            return 16 if address == contract + 0x26 else None

        @staticmethod
        def u32(address):
            if address == person + ENTITY_UID:
                return 77
            if address == contract + 0x48:
                return player_movement._encode_date(date(2029, 6, 30))
            return None

        @staticmethod
        def team(address):
            assert address == team
            return {"id": 42, "name": "来源俱乐部", "team_type": "club"}

    monkeypatch.setattr(club_reader, "_staff_scan_entries", lambda *_args: [(person, contract)])
    monkeypatch.setattr(club_reader, "_name", lambda *_args: "候选教练")

    assert club_reader._scan_head_coaches(FakeReader()) == [{
        "id": 77,
        "name": "候选教练",
        "team_id": 42,
        "team_name": "来源俱乐部",
        "contract_expiry_date": "2029-06-30",
        "address": hex(person),
        "contract_address": hex(contract),
        "team_address": hex(team),
    }]


def test_head_coach_scan_accepts_employed_manager_with_nonstandard_job_code(monkeypatch) -> None:
    person = 0x3100
    contract = 0x5000
    team = 0x7000
    manager_object = person - 0xF8
    layout = SimpleNamespace(
        staff_job_type_offset=0x26,
        staff_complete_object_offset=0x100,
        team_manager_person_offset=0xF8,
        staff_contract_expiry_offset=0x48,
    )

    class FakeReader:
        def __init__(self):
            self.layout = layout

        @staticmethod
        def ptr(address):
            return {
                contract + 0x08: person,
                contract + 0x10: team,
                team + TEAM_MANAGER: manager_object,
            }.get(address)

        @staticmethod
        def u8(address):
            return 35 if address == contract + 0x26 else None

        @staticmethod
        def u32(address):
            return 77 if address == person + ENTITY_UID else None

        @staticmethod
        def team(address):
            assert address == team
            return {"id": 42, "name": "Source Club", "team_type": "club"}

    monkeypatch.setattr(club_reader, "_staff_scan_entries", lambda *_args: [(person, contract)])
    monkeypatch.setattr(club_reader, "_name", lambda *_args: "Employed Coach")

    rows = club_reader._scan_head_coaches(FakeReader())

    assert rows[0]["id"] == 77
    assert rows[0]["team_id"] == 42
    assert rows[0]["name"] == "Employed Coach"


def test_head_coach_search_returns_only_public_fields(monkeypatch) -> None:
    layout = SimpleNamespace(module_name="game_plugin.dll")
    layout.module = lambda _process: SimpleNamespace(base_address=0x100000)
    monkeypatch.setattr(club_reader, "select_process_layout", lambda: (26, "fm.exe", layout))
    monkeypatch.setattr(club_reader, "open_process", lambda *_args, **_kwargs: nullcontext(SimpleNamespace(pid=26)))
    monkeypatch.setattr(club_reader, "Reader", lambda *_args: object())
    monkeypatch.setattr(club_reader, "_scan_head_coaches", lambda *_args: [{
        "id": 7, "name": "Carlo Ancelotti", "team_id": 10,
        "team_name": "皇家马德里", "contract_expiry_date": "2028-06-30",
        "address": "0x1", "contract_address": "0x2", "team_address": "0x3",
    }])
    monkeypatch.setattr(club_reader, "_scan_free_manager_candidates", lambda *_args: [])

    result = club_reader.search_head_coaches("ancel", exclude_team_id=20)

    assert result["head_coaches"] == [{
        "id": 7, "name": "Carlo Ancelotti", "team_id": 10,
        "team_name": "皇家马德里", "contract_expiry_date": "2028-06-30",
        "free_agent": False,
    }]
    assert "address" not in result["head_coaches"][0]


def test_head_coach_search_includes_free_manager_candidates(monkeypatch) -> None:
    layout = SimpleNamespace(module_name="game_plugin.dll")
    layout.module = lambda _process: SimpleNamespace(base_address=0x100000)
    monkeypatch.setattr(club_reader, "select_process_layout", lambda: (26, "fm.exe", layout))
    monkeypatch.setattr(
        club_reader, "open_process",
        lambda *_args, **_kwargs: nullcontext(SimpleNamespace(pid=26)),
    )
    monkeypatch.setattr(club_reader, "Reader", lambda *_args: object())
    monkeypatch.setattr(club_reader, "_scan_head_coaches", lambda *_args: [])
    monkeypatch.setattr(club_reader, "_scan_free_manager_candidates", lambda *_args: [{
        "id": 1407, "name": "Nenad Bjelica", "team_id": 0,
        "team_name": "自由职员", "contract_expiry_date": None,
        "free_agent": True,
    }])

    result = club_reader.search_head_coaches("bjelica", exclude_team_id=927)

    assert result["head_coaches"] == [{
        "id": 1407, "name": "Nenad Bjelica", "team_id": 0,
        "team_name": "自由职员", "contract_expiry_date": None,
        "free_agent": True,
    }]


def test_owned_manager_operations_require_owned_target_and_forward(monkeypatch) -> None:
    state = fm_odds_web.LocalOddsState.__new__(fm_odds_web.LocalOddsState)
    state.memory_lock = threading.RLock()
    output = {"save_instance_id": "save-a"}
    club = {"id": 42, "name": "目标俱乐部", "address": "0x4200"}
    state._owned_world_club_target = lambda team_id: ("scope-a", output, club)
    cache_invalidations = []
    monkeypatch.setattr(
        fm_odds_web, "invalidate_head_coach_search_cache",
        lambda: cache_invalidations.append(True),
    )
    charges = []
    monkeypatch.setattr(
        fm_odds_web, "charge_combined_funds",
        lambda amount, event, **metadata: charges.append(
            (amount, event, metadata),
        ) or {
            "total": float(amount), "bank": float(amount), "wallet": 0.0,
            "transaction_id": f"payment-{len(charges)}",
        },
    )

    search_calls = []
    monkeypatch.setattr(
        fm_odds_web, "search_head_coaches",
        lambda *args, **kwargs: search_calls.append((args, kwargs)) or {
            "head_coaches": [], "count": 0, "total_matches": 0, "truncated": False,
        },
    )
    search = state.search_world_head_coaches(42, "pep")
    assert search["target_team_id"] == 42
    assert search_calls == [(('pep',), {"exclude_team_id": 42, "manager_id": 0})]

    extend_calls = []
    monkeypatch.setattr(
        fm_odds_web, "extend_owned_club_manager_contract",
        lambda **kwargs: extend_calls.append(kwargs) or {
            "verified": True, "contract_expiry_date": "2030-06-30",
        },
    )
    extended = state.extend_owned_world_club_manager_contract({
        "team_id": 42, "manager_id": 77,
    })
    assert extended["team_name"] == "目标俱乐部"
    assert extend_calls == [{
        "team_id": 42, "team_address": "0x4200", "manager_id": 77, "years": 2,
    }]

    replace_calls = []
    monkeypatch.setattr(
        fm_odds_web, "replace_owned_club_manager",
        lambda **kwargs: replace_calls.append(kwargs) or {
            "verified": True, "manager_id": 88, "manager_name": "候选教练",
        },
    )
    replaced = state.replace_owned_world_club_manager({
        "team_id": 42, "current_manager_id": 77,
        "candidate_manager_id": 88, "candidate_team_id": 99,
    })
    assert replaced["target_team_name"] == "目标俱乐部"
    assert replace_calls == [{
        "target_team_id": 42, "target_team_address": "0x4200",
        "current_manager_id": 77, "candidate_manager_id": 88,
        "candidate_team_id": 99, "scan_manager_id": 0,
    }]
    assert replaced["payment"]["total"] == 1_000_000.0

    appoint_calls = []
    monkeypatch.setattr(
        fm_odds_web, "appoint_free_agent_manager",
        lambda **kwargs: appoint_calls.append(kwargs) or {
            "verified": True, "manager_id": 1407, "manager_name": "Nenad Bjelica",
        },
    )
    appointed = state.replace_owned_world_club_manager({
        "team_id": 42, "current_manager_id": 0,
        "candidate_manager_id": 1407, "candidate_team_id": 0,
        "candidate_free_agent": True, "candidate_name": "Nenad Bjelica",
    })
    assert appointed["target_team_name"] == "目标俱乐部"
    assert appoint_calls == [{
        "target_team_id": 42, "target_team_address": "0x4200",
        "candidate_staff_id": 1407, "candidate_name": "Nenad Bjelica",
    }]
    assert cache_invalidations == [True, True]
    assert [row[:2] for row in charges] == [
        (1_000_000, "world_club_manager_replace"),
        (1_000_000, "world_club_manager_replace"),
    ]


def test_owned_manager_replacement_refunds_when_memory_write_fails(monkeypatch) -> None:
    state = fm_odds_web.LocalOddsState.__new__(fm_odds_web.LocalOddsState)
    state.memory_lock = threading.RLock()
    state._owned_world_club_target = lambda _team_id: (
        "scope-a", {}, {"id": 42, "name": "目标俱乐部", "address": "0x4200"},
    )
    payment = {
        "total": 1_000_000.0, "bank": 600_000.0, "wallet": 400_000.0,
        "transaction_id": "payment-1",
    }
    monkeypatch.setattr(
        fm_odds_web, "charge_combined_funds",
        lambda amount, event, **_metadata: payment
        if (amount, event) == (1_000_000, "world_club_manager_replace")
        else pytest.fail("主教练更换扣款参数错误"),
    )
    refunds = []
    monkeypatch.setattr(
        fm_odds_web, "refund_combined_funds",
        lambda paid, event, **metadata: refunds.append((paid, event, metadata)),
    )
    monkeypatch.setattr(
        fm_odds_web, "replace_owned_club_manager",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("写入失败")),
    )

    with pytest.raises(RuntimeError, match="写入失败"):
        state.replace_owned_world_club_manager({
            "team_id": 42, "current_manager_id": 77,
            "candidate_manager_id": 88, "candidate_team_id": 99,
        })

    assert refunds[0][0] is payment
    assert refunds[0][1] == "world_club_manager_replace_rollback"


def test_player_manager_cannot_be_extended_or_replaced(monkeypatch) -> None:
    state = fm_odds_web.LocalOddsState.__new__(fm_odds_web.LocalOddsState)
    state.memory_lock = threading.RLock()
    output = {
        "selected_manager_id": 77,
        "manager": {"id": 77, "name": "玩家经理"},
        "managed_team": {"id": 42, "team_type": "club"},
    }
    state._owned_world_club_target = lambda _team_id: (
        "scope-a", output, {"id": 42, "address": "0x4200"},
    )
    monkeypatch.setattr(
        fm_odds_web, "extend_owned_club_manager_contract",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("不应写入")),
    )
    monkeypatch.setattr(
        fm_odds_web, "replace_owned_club_manager",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("不应写入")),
    )

    try:
        state.extend_owned_world_club_manager_contract({"team_id": 42, "manager_id": 77})
        raise AssertionError("应拒绝玩家主教练续约")
    except ValueError as error:
        assert "玩家主教练" in str(error)
    try:
        state.replace_owned_world_club_manager({
            "team_id": 42, "current_manager_id": 77,
            "candidate_manager_id": 88, "candidate_team_id": 99,
        })
        raise AssertionError("应拒绝更换玩家主教练")
    except ValueError as error:
        assert "玩家主教练" in str(error)


def test_frontend_exposes_manager_contract_and_replacement_controls() -> None:
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    markup = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    assert "manager-contract-extend" in script
    assert "head-coaches?team_id=" in script
    assert "manager-replace" in script
    assert "manager_replace_price" in script
    assert 'ownedClubAction("续约两年","calendar-plus","manager-extend")' in script
    assert 'ownedClubAction(`更换主教练 · ${money(managerReplacePrice)}`,"replace","manager-replace")' in script
    assert 'ownedClubAction(`聘请主教练 · ${money(managerReplacePrice)}`,"user-round-plus","manager-replace")' in script
    assert "candidate_free_agent:Boolean(candidate.free_agent)" in script
    assert 'row.free_agent\n        ? "自由职员"' in script
    assert 'id="owned-manager-replace-dialog"' in markup
