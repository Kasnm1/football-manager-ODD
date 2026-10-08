from __future__ import annotations

import struct
from typing import Any

from fm_collector.win32 import open_process, write_process_memory
from tools.initial_data_audit import (
    ENTITY_UID, Reader,
    select_process_layout,
)


SUPPORTED = {"remove_injury", "restore_fitness"}


def _apply_layout_roster_effects(
    club_profile: dict[str, Any], desired: set[str], pid: int, layout: Any,
) -> dict[str, str]:
    errors: dict[str, str] = {}
    with open_process(pid, write_memory=True) as process:
        module = layout.module(process)
        if not module:
            return {key: "fm.exe 尚未加载" for key in desired}
        reader = Reader(process, module.base_address, layout)
        for player in club_profile.get("players", []):
            try:
                address = int(str(player["address"]), 16)
                vtable = reader.ptr(address)
                player_rva = vtable - reader.module_base if vtable else 0
                if player_rva in layout.actual_player_vtable_rvas:
                    person = address + layout.player_person_offset
                    identifier = int(reader.u32(person + ENTITY_UID) or 0)
                elif player_rva in layout.player_and_non_player_vtable_rvas:
                    person = address + layout.player_and_non_player_person_offset
                    identifier = address
                else:
                    raise ValueError("球员对象已经失效")
                if identifier != int(player.get("id") or 0):
                    raise ValueError("球员 ID 与当前内存对象不一致")
                if "remove_injury" in desired:
                    injury_offset = layout.player_injury_list_offset
                    if injury_offset is None:
                        raise ValueError("当前游戏版本的伤病字段尚未映射")
                    injury = int(reader.ptr(address + int(injury_offset)) or 0)
                    original = reader.bytes(injury, 24) if injury > 0xFFFFF else None
                    if original and len(original) == 24:
                        write_process_memory(process, injury, b"\0" * 24)
                        if reader.bytes(injury, 24) != b"\0" * 24:
                            write_process_memory(process, injury, original)
                            raise RuntimeError("清除伤病后的数据校验失败")
                if "restore_fitness" in desired:
                    offsets = (
                        layout.player_sharpness_offset, layout.player_fatigue_offset,
                        layout.player_fitness_offset, layout.player_morale_offset,
                    )
                    if any(value is None for value in offsets):
                        raise ValueError("当前游戏版本的体能字段尚未映射完整")
                    write_process_memory(process, address + int(layout.player_sharpness_offset), struct.pack("<H", 10000))
                    write_process_memory(process, address + int(layout.player_fatigue_offset), struct.pack("<h", -500))
                    write_process_memory(process, address + int(layout.player_fitness_offset), struct.pack("<H", 10000))
                    write_process_memory(process, address + int(layout.player_morale_offset), bytes([20]))
            except Exception as error:
                for key in desired:
                    errors.setdefault(key, str(error))
    return errors


def apply_roster_effects(club_profile: dict[str, Any] | None, active_skus: list[str]) -> dict[str, str]:
    desired = set(active_skus) & SUPPORTED
    if not desired or not club_profile:
        return {}
    layout_pid, _layout_path, layout = select_process_layout()
    return _apply_layout_roster_effects(club_profile, desired, layout_pid, layout)
