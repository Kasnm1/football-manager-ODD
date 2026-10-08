from __future__ import annotations

import struct
from contextlib import contextmanager
from typing import Any

from fm_collector.win32 import (
    open_process, read_process_memory, write_process_memory,
)
from tools.initial_data_audit import Reader, select_process_layout


_TEAM_POINTER_OFFSETS = {"fm24": 0x78, "fm26": 0x88}
_POINTS_OFFSET = 4


@contextmanager
def _open_supported_reader(write: bool = False):
    pid, _process_path, layout = select_process_layout()
    if layout.key not in _TEAM_POINTER_OFFSETS:
        raise RuntimeError("当前游戏版本尚未支持原生积分写入")
    with open_process(pid, read_memory=True, write_memory=write) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 未加载")
        yield process, Reader(process, module.base_address, layout)


def _expected_stats(row: dict[str, Any]) -> tuple[int, int, int, int, int]:
    return tuple(int(row[key]) for key in (
        "goals_for", "goals_against", "played", "won", "drawn",
    ))


def _valid_row(data: bytes | None, row: dict[str, Any]) -> bool:
    if not data or len(data) < 12:
        return False
    goals_for, goals_against, played, won, drawn = _expected_stats(row)
    lost = int(row["lost"])
    return bool(
        struct.unpack_from("<H", data, 0)[0] == goals_for
        and struct.unpack_from("<H", data, 2)[0] == goals_against
        and data[6] == played and data[7] == played
        and data[8] == won and data[9] == drawn and data[10] == lost
        and played == won + drawn + lost
    )


def update_league_points(
    team_address: str | int | None, row: dict[str, Any], delta: int,
) -> dict[str, Any]:
    with _open_supported_reader(write=True) as (process, reader):
        standing_value = row.get("native_standing_address")
        try:
            standing_address = (
                int(standing_value, 16) if isinstance(standing_value, str)
                else int(standing_value or 0)
            )
        except (TypeError, ValueError):
            standing_address = 0
        if not standing_address:
            raise RuntimeError("当前联赛阶段缺少唯一 Standing 地址，已拒绝积分写入")
        expected_team_address = (
            int(team_address, 16) if isinstance(team_address, str)
            else int(team_address or 0)
        )
        team_pointer_address = standing_address + _TEAM_POINTER_OFFSETS[reader.layout.key]
        address = int(reader.ptr(team_pointer_address) or 0)
        if expected_team_address and address != expected_team_address:
            raise RuntimeError("Standing 反向球队地址与当前球队不一致")
        team = reader.team(address) if address else None
        if not team or int(team.get("id") or 0) != int(row["team_id"]):
            raise RuntimeError("Standing 球队对象校验失败，请刷新后重试")
        primary = read_process_memory(process, standing_address + 0x08, 12)
        if not _valid_row(primary, row):
            raise RuntimeError("当前 Standing 统计已变化，请刷新后重试")
        point_address = standing_address + 0x08 + _POINTS_OFFSET
        original = read_process_memory(process, point_address, 2)
        if not original or len(original) != 2:
            raise RuntimeError("无法读取当前 Standing 积分")
        before = struct.unpack("<h", original)[0]
        target_value = before + int(delta)
        if not -32768 <= target_value <= 32767:
            raise ValueError("调整后的原生积分超出范围")
        encoded = struct.pack("<h", target_value)
        try:
            write_process_memory(process, point_address, encoded)
            verified = read_process_memory(process, point_address, 2)
            if not verified or struct.unpack("<h", verified)[0] != target_value:
                raise RuntimeError("原生积分写入回读不一致")
        except Exception:
            write_process_memory(process, point_address, original)
            raise
        return {
            "before": before, "after": target_value,
            "standing_address": hex(standing_address),
            "point_addresses": [hex(point_address)],
        }


def restore_league_points(result: dict[str, Any]) -> None:
    targets = [int(value, 16) for value in result.get("point_addresses", [])]
    before = int(result["before"])
    if not targets:
        return
    with _open_supported_reader(write=True) as (process, _reader):
        encoded = struct.pack("<h", before)
        for target in targets:
            write_process_memory(process, target, encoded)
