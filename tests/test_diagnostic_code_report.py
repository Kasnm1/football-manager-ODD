from __future__ import annotations

from dataclasses import fields
import inspect
from pathlib import Path
import subprocess
import sys
import struct
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tools.fm24_process_diagnostic import (
    APP_TITLE,
    CLUB_CULTURE_CONSTRUCTOR_PATTERN,
    CLUB_RUNTIME_RVA_FIELDS,
    CODE_REPORT_BEGIN,
    CODE_REPORT_END,
    COMPLETION_MESSAGE,
    DETECTING_MESSAGE,
    DiagnosticWindow,
    _layout_field_code,
    _club_read_probe_rows,
    _club_read_sample_summary,
    _memory_pattern_occurrences,
    _player_performance_sample_summary,
    _staff_structure_sample_summary,
    _team_type_sample_summary,
    build_code_report,
    decode_code_report,
    layout_code_manifest,
    runtime_rva_probe_manifest,
)
from tools.game_layout import FM24_EPIC_LAYOUT, FM26_LAYOUT, GameLayout


def test_diagnostic_window_hides_bundle_behind_english_controls() -> None:
    source = inspect.getsource(DiagnosticWindow)

    assert APP_TITLE == "FMODD Diagnostic Tool"
    assert DETECTING_MESSAGE == "Detecting... This may take up to 2 minutes."
    assert COMPLETION_MESSAGE == "Detection complete. Please send the report to the developer."
    assert "支持 FM2024 / FM2026" not in source
    assert 'actions.pack(side="bottom", fill="x")' in source
    assert 'self.output.pack(side="top", fill="both", expand=True)' in source
    assert "self.set_report(COMPLETION_MESSAGE, code_report)" in source
    assert "self.set_report(code_report, code_report)" not in source
    for label in ("Run Again", "Detecting...", "Copy Report", "Copied", "Close"):
        assert label in source
    for label in ("重新检测", "正在检测", "复制检测报告", "关闭"):
        assert label not in source


def test_club_reader_frozen_import_does_not_require_odds_native_core(tmp_path: Path) -> None:
    script = (
        "import sys; "
        f"sys._MEIPASS = {str(tmp_path)!r}; "
        "sys.frozen = True; "
        "import tools.club_reader; "
        "assert 'tools.preview_cup_odds' not in sys.modules"
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=(Path(__file__).resolve().parents[1] / "src"),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_code_report_round_trip_hides_readable_details() -> None:
    readable = "Epic path C:/Users/Example/fm.exe; target RVA 0x12345678"
    manifests = [{"process": 1, "manifest": layout_code_manifest(FM24_EPIC_LAYOUT)}]

    bundle = build_code_report(readable, manifests=manifests)
    decoded = decode_code_report(bundle)

    assert bundle.startswith(CODE_REPORT_BEGIN)
    assert bundle.endswith(CODE_REPORT_END)
    assert readable not in bundle
    assert "C:/Users/Example" not in bundle
    assert "0x12345678" not in bundle
    assert decoded["report"] == readable
    assert decoded["layouts"] == manifests


@pytest.mark.parametrize("layout", [FM24_EPIC_LAYOUT, FM26_LAYOUT])
def test_layout_manifest_covers_every_current_layout_field(layout: GameLayout) -> None:
    manifest = layout_code_manifest(layout)
    expected_names = {field.name for field in fields(GameLayout)}

    assert set(manifest["codebook"].values()) == expected_names
    assert set(manifest["values"]) == set(manifest["codebook"])
    assert len(manifest["codebook"]) == len(expected_names)
    assert manifest["values"][_layout_field_code("distribution")] == layout.distribution
    assert manifest["values"][_layout_field_code("team_type_offset")] == layout.team_type_offset
    assert manifest["values"][_layout_field_code("club_culture_constructor_rva")] == (
        layout.club_culture_constructor_rva
    )


def test_club_runtime_probe_fields_are_current_layout_fields() -> None:
    expected_names = {field.name for field in fields(GameLayout)}

    assert set(CLUB_RUNTIME_RVA_FIELDS) <= expected_names


def test_fm24_epic_profile_exposes_read_only_verified_runtime_layout() -> None:
    assert FM24_EPIC_LAYOUT.database_root_probe_rva == 0x3BDC363
    assert FM24_EPIC_LAYOUT.human_manager_root_probe_rva == 0x3A278B1
    assert FM24_EPIC_LAYOUT.fixture_pool_rva == 0x6429A68
    assert FM24_EPIC_LAYOUT.team_type_offset == 0x28
    assert FM24_EPIC_LAYOUT.stadium_vtable_rva == 0x5A78248


def test_code_report_rejects_damaged_payload() -> None:
    bundle = build_code_report("ok", manifests=[])
    lines = bundle.splitlines()
    lines[1] = ("A" if lines[1][0] != "A" else "B") + lines[1][1:]
    damaged = "\n".join(lines)

    with pytest.raises(ValueError):
        decode_code_report(damaged)


class _ProcessContext:
    def __init__(self, process: object) -> None:
        self.process = process

    def __enter__(self) -> object:
        return self.process

    def __exit__(self, *_args: object) -> None:
        return None


def test_runtime_rva_probe_reports_health_without_absolute_addresses() -> None:
    process = object()
    module = SimpleNamespace(base_address=0x10000000, size=0x20000000)
    layout = SimpleNamespace(
        module_name="fm.exe",
        game_date_rva=0x100,
        database_root_probe_rva=0x200,
        human_manager_root_probe_rva=None,
        savegame_root_rvas=(0x300,),
        fixture_pool_rva=0x400,
        match_session_pointer_rva=0x500,
        play_fixture_manager_pointer_rva=0x600,
        fixture_vtable_rva=0x700,
        fixture_result_vtable_rva=0x800,
        season_result_vtable_rva=0x900,
        team_vtable_rva=0xA00,
        national_team_vtable_rva=0xB00,
        club_vtable_rva=0xC00,
        competition_vtable_rva=0xD00,
        actual_player_vtable_rvas=(0xE00,),
        human_manager_vtable_rvas=(0xF00,),
        staff_person_vtable_rva=0x30000000,
    )

    def read(_process: object, address: int, size: int) -> bytes | None:
        rva = address - module.base_address
        if rva == 0x100 and size == 4:
            return struct.pack("<I", 123)
        if rva == 0x300 and size == 8:
            return struct.pack("<Q", 0x50000000)
        if rva in {0x500, 0x600} and size == 8:
            return bytes(8)
        if 0 < rva < module.size and size == 1:
            return b"\x90"
        if address == 0x50000000 and size == 1:
            return b"\x01"
        return None

    with (
        patch("fm_collector.win32.open_process", return_value=_ProcessContext(process)),
        patch("fm_collector.win32.find_module", return_value=module),
        patch("fm_collector.win32.read_process_memory", side_effect=read),
    ):
        probe = runtime_rva_probe_manifest(7, layout)

    rendered = str(probe)
    assert probe["state"] == "probed"
    assert {row["group"] for row in probe["fields"]} == {"core", "club"}
    club_vtable = next(
        row for row in probe["fields"]
        if row["field"] == _layout_field_code("club_vtable_rva")
    )
    assert club_vtable["samples"] == [{"rva": 0xC00, "state": "readable"}]
    assert "0x10000000" not in rendered
    assert "0x50000000" not in rendered
    assert "outside_module" in rendered
    assert "target_state': 'readable" in rendered
    assert "target_state': 'null" in rendered


def test_club_culture_constructor_probe_masks_relocations() -> None:
    sample = bytes.fromhex(
        "55 56 57 48 83 ec 40 48 8d 6c 24 40 48 c7 45 f8 "
        "fe ff ff ff 48 89 ce 48 8d 05 1a 56 a0 02 48 89 "
        "01 e8 5a 6d fe fe 48 89 c1 48 83 c1 08 48 c7 45 f0 "
        "00 00 00 00"
    )

    assert _memory_pattern_occurrences(b"prefix" + sample + b"suffix", CLUB_CULTURE_CONSTRUCTOR_PATTERN) == [6]


def test_team_type_summary_keeps_only_counts() -> None:
    summary = _team_type_sample_summary([
        (1001, 0xAA00, 0),
        (1002, 0xAA00, 10),
        (1003, 0xBB00, 0),
        (1004, 0xBB00, 12),
        (1005, 0xCC00, 0),
    ])

    assert summary == {
        "objects": 5,
        "values": {0: 3, 10: 1, 12: 1},
        "clubs": 3,
        "linked_groups": 2,
    }
    assert "1001" not in str(summary)
    assert "43520" not in str(summary)


def test_player_performance_summary_keeps_only_aggregate_evidence() -> None:
    summary = _player_performance_sample_summary([
        {
            "home_goals": 2,
            "away_goals": 1,
            "goal_events": [
                {
                    "scorer": {"match_player_id": 7, "name": "Alpha"},
                    "assist": {"match_player_id": 8, "name": "Beta"},
                },
                {
                    "scorer": {"match_player_id": 9, "name": None},
                    "assist": None,
                },
                {
                    "scorer": {"match_player_id": 10, "name": "Gamma"},
                    "assist": {"match_player_id": 11, "name": None},
                },
            ],
            "player_ratings": [6.4, 7.85, 12, "bad"],
        },
        {"home_goals": 0, "away_goals": 0, "goal_events": None},
    ])

    assert summary == {
        "matches": 2,
        "score_goals": 3,
        "event_matches": 1,
        "event_goals": 3,
        "scorer_links": 2,
        "assist_events": 2,
        "assist_links": 1,
        "ratings": 2,
        "rating_min": 6.4,
        "rating_max": 7.85,
    }
    assert "Alpha" not in str(summary)
    assert "Beta" not in str(summary)
    assert "match_player_id" not in str(summary)


def test_club_read_summary_covers_domains_without_identity_values() -> None:
    summary = _club_read_sample_summary([{
        "club_information": {
            "address": "0xABCDEF",
            "year_founded": 1905,
            "attendance": {"average": 32000, "minimum": 20000, "maximum": 40000},
            "supporters": {
                "season_ticket_holders": 12000,
                "social_media_followers": 550000,
                "profile": {"loyalty": 18},
                "distribution": {"core": 30},
            },
            "ownership": {"address": "0xFEDCBA", "type": 3},
            "culture": [{"type_name": "Secret Culture"}],
            "facilities": {
                "training": 18,
                "youth": 16,
                "junior_coaching": 17,
                "youth_recruitment": 15,
            },
            "stadium": {
                "name": "Private Ground",
                "capacity": 40000,
                "seating_capacity": 39000,
                "used_capacity": 38000,
                "expansion_capacity": 52000,
                "pitch_condition": 92,
                "pitch_type": 2,
            },
            "training_ground": {"name": "Private Training Centre"},
            "finances": {
                "balance": -20_000_000,
                "remaining_transfer_budget": 30_000_000,
                "income_statement": {"income": [{"name": "Private Income"}]},
                "monthly_summary": [],
                "debts": [{"address": "0x1234"}],
                "sponsors": [{"address": "0x5678"}],
            },
        },
        "roster": [{
            "id": 1001,
            "name": "Private Player",
            "address": "0x9999",
            "ca": 150,
            "pa": 175,
            "positions": {"ST": 20},
            "fitness_raw": 9400,
            "sharpness_raw": 8800,
            "morale_raw": 17,
        }],
    }])

    assert summary == {
        "clubs": 1,
        "year_founded": 1,
        "attendance": 1,
        "supporters": 1,
        "ownership": 1,
        "culture": 1,
        "culture_records": 1,
        "facilities": 1,
        "facility_values": 4,
        "facility_min": 15,
        "facility_max": 18,
        "facility_training": 1,
        "facility_training_min": 18,
        "facility_training_max": 18,
        "facility_youth": 1,
        "facility_youth_min": 16,
        "facility_youth_max": 16,
        "facility_junior_coaching": 1,
        "facility_junior_coaching_min": 17,
        "facility_junior_coaching_max": 17,
        "facility_youth_recruitment": 1,
        "facility_youth_recruitment_min": 15,
        "facility_youth_recruitment_max": 15,
        "stadiums": 1,
        "stadium_capacity": 1,
        "stadium_pitch": 1,
        "training_grounds": 1,
        "finances": 1,
        "finance_core": 1,
        "income_statements": 1,
        "monthly_summaries": 1,
        "debt_lists": 1,
        "debt_records": 1,
        "sponsor_lists": 1,
        "sponsor_records": 1,
        "roster_teams": 1,
        "players": 1,
        "player_ca": 1,
        "player_pa": 1,
        "player_positions": 1,
        "player_fitness": 1,
        "player_sharpness": 1,
        "player_morale": 1,
    }
    rows = "\n".join(_club_read_probe_rows(summary))
    for private_value in (
        "Private", "Secret", "1001", "0xABCDEF", "0xFEDCBA", "0x9999",
        "-20000000", "30000000", "40000", "550000",
    ):
        assert private_value not in rows
    assert (
        "检测项 F-C1：C1 V1 Q4 N15 X18 "
        "T1:18-18 Y1:16-16 J1:17-17 R1:15-15"
    ) in rows
    assert "检测项 R-C1：T1 P1 C1 A1 O1 F1 H1 M1" in rows


def test_staff_structure_summary_keeps_only_validation_counts() -> None:
    summary = _staff_structure_sample_summary([
        {
            "uid": True, "contract": True, "backref": True, "team": True,
            "job": True, "wage": True, "dates": True,
        },
        {
            "uid": True, "contract": True, "backref": False, "team": False,
            "job": False, "wage": False, "dates": False,
        },
    ])

    assert summary == {
        "objects": 2,
        "uid": 2,
        "contract": 2,
        "backref": 1,
        "team": 1,
        "job": 1,
        "wage": 1,
        "dates": 1,
    }
