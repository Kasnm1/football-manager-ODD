import struct
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tools import game_layout, initial_data_audit
from tools.game_layout import (
    FM26_GAME_DATE_PATTERN, FM26_LAYOUT, FM26_STEAM_PLUGIN_SHA256,
    FM26_XGP_TEMPLATE,
)


class _ProcessContext:
    def __enter__(self):
        return object()

    def __exit__(self, *_args):
        return None


def test_fm26_steam_rejects_mixed_game_plugin_build():
    layout = SimpleNamespace(
        key="fm26", distribution="steam", module_name="game_plugin.dll",
    )
    module = SimpleNamespace(
        path="D:/FM/fm_Data/Plugins/x86_64/game_plugin.dll",
        base_address=0, size=0,
    )
    initial_data_audit.invalidate_process_layout_cache()
    with (
        patch.object(initial_data_audit, "select_process", return_value=(37008, "D:/FM/fm.exe")),
        patch.object(initial_data_audit, "layout_for_executable", return_value=layout),
        patch.object(initial_data_audit, "open_process", return_value=_ProcessContext()),
        patch.object(initial_data_audit, "find_module", return_value=module),
        patch.object(
            initial_data_audit, "resolve_fm26_steam_layout",
            side_effect=RuntimeError("game_plugin.dll build mismatch"),
        ),
    ):
        with pytest.raises(RuntimeError, match="game_plugin.dll build mismatch"):
            initial_data_audit.select_process_layout()


def test_selected_fm26_xgp_template_is_resolved_before_connection():
    module = SimpleNamespace(
        path="D:/FM/fm_Data/Plugins/x86_64/game_plugin.dll",
        base_address=0x10000000,
        size=0x1EDF4000,
    )
    resolved = SimpleNamespace(
        key="fm26", distribution="xgp", game_date_rva=0x1234,
    )
    with (
        patch.object(initial_data_audit, "open_process", return_value=_ProcessContext()),
        patch.object(initial_data_audit, "find_module", return_value=module),
        patch.object(
            initial_data_audit, "resolve_fm26_xgp_layout", return_value=resolved,
        ) as resolve,
    ):
        selected = initial_data_audit.resolve_selected_process_layout(
            (37008, "D:/FM/fm.exe", FM26_XGP_TEMPLATE),
        )

    assert selected == (37008, "D:/FM/fm.exe", resolved)
    resolve.assert_called_once_with(
        37008, "D:/FM/fm.exe", module.base_address, module.size, module.path,
    )


def test_select_game_process_never_caches_unresolved_xgp_template():
    resolved = SimpleNamespace(
        key="fm26", distribution="xgp", game_date_rva=0x4D24DD8,
    )
    selected = (37008, "D:/FM/fm.exe", FM26_XGP_TEMPLATE)
    resolved_selected = (37008, "D:/FM/fm.exe", resolved)
    initial_data_audit.invalidate_process_layout_cache()
    initial_data_audit._PREFERRED_LAYOUT_KEY = None
    initial_data_audit._PREFERRED_PROCESS_PID = None

    with (
        patch.object(
            initial_data_audit, "resolve_selected_process_layout",
            return_value=resolved_selected,
        ) as resolve,
        patch("tools.game_session.invalidate_game_read_sessions") as invalidate,
    ):
        changed = initial_data_audit.select_game_process(
            "fm26", 37008, selected=selected,
        )

    assert changed is True
    assert initial_data_audit._PROCESS_LAYOUT_CACHE[3] == resolved_selected
    resolve.assert_called_once_with(selected)
    invalidate.assert_called_once_with()


def test_verified_fm26_steam_plugin_is_accepted():
    layout = SimpleNamespace(
        key="fm26", distribution="steam", module_name="game_plugin.dll",
    )
    module = SimpleNamespace(
        path="D:/FM/fm_Data/Plugins/x86_64/game_plugin.dll",
        base_address=0, size=0,
    )
    initial_data_audit.invalidate_process_layout_cache()
    with (
        patch.object(initial_data_audit, "select_process", return_value=(37008, "D:/FM/fm.exe")),
        patch.object(initial_data_audit, "layout_for_executable", return_value=layout),
        patch.object(initial_data_audit, "open_process", return_value=_ProcessContext()),
        patch.object(initial_data_audit, "find_module", return_value=module),
        patch.object(
            initial_data_audit, "resolve_fm26_steam_layout",
            return_value=layout,
        ) as resolve,
    ):
        assert initial_data_audit.select_process_layout()[2] is layout
    resolve.assert_called_once_with(37008, 0, 0, module.path)


def _date_signature_image(hit: int, target: int, size: int = 160) -> bytes:
    raw = bytearray(size)
    raw[hit:hit + len(FM26_GAME_DATE_PATTERN)] = bytes(
        0 if value is None else value for value in FM26_GAME_DATE_PATTERN
    )
    struct.pack_into("<i", raw, hit + 16, target - (hit + 20))
    return bytes(raw)


@pytest.mark.parametrize(("hit", "target"), ((16, 120), (96, 24)))
def test_fm26_game_date_signature_resolves_signed_rel32(hit, target):
    raw = _date_signature_image(hit, target)
    sections = [(len(raw), 0, len(raw), 0)]

    assert game_layout._resolve_fm26_game_date_rva(raw, sections) == target


@pytest.mark.parametrize("count", (0, 2))
def test_fm26_game_date_signature_requires_unique_match(count):
    raw = bytearray(320)
    for hit in (24, 184)[:count]:
        signature = _date_signature_image(hit, 120, len(raw))
        raw[hit:hit + len(FM26_GAME_DATE_PATTERN)] = signature[
            hit:hit + len(FM26_GAME_DATE_PATTERN)
        ]
    sections = [(len(raw), 0, len(raw), 0)]

    with pytest.raises(RuntimeError, match=f"matched {count} locations"):
        game_layout._resolve_fm26_game_date_rva(bytes(raw), sections)


def test_fm26_game_date_rva_cache_invalidates_with_file_identity(tmp_path):
    image = tmp_path / "game_plugin.dll"
    image.write_bytes(b"first")
    game_layout._cached_fm26_game_date_rva.cache_clear()
    with patch.object(
        game_layout, "_resolve_fm26_game_date_rva_from_path",
        side_effect=(0x1000, 0x2000),
    ) as resolve:
        assert game_layout.resolve_fm26_game_date_rva(
            str(image), "A" * 64,
        ) == 0x1000
        assert game_layout.resolve_fm26_game_date_rva(
            str(image), "A" * 64,
        ) == 0x1000
        image.write_bytes(b"second-build")
        assert game_layout.resolve_fm26_game_date_rva(
            str(image), "B" * 64,
        ) == 0x2000

    assert resolve.call_count == 2


def test_unknown_steam_plugin_reports_dynamic_date_without_reusing_layout():
    old_hash = "6A65CEF293DA8EB0FC5EE749373275F7C4B536D0A77A165F2F9AB69ABAD7C4CD"
    with (
        patch.object(game_layout, "executable_hash", return_value=old_hash),
        patch.object(
            game_layout, "resolve_fm26_game_date_rva", return_value=0x4DF1C18,
        ),
    ):
        with pytest.raises(RuntimeError, match="0x4DF1C18"):
            game_layout.resolve_fm26_steam_layout(
                37008, 0x10000000, 0x1DEC2000, "game_plugin.dll",
            )


def test_verified_steam_plugin_uses_stable_dynamic_date():
    date_code = struct.pack("<I", (2026 << 16) | 100)
    process = object()

    class _VerifiedProcessContext:
        def __enter__(self):
            return process

        def __exit__(self, *_args):
            return None

    with (
        patch.object(
            game_layout, "executable_hash", return_value=FM26_STEAM_PLUGIN_SHA256,
        ),
        patch.object(
            game_layout, "resolve_fm26_game_date_rva",
            return_value=FM26_LAYOUT.game_date_rva,
        ),
        patch("fm_collector.win32.open_process", return_value=_VerifiedProcessContext()),
        patch("fm_collector.win32.read_process_memory", return_value=date_code) as read,
    ):
        resolved = game_layout.resolve_fm26_steam_layout(
            37008, 0x10000000, 0x6000000, "game_plugin.dll",
        )

    assert resolved.game_date_rva == FM26_LAYOUT.game_date_rva
    assert read.call_count == 2


@pytest.mark.parametrize(
    "reads, error",
    (
        ((struct.pack("<I", (2026 << 16) | 100),) * 2, None),
        ((b"\0\0\0\0",) * 2, "failed validation"),
        ((struct.pack("<I", (1900 << 16) | 1),) * 2, "failed validation"),
        ((struct.pack("<I", (2026 << 16) | 100), struct.pack("<I", (2026 << 16) | 101)), "changed"),
    ),
)
def test_runtime_game_date_validation_is_stable_and_legal(reads, error):
    with patch("fm_collector.win32.read_process_memory", side_effect=reads):
        if error:
            with pytest.raises(RuntimeError, match=error):
                game_layout._validate_fm26_runtime_game_date(
                    object(), 0x10000000, 0x6000000, 0x1000,
                )
        else:
            game_layout._validate_fm26_runtime_game_date(
                object(), 0x10000000, 0x6000000, 0x1000,
            )


def test_runtime_game_date_target_must_stay_inside_module():
    with pytest.raises(RuntimeError, match="outside game_plugin.dll"):
        game_layout._validate_fm26_runtime_game_date(
            object(), 0x10000000, 0x1000, 0x1000,
        )
