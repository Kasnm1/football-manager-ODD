from __future__ import annotations

import struct
from contextlib import nullcontext
from types import SimpleNamespace

from tools.game_layout import (
    FM24_240_LAYOUT,
    FM24_241_LAYOUT,
    FM24_EPIC_LAYOUT,
    FM24_LAYOUT,
    FM24_XGP_LAYOUT,
    FM26_LAYOUT,
    FM26_XGP_TEMPLATE,
)
from tools.youth_generation_hook import (
    ALLOCATION_SIZE,
    FM24_SON_ORIGINAL,
    FM26_CAPTURE_ORIGINAL,
    FM26_PA_ORIGINAL,
    FM26_SON_ORIGINAL,
    GATE_ORIGINAL,
    LAST_CAPTURED_CLUB_OFFSET,
    LAST_PA_CLUB_OFFSET,
    LAST_SON_CLUB_OFFSET,
    MARKER,
    MARKER_OFFSET,
    PA_ORIGINAL,
    QUALITY_CAPTURE_HITS_OFFSET,
    QUALITY_MATCH_HITS_OFFSET,
    QUALITY_PA_HITS_OFFSET,
    RUNTIME_OFFSET,
    SON_APPLIED_OFFSET,
    SON_HITS_OFFSET,
    SON_MATCH_HITS_OFFSET,
    SON_PENDING_OFFSET,
    SON_REMAINING_OFFSET,
    SON_TARGET_OFFSET,
    STATE_ORIGINAL,
    TABLE_OFFSET,
    YouthGenerationHookController,
    _build_fm26_capture_code,
    _build_fm26_pa_code,
    _build_gate_code,
    _build_pa_code,
    _build_son_code,
    _build_state_code,
    _pattern_matches,
)
from tools import youth_generation_hook


def _rel32_target(code: bytes, opcode_offset: int, instruction_address: int) -> int:
    displacement = struct.unpack_from("<i", code, opcode_offset + 1)[0]
    return instruction_address + opcode_offset + 5 + displacement


def test_only_verified_builds_expose_fixed_youth_generation_hooks() -> None:
    assert FM24_LAYOUT.youth_generation_gate_hook_rva == 0x217CB60
    assert FM24_LAYOUT.youth_generation_pa_hook_rva == 0x21271A2
    assert FM24_LAYOUT.youth_generation_state_hook_rva == 0x36238D5
    assert FM24_LAYOUT.youth_son_hook_rva == 0x3618E59
    assert FM26_LAYOUT.youth_generation_gate_hook_rva == 0xEE5F2E
    assert FM26_LAYOUT.youth_generation_pa_hook_rva == 0x1711767
    assert FM26_LAYOUT.youth_generation_state_hook_rva is None
    assert FM26_LAYOUT.youth_son_hook_rva == 0x2D05180
    assert FM24_EPIC_LAYOUT.youth_generation_gate_hook_rva == 0x217CB60
    assert FM24_EPIC_LAYOUT.youth_generation_pa_hook_rva == 0x21271A2
    assert FM24_EPIC_LAYOUT.youth_generation_state_hook_rva == 0x36238D5
    assert FM24_EPIC_LAYOUT.youth_son_hook_rva == 0x3618E59
    for layout in (
        FM24_240_LAYOUT, FM24_241_LAYOUT, FM24_XGP_LAYOUT,
        FM26_XGP_TEMPLATE,
    ):
        assert layout.youth_generation_gate_hook_rva is None
        assert layout.youth_generation_pa_hook_rva is None
        assert layout.youth_generation_state_hook_rva is None
        assert layout.youth_son_hook_rva is None


def test_hook_status_declares_native_first_business_priority() -> None:
    status = YouthGenerationHookController().status()

    assert status["priority"] == "native_first"
    assert status["completion_authority"] == "final_roster_reconcile"


def test_gate_code_preserves_original_and_returns_after_six_bytes() -> None:
    cave = 0x180000000
    return_address = 0x14217CB66
    code = _build_gate_code(cave, GATE_ORIGINAL, return_address)
    assert code.startswith(GATE_ORIGINAL)
    assert len(code) < 0x200
    assert code[-5] == 0xE9
    assert _rel32_target(code, len(code) - 5, cave) == return_address
    assert b"\xC6\x42\x33\x01" in code


def test_fm24_pa_code_writes_bounds_and_consumes_one_paid_slot() -> None:
    cave = 0x180000200
    return_address = 0x1421271AA
    code = _build_pa_code(cave, PA_ORIGINAL, return_address)
    assert len(code) < 0x200
    assert b"\x66\x44\x8B\x4B\x08" in code
    assert b"\x66\x44\x8B\x53\x0A" in code
    assert b"\xFF\x4B\x0C" in code
    assert b"\x66\x44\x8B\x48\x16" in code
    assert b"\x66\x44\x8B\x50\x16" in code
    assert struct.pack("<Q", 0x180000000 + SON_PENDING_OFFSET) in code
    assert PA_ORIGINAL in code
    assert code[-5] == 0xE9
    assert _rel32_target(code, len(code) - 5, cave) == return_address


def test_state_code_reproduces_trainer_state_normalization() -> None:
    cave = 0x180000400
    return_address = 0x1436238DA
    code = _build_state_code(cave, STATE_ORIGINAL, return_address)
    assert b"\x41\x80\x7E\x33\x09" in code
    assert b"\x41\xC6\x46\x33\x01" in code
    assert b"\x49\x8B\x46\x10" in code
    assert struct.pack("<Q", 0x180000000 + SON_TARGET_OFFSET) in code
    assert STATE_ORIGINAL + b"\x24\xFE" in code
    assert code[-5] == 0xE9
    assert _rel32_target(code, len(code) - 5, cave) == return_address


def test_fm26_quality_code_observes_match_without_replacing_native_pa() -> None:
    capture_cave = 0x180000000
    capture_return = 0x140EE5F33
    capture = _build_fm26_capture_code(
        capture_cave, FM26_CAPTURE_ORIGINAL, capture_return,
    )
    assert capture.startswith(FM26_CAPTURE_ORIGINAL)
    assert b"\x4C\x89\x30" in capture
    assert struct.pack("<Q", 0x180000000 + QUALITY_CAPTURE_HITS_OFFSET) in capture
    assert struct.pack("<Q", 0x180000000 + LAST_CAPTURED_CLUB_OFFSET) in capture
    assert _rel32_target(capture, len(capture) - 5, capture_cave) == capture_return

    pa_cave = 0x180000200
    pa_return = 0x14171176E
    pa = _build_fm26_pa_code(pa_cave, FM26_PA_ORIGINAL, pa_return)
    assert pa.startswith(FM26_PA_ORIGINAL)
    assert b"\x66\x44\x89\x0A" not in pa
    assert b"\x66\x45\x89\x10" not in pa
    assert pa.count(b"\x66\xC7\x06\xF6\xFF") == 1
    assert b"\xFF\x4B\x0C" not in pa
    assert b"\x0F\xB7\x4B\x16" in pa
    assert struct.pack("<Q", 0x180000000 + SON_PENDING_OFFSET) in pa
    assert struct.pack("<Q", 0x180000000 + QUALITY_PA_HITS_OFFSET) in pa
    assert struct.pack("<Q", 0x180000000 + QUALITY_MATCH_HITS_OFFSET) in pa
    assert struct.pack("<Q", 0x180000000 + LAST_PA_CLUB_OFFSET) in pa
    assert _rel32_target(pa, len(pa) - 5, pa_cave) == pa_return


def test_son_code_uses_native_mode_nine_at_saved_random_slot() -> None:
    for game_key, original in (
        ("fm24", FM24_SON_ORIGINAL), ("fm26", FM26_SON_ORIGINAL),
    ):
        cave = 0x180000C00
        return_address = 0x143618E60
        code = _build_son_code(
            cave, original, return_address, game_key=game_key,
        )
        assert code.startswith(original)
        assert b"\x66\x44\x39\x28" in code
        assert b"\x40\xB6\x09" in code
        assert b"\x66\x41\x83\xFD\x00" not in code
        assert struct.pack("<Q", 0x180000000 + SON_PENDING_OFFSET) in code
        assert struct.pack("<Q", 0x180000000 + SON_APPLIED_OFFSET) in code
        assert struct.pack("<Q", 0x180000000 + SON_HITS_OFFSET) in code
        assert struct.pack("<Q", 0x180000000 + SON_MATCH_HITS_OFFSET) in code
        assert struct.pack("<Q", 0x180000000 + LAST_SON_CLUB_OFFSET) in code
        assert _rel32_target(code, len(code) - 5, cave) == return_address


def test_adaptive_pattern_matching_supports_variable_fm24_displacements() -> None:
    pattern = youth_generation_hook.FM24_ADAPTIVE_PATTERNS["son"][0]
    sample = bytes.fromhex(
        "90 E8 01 02 03 04 48 89 85 E8 00 00 00 48 8B 47 18 48 3B 47 20 90"
    )
    assert _pattern_matches(sample, pattern) == [1]


def test_fm24_non_steam_layout_uses_adaptive_hook_resolution(monkeypatch) -> None:
    controller = YouthGenerationHookController()
    layout = SimpleNamespace(
        key="fm24", distribution="epic",
        youth_generation_gate_hook_rva=None,
        youth_generation_pa_hook_rva=None,
        youth_generation_state_hook_rva=None,
        youth_son_hook_rva=None,
    )
    monkeypatch.setattr(
        youth_generation_hook, "select_process_layout", lambda: (7, "fm.exe", layout),
    )
    monkeypatch.setattr(controller, "_resolve_targets", lambda *_args: ([{}], None))
    monkeypatch.setattr(controller, "_adopt_existing", lambda *_args: False)

    def install(*_args, **_kwargs):
        controller.hooks = {"quality_gate": 1, "quality_pa": 2}

    monkeypatch.setattr(controller, "_install", install)
    monkeypatch.setattr(controller, "_write_targets", lambda *_args: None)

    status = controller.sync([{"kind": "golden_generation"}])

    assert status["tracked"] is True
    assert status["error"] is None


def test_quality_hook_desired_names_exclude_legacy_fm24_state_hook() -> None:
    controller = YouthGenerationHookController()
    fm24 = SimpleNamespace(key="fm24")
    fm26 = SimpleNamespace(key="fm26")

    assert controller._desired_names(fm24, quality=True, son=False) == {
        "quality_gate", "quality_pa",
    }
    assert controller._desired_names(fm26, quality=True, son=False) == {
        "quality_capture", "quality_pa",
    }
    assert "quality_state" not in controller._desired_names(
        fm24, quality=True, son=True,
    )


def test_target_table_is_written_as_bytes(monkeypatch) -> None:
    controller = YouthGenerationHookController()
    controller.pid = 1
    controller.cave = 0x100000
    memory = bytearray(ALLOCATION_SIZE)

    def read_memory(_process, address, size):
        offset = address - controller.cave
        return bytes(memory[offset:offset + size])

    def write_memory(_process, address, data):
        assert isinstance(data, bytes)
        offset = address - controller.cave
        memory[offset:offset + len(data)] = data

    monkeypatch.setattr(youth_generation_hook, "open_process", lambda *_args, **_kwargs: nullcontext(object()))
    monkeypatch.setattr(youth_generation_hook, "read_process_memory", read_memory)
    monkeypatch.setattr(youth_generation_hook, "write_process_memory", write_memory)

    controller._write_targets([{
        "team_id": 42, "club_address": 0x12345678,
        "min_pa": 130, "max_pa": 140, "count": 2, "token": 7,
    }], {
        "kind": "academy_son", "team_id": 77,
        "club_address": 0x87654321, "count": 1, "token": 9,
        "ca": 97, "pa": 177, "slot": 6,
    })

    count = struct.unpack_from("<I", memory, TABLE_OFFSET)[0]
    row = struct.unpack_from("<QHHIII", memory, TABLE_OFFSET + 8)
    assert count == 1
    assert row == (0x12345678, 130, 140, 2, 42, 7)
    assert controller.targets[0]["remaining"] == 2
    son = struct.unpack_from("<QIIIIQ", memory, SON_TARGET_OFFSET)
    assert son == (0x87654321, 77, 9, 6, (177 << 16) | 97, 0)
    assert controller.son_target["team_id"] == 77
    assert struct.unpack_from("<I", memory, SON_REMAINING_OFFSET)[0] == 6


def test_resolve_targets_relocates_plan_by_team_uid(monkeypatch) -> None:
    controller = YouthGenerationHookController()
    module = SimpleNamespace(base_address=0x100000)
    layout = SimpleNamespace(module=lambda _process: module)
    process = SimpleNamespace()
    calls = []

    monkeypatch.setattr(
        youth_generation_hook, "open_process",
        lambda *_args, **_kwargs: nullcontext(process),
    )
    monkeypatch.setattr(
        youth_generation_hook, "Reader",
        lambda *_args: SimpleNamespace(),
    )

    def resolve(_reader, team_address, team_id):
        calls.append((team_address, team_id))
        return SimpleNamespace(team_address=0x5000, club_address=0x6000)

    monkeypatch.setattr(youth_generation_hook, "resolve_team_club", resolve)

    targets, son_target = controller._resolve_targets(
        1, layout, [{
            "team_id": 42, "team_address": hex(0x1000),
            "kind": "golden_generation", "min_pa": 170,
            "max_pa": 180, "count": 2, "token": 9,
        }],
    )

    assert calls == [(0x1000, 42)]
    assert targets == [{
        "team_id": 42, "club_address": 0x6000, "count": 2,
        "token": 9, "kind": "golden_generation", "min_pa": 170,
        "max_pa": 180,
    }]
    assert son_target is None


def test_resolve_targets_uses_session_reader_for_uid_directory(monkeypatch) -> None:
    controller = YouthGenerationHookController()
    module = SimpleNamespace(base_address=0x100000)
    layout = SimpleNamespace(module=lambda _process: module)
    session_reader = SimpleNamespace(
        module=module, process=SimpleNamespace(name="session-process"),
    )

    monkeypatch.setattr(
        youth_generation_hook, "borrow_game_reader",
        lambda selected: (
            selected == (7, "fm.exe", layout)
            and nullcontext(session_reader)
            or pytest.fail("应借用当前游戏读取会话")
        ),
    )
    monkeypatch.setattr(
        youth_generation_hook, "open_process",
        lambda *_args, **_kwargs: pytest.fail("有会话时不应重新打开裸 Reader"),
    )

    def resolve(reader, team_address, team_id):
        assert reader is session_reader
        assert (team_address, team_id) == (0x1000, 42)
        return SimpleNamespace(team_address=0x5000, club_address=0x6000)

    monkeypatch.setattr(youth_generation_hook, "resolve_team_club", resolve)

    targets, son_target = controller._resolve_targets(
        7, layout, [{
            "team_id": 42, "team_address": hex(0x1000),
            "kind": "golden_generation", "min_pa": 170,
            "max_pa": 180, "count": 2, "token": 9,
        }], "fm.exe",
    )

    assert targets[0]["club_address"] == 0x6000
    assert son_target is None


def test_son_hook_bounds_cover_random_ability_tiers() -> None:
    assert (youth_generation_hook.SON_CA_MIN, youth_generation_hook.SON_CA_MAX) == (50, 150)
    assert (youth_generation_hook.SON_PA_MIN, youth_generation_hook.SON_PA_MAX) == (80, 200)


def test_status_reports_hook_stage_counters_and_last_club_addresses(monkeypatch) -> None:
    controller = YouthGenerationHookController()
    controller.pid = 1
    controller.cave = 0x100000
    controller.game_key = "fm26"
    controller.hooks = {"quality_capture": 0x200000, "quality_pa": 0x200100}
    controller.patches = {"quality_capture": b"abcde", "quality_pa": b"vwxyz"}
    memory = bytearray(ALLOCATION_SIZE)
    runtime_values = {
        RUNTIME_OFFSET: 0,
        RUNTIME_OFFSET + 8: 0xAAA0,
        RUNTIME_OFFSET + 16: 2,
        QUALITY_CAPTURE_HITS_OFFSET: 5,
        QUALITY_PA_HITS_OFFSET: 4,
        QUALITY_MATCH_HITS_OFFSET: 2,
        SON_HITS_OFFSET: 3,
        SON_MATCH_HITS_OFFSET: 1,
        LAST_CAPTURED_CLUB_OFFSET: 0xAAA0,
        LAST_PA_CLUB_OFFSET: 0xAAA0,
        LAST_SON_CLUB_OFFSET: 0xBBB0,
        SON_APPLIED_OFFSET: 1,
        SON_PENDING_OFFSET: 1,
    }
    for offset, value in runtime_values.items():
        struct.pack_into("<Q", memory, offset, value)
    memory[MARKER_OFFSET:MARKER_OFFSET + len(MARKER)] = MARKER

    def read_memory(_process, address, size):
        if address == 0x200000:
            return b"abcde"[:size]
        if address == 0x200100:
            return b"vwxyz"[:size]
        offset = address - controller.cave
        return bytes(memory[offset:offset + size])

    monkeypatch.setattr(
        youth_generation_hook, "open_process",
        lambda *_args, **_kwargs: nullcontext(object()),
    )
    monkeypatch.setattr(youth_generation_hook, "read_process_memory", read_memory)

    status = controller.status()

    assert status["installed"] is True
    assert status["verification"] == "runtime_verified"
    assert status["applied_count"] == 2
    assert status["son_applied_count"] == 1
    assert status["son_pending"] is True
    assert status["hooks"]["quality_capture"]["patched"] is True
    assert status["diagnostics"] == {
        "quality_capture_hits": 5,
        "quality_pa_hits": 4,
        "quality_match_hits": 2,
        "son_hits": 3,
        "son_match_hits": 1,
        "last_captured_club": "0xaaa0",
        "last_pa_club": "0xaaa0",
        "last_son_club": "0xbbb0",
        "active_calls": 0,
        "selected_club": "0xaaa0",
    }


def test_son_quality_reconciliation_only_clears_pending_marker(monkeypatch) -> None:
    controller = YouthGenerationHookController()
    controller.pid = 1
    controller.cave = 0x100000
    controller.targets = [{
        "team_id": 42, "club_address": 0x12345678,
        "min_pa": 190, "max_pa": 200, "count": 2, "token": 7,
        "remaining": 1,
    }]
    controller.son_target = {
        "team_id": 42, "club_address": 0x12345678,
        "token": 9, "ca": 95, "pa": 180,
    }
    memory = bytearray(ALLOCATION_SIZE)
    struct.pack_into("<I", memory, TABLE_OFFSET, 1)
    struct.pack_into(
        "<QHHIII", memory, TABLE_OFFSET + 8,
        0x12345678, 190, 200, 1, 42, 7,
    )
    struct.pack_into("<Q", memory, SON_APPLIED_OFFSET, 1)
    struct.pack_into("<Q", memory, SON_PENDING_OFFSET, 1)

    def read_memory(_process, address, size):
        offset = address - controller.cave
        return bytes(memory[offset:offset + size])

    def write_memory(_process, address, data):
        offset = address - controller.cave
        memory[offset:offset + len(data)] = data

    monkeypatch.setattr(
        youth_generation_hook, "open_process",
        lambda *_args, **_kwargs: nullcontext(object()),
    )
    monkeypatch.setattr(youth_generation_hook, "read_process_memory", read_memory)
    monkeypatch.setattr(youth_generation_hook, "write_process_memory", write_memory)

    result = controller.reconcile_son_quality()

    assert result == {
        "reconciled": True, "pending": False, "compensated": False,
    }
    assert struct.unpack_from("<I", memory, TABLE_OFFSET + 20)[0] == 1
    assert struct.unpack_from("<Q", memory, SON_PENDING_OFFSET)[0] == 0
    assert controller.targets[0]["remaining"] == 1


def test_first_install_failure_uninstalls_partial_hook(monkeypatch) -> None:
    controller = YouthGenerationHookController()
    layout = SimpleNamespace(
        key="fm24", distribution="steam",
        youth_generation_gate_hook_rva=1,
        youth_generation_pa_hook_rva=2,
        youth_generation_state_hook_rva=3,
        youth_son_hook_rva=4,
    )
    monkeypatch.setattr(
        youth_generation_hook, "select_process_layout", lambda: (1, "fm.exe", layout),
    )
    monkeypatch.setattr(controller, "_resolve_targets", lambda *_args: ([{}], None))
    monkeypatch.setattr(
        controller, "_definitions",
        lambda *_args, **_kwargs: {"quality_gate": (1, b"12345", b"x", 0)},
    )
    monkeypatch.setattr(
        controller, "_desired_names",
        lambda *_args, **_kwargs: {"quality_gate"},
    )
    monkeypatch.setattr(controller, "_adopt_existing", lambda *_args: False)

    def install(*_args, **_kwargs):
        controller.hooks = {"quality_gate": 1}

    uninstalled = []
    monkeypatch.setattr(controller, "_install", install)
    monkeypatch.setattr(controller, "_write_targets", lambda *_args: (_ for _ in ()).throw(TypeError("bad table")))
    monkeypatch.setattr(controller, "_uninstall", lambda: (uninstalled.append(True), controller._clear()))

    status = controller.sync([{"team_id": 42}])

    assert uninstalled == [True]
    assert status["installed"] is False
    assert status["error"] == "bad table"


def test_sync_reinstalls_when_tracked_hook_integrity_is_lost(monkeypatch) -> None:
    controller = YouthGenerationHookController()
    controller.pid = 7
    controller.cave = 0x100000
    controller.hooks = {"quality_gate": 0x200000}
    controller.patches = {"quality_gate": b"patch"}
    controller.originals = {"quality_gate": b"origx"}
    layout = SimpleNamespace(
        key="fm24",
        youth_generation_gate_hook_rva=None,
        youth_generation_pa_hook_rva=None,
        youth_generation_state_hook_rva=None,
        youth_son_hook_rva=None,
    )
    monkeypatch.setattr(
        youth_generation_hook, "select_process_layout",
        lambda: (7, "fm.exe", layout),
    )
    monkeypatch.setattr(controller, "_resolve_targets", lambda *_args: ([{}], None))
    monkeypatch.setattr(controller, "_hooks_intact", lambda _pid: False)
    calls = []

    def uninstall():
        calls.append("uninstall")
        controller._clear()

    def install(*_args, **_kwargs):
        calls.append("install")
        controller.pid = 7
        controller.cave = 0x110000
        controller.hooks = {"quality_gate": 1, "quality_pa": 2}

    monkeypatch.setattr(controller, "_uninstall", uninstall)
    monkeypatch.setattr(controller, "_adopt_existing", lambda *_args: False)
    monkeypatch.setattr(controller, "_install", install)
    monkeypatch.setattr(controller, "_write_targets", lambda *_args: None)
    monkeypatch.setattr(
        controller, "status",
        lambda: {"installed": True, "tracked": True, "error": controller.error},
    )

    status = controller.sync([{"kind": "golden_generation"}])

    assert status["installed"] is True
    assert calls == ["uninstall", "install"]
