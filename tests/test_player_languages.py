from contextlib import contextmanager
import struct
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from tools import player_languages


class FakeReader:
    def __init__(self, layout):
        self.layout = layout
        self.module_base = 0x100000
        self.memory = {}
        self.strings = {}

    def put(self, address, raw):
        self.memory[int(address)] = bytearray(raw)

    def bytes(self, address, size):
        for base, raw in self.memory.items():
            offset = int(address) - base
            if 0 <= offset and offset + int(size) <= len(raw):
                return bytes(raw[offset:offset + int(size)])
        return None

    def write(self, address, raw):
        address = int(address)
        raw = bytes(raw)
        for base, block in self.memory.items():
            offset = address - base
            if 0 <= offset and offset + len(raw) <= len(block):
                block[offset:offset + len(raw)] = raw
                return
        self.memory[address] = bytearray(raw)

    def ptr(self, address):
        raw = self.bytes(address, 8)
        return struct.unpack("<Q", raw)[0] if raw else 0

    def u32(self, address):
        raw = self.bytes(address, 4)
        return struct.unpack("<I", raw)[0] if raw else 0

    def u8(self, address):
        raw = self.bytes(address, 1)
        return raw[0] if raw else None

    def fm_string_at(self, address):
        return self.strings.get(int(address))


def harness():
    process = SimpleNamespace(handle=1)
    module = SimpleNamespace(base_address=0x100000, size=0x100000, path="fm.exe")
    layout = SimpleNamespace(
        key="fm-test", display_name="FM Test", module_name="fm.exe",
        person_languages_offset=0xF0,
    )
    layout.module = lambda _process: module
    reader = FakeReader(layout)
    language = 0x4000
    language_raw = bytearray(0x30)
    struct.pack_into("<Q", language_raw, 0, 0x100100)
    struct.pack_into("<I", language_raw, 0x0C, 77)
    reader.put(language, language_raw)
    reader.strings[language + 0x18] = "英语"

    @contextmanager
    def opened(*_args, **_kwargs):
        yield process

    patches = (
        patch.object(player_languages, "select_process_layout", return_value=(7, "fm.exe", layout)),
        patch.object(player_languages, "_resolve_language_address", return_value=(language, "英语")),
        patch.object(player_languages, "open_process", side_effect=opened),
        patch.object(player_languages, "Reader", return_value=reader),
        patch.object(player_languages, "_validated_player_person", return_value=0x2000),
        patch.object(player_languages, "write_process_memory", side_effect=lambda _process, address, raw: reader.write(address, raw)),
    )
    return reader, patches


def test_existing_language_value_has_readback_and_rollback():
    reader, patches = harness()
    container, pointers, record = 0x20F0, 0x3000, 0x3100
    reader.put(container, struct.pack("<QQQ", pointers, pointers + 8, pointers + 8))
    reader.put(pointers, struct.pack("<Q", record))
    reader.put(record, struct.pack("<QB7x", 0x4000, 3))
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
        result = player_languages.apply_player_language_level(1, 0x1000, 77, 4)
        assert reader.u8(record + 8) == 4
        player_languages.restore_player_language_level(result)
    assert reader.u8(record + 8) == 3


def test_missing_language_inserts_vector_record_and_can_restore_it():
    reader, patches = harness()
    container = 0x20F0
    reader.put(container, b"\0" * 24)
    allocations = iter(((0x5000, 0x9000), (0x6000, 0x9000)))
    freed = MagicMock()
    with (
        patches[0], patches[1], patches[2], patches[3], patches[4], patches[5],
        patch.object(player_languages, "_record_template", return_value=struct.pack("<QB7x", 0x4100, 10)),
        patch.object(player_languages, "_remote_malloc_block", side_effect=lambda *_args: next(allocations)),
        patch.object(player_languages, "_remote_free_block", side_effect=freed),
    ):
        result = player_languages.apply_player_language_level(1, 0x1000, 77, 1)
        begin, end, capacity = struct.unpack("<QQQ", reader.bytes(container, 24))
        assert (begin, end, capacity) == (0x6000, 0x6008, 0x6008)
        assert reader.ptr(0x6000) == 0x5000
        assert reader.ptr(0x5000) == 0x4000
        assert reader.u8(0x5008) == 1
        player_languages.restore_player_language_level(result)
    assert reader.bytes(container, 24) == b"\0" * 24
    assert freed.call_count == 2


def test_empty_language_list_borrows_session_reader_for_validated_template():
    layout = SimpleNamespace(
        key="fm-test", display_name="FM Test", module_name="fm.exe",
        person_languages_offset=0xF0,
    )
    module = SimpleNamespace(base_address=0x100000, size=0x100000, path="fm.exe")
    local_reader = FakeReader(layout)
    local_reader.module = module
    local_person = 0x2000
    local_reader.put(local_person + 0xF0, b"\0" * 24)

    donor_reader = FakeReader(layout)
    donor_reader.module = module
    donor_person, donor_slots, donor_record, language = 0x3000, 0x4000, 0x5000, 0x6000
    donor_reader.put(
        donor_person + 0xF0,
        struct.pack("<QQQ", donor_slots, donor_slots + 8, donor_slots + 8),
    )
    donor_reader.put(donor_slots, struct.pack("<Q", donor_record))
    donor_reader.put(donor_record, struct.pack("<QB7x", language, 7))
    language_raw = bytearray(0x30)
    struct.pack_into("<Q", language_raw, 0, 0x100100)
    struct.pack_into("<I", language_raw, 0x0C, 16)
    donor_reader.put(language, language_raw)
    donor_reader.strings[language + 0x18] = "日语"
    directory = SimpleNamespace(addresses=lambda name: (donor_person,) if name == "person" else ())

    @contextmanager
    def borrowed(_selected):
        yield donor_reader

    player_languages._TEMPLATE_CACHE.clear()
    with (
        patch.object(player_languages, "borrow_game_reader", side_effect=borrowed),
        patch.object(
            player_languages, "database_index_for_reader",
            side_effect=lambda reader: directory if reader is donor_reader else None,
        ),
    ):
        template = player_languages._record_template(
            local_reader, local_person, 7, "fm.exe",
        )

    assert template == struct.pack("<QB7x", language, 7)


def test_public_language_catalog_translates_without_merging_native_language_ids():
    rows = [
        {"id": 8, "name": "英\u200b语"},
        {"id": 1000004, "name": "英\u200b语 (美\u200b国)"},
        {"id": 4, "name": "汉\u200b语"},
        {"id": 1000114, "name": "Mandarin"},
        {"id": 1000071, "name": "Guoyu"},
        {"id": 1000002, "name": "中\u200b文 (简\u200b体)"},
        {"id": 1000115, "name": "Min Chinese"},
        {"id": 1000118, "name": "Huizhou Chinese"},
        {"id": 1000117, "name": "Xiang Chinese"},
        {"id": 122, "name": "豪\u200b萨语"},
        {"id": 1000033, "name": "Hausa"},
    ]

    catalog = player_languages.public_language_catalog(rows)
    by_id = {row["id"]: row for row in catalog}

    assert len(catalog) == len(rows)
    assert by_id[8]["name"] == "英语"
    assert by_id[1000004]["name"] == "英语 (美国)"
    assert by_id[4]["name"] == "汉语"
    assert by_id[1000114]["name"] == "中文（普通话）"
    assert by_id[1000071]["name"] == "中文（国语）"
    assert by_id[1000002]["name"] == "中文（简体）"
    assert by_id[1000115]["name"] == "中文（闽语）"
    assert by_id[1000118]["name"] == "中文（徽州话）"
    assert by_id[1000117]["name"] == "中文（湘语）"
    assert by_id[122]["name"] == by_id[1000033]["name"] == "豪萨语"
