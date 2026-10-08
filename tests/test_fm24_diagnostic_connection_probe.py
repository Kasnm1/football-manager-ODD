from __future__ import annotations

import struct
import unittest
from types import SimpleNamespace

from tools.fm24_process_diagnostic import (
    FM24_RTTI_NAMES,
    FM24_SAVE_PROVIDER_RTTI,
    _fm24_runtime_connection_rows,
    _fm24_runtime_connection_summary,
    _valid_local_save_pair,
)


class _FakeMemory:
    def __init__(self) -> None:
        self.segments: list[tuple[int, bytearray]] = []

    def add(self, address: int, data: bytes | bytearray) -> None:
        self.segments.append((address, bytearray(data)))

    def read(self, _process: object, address: int, size: int) -> bytes | None:
        for base, data in self.segments:
            offset = address - base
            if 0 <= offset and offset + size <= len(data):
                return bytes(data[offset:offset + size])
        return None


class FM24DiagnosticConnectionProbeTests(unittest.TestCase):
    def test_local_save_pair_requires_matching_fm_filename_and_label(self) -> None:
        self.assertTrue(_valid_local_save_pair(
            "D:/Sports Interactive/Football Manager 2024/games/My Career.fm",
            "My Career",
        ))
        self.assertFalse(_valid_local_save_pair(
            "D:/Sports Interactive/Football Manager 2024/games/My Career.fm",
            "Other Career",
        ))
        self.assertFalse(_valid_local_save_pair("My Career.txt", "My Career"))

    def test_runtime_summary_validates_provider_and_manager_team_back_reference(self) -> None:
        module_base = 0x10000000
        provider_rva = 0x1000
        manager_rva = 0x2000
        team_rva = 0x3000
        national_team_rva = 0x4000
        region_base = 0x50000000
        region = bytearray(0x1000)
        provider = region_base + 0x100
        person = region_base + 0x500
        manager = person - 0x450
        contract = 0x60000000
        team = 0x70000000
        path_blob = 0x80000000
        label_blob = 0x81000000

        struct.pack_into("<Q", region, provider - region_base, module_base + provider_rva)
        struct.pack_into("<Q", region, provider - region_base + 0xB0, path_blob)
        struct.pack_into("<Q", region, provider - region_base + 0x1F0, label_blob)
        struct.pack_into("<Q", region, person - region_base, module_base + manager_rva)
        struct.pack_into("<I", region, person - region_base + 0x0C, 12345)
        struct.pack_into("<Q", region, person - region_base + 0xC8, contract)

        path_value = b"D:/FM/games/Verified Career.fm"
        label_value = b"Verified Career"
        contract_raw = bytearray(0x18)
        struct.pack_into("<Q", contract_raw, 0x08, person)
        struct.pack_into("<Q", contract_raw, 0x10, team)
        team_raw = bytearray(0x88)
        struct.pack_into("<Q", team_raw, 0x00, module_base + team_rva)
        struct.pack_into("<I", team_raw, 0x0C, 67890)
        struct.pack_into("<Q", team_raw, 0x80, manager)

        memory = _FakeMemory()
        memory.add(region_base, region)
        memory.add(path_blob, struct.pack("<I", len(path_value)) + path_value)
        memory.add(label_blob, struct.pack("<I", len(label_value)) + label_value)
        memory.add(contract, contract_raw)
        memory.add(team, team_raw)
        summary = _fm24_runtime_connection_summary(
            object(), module_base,
            [SimpleNamespace(base_address=region_base, size=len(region))],
            memory.read,
            {
                FM24_SAVE_PROVIDER_RTTI: [provider_rva],
                FM24_RTTI_NAMES["人类经理"]: [manager_rva],
                FM24_RTTI_NAMES["球队"]: [team_rva],
                FM24_RTTI_NAMES["国家队"]: [national_team_rva],
            },
        )

        self.assertEqual(summary["provider_object_count"], 1)
        self.assertEqual(summary["valid_local_provider_count"], 1)
        self.assertEqual(summary["manager_object_count"], 1)
        self.assertEqual(summary["linked_manager_count"], 1)

    def test_report_exposes_only_counts_and_connection_classification(self) -> None:
        rows = _fm24_runtime_connection_rows({
            "provider_type_count": 1,
            "provider_object_count": 1,
            "valid_local_provider_count": 1,
            "manager_type_count": 3,
            "manager_object_count": 1,
            "linked_manager_count": 0,
        })
        report = "\n".join(rows)

        self.assertIn("本地存档身份可读取，但经理会话证据缺失", report)
        self.assertNotIn("Verified Career", report)
        self.assertNotIn("12345", report)
        self.assertNotIn("67890", report)

    def test_only_authoritative_human_manager_vtable_is_counted(self) -> None:
        module_base = 0x10000000
        region_base = 0x50000000
        region = bytearray(0x1000)
        person = region_base + 0x500
        struct.pack_into("<Q", region, person - region_base, module_base + 0x1800)
        struct.pack_into("<I", region, person - region_base + 0x0C, 12345)
        memory = _FakeMemory()
        memory.add(region_base, region)

        summary = _fm24_runtime_connection_summary(
            object(), module_base,
            [SimpleNamespace(base_address=region_base, size=len(region))],
            memory.read,
            {
                FM24_SAVE_PROVIDER_RTTI: [],
                FM24_RTTI_NAMES["人类经理"]: [0x1800, 0x1900, 0x2000],
                FM24_RTTI_NAMES["球队"]: [0x3000],
                FM24_RTTI_NAMES["国家队"]: [0x4000],
            },
        )

        self.assertEqual(summary["manager_rtti_variant_count"], 3)
        self.assertEqual(summary["manager_type_count"], 1)
        self.assertEqual(summary["manager_candidate_count"], 0)
        self.assertEqual(summary["manager_object_count"], 0)

    def test_deep_scan_counts_are_reported_without_addresses(self) -> None:
        rows = _fm24_runtime_connection_rows({
            "provider_type_count": 1,
            "provider_object_count": 1,
            "valid_local_provider_count": 1,
            "provider_deep_scan": 1,
            "deep_provider_object_count": 1,
            "deep_valid_local_provider_count": 1,
            "manager_type_count": 1,
            "manager_object_count": 0,
            "manager_candidate_count": 0,
            "manager_contract_count": 0,
            "manager_contract_backref_count": 0,
            "manager_team_pointer_count": 0,
            "manager_team_type_count": 0,
            "manager_team_uid_count": 0,
            "linked_manager_count": 0,
            "manager_deep_scan": 1,
            "deep_manager_candidate_count": 0,
            "deep_linked_manager_count": 0,
        })
        report = "\n".join(rows)

        self.assertIn("深层区域补充：存档候选 1 / 有效 1", report)
        self.assertNotIn("0x", report)


if __name__ == "__main__":
    unittest.main()
