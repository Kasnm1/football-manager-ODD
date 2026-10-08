from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fm_collector.win32 import find_module, open_process
from tools.initial_data_audit import (
    FIXTURE_RESULT_VTABLE_RVA,
    FIXTURE_SIZE,
    FIXTURE_VTABLE_RVA,
    GAME_DATE_RVA,
    GAME_PLUGIN,
    SEASON_RESULT_VTABLE_RVA,
    Reader,
    decode_date,
    decode_kickoff_minutes,
    parse_completed_result,
    parse_fixture,
    parse_season_completed_result,
    scan_fixture_addresses,
    select_process,
)


def parsed_result(reader: Reader, address: int) -> dict[str, Any] | None:
    if not address:
        return None
    return parse_completed_result(reader, address) or parse_season_completed_result(reader, address)


def locate_fixture(
    reader: Reader,
    fixture_date: str,
    competition_id: int,
    home_id: int,
    away_id: int,
) -> list[int]:
    addresses, _bytes_scanned = scan_fixture_addresses(reader)
    matches: list[int] = []
    for address in addresses:
        fixture = parse_fixture(reader, address)
        if not fixture or fixture.match_date.isoformat() != fixture_date:
            continue
        home = reader.team(fixture.home_team)
        away = reader.team(fixture.away_team)
        competition = reader.competition(fixture.competition_season)
        if not home or not away or not competition:
            continue
        if (
            int(home["id"]) == home_id
            and int(away["id"]) == away_id
            and int(competition["id"]) == competition_id
        ):
            matches.append(address)
    if not matches:
        raise RuntimeError("target fixture was not found")
    return sorted(set(matches))


def clock_payload(reader: Reader) -> dict[str, Any]:
    code = reader.u32(reader.module_base + GAME_DATE_RVA) or 0
    value = decode_date(code)
    minutes = decode_kickoff_minutes(code)
    return {
        "raw": code,
        "date": value.isoformat() if value else None,
        "minutes": minutes,
        "time": f"{minutes // 60:02d}:{minutes % 60:02d}" if minutes is not None else None,
    }


def sample_fixture(reader: Reader, fixture_address: int) -> dict[str, Any]:
    fixture_raw = reader.bytes(fixture_address, FIXTURE_SIZE)
    fixture_vtable = reader.ptr(fixture_address) or 0
    fixture = parse_fixture(reader, fixture_address)
    result_address = fixture.result_or_state if fixture else 0
    result_vtable = reader.ptr(result_address) if result_address else 0
    result_raw = reader.bytes(result_address, 0xC0) if result_address else None
    result = parsed_result(reader, result_address)
    known_vtables = {
        reader.module_base + FIXTURE_VTABLE_RVA: "fixture",
        reader.module_base + FIXTURE_RESULT_VTABLE_RVA: "fixture_result",
        reader.module_base + SEASON_RESULT_VTABLE_RVA: "season_result",
    }
    return {
        "fixture_address": hex(fixture_address),
        "fixture_vtable": hex(fixture_vtable) if fixture_vtable else None,
        "fixture_vtable_rva": hex(fixture_vtable - reader.module_base) if fixture_vtable else None,
        "fixture_type": known_vtables.get(fixture_vtable, "unknown" if fixture_vtable else None),
        "fixture_raw": fixture_raw.hex(" ") if fixture_raw else None,
        "result_or_state_address": hex(result_address) if result_address else None,
        "result_vtable": hex(result_vtable) if result_vtable else None,
        "result_vtable_rva": hex(result_vtable - reader.module_base) if result_vtable else None,
        "result_type": known_vtables.get(result_vtable, "unknown" if result_vtable else None),
        "result_raw": result_raw.hex(" ") if result_raw else None,
        "parsed_result": result,
    }


def sample(reader: Reader, fixture_addresses: list[int]) -> dict[str, Any]:
    return {
        "captured_at": datetime.now().isoformat(timespec="milliseconds"),
        "clock": clock_payload(reader),
        "fixtures": [sample_fixture(reader, address) for address in fixture_addresses],
    }


def comparison_key(row: dict[str, Any]) -> str:
    material = {
        "clock": row["clock"],
        "fixtures": row["fixtures"],
    }
    return json.dumps(material, ensure_ascii=False, sort_keys=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only finality probe for one FM fixture")
    parser.add_argument("--date", required=True)
    parser.add_argument("--competition-id", required=True, type=int)
    parser.add_argument("--home-id", required=True, type=int)
    parser.add_argument("--away-id", required=True, type=int)
    parser.add_argument("--interval", type=float, default=0.25)
    parser.add_argument("--heartbeat", type=float, default=10.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--address", action="append", default=[])
    parser.add_argument("--out-dir", default="data/probes/fixture_finality")
    args = parser.parse_args()

    pid, _process_path = select_process()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / (
        f"fixture_{args.date}_{args.competition_id}_{args.home_id}_{args.away_id}_{pid}_{stamp}.jsonl"
    )

    with open_process(pid) as process:
        module = find_module(process, GAME_PLUGIN)
        if not module:
            raise RuntimeError(f"{GAME_PLUGIN} is not loaded")
        reader = Reader(process, module.base_address)
        fixture_addresses = (
            sorted({int(value, 0) for value in args.address})
            if args.address
            else locate_fixture(reader, args.date, args.competition_id, args.home_id, args.away_id)
        )
        previous = None
        last_written = 0.0
        with out_path.open("a", encoding="utf-8", buffering=1) as output:
            while True:
                row = sample(reader, fixture_addresses)
                key = comparison_key(row)
                now = time.monotonic()
                changed = key != previous
                heartbeat_due = now - last_written >= max(args.heartbeat, args.interval)
                if changed or heartbeat_due:
                    row["reason"] = "changed" if changed else "heartbeat"
                    output.write(json.dumps(row, ensure_ascii=False) + "\n")
                    last_written = now
                    print(
                        json.dumps(
                            {
                                "captured_at": row["captured_at"],
                                "clock": row["clock"],
                                "fixture_count": len(row["fixtures"]),
                                "fixtures": [
                                    {
                                        "fixture": item["fixture_address"],
                                        "fixture_type": item["fixture_type"],
                                        "result": item["result_or_state_address"],
                                        "result_type": item["result_type"],
                                        "parsed_result": item["parsed_result"],
                                    }
                                    for item in row["fixtures"]
                                ],
                                "reason": row["reason"],
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                previous = key
                if args.once:
                    break
                time.sleep(max(0.05, args.interval))

    print(json.dumps({"path": str(out_path.resolve())}, ensure_ascii=False))


if __name__ == "__main__":
    main()
