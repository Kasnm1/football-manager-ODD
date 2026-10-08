from __future__ import annotations

import struct
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tools.database_index import TABLE_COUNT_LIMITS
from tools.fm24_process_diagnostic import (
    render_person_table_probe,
    runtime_person_table_probe,
    summarize_person_table_header,
)


@pytest.mark.parametrize(
    ("header", "failure"),
    [
        (struct.pack("<QQQ", 0, 0, 0), "begin_nonzero"),
        (struct.pack("<QQQ", 0x2000, 0x1FF8, 0x3000), "end_after_begin"),
        (struct.pack("<QQQ", 0x2000, 0x2020, 0x2018), "capacity_after_end"),
        (struct.pack("<QQQ", 0x2000, 0x2021, 0x3000), "size_aligned_8"),
        (struct.pack("<QQQ", 0x2000, 0x2020, 0x3001), "capacity_aligned_8"),
        (struct.pack("<QQQ", 0x2000, 0x2030, 0x3000), "entry_count_within_limit"),
    ],
)
def test_person_table_header_reports_exact_failed_predicate(
    header: bytes, failure: str,
) -> None:
    maximum = 4 if failure == "entry_count_within_limit" else 1_000_000

    result = summarize_person_table_header(header, maximum=maximum)

    assert result["state"] == "invalid"
    assert failure in result["failure_codes"]


def test_person_table_header_accepts_valid_header() -> None:
    result = summarize_person_table_header(
        struct.pack("<QQQ", 0x2000, 0x2020, 0x2040), maximum=8,
    )

    assert result == {
        "state": "valid",
        "entry_limit": 8,
        "checks": {
            "begin_nonzero": True,
            "end_after_begin": True,
            "capacity_after_end": True,
            "size_aligned_8": True,
            "capacity_aligned_8": True,
            "entry_count_within_limit": True,
        },
        "failure_codes": [],
        "entry_count": 4,
        "capacity_count": 8,
    }


def test_person_table_header_accepts_verified_large_database() -> None:
    begin = 0x50000000
    result = summarize_person_table_header(
        struct.pack(
            "<QQQ",
            begin,
            begin + 1_034_663 * 8,
            begin + 1_036_163 * 8,
        ),
        maximum=TABLE_COUNT_LIMITS["person"],
    )

    assert result["state"] == "valid"
    assert result["entry_count"] == 1_034_663
    assert result["capacity_count"] == 1_036_163
    assert result["failure_codes"] == []


class _ProcessContext:
    def __enter__(self) -> object:
        return object()

    def __exit__(self, *_args: object) -> None:
        return None


class _Reader:
    def __init__(self) -> None:
        self.module = None
        self._reads = {
            0x1068: struct.pack("<Q", 0x4000),
            0x4080: struct.pack("<Q", 0x5000),
            0x5000: struct.pack("<QQQ", 0x6000, 0x6040, 0x6038),
        }

    def bytes(self, address: int, size: int) -> bytes | None:
        value = self._reads.get(address)
        return value if value is not None and len(value) == size else None


def _layout(module: object) -> SimpleNamespace:
    return SimpleNamespace(
        module=lambda _process: module,
        database_root_pattern=(0x90,),
        database_root_rel32_offset=1,
        database_root_instruction_size=5,
        database_root_probe_rva=0x1234,
    )


def test_runtime_person_table_probe_is_precise_and_address_free() -> None:
    module = SimpleNamespace(base_address=0x10000000, size=0x200000, path="hidden")
    reader = _Reader()

    with (
        patch("fm_collector.win32.open_process", return_value=_ProcessContext()),
        patch("tools.initial_data_audit.Reader", return_value=reader),
        patch("tools.database_index._resolve_relative_global", return_value=0x1000),
    ):
        result = runtime_person_table_probe(7, _layout(module))

    assert result["state"] == "invalid"
    assert result["header_stable"] is True
    assert result["entry_count"] == 8
    assert result["capacity_count"] == 7
    assert result["failure_codes"] == ["capacity_after_end"]
    rendered = render_person_table_probe(result)
    assert "DB-P1" in rendered
    assert "capacity_after_end" in rendered
    for address in ("0x1000", "0x4000", "0x5000", "0x6000", "0x6040"):
        assert address not in str(result)
        assert address not in rendered


def test_runtime_person_table_probe_detects_changing_header() -> None:
    module = SimpleNamespace(base_address=0x10000000, size=0x200000, path="hidden")
    reader = _Reader()
    headers = iter((
        struct.pack("<QQQ", 0x6000, 0x6040, 0x6080),
        struct.pack("<QQQ", 0x7000, 0x7040, 0x7080),
    ))
    original_read = reader.bytes

    def read(address: int, size: int) -> bytes | None:
        if address == 0x5000 and size == 0x18:
            return next(headers)
        return original_read(address, size)

    reader.bytes = read  # type: ignore[method-assign]
    with (
        patch("fm_collector.win32.open_process", return_value=_ProcessContext()),
        patch("tools.initial_data_audit.Reader", return_value=reader),
        patch("tools.database_index._resolve_relative_global", return_value=0x1000),
    ):
        result = runtime_person_table_probe(7, _layout(module))

    assert result["state"] == "changing"
    assert result["header_stable"] is False
    assert result["failure_codes"] == ["header_changed_between_reads"]
