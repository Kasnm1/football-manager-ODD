from __future__ import annotations

import struct
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tools.ca_growth_hook import (
    CODE_PREFIX,
    FM24_SIGNATURE,
    FM26_SIGNATURE,
    FM24_ORIGINAL,
    HOOK_ORIGINAL,
    LEGACY_CODE_PREFIX,
    MAX_PLAUSIBLE_CA_DELTA,
    MAX_PLAYER_ARRAY_INDEX,
    TELEMETRY_OFFSET,
    TELEMETRY_STRUCT,
    CAGrowthHookController,
    _build_code,
    _build_fm24_code,
    _decode_telemetry,
    _expected_applied_delta,
    _managed_code_addresses,
    _managed_code_match,
)
from tools.game_layout import (
    FM24_240_LAYOUT,
    FM24_241_LAYOUT,
    FM24_EPIC_LAYOUT,
    FM24_LAYOUT,
    FM26_LAYOUT,
    FM26_XGP_TEMPLATE,
)
from tools.redbull_hook import _rel32


def test_ca_growth_layout_keeps_fixed_rva_only_for_verified_fm26_steam() -> None:
    assert FM26_LAYOUT.ca_growth_hook_rva == 0x3174E3C
    assert FM26_XGP_TEMPLATE.ca_growth_hook_rva is None


def test_fm24_exact_layouts_and_dynamic_candidates_are_separate() -> None:
    assert FM24_LAYOUT.ca_growth_hook_rva == 0x4F7C463
    assert FM24_EPIC_LAYOUT.ca_growth_hook_rva == 0x4F7C463
    assert FM24_240_LAYOUT.ca_growth_hook_rva is None
    assert FM24_241_LAYOUT.ca_growth_hook_rva is None


def test_managed_code_match_accepts_current_and_legacy_code() -> None:
    cave = 0x70000000
    hook = 0x71000000
    team = 0x123456789ABC
    expected = _build_code(cave, HOOK_ORIGINAL, hook + 8, 0)

    current = _build_code(cave, HOOK_ORIGINAL, hook + 8, team)
    legacy = _build_code(cave, HOOK_ORIGINAL, hook + 8, team, marker=b"")

    assert current.startswith(CODE_PREFIX)
    assert _managed_code_match(current, expected) == (True, team)
    legacy_expected = _build_code(cave, HOOK_ORIGINAL, hook + 8, 0, marker=b"")
    assert _managed_code_match(legacy, legacy_expected) == (True, team)
    corrupted = bytearray(current)
    corrupted[-8] ^= 0x01
    assert _managed_code_match(bytes(corrupted), expected) == (False, 0)


def test_fm24_code_uses_trainer_registers_and_club_scope() -> None:
    cave = 0x70000000
    hook = 0x71000000
    team = 0x123456789ABC
    club = 0xABCDEF123456
    current = _build_fm24_code(
        cave, FM24_ORIGINAL, hook + 8, team, club,
    )
    expected = _build_fm24_code(
        cave, FM24_ORIGINAL, hook + 8, 0, 0,
    )

    assert _managed_code_addresses(current, expected) == (True, (team, club))
    assert b"\x4F\x8B\x84\x05\xD0\x00\x00\x00" in current
    assert b"\x66\x45\x6B\xF6\x02" in current
    assert b"\x66\x45\x6B\xFF\x02" not in current


def test_hook_code_supports_three_times_multiplier() -> None:
    cave = 0x70000000
    hook = 0x71000000

    fm26 = _build_code(
        cave, HOOK_ORIGINAL, hook + 8, 0x9000, multiplier=3,
    )
    fm24 = _build_fm24_code(
        cave, FM24_ORIGINAL, hook + 8, 0x9000, 0xA000,
        multiplier=3,
    )

    assert b"\x66\x6B\xFF\x03" in fm26
    assert b"\x66\x45\x6B\xF6\x03" in fm24
    assert b"\x66\xBF\x02\x00" in fm26
    assert b"\x66\x41\xBE\x02\x00" in fm24


def test_hook_code_supports_real_one_point_five_multiplier() -> None:
    cave = 0x70000000
    hook = 0x71000000

    fm26 = _build_code(
        cave, HOOK_ORIGINAL, hook + 8, 0x9000, multiplier=1.5,
    )
    fm24 = _build_fm24_code(
        cave, FM24_ORIGINAL, hook + 8, 0x9000, 0xA000,
        multiplier=1.5,
    )

    assert b"\x66\x6B\xFF\x03\x66\xFF\xC7\x66\xD1\xEF" in fm26
    assert b"\x66\x45\x6B\xF6\x03\x66\x41\xFF\xC6\x66\x41\xD1\xEE" in fm24
    assert b"\x66\xBF\x01\x00" in fm26
    assert b"\x66\x41\xBE\x01\x00" in fm24


def test_fm26_hook_records_raw_and_applied_delta_before_publishing() -> None:
    cave = 0x70000000
    hook = 0x71000000
    telemetry = cave + TELEMETRY_OFFSET
    code = _build_code(cave, HOOK_ORIGINAL, hook + 8, 0x9000, multiplier=2)

    assert b"\x49\xB9" + struct.pack("<Q", telemetry) in code
    assert b"\x66\x41\x89\x79\x20" in code
    assert b"\x66\x41\x89\x79\x30" in code
    assert b"\x66\x41\x89\x79\x32" in code
    assert b"\xF0\x49\xFF\x01" in code
    assert b"\xF0\x49\xFF\x41\x08" in code
    assert code.index(b"\x66\x41\x89\x79\x32") < code.rindex(b"\xF0\x49\xFF\x41\x08")


def test_fm24_hook_records_raw_and_applied_delta_before_publishing() -> None:
    cave = 0x70000000
    hook = 0x71000000
    telemetry = cave + TELEMETRY_OFFSET
    code = _build_fm24_code(
        cave, FM24_ORIGINAL, hook + 8, 0x9000, 0xA000, multiplier=2,
    )

    assert b"\x49\xB9" + struct.pack("<Q", telemetry) in code
    assert b"\x66\x45\x89\x71\x20" in code
    assert b"\x66\x45\x89\x71\x30" in code
    assert b"\x66\x45\x89\x71\x32" in code
    assert b"\xF0\x49\xFF\x01" in code
    assert b"\xF0\x49\xFF\x41\x08" in code
    assert code.index(b"\x66\x45\x89\x71\x32") < code.rindex(b"\xF0\x49\xFF\x41\x08")


def test_fm26_hook_matches_current_club_then_current_national_team() -> None:
    cave = 0x70000000
    hook = 0x71000000
    club_team = 0x9000
    national_team = 0xA000

    code = _build_code(
        cave, HOOK_ORIGINAL, hook + 8, club_team,
        national_team_address=national_team,
    )
    expected = _build_code(
        cave, HOOK_ORIGINAL, hook + 8, 0,
        national_team_address=0,
    )

    assert b"\x4D\x8B\x85\x28\x01\x00\x00" in code
    assert (
        b"\x41\x81\xF8" + struct.pack("<I", MAX_PLAYER_ARRAY_INDEX)
        in code
    )
    assert code.count(b"\x49\x81\xF8\xFF\xFF\x0F\x00") == 2
    assert _managed_code_addresses(code, expected) == (
        True, (club_team, national_team),
    )


def test_ca_growth_telemetry_decodes_observed_transformation() -> None:
    payload = TELEMETRY_STRUCT.pack(
        19, 7, 0x11111111, 0x87654321, -1, 119, 6, 0,
        0x12345678, 1, 2, 120, 1, 1,
    )

    telemetry = _decode_telemetry(payload, 2)

    assert telemetry == {
        "available": True,
        "observed": True,
        "hit_count": 7,
        "instruction_hit_count": 19,
        "last_filter_player_address": "0x11111111",
        "last_filter_raw_delta": -1,
        "last_filter_ca_before": 119,
        "player_address": "0x12345678",
        "resolved_team_address": "0x87654321",
        "filter_stage": 6,
        "filter_stage_name": "team_resolved",
        "target_team_matched": True,
        "matched_scope": "club",
        "matched_team_address": None,
        "raw_delta": 1,
        "applied_delta": 2,
        "ca_before": 120,
        "predicted_ca_after": 122,
        "within_growth_range": True,
        "transformation_verified": True,
    }


def test_ca_growth_telemetry_explains_unmatched_instruction_hit() -> None:
    payload = TELEMETRY_STRUCT.pack(
        4, 0, 0x12345678, 0x87654321, -1, 120, 6, 0,
        0, 0, 0, 0, 0, 0,
    )

    telemetry = _decode_telemetry(payload, 2)

    assert telemetry["observed"] is False
    assert telemetry["instruction_hit_count"] == 4
    assert telemetry["hit_count"] == 0
    assert telemetry["resolved_team_address"] == "0x87654321"
    assert telemetry["filter_stage_name"] == "team_resolved"
    assert telemetry["last_filter_raw_delta"] == -1
    assert telemetry["raw_delta"] is None
    assert telemetry["applied_delta"] is None


def test_expected_growth_delta_matches_hook_branches() -> None:
    assert _expected_applied_delta(1, 2) == 2
    assert _expected_applied_delta(0, 2) == 1
    assert _expected_applied_delta(-1, 2) == 2
    assert _expected_applied_delta(201, 2) == 201
    assert _expected_applied_delta(1, 1.5) == 2
    assert _expected_applied_delta(2, 1.5) == 3


def test_hook_leaves_out_of_range_match_deltas_untouched() -> None:
    cave = 0x70000000
    hook = 0x71000000

    fm26 = _build_code(cave, HOOK_ORIGINAL, hook + 8, 0x9000, multiplier=3)
    fm24 = _build_fm24_code(
        cave, FM24_ORIGINAL, hook + 8, 0x9000, 0xA000, multiplier=3,
    )

    upper = struct.pack("<H", MAX_PLAUSIBLE_CA_DELTA)
    lower = struct.pack("<h", -MAX_PLAUSIBLE_CA_DELTA)
    assert b"\x66\x81\xFF" + upper in fm26
    assert b"\x66\x81\xFF" + lower in fm26
    assert b"\x66\x41\x81\xFE" + upper in fm24
    assert b"\x66\x41\x81\xFE" + lower in fm24


def test_layout_without_fixed_rva_uses_strict_dynamic_hook_resolution() -> None:
    controller = CAGrowthHookController()
    layout = SimpleNamespace(
        ca_growth_hook_rva=None,
        key="fm24",
    )

    with (
        patch(
            "tools.ca_growth_hook.select_process_layout",
            return_value=(42, "fm.exe", layout),
        ),
        patch.object(controller, "_install") as install,
    ):
        status = controller.sync(True, 0x9000, 0xA000, 3)

    assert status["installed"] is False
    assert status["error"] is None
    install.assert_called_once_with(
        42, "fm.exe", layout, 0x9000, 0xA000, 3, 0,
    )


def test_fm26_national_only_scope_can_install() -> None:
    controller = CAGrowthHookController()
    layout = SimpleNamespace(key="fm26")

    with (
        patch(
            "tools.ca_growth_hook.select_process_layout",
            return_value=(42, "fm.exe", layout),
        ),
        patch.object(controller, "_install") as install,
    ):
        status = controller.sync(
            True, 0, 0, 2, national_team_address=0xB000,
        )

    assert status["error"] is None
    install.assert_called_once_with(
        42, "fm.exe", layout, 0, 0, 2, 0xB000,
    )


def test_switch_to_dynamic_layout_uninstalls_existing_hook_first() -> None:
    controller = CAGrowthHookController()
    controller.pid = 24
    controller.hook = 0x71000000
    controller.layout_key = "fm24"
    controller.team_address = 0x9000
    controller.club_address = 0xA000
    controller.multiplier = 3
    layout = SimpleNamespace(ca_growth_hook_rva=None, key="fm24")

    with (
        patch(
            "tools.ca_growth_hook.select_process_layout",
            return_value=(42, "fm.exe", layout),
        ),
        patch.object(
            controller, "_uninstall", side_effect=controller._clear,
        ) as uninstall,
        patch.object(controller, "_install") as install,
    ):
        status = controller.sync(True, 0x9000, 0xA000, 3)

    uninstall.assert_called_once_with()
    install.assert_called_once()
    assert status["installed"] is False
    assert status["error"] is None


def test_enabled_hook_requires_current_team_context() -> None:
    controller = CAGrowthHookController()

    with patch("tools.ca_growth_hook.select_process_layout") as select:
        status = controller.sync(True, 0, 0, 2)

    select.assert_not_called()
    assert status["installed"] is False
    assert status["error_code"] == "team_context_missing"
    assert "刷新" in status["error"]


def test_dynamic_hook_resolution_requires_one_complete_signature() -> None:
    controller = CAGrowthHookController()
    process = SimpleNamespace(handle=1)
    base = 0x50000000
    payload = b"\x90" * 23 + FM26_SIGNATURE + b"\x90" * 17
    module = SimpleNamespace(base_address=base, size=len(payload))

    with patch(
        "tools.ca_growth_hook.read_process_memory",
        side_effect=lambda _process, address, size: payload[address - base:address - base + size],
    ):
        hook, original = controller._resolve_hook_entry(
            process, module,
            SimpleNamespace(key="fm26", ca_growth_hook_rva=None),
        )

    assert hook == base + 23
    assert original == HOOK_ORIGINAL


def test_dynamic_hook_resolution_rejects_ambiguous_signature_without_raw_entry_error() -> None:
    controller = CAGrowthHookController()
    process = SimpleNamespace(handle=1)
    base = 0x50000000
    payload = FM24_SIGNATURE + b"\x90" * 8 + FM24_SIGNATURE
    module = SimpleNamespace(base_address=base, size=len(payload))

    with patch(
        "tools.ca_growth_hook.read_process_memory",
        side_effect=lambda _process, address, size: payload[address - base:address - base + size],
    ):
        try:
            controller._resolve_hook_entry(
                process, module,
                SimpleNamespace(key="fm24", ca_growth_hook_rva=None),
            )
        except RuntimeError as error:
            assert "CA 入口" not in str(error)
            assert "食堂成长加速" in str(error)
        else:
            raise AssertionError("expected ambiguous signature rejection")


def test_dynamic_hook_resolution_recovers_a_stale_fmodd_patch() -> None:
    controller = CAGrowthHookController()
    process = SimpleNamespace(handle=1)
    base = 0x50000000
    cave = 0x60000000
    hook = base + 32
    entry = b"\xE9" + _rel32(hook + 5, cave) + b"\x90" * 3
    payload = b"\x90" * 32 + entry + FM26_SIGNATURE[8:] + b"\x90" * 16
    module = SimpleNamespace(base_address=base, size=len(payload))

    def read(_process, address, size):
        if address == cave:
            return CODE_PREFIX[:size]
        offset = address - base
        if 0 <= offset < len(payload):
            return payload[offset:offset + size]
        return None

    with patch("tools.ca_growth_hook.read_process_memory", side_effect=read):
        resolved, original = controller._resolve_hook_entry(
            process, module,
            SimpleNamespace(key="fm26", ca_growth_hook_rva=None),
        )

    assert resolved == hook
    assert original == HOOK_ORIGINAL


def test_disabling_ca_cleans_orphaned_hook_after_service_restart() -> None:
    controller = CAGrowthHookController()

    with patch.object(controller, "_remove_orphaned_hook") as cleanup:
        status = controller.sync(False, 0, 0, 3)

    cleanup.assert_called_once_with()
    assert status["installed"] is False


def test_orphan_cleanup_restores_only_recognized_fmodd_code() -> None:
    controller = CAGrowthHookController()
    process = SimpleNamespace(handle=1)
    module = SimpleNamespace(base_address=0x70000000)
    layout = SimpleNamespace(
        ca_growth_hook_rva=0x100000,
        key="fm26",
        module=Mock(return_value=module),
    )
    hook = module.base_address + layout.ca_growth_hook_rva
    cave = 0x72000000
    team = 0x123456789ABC
    entry = b"\xE9" + _rel32(hook + 5, cave) + b"\x90" * 3
    code = _build_code(cave, HOOK_ORIGINAL, hook + 8, team, multiplier=3)
    restored = False

    def read(_process, address, size):
        if address == hook:
            return HOOK_ORIGINAL if restored else entry
        if address == cave:
            return code[:size]
        return None

    def write_code(_process, address, data):
        nonlocal restored
        assert address == hook
        assert data == HOOK_ORIGINAL
        restored = True

    with (
        patch(
            "tools.ca_growth_hook.select_process_layout",
            return_value=(42, "fm.exe", layout),
        ),
        patch("tools.ca_growth_hook.open_process", return_value=nullcontext(process)),
        patch("tools.ca_growth_hook.read_process_memory", side_effect=read),
        patch.object(controller, "_write_code", side_effect=write_code) as write,
        patch("tools.ca_growth_hook.kernel32.VirtualFreeEx") as free,
        patch("tools.ca_growth_hook.time.sleep"),
    ):
        controller._remove_orphaned_hook()

    write.assert_called_once()
    free.assert_called_once()
    assert restored is True


def test_install_reads_only_the_verified_hook_entry() -> None:
    controller = CAGrowthHookController()
    process = SimpleNamespace(handle=1)
    module = SimpleNamespace(base_address=0x10000000)
    layout = SimpleNamespace(
        ca_growth_hook_rva=0x2000,
        key="fm26",
        module_name="game_plugin.dll",
        module=Mock(return_value=module),
    )
    hook = module.base_address + layout.ca_growth_hook_rva
    cave = 0x20000000

    with (
        patch("tools.ca_growth_hook.open_process", return_value=nullcontext(process)),
        patch("tools.ca_growth_hook.read_process_memory", return_value=HOOK_ORIGINAL) as read,
        patch("tools.ca_growth_hook.write_process_memory") as write,
        patch.object(controller, "_allocate_near", return_value=cave),
        patch.object(controller, "_write_code") as write_code,
    ):
        controller._install(42, "ignored.exe", layout, 0x9000, 0, 2)

    assert read.call_args_list == [
        ((process, hook, len(HOOK_ORIGINAL)),),
        ((process, hook, len(HOOK_ORIGINAL)),),
    ]
    assert write.call_args.args[1] == cave
    assert write_code.call_args.args[1] == hook
    assert controller.hook == hook


def test_foreign_entry_hook_returns_actionable_message_without_memory_terms() -> None:
    controller = CAGrowthHookController()
    process = SimpleNamespace(handle=1)
    module = SimpleNamespace(base_address=0x10000000)
    layout = SimpleNamespace(
        ca_growth_hook_rva=0x2000,
        key="fm26",
        module_name="game_plugin.dll",
        module=Mock(return_value=module),
    )
    hook = module.base_address + layout.ca_growth_hook_rva
    foreign_patch = b"\xE9\x10\x00\x00\x00\x90\x90\x90"

    with (
        patch("tools.ca_growth_hook.open_process", return_value=nullcontext(process)),
        patch("tools.ca_growth_hook.read_process_memory", return_value=foreign_patch),
        patch.object(controller, "_adopt_or_remove_existing", return_value=False),
    ):
        try:
            controller._install(42, "ignored.exe", layout, 0x9000, 0, 2)
        except RuntimeError as error:
            assert "其他修改工具" in str(error)
            assert "CA 入口" not in str(error)
            assert "原字节" not in str(error)
        else:
            raise AssertionError("expected foreign hook conflict")


def test_existing_legacy_hook_is_adopted_for_the_same_team() -> None:
    controller = CAGrowthHookController()
    process = SimpleNamespace(handle=1)
    hook = 0x71000000
    cave = 0x70000000
    team = 0x9000
    entry = b"\xE9" + _rel32(hook + 5, cave) + b"\x90" * 3
    legacy = _build_code(cave, HOOK_ORIGINAL, hook + 8, team, marker=b"")

    with patch(
        "tools.ca_growth_hook.read_process_memory",
        side_effect=lambda _process, address, size: (
            legacy[:size] if address == cave else None
        ),
    ):
        adopted = controller._adopt_or_remove_existing(
            process, 42, "fm26", hook, entry, HOOK_ORIGINAL, team, 0, 2,
        )

    assert adopted is True
    assert controller.pid == 42
    assert controller.hook == hook
    assert controller.cave == cave
    assert controller.team_address == team


def test_existing_fm26_hook_without_telemetry_is_replaced() -> None:
    controller = CAGrowthHookController()
    process = SimpleNamespace(handle=1)
    hook = 0x71000000
    cave = 0x70000000
    team = 0x9000
    entry = b"\xE9" + _rel32(hook + 5, cave) + b"\x90" * 3
    old_code = _build_code(
        cave, HOOK_ORIGINAL, hook + 8, team, multiplier=2,
        telemetry=False,
    )

    with (
        patch(
            "tools.ca_growth_hook.read_process_memory",
            side_effect=lambda _process, address, size: (
                old_code[:size] + b"\x00" * max(0, size - len(old_code))
                if address == cave else None
            ),
        ),
        patch.object(controller, "_write_code") as write_code,
        patch("tools.ca_growth_hook.kernel32.VirtualFreeEx") as free,
        patch("tools.ca_growth_hook.time.sleep"),
    ):
        adopted = controller._adopt_or_remove_existing(
            process, 42, "fm26", hook, entry, HOOK_ORIGINAL, team, 0, 2,
        )

    assert adopted is False
    write_code.assert_called_once_with(process, hook, HOOK_ORIGINAL)
    free.assert_called_once()


def test_existing_single_scope_fm26_hook_is_replaced() -> None:
    controller = CAGrowthHookController()
    process = SimpleNamespace(handle=1)
    hook = 0x71000000
    cave = 0x70000000
    team = 0x9000
    entry = b"\xE9" + _rel32(hook + 5, cave) + b"\x90" * 3
    old_code = _build_code(
        cave, HOOK_ORIGINAL, hook + 8, team,
        multiplier=2, dual_scope=False,
    )

    with (
        patch(
            "tools.ca_growth_hook.read_process_memory",
            side_effect=lambda _process, address, size: (
                old_code[:size] + b"\x00" * max(0, size - len(old_code))
                if address == cave else None
            ),
        ),
        patch.object(controller, "_write_code") as write_code,
        patch("tools.ca_growth_hook.kernel32.VirtualFreeEx") as free,
        patch("tools.ca_growth_hook.time.sleep"),
    ):
        adopted = controller._adopt_or_remove_existing(
            process, 42, "fm26", hook, entry, HOOK_ORIGINAL,
            team, 0, 2, 0xA000,
        )

    assert adopted is False
    write_code.assert_called_once_with(process, hook, HOOK_ORIGINAL)
    free.assert_called_once()


def test_stale_three_times_zero_delta_hook_is_removed_not_adopted() -> None:
    controller = CAGrowthHookController()
    process = SimpleNamespace(handle=1)
    hook = 0x71000000
    cave = 0x70000000
    team = 0x9000
    entry = b"\xE9" + _rel32(hook + 5, cave) + b"\x90" * 3
    stale = _build_code(
        cave, HOOK_ORIGINAL, hook + 8, team,
        multiplier=3, zero_increment=1,
    )

    with (
        patch(
            "tools.ca_growth_hook.read_process_memory",
            side_effect=lambda _process, address, size: (
                stale[:size] if address == cave else None
            ),
        ),
        patch.object(controller, "_write_code") as write_code,
        patch("tools.ca_growth_hook.kernel32.VirtualFreeEx") as free,
        patch("tools.ca_growth_hook.time.sleep"),
    ):
        adopted = controller._adopt_or_remove_existing(
            process, 42, "fm26", hook, entry, HOOK_ORIGINAL, team, 0, 3,
        )

    assert adopted is False
    write_code.assert_called_once_with(process, hook, HOOK_ORIGINAL)
    free.assert_called_once()


def test_unbounded_v2_hook_is_removed_before_installing_guarded_code() -> None:
    controller = CAGrowthHookController()
    process = SimpleNamespace(handle=1)
    hook = 0x71000000
    cave = 0x70000000
    team = 0x9000
    entry = b"\xE9" + _rel32(hook + 5, cave) + b"\x90" * 3
    unbounded = _build_code(
        cave, HOOK_ORIGINAL, hook + 8, team,
        multiplier=3, marker=LEGACY_CODE_PREFIX, bounded_delta=False,
    )

    with (
        patch(
            "tools.ca_growth_hook.read_process_memory",
            side_effect=lambda _process, address, size: (
                unbounded[:size] if address == cave else None
            ),
        ),
        patch.object(controller, "_write_code") as write_code,
        patch("tools.ca_growth_hook.kernel32.VirtualFreeEx") as free,
        patch("tools.ca_growth_hook.time.sleep"),
    ):
        adopted = controller._adopt_or_remove_existing(
            process, 42, "fm26", hook, entry, HOOK_ORIGINAL, team, 0, 3,
        )

    assert adopted is False
    write_code.assert_called_once_with(process, hook, HOOK_ORIGINAL)
    free.assert_called_once()
