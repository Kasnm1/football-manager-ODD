import struct
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tools import person_relationships
from tools.person_relationships import (
    invalidate_relationship_caches, read_person_relationships,
    read_relationship_pair, rollback_bidirectional_intimacy,
    write_bidirectional_intimacy,
)
from fm_collector.win32 import write_process_memory as native_write_process_memory


class FakeReader:
    def __init__(self, records: dict[int, bytes]):
        self.layout = SimpleNamespace(person_relationships_offset=0x78)
        self.process = SimpleNamespace(pid=42)
        self.session_generation = 9
        self._memory = {}
        for person, raw in records.items():
            container = person + 0x1000
            begin = person + 0x2000
            self._memory[person + 0x78] = container
            self._memory[container] = begin
            self._memory[container + 8] = begin + len(raw)
            self._memory[container + 16] = begin + len(raw)
            self._memory[(begin, len(raw))] = raw

    def ptr(self, address):
        return self._memory.get(address, 0)

    def bytes(self, address, size):
        return self._memory.get((address, size))


class WritableFakeReader:
    def __init__(self, records: dict[int, bytes]):
        self.layout = SimpleNamespace(person_relationships_offset=0x78)
        self.process = SimpleNamespace(pid=42)
        self.session_generation = 9
        self._memory: dict[int, int] = {}
        self.locations: dict[int, tuple[int, int]] = {}
        for person, raw in records.items():
            container = person + 0x1000
            begin = person + 0x2000
            header = struct.pack("<QQQ", begin, begin + len(raw), begin + len(raw))
            self.write(container, header)
            self.write(person + 0x78, struct.pack("<Q", container))
            self.write(begin, raw)
            self.locations[person] = (container, begin)

    def write(self, address, raw):
        for offset, value in enumerate(raw):
            self._memory[address + offset] = value

    def ptr(self, address):
        raw = self.bytes(address, 8)
        return struct.unpack("<Q", raw)[0] if raw else 0

    def bytes(self, address, size):
        try:
            return bytes(self._memory[address + offset] for offset in range(size))
        except KeyError:
            return None

    def u8(self, address):
        return self._memory.get(address)

    def snapshot(self, person):
        container, begin = self.locations[person]
        header = self.bytes(container, 24)
        _, end, _ = struct.unpack("<QQQ", header)
        return header, self.bytes(begin, end - begin)


def record(target, *, reason=5, object_type=3, relation_type=1, level=50, permanence=0):
    return struct.pack("<QHBBBBBB", target, reason, object_type, relation_type,
                       level, permanence, 0, 0xFF)


def test_full_record_model_preserves_sign_reason_and_permanence():
    invalidate_relationship_caches()
    reader = FakeReader({0x10000: record(
        0x20000, reason=10, relation_type=2, level=20, permanence=79,
    )})
    result = read_person_relationships(reader, 0x10000, scope_id="save-a", data_version=3)
    assert result["person_relations"] == {"0x20000": -20}
    assert result["records"] == [{
        "target_key": "0x20000", "target_address": 0x20000,
        "reason": 10, "object_type": 3, "relation_type": 2,
        "level": 20, "signed_score": -20, "permanence": 79,
        "record_address": 0x12000,
    }]


def test_pair_keeps_asymmetric_directions_instead_of_averaging():
    reader = FakeReader({
        0x10000: record(0x20000, reason=5, relation_type=1, level=60),
        0x20000: record(0x10000, reason=10, relation_type=2, level=20),
    })
    result = read_relationship_pair(reader, 0x10000, 0x20000)
    assert result["a_to_b"]["signed_score"] == 60
    assert result["a_to_b"]["reason"] == 5
    assert result["b_to_a"]["signed_score"] == -20
    assert result["b_to_a"]["reason"] == 10


def test_cached_result_is_not_mutable_by_callers():
    invalidate_relationship_caches()
    reader = FakeReader({0x10000: record(0x20000)})
    first = read_person_relationships(reader, 0x10000)
    first["records"][0]["level"] = 1
    second = read_person_relationships(reader, 0x10000)
    assert second["records"][0]["level"] == 50


def test_bidirectional_writer_applies_independent_directional_points():
    operation = SimpleNamespace(reader=object(), process=object(), writable=True)
    writes = [
        {"before": 10, "after": 12, "applied": 2},
        {"before": 20, "after": 25, "applied": 5},
    ]
    with patch.object(
        person_relationships, "_write_person_relationship", side_effect=writes,
    ) as write:
        result = write_bidirectional_intimacy(
            0x1000, 0x2000, reason_a2b=5, reason_b2a=9,
            points_a2b=2, points_b2a=5, operation=operation,
        )
    assert result["a_to_b"]["applied"] == 2
    assert result["b_to_a"]["applied"] == 5
    assert write.call_args_list[0].args[2:4] == (0x1000, 0x2000)
    assert write.call_args_list[0].kwargs["default_reason"] == 5
    assert write.call_args_list[0].kwargs["points"] == 2
    assert write.call_args_list[1].args[2:4] == (0x2000, 0x1000)
    assert write.call_args_list[1].kwargs["default_reason"] == 9
    assert write.call_args_list[1].kwargs["points"] == 5


def test_writer_imports_the_native_memory_dependency():
    assert person_relationships.write_process_memory is native_write_process_memory


def test_existing_bidirectional_records_rollback_byte_for_byte():
    reader = WritableFakeReader({
        0x10000: record(0x20000, reason=5, level=10),
        0x20000: record(0x10000, reason=8, level=20),
    })
    operation = SimpleNamespace(reader=reader, process=reader.process, writable=True)
    before = {
        person: reader.snapshot(person) for person in (0x10000, 0x20000)
    }

    with patch.object(
        person_relationships, "write_process_memory",
        side_effect=lambda _process, address, raw: reader.write(address, raw),
    ):
        result = write_bidirectional_intimacy(
            0x10000, 0x20000, points=3, operation=operation,
        )
        assert reader.u8(reader.locations[0x10000][1] + 12) == 13
        assert reader.u8(reader.locations[0x20000][1] + 12) == 23
        rollback_bidirectional_intimacy(result, operation=operation)

    assert {
        person: reader.snapshot(person) for person in (0x10000, 0x20000)
    } == before


def test_existing_first_direction_rolls_back_when_second_direction_fails():
    reader = WritableFakeReader({
        0x10000: record(0x20000, reason=5, level=10),
        0x20000: record(0x10000, reason=8, level=20),
    })
    operation = SimpleNamespace(reader=reader, process=reader.process, writable=True)
    before = {
        person: reader.snapshot(person) for person in (0x10000, 0x20000)
    }
    second_level_address = reader.locations[0x20000][1] + 12
    failed = False

    def write_with_second_direction_failure(_process, address, raw):
        nonlocal failed
        if address == second_level_address and raw == bytes([21]) and not failed:
            failed = True
            raise RuntimeError("forced second direction failure")
        reader.write(address, raw)

    with patch.object(
        person_relationships, "write_process_memory",
        side_effect=write_with_second_direction_failure,
    ):
        try:
            write_bidirectional_intimacy(
                0x10000, 0x20000, points=1, operation=operation,
            )
        except RuntimeError as error:
            assert "forced second direction failure" in str(error)
        else:
            raise AssertionError("second direction failure was not raised")

    assert failed is True
    assert {
        person: reader.snapshot(person) for person in (0x10000, 0x20000)
    } == before


def test_existing_rollback_rejects_a_changed_record():
    reader = WritableFakeReader({
        0x10000: record(0x20000, reason=5, level=10),
    })
    operation = SimpleNamespace(reader=reader, process=reader.process, writable=True)

    with patch.object(
        person_relationships, "write_process_memory",
        side_effect=lambda _process, address, raw: reader.write(address, raw),
    ):
        result = person_relationships._write_person_relationship(
            reader, reader.process, 0x10000, 0x20000, points=1,
        )
        reader.write(reader.locations[0x10000][1] + 12, bytes([12]))
        try:
            person_relationships._rollback_person_relationship(
                reader, reader.process, result["undo"],
            )
        except RuntimeError as error:
            assert "已被其他操作改变" in str(error)
        else:
            raise AssertionError("changed record was overwritten during rollback")

    assert reader.u8(reader.locations[0x10000][1] + 12) == 12


def test_bidirectional_rollback_validates_both_directions_before_writing():
    reader = WritableFakeReader({
        0x10000: record(0x20000, reason=5, level=10),
        0x20000: record(0x10000, reason=8, level=20),
    })
    operation = SimpleNamespace(reader=reader, process=reader.process, writable=True)

    with patch.object(
        person_relationships, "write_process_memory",
        side_effect=lambda _process, address, raw: reader.write(address, raw),
    ):
        result = write_bidirectional_intimacy(
            0x10000, 0x20000, points=1, operation=operation,
        )
        first_direction = reader.locations[0x10000][1] + 12
        second_direction = reader.locations[0x20000][1] + 12
        reader.write(first_direction, bytes([12]))
        try:
            rollback_bidirectional_intimacy(result, operation=operation)
        except RuntimeError as error:
            assert "已被其他操作改变" in str(error)
        else:
            raise AssertionError("changed direction was not detected before rollback")

    assert reader.u8(first_direction) == 12
    assert reader.u8(second_direction) == 21
