from __future__ import annotations

import struct
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch

import fm_odds_web
from tools import club_reader


class FakeInjuryReader:
    def __init__(self, layout):
        self.layout = layout
        self.module_base = 0x50000000
        self.player = 0x1000
        self.injury_pointer = self.player + layout.player_injury_list_offset
        self.pointers = {self.injury_pointer: 0}
        self.memory: dict[int, bytes] = {}

    def roster(self, _team):
        return [{"id": 42, "address": hex(self.player)}]

    def ptr(self, address):
        if address in self.pointers:
            return self.pointers[address]
        raw = self.memory.get(address)
        return struct.unpack("<Q", raw[:8])[0] if raw and len(raw) >= 8 else 0

    def bytes(self, address, size):
        if address == self.injury_pointer and size == 8:
            return struct.pack("<Q", self.pointers[address])
        raw = self.memory.get(address)
        return raw[:size] if raw and len(raw) >= size else None

    def _availability_details(self, container):
        begin, end, _capacity = struct.unpack("<QQQ", self.memory[container][:24])
        if end - begin != 8:
            return None
        return {
            "injury_count": 1,
            "injuries": [{"type": "病毒感染", "duration_days_low_raw": 3}],
        }


def _virus_runtime(monkeypatch):
    layout = SimpleNamespace(
        module_name="game_plugin.dll",
        player_injury_list_offset=0xF8,
        module=lambda _process: SimpleNamespace(base_address=0x50000000),
    )
    reader = FakeInjuryReader(layout)
    process = SimpleNamespace(pid=7)
    allocations = iter(((0x3000, 0x9000), (0x4000, 0x9000), (0x5000, 0x9000)))
    freed = []

    def write_memory(_process, address, data):
        if address == reader.injury_pointer:
            reader.pointers[address] = struct.unpack("<Q", data)[0]
        else:
            reader.memory[address] = bytes(data)

    monkeypatch.setattr(club_reader, "select_process_layout", lambda: (7, "fm.exe", layout))
    monkeypatch.setattr(club_reader, "open_process", lambda *_args, **_kwargs: nullcontext(process))
    monkeypatch.setattr(club_reader, "Reader", lambda *_args: reader)
    monkeypatch.setattr(club_reader, "_resolve_team_address", lambda *_args: 0x2000)
    monkeypatch.setattr(club_reader, "_validated_player_person", lambda *_args: 0x1100)
    monkeypatch.setattr(
        club_reader, "_native_injury_templates",
        lambda *_args, **_kwargs: [{"name": "其他伤病", "record": bytes(0x50)}],
    )
    monkeypatch.setattr(club_reader, "_native_injury_type", lambda *_args: 0x6000)
    monkeypatch.setattr(club_reader, "_remote_malloc_block", lambda *_args: next(allocations))
    monkeypatch.setattr(club_reader, "_remote_free_block", lambda _p, _f, address: freed.append(address))
    monkeypatch.setattr(club_reader, "write_process_memory", write_memory)
    return reader, freed


def test_virus_delivery_creates_missing_injury_container(monkeypatch):
    reader, freed = _virus_runtime(monkeypatch)

    result = club_reader.create_player_injury(
        42, hex(reader.player), "2030-01-10",
        team_id=9, team_address="0x2000",
    )

    assert result["injury"] == "病毒感染"
    assert reader.pointers[reader.injury_pointer] == 0x3000
    assert struct.unpack("<QQQ", reader.memory[0x3000][:24]) == (0x5000, 0x5008, 0x5008)
    assert freed == []


def test_virus_delivery_restores_new_container_pointer_on_failure(monkeypatch):
    reader, freed = _virus_runtime(monkeypatch)
    monkeypatch.setattr(reader, "_availability_details", lambda _container: None)

    try:
        club_reader.create_player_injury(
            42, hex(reader.player), "2030-01-10",
            team_id=9, team_address="0x2000",
        )
    except RuntimeError as error:
        assert "写入校验失败" in str(error)
    else:
        raise AssertionError("expected delivery verification failure")

    assert reader.pointers[reader.injury_pointer] == 0
    assert freed == [0x5000, 0x4000, 0x3000]


def test_virus_delivery_does_not_free_memory_when_pointer_restore_fails(monkeypatch):
    reader, freed = _virus_runtime(monkeypatch)
    monkeypatch.setattr(reader, "_availability_details", lambda _container: None)
    original_write = club_reader.write_process_memory

    def fail_pointer_restore(process, address, data):
        if address == reader.injury_pointer and data == b"\0" * 8:
            return
        original_write(process, address, data)

    monkeypatch.setattr(club_reader, "write_process_memory", fail_pointer_restore)

    try:
        club_reader.create_player_injury(
            42, hex(reader.player), "2030-01-10",
            team_id=9, team_address="0x2000",
        )
    except RuntimeError as error:
        assert "伤病容器恢复异常" in str(error)
    else:
        raise AssertionError("expected rollback verification failure")

    assert reader.pointers[reader.injury_pointer] == 0x3000
    assert freed == []


def test_virus_failure_mail_contains_the_real_error_and_attempt_identity():
    item = {
        "id": "virus-1", "player_name": "目标球员",
        "assigned_at": "2030-01-01T12:00:00",
    }
    with patch.object(fm_odds_web, "load_mail", return_value=[]), patch.object(
        fm_odds_web, "save_mail",
    ) as save_mail:
        mail = fm_odds_web.archive_virus_rejection_mail(
            item, "2030-01-09", "无法读取球员伤病槽",
        )

    assert mail["source_id"].endswith(":2030-01-01T12:00:00")
    assert mail["detail"] == "无法读取球员伤病槽"
    assert "无法读取球员伤病槽" in mail["message"]
    assert "已退回库存" in mail["message"]
    save_mail.assert_called_once()


def test_odd_sync_delivers_active_virus_item_as_native_injury(monkeypatch):
    item = {
        "id": "virus-1",
        "player_id": 42,
        "player_address": "0x1000",
        "opponent_team_id": 9,
        "opponent_team_address": "0x2000",
    }
    delivered = []
    resolutions = []

    monkeypatch.setattr(fm_odds_web, "active_flu_items", lambda: [item])

    def create_injury(player_id, player_address, game_date, **kwargs):
        delivered.append((player_id, player_address, game_date, kwargs))
        return {"injury": "病毒感染", "duration_days": 3}

    monkeypatch.setattr(fm_odds_web, "create_player_injury", create_injury)
    monkeypatch.setattr(
        fm_odds_web, "resolve_flu_item",
        lambda item_id, **kwargs: resolutions.append((item_id, kwargs)),
    )

    fm_odds_web.LocalOddsState._sync_opponent_flu_items("2030-01-09")

    assert delivered == [(
        42, "0x1000", "2030-01-09",
        {
            "team_id": 9,
            "team_address": "0x2000",
            "injury_name": "病毒感染",
            "duration_days": 3,
        },
    )]
    assert resolutions == [(
        "virus-1",
        {"applied": True, "detail": "病毒感染 · 3天"},
    )]


def test_odd_sync_returns_virus_item_and_archives_rejection_on_delivery_failure(monkeypatch):
    item = {"id": "virus-1", "player_id": 42, "player_name": "目标球员"}
    resolutions = []
    rejection_mails = []

    monkeypatch.setattr(fm_odds_web, "active_flu_items", lambda: [item])
    monkeypatch.setattr(
        fm_odds_web, "create_player_injury",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("目标球员已不在所选对手的一线队")
        ),
    )
    monkeypatch.setattr(
        fm_odds_web, "resolve_flu_item",
        lambda item_id, **kwargs: resolutions.append((item_id, kwargs)),
    )
    monkeypatch.setattr(
        fm_odds_web, "archive_virus_rejection_mail",
        lambda *args: rejection_mails.append(args),
    )

    fm_odds_web.LocalOddsState._sync_opponent_flu_items("2030-01-09")

    failure = "目标球员已不在所选对手的一线队"
    assert resolutions == [(
        "virus-1", {"applied": False, "detail": failure},
    )]
    assert rejection_mails == [(item, "2030-01-09", failure)]
