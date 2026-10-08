from __future__ import annotations

import struct
from contextlib import nullcontext
from types import SimpleNamespace

from fm_odds_web import managed_acquired_club_ids
from tools import board_listens_hook
from tools.board_listens_hook import (
    CLUB_HOOK_OFFSET,
    CLUB_ORIGINAL,
    CLUB_ORIGINAL_SIZE,
    CLUB_PATTERN,
    GENERAL_HOOK_OFFSET,
    GENERAL_ORIGINAL,
    GENERAL_PATTERN,
    VISION_PATCH,
    VISION_PATCH_OFFSET,
    VISION_PATTERN,
    VISION_ORIGINAL,
    BoardListensHookController,
    FM24_CLUB_ONE_HOOK_OFFSET,
    FM24_CLUB_ONE_PATTERN,
    FM24_CLUB_TWO_HOOK_OFFSET,
    FM24_CLUB_TWO_PATTERN,
    FM24_GENERAL_HOOK_OFFSET,
    FM24_GENERAL_PATTERN,
    FM24_VISION_PATCH_OFFSET,
    FM24_VISION_PATTERN,
    _build_fm24_force_club_two_code,
    _build_fm24_force_state_code,
    _build_club_code,
    _build_general_code,
    _recover_exact_fm24_stale_patches,
    _scan_module_patterns,
)
from tools.game_layout import FM24_EXE_SHA256, FM26_EXE_SHA256


def _rel32_target(code: bytes, opcode_offset: int, instruction_address: int) -> int:
    displacement = struct.unpack_from("<i", code, opcode_offset + 1)[0]
    return instruction_address + opcode_offset + 5 + displacement


def test_general_board_code_calls_original_and_forces_success_state() -> None:
    hook = 0x180100000
    target = 0x180200000
    cave = 0x180110000
    original = b"\xE8" + struct.pack("<i", target - (hook + 5))

    code = _build_general_code(cave, original, hook, hook + 5)

    assert code[0] == 0xE8
    assert _rel32_target(code, 0, cave) == target
    assert b"\xC6\x80\x83\x00\x00\x00\x01" in code
    assert code[-5] == 0xE9
    assert _rel32_target(code, len(code) - 5, cave) == hook + 5


def test_club_board_code_preserves_original_and_unlocks_both_states() -> None:
    cave = 0x180110100
    return_address = 0x180120007
    original = b"\x4C\x8B\x8D\x90\x00\x00\x00"
    assert len(original) == CLUB_ORIGINAL_SIZE

    code = _build_club_code(cave, original, return_address)

    assert code.startswith(original)
    assert b"\x80\xB8\x83\x00\x00\x00\x03" in code
    assert b"\xC6\x80\x83\x00\x00\x00\x01" in code
    assert b"\xC6\x80\x8A\x00\x00\x00\x01" in code
    assert _rel32_target(code, len(code) - 5, cave) == return_address
    assert VISION_ORIGINAL == b"\x78\x7C"


def test_fm24_board_code_rewrites_comparisons_using_trainer_semantics() -> None:
    cave = 0x140100000
    return_address = 0x140200008
    original = b"\x41\x80\xBF\x83\x00\x00\x00\x01"

    forced = _build_fm24_force_state_code(cave, original, return_address)
    club_two = _build_fm24_force_club_two_code(cave, original, return_address)

    assert forced.startswith(b"\x41\xC6\x87\x83\x00\x00\x00\x01\x39\xC0")
    assert _rel32_target(forced, len(forced) - 5, cave) == return_address
    assert club_two.startswith(b"\x41\xC6\x87\x83\x00\x00\x00\x01" + original)
    assert _rel32_target(club_two, len(club_two) - 5, cave) == return_address


def test_pattern_scan_falls_back_to_readable_executable_regions(monkeypatch) -> None:
    module_base = 0x140000000
    module = SimpleNamespace(base_address=module_base, size=0x1000)
    process = SimpleNamespace(handle=1)
    region = SimpleNamespace(
        base_address=module_base + 0x100,
        size=0x200,
        type=board_listens_hook.MEM_IMAGE,
        protect=board_listens_hook.PAGE_EXECUTE_READ,
    )
    data = bytearray(region.size)
    data[0x20:0x20 + len(FM24_GENERAL_PATTERN)] = bytes(
        value or 0 for value in FM24_GENERAL_PATTERN
    )

    def read_memory(_process, address, size):
        if address == module_base and size == module.size:
            return b""
        if address == region.base_address and size == region.size:
            return bytes(data)
        return b""

    monkeypatch.setattr(board_listens_hook, "read_process_memory", read_memory)
    monkeypatch.setattr(board_listens_hook, "iter_readable_regions", lambda _process: [region])

    result = _scan_module_patterns(
        process, module, {"general": FM24_GENERAL_PATTERN},
    )

    assert result == {"general": [0x120]}


def test_exact_fm24_stale_patches_are_restored_before_rescan(monkeypatch) -> None:
    process = SimpleNamespace(handle=1)
    module = SimpleNamespace(base_address=0, size=0x100000000)
    cave = 0x40000000
    memory = {}
    for index, (rva, original) in enumerate(
        zip(board_listens_hook.FM24_VERIFIED_HOOK_RVAS, board_listens_hook.FM24_VERIFIED_ORIGINALS),
    ):
        target = cave + (index * 0x100)
        if len(original) > 2:
            patch = b"\xE9" + struct.pack("<i", target - (rva + 5))
            patch += b"\x90" * (len(original) - 5)
        else:
            patch = board_listens_hook.FM24_VISION_PATCH
        memory[rva] = bytearray(patch)

    def read_memory(_process, address, size):
        return bytes(memory[address][:size])

    def write_memory(_process, address, data):
        memory[address][:len(data)] = data

    freed = []
    monkeypatch.setattr(board_listens_hook, "read_process_memory", read_memory)
    monkeypatch.setattr(board_listens_hook, "kernel32", SimpleNamespace(
        VirtualFreeEx=lambda *_args: freed.append(_args[1].value),
    ))

    _recover_exact_fm24_stale_patches(process, module, write_memory)

    for rva, original in zip(
        board_listens_hook.FM24_VERIFIED_HOOK_RVAS, board_listens_hook.FM24_VERIFIED_ORIGINALS,
    ):
        assert bytes(memory[rva]) == original
    assert freed == [cave]


def test_exact_fm24_board_layout_uses_verified_rvas_without_module_scan(
    monkeypatch,
) -> None:
    module_base = 0x140000000
    process = SimpleNamespace(handle=1)
    module = SimpleNamespace(base_address=module_base, size=0x20000000)
    layout = SimpleNamespace(executable_sha256=FM24_EXE_SHA256)
    memory = {
        module_base + rva: original
        for rva, original in zip(
            board_listens_hook.FM24_VERIFIED_HOOK_RVAS,
            board_listens_hook.FM24_VERIFIED_ORIGINALS,
        )
    }

    monkeypatch.setattr(
        board_listens_hook, "read_process_memory",
        lambda _process, address, size: memory.get(address, b"")[:size],
    )
    monkeypatch.setattr(
        board_listens_hook, "_scan_module_patterns",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("exact build must not scan the full module")
        ),
    )

    resolved = BoardListensHookController._resolve_fm24(
        process, module, layout,
    )

    assert [address for address, _original, _patch in resolved] == [
        module_base + rva for rva in board_listens_hook.FM24_VERIFIED_HOOK_RVAS
    ]
    assert [original for _address, original, _patch in resolved] == list(
        board_listens_hook.FM24_VERIFIED_ORIGINALS
    )

    first_address = module_base + board_listens_hook.FM24_VERIFIED_HOOK_RVAS[0]
    memory[first_address] = b"\x90" * len(memory[first_address])
    try:
        BoardListensHookController._resolve_fm24(process, module, layout)
    except RuntimeError as error:
        assert "fixed hook bytes do not match" in str(error)
    else:
        raise AssertionError("mismatched fixed bytes must reject hook installation")


def test_exact_fm26_stale_patches_are_restored_without_module_scan(monkeypatch) -> None:
    module_base = 0x180000000
    process = SimpleNamespace(handle=1)
    module = SimpleNamespace(base_address=module_base, size=0x20000000)
    cave = 0x170000000
    memory = {}
    targets = (cave, cave + board_listens_hook.CLUB_CAVE_OFFSET)
    for index, (rva, original) in enumerate(zip(
        board_listens_hook.FM26_VERIFIED_HOOK_RVAS,
        board_listens_hook.FM26_VERIFIED_ORIGINALS,
    )):
        address = module_base + rva
        if index < 2:
            patch = b"\xE9" + struct.pack("<i", targets[index] - (address + 5))
            patch += b"\x90" * (len(original) - 5)
        else:
            patch = board_listens_hook.FM24_VISION_PATCH
        memory[address] = bytearray(patch)

    def read_memory(_process, address, size):
        return bytes(memory[address][:size])

    def write_memory(_process, address, data):
        memory[address][:len(data)] = data

    freed = []
    monkeypatch.setattr(board_listens_hook, "read_process_memory", read_memory)
    monkeypatch.setattr(board_listens_hook.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(board_listens_hook, "kernel32", SimpleNamespace(
        VirtualFreeEx=lambda *_args: freed.append(_args[1].value),
    ))

    board_listens_hook._recover_exact_fm26_stale_patches(
        process, module, write_memory,
    )

    for rva, original in zip(
        board_listens_hook.FM26_VERIFIED_HOOK_RVAS,
        board_listens_hook.FM26_VERIFIED_ORIGINALS,
    ):
        assert bytes(memory[module_base + rva]) == original
    assert freed == [cave]


def test_exact_fm26_board_layout_uses_verified_rvas_without_module_scan(
    monkeypatch,
) -> None:
    module_base = 0x180000000
    process = SimpleNamespace(handle=1)
    module = SimpleNamespace(base_address=module_base, size=0x20000000)
    layout = SimpleNamespace(executable_sha256=FM26_EXE_SHA256)
    memory = {
        module_base + rva: original
        for rva, original in zip(
            board_listens_hook.FM26_VERIFIED_HOOK_RVAS,
            board_listens_hook.FM26_VERIFIED_ORIGINALS,
        )
    }
    monkeypatch.setattr(
        board_listens_hook, "read_process_memory",
        lambda _process, address, size: memory.get(address, b"")[:size],
    )
    monkeypatch.setattr(
        board_listens_hook, "_scan_module_patterns",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("exact build must not scan the full module")
        ),
    )

    resolved = BoardListensHookController._resolve_fm26(process, module, layout)

    assert [address for address, _original, _patch in resolved] == [
        module_base + rva for rva in board_listens_hook.FM26_VERIFIED_HOOK_RVAS
    ]


def test_board_hook_requires_current_managed_club_to_be_acquired() -> None:
    output = {
        "managed_teams": [
            {"id": 42, "team_type": "club"},
            {"id": 7, "team_type": "national"},
        ],
    }

    assert managed_acquired_club_ids(output, [{"id": 42}, {"id": 99}]) == {42}
    assert managed_acquired_club_ids(output, [{"id": 99}]) == set()
    assert managed_acquired_club_ids(
        {"managed_team": {"id": 7, "team_type": "national"}}, [{"id": 7}],
    ) == set()


def test_controller_suppresses_repeated_enable_failure_for_same_identity(
    monkeypatch,
) -> None:
    layout = SimpleNamespace(
        key="fm26", distribution="steam", module_name="game_plugin.dll",
        executable_sha256="same-build",
    )
    now = [100.0]
    attempts = []
    monkeypatch.setattr(
        board_listens_hook, "select_process_layout",
        lambda: (77, "fm.exe", layout),
    )
    monkeypatch.setattr(board_listens_hook.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        BoardListensHookController, "_enable",
        lambda _self, pid, _layout: (
            attempts.append(pid),
            (_ for _ in ()).throw(RuntimeError("FM26 board signatures are not unique")),
        )[-1],
    )

    controller = BoardListensHookController()
    first = controller.sync(True)
    second = controller.sync(True)

    assert attempts == [77]
    assert first["retry_suppressed"] is True
    assert second["retry_suppressed"] is True
    assert second["retry_after_seconds"] == 300.0
    assert second["error"] == "FM26 board signatures are not unique"


def test_controller_retries_after_cooldown_or_process_identity_change(
    monkeypatch,
) -> None:
    current = {
        "pid": 77,
        "layout": SimpleNamespace(
            key="fm26", distribution="steam", module_name="game_plugin.dll",
            executable_sha256="same-build",
        ),
    }
    now = [100.0]
    attempts = []
    monkeypatch.setattr(
        board_listens_hook, "select_process_layout",
        lambda: (current["pid"], "fm.exe", current["layout"]),
    )
    monkeypatch.setattr(board_listens_hook.time, "monotonic", lambda: now[0])

    def fail(_self, pid, _layout):
        attempts.append(pid)
        raise RuntimeError("scan failed")

    monkeypatch.setattr(BoardListensHookController, "_enable", fail)
    controller = BoardListensHookController()

    controller.sync(True)
    now[0] += controller.FAILURE_RETRY_SECONDS
    controller.sync(True)
    current["pid"] = 88
    controller.sync(True)

    assert attempts == [77, 77, 88]


def test_disabling_controller_clears_failed_retry_suppression(monkeypatch) -> None:
    layout = SimpleNamespace(
        key="fm26", distribution="steam", module_name="game_plugin.dll",
        executable_sha256="same-build",
    )
    attempts = []
    monkeypatch.setattr(
        board_listens_hook, "select_process_layout",
        lambda: (77, "fm.exe", layout),
    )

    def fail(_self, pid, _layout):
        attempts.append(pid)
        raise RuntimeError("scan failed")

    monkeypatch.setattr(BoardListensHookController, "_enable", fail)
    controller = BoardListensHookController()

    controller.sync(True)
    disabled = controller.sync(False)
    controller.sync(True)

    assert disabled["retry_suppressed"] is False
    assert disabled["error"] is None
    assert attempts == [77, 77]


def test_controller_installs_and_restores_all_three_verified_patches(monkeypatch) -> None:
    module_base = 0x180000000
    image = bytearray(0x1000)

    def place(position: int, pattern: tuple[int | None, ...]) -> None:
        image[position:position + len(pattern)] = bytes(value or 0 for value in pattern)

    general_position = 0x100
    club_position = 0x300
    vision_position = 0x500
    place(general_position, GENERAL_PATTERN)
    place(club_position, CLUB_PATTERN)
    place(vision_position, VISION_PATTERN)
    image[general_position + GENERAL_HOOK_OFFSET:general_position + GENERAL_HOOK_OFFSET + 5] = GENERAL_ORIGINAL
    image[club_position + CLUB_HOOK_OFFSET:club_position + CLUB_HOOK_OFFSET + 7] = CLUB_ORIGINAL
    image[vision_position + VISION_PATCH_OFFSET:vision_position + VISION_PATCH_OFFSET + 2] = VISION_ORIGINAL
    cave = module_base + 0x800
    process = SimpleNamespace(handle=1)
    layout = SimpleNamespace(
        key="fm26", distribution="steam", executable_sha256="DYNAMIC_TEST_LAYOUT",
    )

    def read_memory(_process, address, size):
        offset = address - module_base
        return bytes(image[offset:offset + size])

    def write_memory(_process, address, data):
        offset = address - module_base
        image[offset:offset + len(data)] = data

    monkeypatch.setattr(board_listens_hook, "select_process_layout", lambda: (77, "fm.exe", layout))
    monkeypatch.setattr(board_listens_hook, "open_process", lambda *_args, **_kwargs: nullcontext(process))
    monkeypatch.setattr(
        board_listens_hook, "find_module",
        lambda *_args: SimpleNamespace(base_address=module_base, size=len(image)),
    )
    monkeypatch.setattr(board_listens_hook, "read_process_memory", read_memory)
    monkeypatch.setattr(board_listens_hook, "write_process_memory", write_memory)
    monkeypatch.setattr(BoardListensHookController, "_allocate_near", staticmethod(lambda *_args: cave))
    monkeypatch.setattr(BoardListensHookController, "_write_code", staticmethod(write_memory))
    monkeypatch.setattr(board_listens_hook.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(board_listens_hook.kernel32, "VirtualFreeEx", lambda *_args: 1)

    controller = BoardListensHookController()
    status = controller.sync(True)

    assert status["active"] is True
    assert image[general_position + GENERAL_HOOK_OFFSET] == 0xE9
    assert image[club_position + CLUB_HOOK_OFFSET] == 0xE9
    assert image[
        vision_position + VISION_PATCH_OFFSET:vision_position + VISION_PATCH_OFFSET + 2
    ] == VISION_PATCH

    controller.close()
    assert image[
        general_position + GENERAL_HOOK_OFFSET:general_position + GENERAL_HOOK_OFFSET + 5
    ] == GENERAL_ORIGINAL
    assert image[
        club_position + CLUB_HOOK_OFFSET:club_position + CLUB_HOOK_OFFSET + 7
    ] == CLUB_ORIGINAL
    assert image[
        vision_position + VISION_PATCH_OFFSET:vision_position + VISION_PATCH_OFFSET + 2
    ] == VISION_ORIGINAL


def test_controller_installs_and_restores_fm24_trainer_patches(monkeypatch) -> None:
    module_base = 0x140000000
    image = bytearray(0x3000)

    def place(position: int, pattern: tuple[int | None, ...]) -> None:
        image[position:position + len(pattern)] = bytes(value or 0 for value in pattern)

    general_position = 0x100
    club_one_position = 0x500
    club_two_position = 0x900
    vision_position = 0xD00
    place(general_position, FM24_GENERAL_PATTERN)
    place(club_one_position, FM24_CLUB_ONE_PATTERN)
    place(club_two_position, FM24_CLUB_TWO_PATTERN)
    place(vision_position, FM24_VISION_PATTERN)
    image[
        club_one_position + FM24_CLUB_ONE_HOOK_OFFSET:
        club_one_position + FM24_CLUB_ONE_HOOK_OFFSET + 8
    ] = b"\x41\x80\xBF\x83\x00\x00\x00\x01"
    image[club_two_position:club_two_position + 8] = (
        b"\x41\x80\xBF\x8A\x00\x00\x00\x00"
    )
    image[
        vision_position + FM24_VISION_PATCH_OFFSET:
        vision_position + FM24_VISION_PATCH_OFFSET + 2
    ] = b"\x78\x73"
    originals = {
        general_position: bytes(image[general_position:general_position + 8]),
        club_one_position: bytes(image[
            club_one_position + FM24_CLUB_ONE_HOOK_OFFSET:
            club_one_position + FM24_CLUB_ONE_HOOK_OFFSET + 8
        ]),
        club_two_position: bytes(image[club_two_position:club_two_position + 8]),
        vision_position: b"\x78\x73",
    }
    cave = module_base + 0x2000
    process = SimpleNamespace(handle=1)
    layout = SimpleNamespace(
        key="fm24", distribution="steam", module_name="fm.exe",
        executable_sha256="DYNAMIC_TEST_LAYOUT",
    )

    def read_memory(_process, address, size):
        offset = address - module_base
        return bytes(image[offset:offset + size])

    def write_memory(_process, address, data):
        offset = address - module_base
        image[offset:offset + len(data)] = data

    monkeypatch.setattr(board_listens_hook, "select_process_layout", lambda: (24, "fm.exe", layout))
    monkeypatch.setattr(board_listens_hook, "open_process", lambda *_args, **_kwargs: nullcontext(process))
    monkeypatch.setattr(
        board_listens_hook, "find_module",
        lambda *_args: SimpleNamespace(base_address=module_base, size=len(image)),
    )
    monkeypatch.setattr(board_listens_hook, "read_process_memory", read_memory)
    monkeypatch.setattr(board_listens_hook, "write_process_memory", write_memory)
    monkeypatch.setattr(BoardListensHookController, "_allocate_near", staticmethod(lambda *_args: cave))
    monkeypatch.setattr(BoardListensHookController, "_write_code", staticmethod(write_memory))
    monkeypatch.setattr(board_listens_hook.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(board_listens_hook.kernel32, "VirtualFreeEx", lambda *_args: 1)

    controller = BoardListensHookController()
    status = controller.sync(True)

    assert status["active"] is True
    assert status["game_key"] == "fm24"
    assert image[general_position + FM24_GENERAL_HOOK_OFFSET] == 0xE9
    assert image[club_one_position + FM24_CLUB_ONE_HOOK_OFFSET] == 0xE9
    assert image[club_two_position + FM24_CLUB_TWO_HOOK_OFFSET] == 0xE9
    assert image[
        vision_position + FM24_VISION_PATCH_OFFSET:
        vision_position + FM24_VISION_PATCH_OFFSET + 2
    ] == VISION_PATCH

    controller.close()
    assert image[general_position:general_position + 8] == originals[general_position]
    assert image[
        club_one_position + FM24_CLUB_ONE_HOOK_OFFSET:
        club_one_position + FM24_CLUB_ONE_HOOK_OFFSET + 8
    ] == originals[club_one_position]
    assert image[club_two_position:club_two_position + 8] == originals[club_two_position]
    assert image[
        vision_position + FM24_VISION_PATCH_OFFSET:
        vision_position + FM24_VISION_PATCH_OFFSET + 2
    ] == originals[vision_position]
