from __future__ import annotations

import struct
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from fm_collector.win32 import MEM_IMAGE, MEM_PRIVATE, PAGE_READWRITE
from tools import refresh_memory_core
from tools.initial_data_audit import (
    Reader,
    parse_fixture_from_raw,
    read_fixture_pool_addresses,
    read_fixture_snapshots_at_addresses,
    read_result_fingerprints_at_addresses,
    scan_fixture_addresses,
    scan_fixture_and_result_addresses,
    scan_result_addresses,
)
from tools.preview_cup_odds import (
    cached_completed_results,
    cached_generic_results,
    fixture_result_candidates,
    read_result_goal_events,
    recover_persistent_results,
    recover_fixture_hint_results,
)


class FixturePoolOptimizationTests(unittest.TestCase):
    def _reader(self, *, valid_name: bool = True):
        module_base = 0x100000
        memory = bytearray(0x30000)

        def write(address: int, payload: bytes) -> None:
            offset = address - module_base
            memory[offset:offset + len(payload)] = payload

        layout = SimpleNamespace(
            fixture_pool_rva=0x1000,
            fixture_vtable_rva=0x200,
            fixture_result_vtable_rva=0x300,
        )
        root = module_base + layout.fixture_pool_rva
        name = module_base + 0x2000
        pool = module_base + 0x3000
        block_a = module_base + 0x4000
        block_b = module_base + 0x5000
        vector_a = module_base + 0x6000
        vector_b = module_base + 0x6100
        fixture_a = module_base + 0x10000
        result = module_base + 0x10080
        fixture_b = module_base + 0x10100

        header = bytearray(0x40)
        struct.pack_into("<Q", header, 0, name)
        struct.pack_into("<3Q", header, 0x10, pool, pool + 16, pool + 16)
        struct.pack_into("<4I", header, 0x30, 1, 2, 0x18, 2)
        write(root, header)
        write(name, b"FIXTURE_DEL_ARRAY\0" if valid_name else b"NOT_THE_POOL_ARRAY\0")
        write(pool, struct.pack("<2Q", block_a, block_b))
        write(block_a, b"".join((
            struct.pack("<3Q", vector_a, vector_a + 16, vector_a + 16),
            struct.pack("<3Q", vector_b, vector_b + 8, vector_b + 8),
        )))
        write(block_b, bytes(0x18))
        write(vector_a, struct.pack("<2Q", fixture_a, result))
        write(vector_b, struct.pack("<Q", fixture_b))
        write(fixture_a, struct.pack("<Q", module_base + layout.fixture_vtable_rva))
        write(result, struct.pack("<Q", module_base + layout.fixture_result_vtable_rva))
        write(fixture_b, struct.pack("<Q", module_base + layout.fixture_vtable_rva))

        reader = Reader(SimpleNamespace(pid=4242), module_base, layout)

        def read(_process, address: int, size: int):
            offset = address - module_base
            if offset < 0 or offset + size > len(memory):
                return None
            return bytes(memory[offset:offset + size])

        return reader, read, [fixture_a, fixture_b], [result]

    def test_reads_two_level_fixture_pool(self):
        reader, read, expected_fixtures, expected_results = self._reader()
        with patch("tools.initial_data_audit.read_process_memory", side_effect=read):
            fixtures, results, bytes_read = read_fixture_pool_addresses(reader)

        self.assertEqual(fixtures, expected_fixtures)
        self.assertEqual(results, expected_results)
        self.assertGreater(bytes_read, 0)

    def test_combined_scan_supplements_objects_omitted_by_pool_vectors(self):
        reader, read, expected_fixtures, expected_results = self._reader()
        hidden_fixture = 0x110200
        hidden_result = 0x110280

        def read_with_unpooled_objects(process, address, size):
            payload = read(process, address, size)
            if payload is None:
                return None
            payload = bytearray(payload)
            for object_address, vtable_rva in (
                (hidden_fixture, reader.layout.fixture_vtable_rva),
                (hidden_result, reader.layout.fixture_result_vtable_rva),
            ):
                offset = object_address - address
                if 0 <= offset <= size - 8:
                    struct.pack_into(
                        "<Q", payload, offset, reader.module_base + vtable_rva,
                    )
            return bytes(payload)

        with (
            patch(
                "tools.initial_data_audit.read_process_memory",
                side_effect=read_with_unpooled_objects,
            ),
            patch(
                "tools.refresh_memory_core.iter_readable_regions",
                return_value=[SimpleNamespace(
                    type=MEM_PRIVATE, base_address=reader.module_base, size=0x30000,
                )],
            ),
        ):
            fixtures, results, _bytes_read = scan_fixture_and_result_addresses(reader)

        self.assertEqual(fixtures, [*expected_fixtures, hidden_fixture])
        self.assertEqual(results, [*expected_results, hidden_result])

    def test_fixture_only_scan_uses_slab_completion(self):
        reader = SimpleNamespace()
        with (
            patch(
                "tools.refresh_memory_core.read_fixture_pool_addresses",
                return_value=([0x1000], [0x2000], 12),
            ),
            patch(
                "tools.refresh_memory_core.complete_fixture_pool_slab_addresses",
                return_value=([0x1000, 0x1080], [], 34),
            ) as complete,
        ):
            fixtures, bytes_read = scan_fixture_addresses(reader)

        self.assertEqual(fixtures, [0x1000, 0x1080])
        self.assertEqual(bytes_read, 46)
        complete.assert_called_once_with(
            reader, [0x1000], [], cancel_check=None,
        )

    def test_result_scan_falls_back_when_valid_pool_has_no_results(self):
        reader, read, _fixtures, expected_results = self._reader()
        with (
            patch("tools.initial_data_audit.read_process_memory", side_effect=read),
            patch(
                "tools.refresh_memory_core.read_fixture_pool_addresses",
                return_value=([0x110000], [], 12),
            ),
            patch(
                "tools.refresh_memory_core.complete_fixture_pool_slab_addresses",
                return_value=([], [], 34),
            ),
            patch(
                "tools.refresh_memory_core.iter_readable_regions",
                return_value=[SimpleNamespace(
                    type=MEM_PRIVATE, base_address=reader.module_base, size=0x30000,
                )],
            ),
        ):
            results, bytes_read = scan_result_addresses(reader)

        self.assertEqual(results, expected_results)
        self.assertGreater(bytes_read, 46)

    def test_combined_scan_can_bypass_pool_for_verified_full_read(self):
        reader, read, expected_fixtures, expected_results = self._reader()
        with (
            patch("tools.initial_data_audit.read_process_memory", side_effect=read),
            patch(
                "tools.refresh_memory_core.read_fixture_pool_addresses",
                side_effect=AssertionError("forced heap scan must bypass the pool"),
            ),
            patch(
                "tools.refresh_memory_core.iter_readable_regions",
                return_value=[SimpleNamespace(
                    type=MEM_PRIVATE, base_address=reader.module_base, size=0x30000,
                )],
            ),
        ):
            fixtures, results, _bytes_read = scan_fixture_and_result_addresses(
                reader, use_pool=False,
            )

        self.assertEqual(fixtures, expected_fixtures)
        self.assertEqual(results, expected_results)

    def test_invalid_pool_identity_falls_back(self):
        reader, read, _fixtures, _results = self._reader(valid_name=False)
        with (
            patch("tools.initial_data_audit.read_process_memory", side_effect=read),
            patch("tools.refresh_memory_core._dynamic_fixture_pool_candidates") as discover,
        ):
            self.assertIsNone(read_fixture_pool_addresses(reader))
        discover.assert_not_called()

    def test_counter_change_does_not_invalidate_structural_snapshot(self):
        reader, read, expected_fixtures, expected_results = self._reader()
        root_reads = 0

        def changing_read(process, address, size):
            nonlocal root_reads
            payload = read(process, address, size)
            if address == 0x101000 and size == 0x40:
                root_reads += 1
                if root_reads > 1:
                    payload = bytearray(payload)
                    struct.pack_into("<2I", payload, 0x30, 0, 3)
                    return bytes(payload)
            return payload

        with patch(
            "tools.initial_data_audit.read_process_memory",
            side_effect=changing_read,
        ):
            fixtures, results, _bytes_read = read_fixture_pool_addresses(reader)

        self.assertEqual(fixtures, expected_fixtures)
        self.assertEqual(results, expected_results)

    def test_layout_without_fixed_pool_discovers_validated_module_root(self):
        reader, read, expected_fixtures, expected_results = self._reader()
        reader.layout = SimpleNamespace(
            fixture_pool_rva=None,
            fixture_vtable_rva=0x200,
            fixture_result_vtable_rva=0x300,
        )
        refresh_memory_core._DYNAMIC_FIXTURE_POOL_RVAS.clear()
        refresh_memory_core._DYNAMIC_FIXTURE_POOL_PROBED_AT.clear()
        module = SimpleNamespace(
            base_address=reader.module_base, size=0x30000,
        )
        image_region = SimpleNamespace(
            type=MEM_IMAGE, protect=PAGE_READWRITE,
            base_address=reader.module_base, size=0x30000,
        )
        with (
            patch("tools.initial_data_audit.read_process_memory", side_effect=read),
            patch("tools.refresh_memory_core.find_module", return_value=module),
            patch("tools.refresh_memory_core.iter_readable_regions", return_value=[image_region]),
        ):
            fixtures, results, bytes_read = read_fixture_pool_addresses(reader)

        self.assertEqual(fixtures, expected_fixtures)
        self.assertEqual(results, expected_results)
        self.assertGreater(bytes_read, 0)
        self.assertEqual(
            refresh_memory_core._DYNAMIC_FIXTURE_POOL_RVAS[(4242, reader.module_base)],
            0x1000,
        )
        with (
            patch("tools.initial_data_audit.read_process_memory", side_effect=read),
            patch(
                "tools.refresh_memory_core._dynamic_fixture_pool_candidates",
                side_effect=AssertionError("cached pool must not rescan the module"),
            ),
        ):
            cached = read_fixture_pool_addresses(reader)
        self.assertEqual(cached[:2], (expected_fixtures, expected_results))

    def test_result_fingerprints_coalesce_adjacent_objects(self):
        base = 0x500000
        result_vtable = 0x900000
        addresses = [base, base + 0x80, base + 0x100]
        payload = bytearray(0x180)
        for address in addresses:
            struct.pack_into("<Q", payload, address - base, result_vtable)
        reader = Reader(
            object(), 0x800000,
            SimpleNamespace(fixture_result_vtable_rva=result_vtable - 0x800000),
        )

        with patch(
            "tools.initial_data_audit.read_process_memory",
            return_value=bytes(payload),
        ) as read:
            fingerprints, bytes_read = read_result_fingerprints_at_addresses(
                reader, addresses,
            )

        self.assertEqual(sorted(fingerprints), addresses)
        self.assertEqual(bytes_read, len(payload))
        read.assert_called_once_with(reader.process, base, len(payload))

    def test_fixture_headers_coalesce_adjacent_objects(self):
        reader, read, expected_fixtures, _results = self._reader()
        progress = []
        with patch("tools.initial_data_audit.read_process_memory", side_effect=read) as memory_read:
            snapshots, bytes_read = read_fixture_snapshots_at_addresses(
                reader, expected_fixtures,
                progress=lambda completed, total: progress.append((completed, total)),
            )

        self.assertEqual(sorted(snapshots), expected_fixtures)
        self.assertGreater(bytes_read, 0)
        self.assertEqual(memory_read.call_count, 1)
        self.assertEqual(progress[-1], (len(expected_fixtures), len(expected_fixtures)))

    def test_fixture_header_can_filter_by_date_before_expanding_relationships(self):
        raw = bytearray(0x58)
        struct.pack_into(
            "<QQQQQ", raw, 0x08,
            0x1000, 0x2000, 0x4000, 0x3000, 0x6000,
        )
        struct.pack_into("<I", raw, 0x4C, (2028 << 16) | 200)
        relationship_reads = []
        reader = SimpleNamespace(
            layout=SimpleNamespace(
                fixture_stage_index_offset=None,
                fixture_group_index_offset=None,
                fixture_round_number_offset=None,
            ),
            team=lambda address: relationship_reads.append(("team", address)) or {"id": 1},
            competition=lambda address: relationship_reads.append(("competition", address)) or {"id": 2},
        )

        fixture = parse_fixture_from_raw(
            reader, 0x5000, bytes(raw), validate_references=False,
        )

        self.assertEqual(fixture.match_date, date(2028, 7, 18))
        self.assertEqual(relationship_reads, [])

    def test_fixture_header_batch_honors_refresh_cancellation(self):
        reader, read, expected_fixtures, _results = self._reader()
        with patch(
            "tools.initial_data_audit.read_process_memory", side_effect=read,
        ) as memory_read:
            with self.assertRaisesRegex(RuntimeError, "refresh cancelled"):
                read_fixture_snapshots_at_addresses(
                    reader, expected_fixtures, cancel_check=lambda: True,
                )

        memory_read.assert_not_called()

    def test_result_fingerprint_batch_honors_refresh_cancellation(self):
        reader = MagicMock()
        reader.layout = SimpleNamespace(fixture_result_vtable_rva=0x300)
        reader.module_base = 0x100000
        reader.bytes.return_value = struct.pack("<Q", 0x100300) + bytes(0x78)

        with self.assertRaisesRegex(RuntimeError, "refresh cancelled"):
            read_result_fingerprints_at_addresses(
                reader, [0x200000, 0x202000],
                cancel_check=lambda: reader.bytes.call_count >= 1,
            )

    def test_cached_result_archive_cancels_between_native_objects(self):
        state = {
            "result_game_date": None,
            "generic_results": [],
            "result_addresses": [],
        }
        fingerprints = {0x2000: bytes(0x80), 0x3000: bytes(0x80)}
        parser = MagicMock(return_value={"date": "2028-06-06"})
        with (
            patch("tools.preview_cup_odds._runtime_state", return_value=state),
            patch("tools.preview_cup_odds.result_region_spans", return_value=[]),
            patch(
                "tools.preview_cup_odds.read_result_fingerprints_at_addresses",
                return_value=(fingerprints, len(fingerprints) * 0x80),
            ),
            patch(
                "tools.preview_cup_odds.parse_completed_result_from_raw", parser,
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "refresh cancelled"):
                cached_generic_results(
                    object(), "2028-06-07",
                    addresses_override=list(fingerprints),
                    cancel_check=lambda: parser.call_count >= 1,
                )
        self.assertEqual(parser.call_count, 1)

    def test_cached_result_archive_reports_parse_progress(self):
        state = {
            "result_game_date": None,
            "generic_results": [],
            "result_addresses": [],
        }
        fingerprints = {0x2000: bytes(0x80), 0x3000: bytes(0x80)}
        observed_progress = []

        def read_fingerprints(_reader, _addresses, *, progress=None, **_kwargs):
            if progress:
                progress(2, 2)
            return fingerprints, len(fingerprints) * 0x80

        with (
            patch("tools.preview_cup_odds._runtime_state", return_value=state),
            patch("tools.preview_cup_odds.result_region_spans", return_value=[]),
            patch(
                "tools.preview_cup_odds.read_result_fingerprints_at_addresses",
                side_effect=read_fingerprints,
            ),
            patch(
                "tools.preview_cup_odds.parse_completed_result_from_raw",
                side_effect=[{"id": 1}, {"id": 2}],
            ),
            patch(
                "tools.preview_cup_odds.deduplicate_results",
                side_effect=lambda rows: rows,
            ),
        ):
            rows, cache_hit = cached_generic_results(
                object(), "2028-06-07",
                addresses_override=list(fingerprints),
                progress_callback=lambda completed, total: observed_progress.append(
                    (completed, total)
                ),
            )

        self.assertFalse(cache_hit)
        self.assertEqual(rows, [{"id": 1}, {"id": 2}])
        self.assertIn((2, 4), observed_progress)
        self.assertEqual(observed_progress[-1], (4, 4))

    def test_completed_result_refresh_does_not_eagerly_read_goal_events(self):
        result = {
            "date": "2028-06-06", "competition": {"id": 100},
            "home_team": {"id": 10}, "away_team": {"id": 20},
            "home_goals": 2, "away_goals": 1,
            "result_source": "fixture_result_archive",
            "result_address": "0x2000",
        }
        state = {
            "merged_result_game_date": None,
            "merged_results": [],
            "result_addresses": [0x2000],
        }
        yield_memory_lock = Mock()
        with (
            patch("tools.preview_cup_odds._runtime_state", return_value=state),
            patch(
                "tools.preview_cup_odds.cached_generic_results",
                return_value=([result], False),
            ),
            patch(
                "tools.preview_cup_odds.fixture_result_candidates", return_value=[],
            ) as fixture_candidates,
            patch(
                "tools.preview_cup_odds.merge_verified_results",
                side_effect=lambda rows: rows,
            ),
            patch(
                "tools.preview_cup_odds.merge_result_history",
                side_effect=lambda rows, _save_id: rows,
            ),
            patch(
                "tools.preview_cup_odds.resolve_daily_team_conflicts",
                side_effect=lambda rows: rows,
            ),
            patch("tools.preview_cup_odds.parse_goal_events") as parse_events,
        ):
            rows, _cache_hit = cached_completed_results(
                object(), "2028-06-07", "career-a",
                yield_memory_lock=yield_memory_lock,
            )

        self.assertEqual(rows, [result])
        self.assertIs(
            fixture_candidates.call_args.kwargs["yield_memory_lock"],
            yield_memory_lock,
        )
        parse_events.assert_not_called()

    def test_result_goal_events_are_read_only_for_matching_live_result(self):
        expected = {
            "date": "2028-06-06", "competition": {"id": 100},
            "home_team": {"id": 10}, "away_team": {"id": 20},
            "home_goals": 2, "away_goals": 1,
            "result_source": "fixture_result_archive",
            "result_address": "0x2000",
        }
        events = [{"time": {"minute": 88}, "scorer": {"id": 9}}]
        context = MagicMock()
        reader = object()
        context.__enter__.return_value = ("fm.exe", object(), object(), reader)
        with (
            patch("tools.preview_cup_odds.open_supported_reader", return_value=context),
            patch(
                "tools.preview_cup_odds.parse_completed_result",
                return_value=dict(expected),
            ),
            patch(
                "tools.preview_cup_odds.parse_goal_events", return_value=events,
            ) as parse_events,
        ):
            observed = read_result_goal_events(expected)

        self.assertEqual(observed, events)
        self.assertIsNot(observed, events)
        parse_events.assert_called_once_with(reader, 0x2000)

    def test_fixture_result_recovery_skips_pointers_already_in_result_pool(self):
        raw = bytearray(0x58)
        struct.pack_into("<QQQQ", raw, 0x08, 0x10, 0x20, 0x9000, 0x30)
        struct.pack_into("<I", raw, 0x4C, (2028 << 16) | 1)
        performance = {}
        reader = SimpleNamespace(
            competition=lambda _address: {"id": 1},
            team=lambda address: {"id": 2 if address == 0x10 else 3},
        )
        with (
            patch("tools.preview_cup_odds.cached_fixture_addresses", return_value=([0x1000], True)),
            patch(
                "tools.preview_cup_odds.read_fixture_snapshots_at_addresses",
                return_value=({0x1000: bytes(raw)}, len(raw)),
            ),
            patch("tools.preview_cup_odds.parse_completed_result") as parse,
        ):
            recovered = fixture_result_candidates(
                reader, "2028-01-02", known_result_addresses={0x9000},
                known_result_keys={("2028-01-01", 1, 2, 3)},
                performance=performance,
            )

        self.assertEqual(recovered, [])
        self.assertEqual(performance["result_fixture_pointer_skipped_known"], 1)
        parse.assert_not_called()

    def test_fixture_result_recovery_reuses_prefetched_fixture_headers(self):
        raw = bytearray(0x58)
        struct.pack_into("<QQQQ", raw, 0x08, 0x10, 0x20, 0x9000, 0x30)
        struct.pack_into("<I", raw, 0x4C, (2028 << 16) | 1)
        performance = {}
        progress = []
        reader = SimpleNamespace(
            competition=lambda _address: {"id": 1},
            team=lambda address: {"id": 2 if address == 0x10 else 3},
        )
        with (
            patch("tools.preview_cup_odds.cached_fixture_addresses") as cached_addresses,
            patch("tools.preview_cup_odds.read_fixture_snapshots_at_addresses") as read_headers,
            patch("tools.preview_cup_odds.parse_completed_result") as parse,
        ):
            recovered = fixture_result_candidates(
                reader,
                "2028-01-02",
                known_result_addresses={0x9000},
                known_result_keys={("2028-01-01", 1, 2, 3)},
                performance=performance,
                fixture_snapshots_override={0x1000: bytes(raw)},
                progress_callback=lambda completed, total: progress.append((completed, total)),
            )

        self.assertEqual(recovered, [])
        self.assertEqual(performance["result_fixture_headers_reused"], 1)
        self.assertEqual(performance["result_fixture_header_bytes"], 0)
        self.assertEqual(progress[-1], (1, 1))
        cached_addresses.assert_not_called()
        read_headers.assert_not_called()
        parse.assert_not_called()

    def test_fixture_result_recovery_fast_skips_known_result_before_object_reads(self):
        raw = bytearray(0x58)
        struct.pack_into("<QQQQ", raw, 0x08, 0x10, 0x20, 0x9000, 0x30)
        struct.pack_into("<I", raw, 0x4C, (2028 << 16) | 1)
        performance = {}
        reader = SimpleNamespace(
            layout=None,
            competition=lambda _address: (_ for _ in ()).throw(
                AssertionError("known result should not read competition")
            ),
            team=lambda _address: (_ for _ in ()).throw(
                AssertionError("known result should not read team")
            ),
        )
        known_item = {
            "date": "2028-01-01",
            "competition": {"id": 1},
            "home_team": {"id": 2},
            "away_team": {"id": 3},
            "result_address": "0x9000",
        }
        with patch(
            "tools.preview_cup_odds.read_fixture_snapshots_at_addresses",
            return_value=({0x1000: bytes(raw)}, len(raw)),
        ):
            recovered = fixture_result_candidates(
                reader,
                "2028-01-02",
                known_result_addresses={0x9000},
                known_result_keys={("2028-01-01", 1, 2, 3)},
                known_result_items={("2028-01-01", 1, 2, 3): known_item},
                performance=performance,
                fixture_snapshots_override={0x1000: bytes(raw)},
            )

        self.assertEqual(recovered, [])
        self.assertEqual(performance["result_fixture_pointer_fast_skipped_known"], 1)
        self.assertEqual(performance["result_fixture_pointer_skipped_known"], 1)

    def test_fixture_hint_recovers_persistent_result_with_fixture_competition(self):
        key = ("2028-06-06", 100, 10, 20)
        fixture = SimpleNamespace(
            match_date=date(2028, 6, 6),
            competition_season=0x3000,
            home_team=0x1000,
            away_team=0x2000,
            result_or_state=0x4000,
        )
        competition = {"id": 100, "name": "Cup"}
        reader = SimpleNamespace(
            competition=lambda _address: competition,
            team=lambda address: (
                {"id": 10, "name": "Home"}
                if address == 0x1000 else {"id": 20, "name": "Away"}
            ),
        )
        persistent = {
            "date": "2028-06-07", "competition": {"id": 999},
            "home_team": {"id": 10}, "away_team": {"id": 20},
            "home_goals": 2, "away_goals": 1,
        }
        with (
            patch("tools.preview_cup_odds.parse_fixture", return_value=fixture),
            patch("tools.preview_cup_odds.parse_completed_result", return_value=None),
            patch("tools.preview_cup_odds.parse_season_completed_result", return_value=persistent),
        ):
            recovered = recover_fixture_hint_results(reader, {key: 0x5000})

        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0]["date"], key[0])
        self.assertEqual(recovered[0]["source_result_date"], "2028-06-07")
        self.assertEqual(recovered[0]["competition"]["id"], 100)
        self.assertEqual(recovered[0]["result_source"], "persistent_fixture_pointer")
        self.assertTrue(recovered[0]["settlement_verified"])
        self.assertEqual(
            recovered[0]["settlement_evidence"], "bet_fixture_result_pointer",
        )
        self.assertEqual(recovered[0]["fixture_address"], "0x5000")

    def test_fixture_hint_rejects_reused_address_with_different_identity(self):
        key = ("2028-06-06", 100, 10, 20)
        reused_fixture = SimpleNamespace(
            match_date=date(2028, 6, 7),
            competition_season=0x3000,
            home_team=0x1000,
            away_team=0x2000,
            result_or_state=0x4000,
        )
        reader = SimpleNamespace(
            competition=lambda _address: {"id": 100, "name": "Cup"},
            team=lambda address: (
                {"id": 10, "name": "Home"}
                if address == 0x1000 else {"id": 20, "name": "Away"}
            ),
        )

        with (
            patch(
                "tools.preview_cup_odds.parse_fixture",
                return_value=reused_fixture,
            ),
            patch("tools.preview_cup_odds.parse_completed_result") as parser,
            patch("tools.preview_cup_odds.parse_season_completed_result"),
        ):
            recovered = recover_fixture_hint_results(reader, {key: 0x5000})

        self.assertEqual(recovered, [])
        parser.assert_not_called()

    def test_persistent_recovery_accepts_unique_team_date_competition_alias(self):
        key = ("2028-06-06", 100, 10, 20)
        candidate = {
            "date": "2028-06-07", "competition": {"id": 999, "name": "Cup stage"},
            "home_team": {"id": 10}, "away_team": {"id": 20},
            "home_goals": 2, "away_goals": 1,
        }
        state = {
            "persistent_checked_game_date": None,
            "persistent_checked_keys": {},
        }
        with (
            patch("tools.preview_cup_odds._runtime_state", return_value=state),
            patch("tools.preview_cup_odds.monotonic", return_value=100.0),
            patch("tools.preview_cup_odds.scan_season_result_addresses", return_value=([1], 10)),
            patch("tools.preview_cup_odds.parse_season_completed_result", return_value=candidate),
        ):
            recovered = recover_persistent_results(object(), "2028-06-07", {key})

        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0]["date"], key[0])
        self.assertEqual(recovered[0]["source_result_date"], "2028-06-07")
        self.assertEqual(recovered[0]["competition"]["id"], 100)
        self.assertEqual(recovered[0]["result_source"], "persistent_competition_alias")

    def test_persistent_recovery_rejects_ambiguous_nearest_aliases(self):
        key = ("2028-06-06", 100, 10, 20)
        candidates = [
            {
                "date": result_date,
                "competition": {"id": competition_id},
                "home_team": {"id": 10}, "away_team": {"id": 20},
                "home_goals": 2, "away_goals": 1,
            }
            for result_date, competition_id in (
                ("2028-06-05", 998), ("2028-06-07", 999),
            )
        ]
        state = {
            "persistent_checked_game_date": None,
            "persistent_checked_keys": {},
        }
        with (
            patch("tools.preview_cup_odds._runtime_state", return_value=state),
            patch("tools.preview_cup_odds.monotonic", return_value=100.0),
            patch(
                "tools.preview_cup_odds.scan_season_result_addresses",
                return_value=([1, 2], 20),
            ),
            patch(
                "tools.preview_cup_odds.parse_season_completed_result",
                side_effect=candidates,
            ),
        ):
            recovered = recover_persistent_results(
                object(), "2028-06-08", {key},
            )

        self.assertEqual(recovered, [])

    def test_persistent_recovery_reuses_native_rows_during_scan_throttle(self):
        key = ("2028-06-06", 100, 10, 20)
        candidate = {
            "date": key[0], "competition": {"id": key[1]},
            "home_team": {"id": key[2]}, "away_team": {"id": key[3]},
            "home_goals": 2, "away_goals": 1,
            "result_source": "persistent_season_record",
            "result_address": "0x3000",
        }
        state = {
            "persistent_checked_game_date": None,
            "persistent_checked_keys": {},
        }
        with (
            patch("tools.preview_cup_odds._runtime_state", return_value=state),
            patch("tools.preview_cup_odds.monotonic", side_effect=[100.0, 110.0]),
            patch(
                "tools.preview_cup_odds.scan_season_result_addresses",
                return_value=([0x3000], 10),
            ) as scan,
            patch(
                "tools.preview_cup_odds.parse_season_completed_result",
                return_value=candidate,
            ),
        ):
            first = recover_persistent_results(object(), "2028-06-07", {key})
            second = recover_persistent_results(object(), "2028-06-07", {key})

        self.assertEqual(first, [candidate])
        self.assertEqual(second, [candidate])
        scan.assert_called_once()

    def test_persistent_recovery_preserves_conflicting_exact_scores(self):
        key = ("2028-06-06", 100, 10, 20)
        candidates = [
            {
                "date": key[0], "competition": {"id": key[1]},
                "home_team": {"id": key[2]}, "away_team": {"id": key[3]},
                "home_goals": home, "away_goals": away,
                "result_source": "persistent_season_record",
                "result_address": hex(address),
            }
            for home, away, address in ((2, 1, 0x3000), (0, 3, 0x4000))
        ]
        state = {
            "persistent_checked_game_date": None,
            "persistent_checked_keys": {},
        }
        with (
            patch("tools.preview_cup_odds._runtime_state", return_value=state),
            patch("tools.preview_cup_odds.monotonic", return_value=100.0),
            patch(
                "tools.preview_cup_odds.scan_season_result_addresses",
                return_value=([0x3000, 0x4000], 20),
            ),
            patch(
                "tools.preview_cup_odds.parse_season_completed_result",
                side_effect=candidates,
            ),
        ):
            recovered = recover_persistent_results(
                object(), "2028-06-07", {key},
            )

        self.assertEqual(
            {(item["home_goals"], item["away_goals"]) for item in recovered},
            {(2, 1), (0, 3)},
        )


if __name__ == "__main__":
    unittest.main()
