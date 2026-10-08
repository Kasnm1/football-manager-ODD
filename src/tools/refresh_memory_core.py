"""Protected fixture/result discovery core used by FMODD refreshes.

This module owns native pool validation, cross-build pool discovery and the
bounded heap fallbacks.  Higher-level refresh orchestration remains in Python.
"""

from __future__ import annotations

from bisect import bisect_left
import struct
import threading
import time
from typing import Any, Callable

from fm_collector.win32 import (
    MEM_IMAGE,
    MEM_PRIVATE,
    PAGE_EXECUTE_READWRITE,
    PAGE_EXECUTE_WRITECOPY,
    PAGE_READWRITE,
    PAGE_WRITECOPY,
    find_module,
    iter_readable_regions,
)


GAME_PLUGIN = "game_plugin.dll"
_DYNAMIC_FIXTURE_POOL_RVAS: dict[tuple[int, int], int] = {}
_DYNAMIC_FIXTURE_POOL_PROBED_AT: dict[tuple[int, int], float] = {}
_DYNAMIC_FIXTURE_POOL_LOCK = threading.RLock()
_DYNAMIC_FIXTURE_POOL_RETRY_SECONDS = 10.0


def _read_fixture_pool_at_root(
    reader: Any, root: int, cancel_check: Callable[[], bool] | None = None,
) -> tuple[list[int], list[int], int] | None:
    """Validate and read one candidate FIXTURE_DEL_ARRAY root."""
    initial_bytes = reader.returned_bytes
    for _attempt in range(2):
        if cancel_check and cancel_check():
            raise RuntimeError("refresh cancelled")
        header = reader.bytes(root, 0x40)
        if not header or len(header) != 0x40:
            return None
        name_address = struct.unpack_from("<Q", header, 0)[0]
        name = reader.bytes(name_address, 18) if name_address else None
        if not name or not name.startswith(b"FIXTURE_DEL_ARRAY\0"):
            return None

        pool_begin, pool_end, pool_capacity = struct.unpack_from("<3Q", header, 0x10)
        available, in_use, descriptor_size, descriptors_per_block = struct.unpack_from(
            "<4I", header, 0x30,
        )
        total_descriptors = available + in_use
        if (
            descriptor_size != 0x18
            or not 0 < descriptors_per_block <= 100_000
            or not 0 < in_use <= total_descriptors <= 200_000
            or not pool_begin <= pool_end <= pool_capacity
            or (pool_end - pool_begin) % 8
            or pool_end - pool_begin > 8 * 256
        ):
            return None

        block_addresses = [
            address for address in reader.ptr_array(
                pool_begin, (pool_end - pool_begin) // 8,
            )
            if address
        ]
        required_blocks = (
            total_descriptors + descriptors_per_block - 1
        ) // descriptors_per_block
        if len(block_addresses) < required_blocks:
            return None

        vectors: list[tuple[int, int]] = []
        remaining = total_descriptors
        for block_address in block_addresses[:required_blocks]:
            count = min(remaining, descriptors_per_block)
            raw = reader.bytes(block_address, count * descriptor_size)
            if not raw or len(raw) != count * descriptor_size:
                return None
            for index in range(count):
                begin, end, capacity = struct.unpack_from(
                    "<3Q", raw, index * descriptor_size,
                )
                if begin == end:
                    continue
                if (
                    not begin < end <= capacity
                    or (end - begin) % 8
                    or end - begin > 8 * 1_000_000
                ):
                    continue
                vectors.append((begin, end))
            remaining -= count

        if abs(len(vectors) - in_use) > 1 and _attempt == 0:
            continue

        object_addresses: list[int] = []
        vectors.sort()
        vector_index = 0
        while vector_index < len(vectors):
            if cancel_check and cancel_check():
                raise RuntimeError("refresh cancelled")
            group_end = vector_index + 1
            span_begin, span_end = vectors[vector_index]
            while group_end < len(vectors):
                next_begin, next_end = vectors[group_end]
                if (
                    next_begin - span_end > 0x1000
                    or next_end - span_begin > 8 * 1024 * 1024
                ):
                    break
                span_end = max(span_end, next_end)
                group_end += 1
            raw = reader.bytes(span_begin, span_end - span_begin)
            if raw and len(raw) == span_end - span_begin:
                for begin, end in vectors[vector_index:group_end]:
                    offset = begin - span_begin
                    vector_raw = raw[offset:offset + end - begin]
                    object_addresses.extend(
                        address for (address,) in struct.iter_unpack("<Q", vector_raw)
                        if address
                    )
            else:
                for begin, end in vectors[vector_index:group_end]:
                    vector_raw = reader.bytes(begin, end - begin)
                    if not vector_raw or len(vector_raw) != end - begin:
                        return None
                    object_addresses.extend(
                        address for (address,) in struct.iter_unpack("<Q", vector_raw)
                        if address
                    )
            vector_index = group_end
        object_addresses = sorted(set(object_addresses))
        if not object_addresses:
            return None

        fixture_vtable = reader.module_base + reader.layout.fixture_vtable_rva
        result_vtable = (
            reader.module_base + reader.layout.fixture_result_vtable_rva
            if reader.layout.fixture_result_vtable_rva is not None else None
        )
        fixtures: list[int] = []
        results: list[int] = []
        classified = 0
        start = 0
        while start < len(object_addresses):
            stop = start + 1
            span_start = object_addresses[start]
            while (
                stop < len(object_addresses)
                and object_addresses[stop] - object_addresses[stop - 1] <= 0x1000
                and object_addresses[stop] - span_start <= 8 * 1024 * 1024
            ):
                stop += 1
            span_end = object_addresses[stop - 1] + 8
            raw = reader.bytes(span_start, span_end - span_start)
            if raw and len(raw) == span_end - span_start:
                for address in object_addresses[start:stop]:
                    vtable = struct.unpack_from("<Q", raw, address - span_start)[0]
                    if vtable == fixture_vtable:
                        fixtures.append(address)
                        classified += 1
                    elif result_vtable is not None and vtable == result_vtable:
                        results.append(address)
                        classified += 1
            else:
                for address in object_addresses[start:stop]:
                    vtable = reader.ptr(address)
                    if vtable == fixture_vtable:
                        fixtures.append(address)
                        classified += 1
                    elif result_vtable is not None and vtable == result_vtable:
                        results.append(address)
                        classified += 1
            start = stop

        if not fixtures or classified < len(object_addresses) * 0.95:
            return None
        final_header = reader.bytes(root, 0x40)
        if (
            not final_header
            or len(final_header) != 0x40
            or final_header[0:8] != header[0:8]
            or final_header[0x10:0x28] != header[0x10:0x28]
            or final_header[0x38:0x40] != header[0x38:0x40]
        ):
            continue
        return fixtures, results, reader.returned_bytes - initial_bytes
    return None


def _dynamic_fixture_pool_candidates(
    reader: Any, cancel_check: Callable[[], bool] | None = None,
):
    """Find structural pool roots only inside the active module's writable image."""
    module = next((
        candidate for name in (GAME_PLUGIN, "fm.exe")
        if (
            (candidate := find_module(reader.process, name)) is not None
            and candidate.base_address == reader.module_base
        )
    ), None)
    if module is None:
        return

    module_end = module.base_address + module.size
    writable = {
        PAGE_READWRITE, PAGE_WRITECOPY,
        PAGE_EXECUTE_READWRITE, PAGE_EXECUTE_WRITECOPY,
    }
    descriptor_marker = struct.pack("<I", 0x18)
    seen: set[int] = set()
    for region in iter_readable_regions(reader.process):
        if cancel_check and cancel_check():
            raise RuntimeError("refresh cancelled")
        if region.type != MEM_IMAGE or region.protect not in writable:
            continue
        start = max(module.base_address, region.base_address)
        end = min(module_end, region.base_address + region.size)
        if start >= end or end - start > 32 * 1024 * 1024:
            continue
        carry = b""
        for offset in range(0, end - start, 8 * 1024 * 1024):
            if cancel_check and cancel_check():
                raise RuntimeError("refresh cancelled")
            raw = reader.bytes(
                start + offset,
                min(8 * 1024 * 1024, end - start - offset),
            )
            if not raw:
                carry = b""
                continue
            data = carry + raw
            origin = start + offset - len(carry)
            cursor = 0
            while True:
                found = data.find(descriptor_marker, cursor)
                if found < 0:
                    break
                candidate = origin + found - 0x38
                cursor = found + 1
                if candidate in seen or candidate < start or candidate % 8:
                    continue
                header_offset = candidate - origin
                if header_offset < 0 or header_offset + 0x40 > len(data):
                    continue
                available, in_use, descriptor_size, descriptors_per_block = (
                    struct.unpack_from("<4I", data, header_offset + 0x30)
                )
                if (
                    descriptor_size != 0x18
                    or not 0 < in_use <= available + in_use <= 200_000
                    or not 0 < descriptors_per_block <= 100_000
                ):
                    continue
                seen.add(candidate)
                name_address = struct.unpack_from("<Q", data, header_offset)[0]
                name = reader.bytes(name_address, 18) if name_address else None
                if name and name.startswith(b"FIXTURE_DEL_ARRAY\0"):
                    yield candidate
            carry = data[-0x40:] if len(data) >= 0x40 else data


def read_fixture_pool_addresses(
    reader: Any, cancel_check: Callable[[], bool] | None = None,
) -> tuple[list[int], list[int], int] | None:
    """Read FIXTURE_DEL_ARRAY via a fixed or runtime-discovered module root."""
    initial_bytes = reader.returned_bytes
    reader.fixture_pool_source = None
    fixed_rva = reader.layout.fixture_pool_rva
    if fixed_rva is not None:
        pooled = _read_fixture_pool_at_root(
            reader, reader.module_base + fixed_rva, cancel_check=cancel_check,
        )
        if pooled is not None:
            reader.fixture_pool_source = "fixed"
            return pooled[0], pooled[1], reader.returned_bytes - initial_bytes
        return None

    cache_key = (reader.process.pid, reader.module_base)
    with _DYNAMIC_FIXTURE_POOL_LOCK:
        cached_rva = _DYNAMIC_FIXTURE_POOL_RVAS.get(cache_key)
        last_probe = float(_DYNAMIC_FIXTURE_POOL_PROBED_AT.get(cache_key) or 0.0)
    if cached_rva is not None and cached_rva != fixed_rva:
        pooled = _read_fixture_pool_at_root(
            reader, reader.module_base + cached_rva, cancel_check=cancel_check,
        )
        if pooled is not None:
            reader.fixture_pool_source = "dynamic"
            return pooled[0], pooled[1], reader.returned_bytes - initial_bytes
        with _DYNAMIC_FIXTURE_POOL_LOCK:
            _DYNAMIC_FIXTURE_POOL_RVAS.pop(cache_key, None)
    if time.monotonic() - last_probe < _DYNAMIC_FIXTURE_POOL_RETRY_SECONDS:
        return None

    for root in _dynamic_fixture_pool_candidates(reader, cancel_check=cancel_check):
        pooled = _read_fixture_pool_at_root(reader, root, cancel_check=cancel_check)
        if pooled is None:
            continue
        with _DYNAMIC_FIXTURE_POOL_LOCK:
            _DYNAMIC_FIXTURE_POOL_RVAS[cache_key] = root - reader.module_base
            _DYNAMIC_FIXTURE_POOL_PROBED_AT.pop(cache_key, None)
        reader.fixture_pool_source = "dynamic"
        return pooled[0], pooled[1], reader.returned_bytes - initial_bytes
    with _DYNAMIC_FIXTURE_POOL_LOCK:
        _DYNAMIC_FIXTURE_POOL_PROBED_AT[cache_key] = time.monotonic()
    return None


def complete_fixture_pool_slab_addresses(
    reader: Any,
    fixture_addresses: list[int],
    result_addresses: list[int],
    cancel_check: Callable[[], bool] | None = None,
) -> tuple[list[int], list[int], int]:
    """Recover live objects omitted by active pool descriptors."""
    fixtures = set(fixture_addresses)
    results = set(result_addresses)
    if not fixtures and not results:
        return [], [], 0
    fixture_needle = struct.pack(
        "<Q", reader.module_base + reader.layout.fixture_vtable_rva,
    )
    result_rva = reader.layout.fixture_result_vtable_rva
    result_needle = (
        struct.pack("<Q", reader.module_base + result_rva)
        if result_rva is not None else None
    )
    fixture_members = sorted(fixtures)
    result_members = sorted(results)
    bytes_scanned = 0

    def has_member(addresses: list[int], start: int, end: int) -> bool:
        index = bisect_left(addresses, start)
        return index < len(addresses) and addresses[index] < end

    for region in iter_readable_regions(reader.process):
        if cancel_check and cancel_check():
            raise RuntimeError("refresh cancelled")
        if region.type != MEM_PRIVATE or not 0 < region.size <= 128 * 1024 * 1024:
            continue
        region_end = region.base_address + region.size
        scan_fixtures = has_member(fixture_members, region.base_address, region_end)
        scan_results = bool(
            result_needle
            and has_member(result_members, region.base_address, region_end)
        )
        if not scan_fixtures and not scan_results:
            continue
        carry = b""
        for offset in range(0, region.size, 8 * 1024 * 1024):
            if cancel_check and cancel_check():
                raise RuntimeError("refresh cancelled")
            size = min(8 * 1024 * 1024, region.size - offset)
            block = reader.bytes(region.base_address + offset, size)
            if not block:
                carry = b""
                continue
            bytes_scanned += len(block)
            data = carry + block
            origin = region.base_address + offset - len(carry)
            pairs = (
                (fixture_needle, fixtures) if scan_fixtures else (None, None),
                (result_needle, results) if scan_results else (None, None),
            )
            for needle, addresses in pairs:
                if needle is None or addresses is None:
                    continue
                cursor = 0
                while True:
                    found = data.find(needle, cursor)
                    if found < 0:
                        break
                    addresses.add(origin + found)
                    cursor = found + 1
            carry = data[-7:] if len(data) >= 7 else data
    return sorted(fixtures), sorted(results), bytes_scanned


def scan_fixture_addresses(
    reader: Any, cancel_check: Callable[[], bool] | None = None,
) -> tuple[list[int], int]:
    pooled = read_fixture_pool_addresses(reader, cancel_check=cancel_check)
    if pooled is not None:
        fixtures, _results, supplemental_bytes = complete_fixture_pool_slab_addresses(
            reader, pooled[0], [], cancel_check=cancel_check,
        )
        reader.last_fixture_scan_mode = "pool"
        reader.last_fixture_scan_bytes = pooled[2] + supplemental_bytes
        return fixtures, pooled[2] + supplemental_bytes

    needle = struct.pack("<Q", reader.module_base + reader.layout.fixture_vtable_rva)
    addresses: list[int] = []
    bytes_scanned = 0
    overlap = len(needle) - 1
    private_regions = [
        region for region in iter_readable_regions(reader.process)
        if region.type == MEM_PRIVATE
    ]
    dense_regions = []
    for region in private_regions:
        if cancel_check and cancel_check():
            raise RuntimeError("refresh cancelled")
        if not 0x10000 <= region.size <= 64 * 1024 * 1024:
            continue
        sample_size = min(region.size, 1024 * 1024)
        sample = reader.bytes(region.base_address, sample_size)
        if not sample:
            continue
        bytes_scanned += len(sample)
        if sample.count(needle) >= 100:
            dense_regions.append(region)
    regions = dense_regions or private_regions
    for region in regions:
        if cancel_check and cancel_check():
            raise RuntimeError("refresh cancelled")
        offset = 0
        carry = b""
        while offset < region.size:
            if cancel_check and cancel_check():
                raise RuntimeError("refresh cancelled")
            length = min(8 * 1024 * 1024, region.size - offset)
            block = reader.bytes(region.base_address + offset, length)
            if not block:
                carry = b""
                offset += length
                continue
            bytes_scanned += len(block)
            data = carry + block
            data_base = region.base_address + offset - len(carry)
            cursor = 0
            while True:
                found = data.find(needle, cursor)
                if found < 0:
                    break
                addresses.append(data_base + found)
                cursor = found + 1
            carry = data[-overlap:] if len(data) >= overlap else data
            offset += length
    reader.last_fixture_scan_mode = "heap"
    reader.last_fixture_scan_bytes = bytes_scanned
    return sorted(set(addresses)), bytes_scanned


def scan_result_addresses(
    reader: Any, cancel_check: Callable[[], bool] | None = None,
) -> tuple[list[int], int]:
    pooled = read_fixture_pool_addresses(reader, cancel_check=cancel_check)
    pooled_bytes = 0
    if pooled is not None:
        _fixtures, results, supplemental_bytes = complete_fixture_pool_slab_addresses(
            reader, [], pooled[1], cancel_check=cancel_check,
        )
        pooled_bytes = pooled[2] + supplemental_bytes
        if results:
            return results, pooled_bytes
    result_rva = reader.layout.fixture_result_vtable_rva
    if result_rva is None:
        return [], pooled_bytes
    needle = struct.pack("<Q", reader.module_base + result_rva)
    bytes_scanned = pooled_bytes
    candidate_regions = []
    for region in iter_readable_regions(reader.process):
        if cancel_check and cancel_check():
            raise RuntimeError("refresh cancelled")
        if region.type != MEM_PRIVATE or not 0x10000 <= region.size <= 128 * 1024 * 1024:
            continue
        sample = reader.bytes(region.base_address, min(region.size, 1024 * 1024))
        if not sample:
            continue
        bytes_scanned += len(sample)
        if needle in sample:
            candidate_regions.append(region)
    addresses = []
    for region in candidate_regions:
        if cancel_check and cancel_check():
            raise RuntimeError("refresh cancelled")
        block = reader.bytes(region.base_address, region.size)
        if not block:
            continue
        bytes_scanned += len(block)
        cursor = 0
        while True:
            found = block.find(needle, cursor)
            if found < 0:
                break
            addresses.append(region.base_address + found)
            cursor = found + 1
    return sorted(set(addresses)), bytes_scanned


def scan_fixture_and_result_addresses(
    reader: Any, cancel_check: Callable[[], bool] | None = None, *,
    use_pool: bool = True,
) -> tuple[list[int], list[int], int]:
    pooled = (
        read_fixture_pool_addresses(reader, cancel_check=cancel_check)
        if use_pool else None
    )
    if pooled is not None:
        fixtures, results, supplemental_bytes = complete_fixture_pool_slab_addresses(
            reader, pooled[0], pooled[1], cancel_check=cancel_check,
        )
        reader.last_fixture_scan_mode = "pool"
        reader.last_fixture_scan_bytes = pooled[2] + supplemental_bytes
        return fixtures, results, pooled[2] + supplemental_bytes

    fixture_needle = struct.pack(
        "<Q", reader.module_base + reader.layout.fixture_vtable_rva,
    )
    result_rva = reader.layout.fixture_result_vtable_rva
    result_needle = (
        struct.pack("<Q", reader.module_base + result_rva)
        if result_rva is not None else None
    )
    bytes_scanned = 0
    private_regions = [
        region for region in iter_readable_regions(reader.process)
        if region.type == MEM_PRIVATE
    ]
    regions = {
        (region.base_address, region.size): region
        for region in private_regions
        if 0x10000 <= region.size <= 64 * 1024 * 1024
    }
    fixture_keys = set(regions)
    result_keys = set(regions)
    for region in private_regions:
        if cancel_check and cancel_check():
            raise RuntimeError("refresh cancelled")
        if not 64 * 1024 * 1024 < region.size <= 128 * 1024 * 1024:
            continue
        sample_size = 64 * 1024
        offsets = {
            0, region.size // 4, region.size // 2,
            region.size * 3 // 4, region.size - sample_size,
        }
        found_result = False
        for offset in offsets:
            sample = reader.bytes(region.base_address + offset, sample_size)
            if not sample:
                continue
            bytes_scanned += len(sample)
            if result_needle and result_needle in sample:
                found_result = True
        if found_result:
            key = (region.base_address, region.size)
            regions[key] = region
            result_keys.add(key)

    fixture_addresses: list[int] = []
    result_addresses: list[int] = []
    for key, region in sorted(regions.items()):
        if cancel_check and cancel_check():
            raise RuntimeError("refresh cancelled")
        carry = b""
        for offset in range(0, region.size, 8 * 1024 * 1024):
            if cancel_check and cancel_check():
                raise RuntimeError("refresh cancelled")
            size = min(8 * 1024 * 1024, region.size - offset)
            block = reader.bytes(region.base_address + offset, size)
            if not block:
                carry = b""
                continue
            bytes_scanned += len(block)
            data = carry + block
            origin = region.base_address + offset - len(carry)
            if key in fixture_keys:
                cursor = 0
                while True:
                    found = data.find(fixture_needle, cursor)
                    if found < 0:
                        break
                    fixture_addresses.append(origin + found)
                    cursor = found + 1
            if result_needle and key in result_keys:
                cursor = 0
                while True:
                    found = data.find(result_needle, cursor)
                    if found < 0:
                        break
                    result_addresses.append(origin + found)
                    cursor = found + 1
            carry = data[-7:] if len(data) >= 7 else data
    reader.last_fixture_scan_mode = "heap"
    reader.last_fixture_scan_bytes = bytes_scanned
    return sorted(set(fixture_addresses)), sorted(set(result_addresses)), bytes_scanned
