from __future__ import annotations

import struct
import threading
from typing import Any

from fm_collector.win32 import open_process, write_process_memory
from tools.club_reader import (
    _address, _remote_free_block, _remote_malloc_block,
    _validated_player_person, resolve_public_person_address,
)
from tools.database_index import database_index_for_reader
from tools.game_session import borrow_game_reader
from tools.initial_data_audit import Reader, select_process_layout


LANGUAGE_RECORD_SIZE = 0x10
LANGUAGE_VECTOR_LIMIT = 64
_TEMPLATE_LOCK = threading.RLock()
_TEMPLATE_CACHE: dict[tuple[int, str], bytes] = {}

LANGUAGE_DISPLAY_NAMES = {
    "Abaza": "阿巴扎语", "Adyghe": "阿迪格语", "Afar": "阿法尔语",
    "Akan": "阿坎语", "Altai": "阿尔泰语", "Amharic": "阿姆哈拉语",
    "Assamese": "阿萨姆语", "B'aga": "巴加语", "Balochi": "俾路支语",
    "Bari": "巴里语", "Bashkir": "巴什基尔语", "Bislama": "比斯拉马语",
    "Breton": "布列塔尼语", "Buryat": "布里亚特语", "Chechen": "车臣语",
    "Chokwe": "乔克韦语", "Chuuk": "楚克语", "Chuvash": "楚瓦什语",
    "Corsican": "科西嘉语", "Creole (Eng)": "英语克里奥尔语",
    "Crimean Tatar": "克里米亚鞑靼语", "Dagbani": "达格巴尼语",
    "Dinka": "丁卡语", "Dyula": "迪乌拉语", "Ebon": "埃邦语",
    "Erzya": "厄尔兹亚语", "Ewe": "埃维语", "Fijian": "斐济语",
    "Flemish": "弗拉芒语", "Formosan": "台湾南岛语", "Frisian": "弗里斯兰语",
    "Fulani": "富拉尼语", "Gagauz": "加告兹语", "Galician": "加利西亚语",
    "Gallo": "加洛语", "Gebeto": "盖贝托语", "Gilbertese": "吉尔伯特语",
    "Gourmanché": "古尔曼切语", "Gujarati": "古吉拉特语",
    "Haitian Creole": "海地克里奥尔语", "Harari": "哈勒尔语",
    "Hausa": "豪萨语", "Hill Mari": "山地马里语", "Hiri Motu": "希里莫图语",
    "Huizhou Chinese": "中文（徽州话）", "Hunsrik": "洪斯吕克语",
    "Igbo": "伊博语", "Ingush": "印古什语", "Jola-Fonyi": "丰伊朱拉语",
    "Kabardian": "卡巴尔达语", "Kalanga": "卡兰加语", "Kalmyk": "卡尔梅克语",
    "Kannada": "卡纳达语", "Kanuri": "卡努里语",
    "Karachay-Balkar": "卡拉恰伊-巴尔卡尔语", "Karakalpak": "卡拉卡尔帕克语",
    "Khakas": "哈卡斯语", "Khoisan": "科伊桑语", "Kimbundu": "金邦杜语",
    "Kinyarwanda": "卢旺达语", "Kituba": "基图巴语", "Komi-Zyryan": "科米-兹良语",
    "Konkani": "孔卡尼语", "Kriolu": "佛得角克里奥尔语", "Kunama": "库纳马语",
    "Luchazi": "卢查齐语", "Luo": "卢奥语", "Luvale": "卢瓦莱语",
    "Maithili": "迈蒂利语", "Mandinka": "曼丁卡语", "Mapuche": "马普切语",
    "Marathi": "马拉地语", "Mbangala": "姆班加拉语", "Meitei": "梅泰语",
    "Min Chinese": "中文（闽语）", "Moksha": "莫克沙语", "Mossi": "莫西语",
    "Murle": "穆尔勒语", "Māori": "毛利语", "Nambya": "南比亚语",
    "Ndau": "恩道语", "Nogai": "诺盖语", "Nuristani": "努里斯坦语",
    "Occitan": "奥克语", "Odia": "奥里亚语", "Oromo": "奥罗莫语",
    "Ossetian": "奥塞梯语", "Papiamentu": "帕皮阿门托语", "Pashayi": "帕沙伊语",
    "Patois": "牙买加克里奥尔语", "Pulaar": "普拉尔语", "Quiché": "基切语",
    "Romani": "罗姆语", "Rumantsch": "罗曼什语", "Rusyn": "卢森尼亚语",
    "Samoan": "萨摩亚语", "Sango": "桑戈语", "Scots": "低地苏格兰语",
    "Sena": "塞纳语", "Serakhulle": "塞拉胡里语", "Serer": "塞雷尔语",
    "Shanghainese": "中文（上海话）", "Shikomori": "科摩罗语",
    "Sichuanese": "中文（四川话）", "Sidama": "锡达莫语",
    "Sranan Tongo": "苏里南汤加语", "Taigi": "中文（台湾闽南语）",
    "Tatar": "鞑靼语", "Thok Naath": "努埃尔语", "Tibetan": "藏语",
    "Tigrinya": "提格里尼亚语", "Tiv": "蒂夫语", "Tok Pisin": "托克皮辛语",
    "Tongan": "汤加语", "Trasianka": "特拉相卡语", "Tshiluba": "奇卢巴语",
    "Tswana": "茨瓦纳语", "Tuvalu": "图瓦卢语", "Tuvan": "图瓦语",
    "Udmurt": "乌德穆尔特语", "Umbundu": "翁本杜语", "Venda": "文达语",
    "Walloon": "瓦隆语", "Xiang Chinese": "中文（湘语）", "Xitsongа": "聪加语",
    "Xitsonga": "聪加语", "Yakutian": "雅库特语", "Zande": "赞德语",
    "Zarma": "扎尔马语", "Mandarin": "中文（普通话）", "Guoyu": "中文（国语）",
    "中文 (简体)": "中文（简体）", "中文 (繁体)": "中文（繁体）",
}


def _clean_name(value: Any) -> str:
    return " ".join(
        str(value or "").replace("\x00", " ").replace("\u200b", "")
        .replace("\u200c", "").replace("\u200d", "").replace("\ufeff", "").split()
    ).strip()


def public_language_catalog(rows: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Return Chinese display labels while preserving every native language ID."""
    source = native_language_catalog() if rows is None else rows
    displayed: list[dict[str, Any]] = []
    for row in source:
        language_id = int(row.get("id") or 0)
        native_name = _clean_name(row.get("name"))
        if language_id <= 0 or not native_name:
            continue
        display_name = LANGUAGE_DISPLAY_NAMES.get(native_name, native_name)
        displayed.append({
            "id": language_id, "name": display_name, "native_name": native_name,
        })
    return sorted(
        displayed,
        key=lambda row: (
            str(row["name"]).casefold(), str(row["native_name"]).casefold(), int(row["id"]),
        ),
    )


def _valid_language_object(reader: Any, address: int, language_id: int = 0) -> dict[str, Any] | None:
    address = int(address or 0)
    if not address or address % 8:
        return None
    vtable = int(reader.ptr(address) or 0)
    module_start = int(reader.module_base)
    module_end = module_start + int(reader.module.size)
    uid = int(reader.u32(address + 0x0C) or 0)
    name = _clean_name(reader.fm_string_at(address + 0x18))
    if (
        not module_start <= vtable < module_end
        or uid <= 0 or (int(language_id) > 0 and uid != int(language_id))
        or not name or len(name) > 100
    ):
        return None
    return {"id": uid, "name": name, "address": address}


def native_language_catalog() -> list[dict[str, Any]]:
    """Read the native language table without exposing process addresses."""
    with borrow_game_reader() as reader:
        directory = database_index_for_reader(reader)
        if directory is None:
            raise RuntimeError("当前游戏会话尚未建立原生语言目录")
        rows: dict[int, dict[str, Any]] = {}
        for address in directory.addresses("language"):
            row = _valid_language_object(reader, int(address))
            if row:
                rows[int(row["id"])] = {"id": int(row["id"]), "name": str(row["name"])}
        if not rows:
            raise RuntimeError("当前游戏版本的原生语言目录校验失败")
        return sorted(rows.values(), key=lambda row: str(row["name"]).casefold())


def staff_language_learning_capability() -> dict[str, Any]:
    """Expose the implementation/verification boundary without enabling writes."""
    _pid, _path, layout = select_process_layout()
    implemented = layout.person_languages_offset is not None
    verified = implemented and bool(layout.staff_language_write_verified)
    return {
        "implemented": implemented,
        "write_enabled": verified,
        "reason": "" if verified else "职员语言写入仍待 FM24/FM26 实机回滚验证",
    }


def read_person_languages(
    person_id: int, person_address: Any, *, person_kind: str = "player",
) -> list[dict[str, Any]]:
    """Read one validated Person language vector for lazy activity UI use."""
    if person_kind not in {"player", "staff"}:
        raise ValueError("不支持的语言学习人物类型")
    with borrow_game_reader() as reader:
        address = _address(person_address)
        person = (
            _validated_player_person(reader, address, int(person_id))
            if person_kind == "player"
            else resolve_public_person_address(
                reader, {"id": int(person_id), "address": person_address}, "staff",
            )
        )
        _container, _header, begin, end, _capacity = _vector_header(reader, person)
        rows: dict[int, dict[str, Any]] = {}
        for slot in range(begin, end, 8) if begin else ():
            record = int(reader.ptr(slot) or 0)
            language = int(reader.ptr(record) or 0) if record else 0
            value = reader.u8(record + 0x08) if record else None
            language_row = _valid_language_object(reader, language) if language else None
            if language_row is None or value is None or not 0 <= int(value) <= 10:
                continue
            language_id = int(language_row["id"])
            rows[language_id] = {
                "id": language_id, "name": str(language_row["name"]),
                "proficiency": int(value), "maximum": 10,
            }
        return sorted(rows.values(), key=lambda row: str(row["name"]).casefold())


def _resolve_language_address(pid: int, path: str, layout: Any, language_id: int) -> tuple[int, str]:
    with borrow_game_reader((pid, path, layout)) as reader:
        directory = database_index_for_reader(reader)
        if directory is None:
            raise RuntimeError("当前游戏会话尚未建立原生语言目录")
        for address in directory.addresses_for_uid("language", int(language_id)):
            row = _valid_language_object(reader, int(address), int(language_id))
            if row:
                return int(row["address"]), str(row["name"])
    raise ValueError("所选语言已经不在当前游戏语言目录中")


def _vector_header(reader: Any, person: int) -> tuple[int, bytes, int, int, int]:
    offset = reader.layout.person_languages_offset
    if offset is None:
        raise RuntimeError(f"{reader.layout.display_name} 尚未适配人物语言结构")
    container = int(person) + int(offset)
    header = reader.bytes(container, 24)
    if not header or len(header) != 24:
        raise RuntimeError("无法读取球员语言列表")
    begin, end, capacity = struct.unpack("<QQQ", header)
    if not begin and not end and not capacity:
        return container, header, 0, 0, 0
    if (
        not begin or begin > end or end > capacity
        or (end - begin) % 8 or (capacity - begin) % 8
        or (end - begin) // 8 > LANGUAGE_VECTOR_LIMIT
        or (capacity - begin) // 8 > LANGUAGE_VECTOR_LIMIT * 4
    ):
        raise RuntimeError("球员语言列表结构无效")
    return container, header, int(begin), int(end), int(capacity)


def _record_template_from_directory(reader: Any) -> bytes | None:
    directory = database_index_for_reader(reader)
    if directory is None:
        return None
    for donor in directory.addresses("person")[:8192]:
        try:
            _container, _header, begin, end, _capacity = _vector_header(
                reader, int(donor),
            )
            record = int(reader.ptr(begin) or 0) if begin and end > begin else 0
            language = int(reader.ptr(record) or 0) if record else 0
            value = reader.u8(record + 0x08) if record else None
            raw = reader.bytes(record, LANGUAGE_RECORD_SIZE) if record else None
            if (
                raw and len(raw) == LANGUAGE_RECORD_SIZE
                and value is not None and 0 <= int(value) <= 10
                and _valid_language_object(reader, language) is not None
            ):
                return bytes(raw)
        except (OSError, RuntimeError, TypeError, ValueError):
            continue
    return None


def _record_template(
    reader: Any, person: int, pid: int, process_path: str,
) -> bytes:
    _container, _header, begin, end, _capacity = _vector_header(reader, person)
    if begin and end > begin:
        record = int(reader.ptr(begin) or 0)
        language = int(reader.ptr(record) or 0) if record else 0
        value = reader.u8(record + 0x08) if record else None
        raw = reader.bytes(record, LANGUAGE_RECORD_SIZE) if record else None
        if (
            raw and len(raw) == LANGUAGE_RECORD_SIZE
            and value is not None and 0 <= int(value) <= 10
            and _valid_language_object(reader, language) is not None
        ):
            return bytes(raw)
    cache_key = (int(pid), str(reader.layout.key))
    with _TEMPLATE_LOCK:
        cached = _TEMPLATE_CACHE.get(cache_key)
    if cached:
        return cached
    template = _record_template_from_directory(reader)
    if template is None:
        # Write-capable Readers are intentionally local and do not own the
        # session database-index provider. Borrow a read-only Reader bound to
        # the same process/layout to locate a validated donor record.
        with borrow_game_reader((int(pid), str(process_path), reader.layout)) as donor_reader:
            template = _record_template_from_directory(donor_reader)
    if template is not None:
        with _TEMPLATE_LOCK:
            _TEMPLATE_CACHE[cache_key] = template
        return template
    raise RuntimeError("当前会话中没有找到可验证的语言记录模板")


def apply_person_language_level(
    person_id: int, person_address: Any, language_id: int, target_level: int,
    *, person_kind: str = "player",
) -> dict[str, Any]:
    """Raise a player/staff Person language with identity checks and rollback."""
    player_id, player_address = int(person_id), person_address
    if person_kind not in {"player", "staff"}:
        raise ValueError("不支持的语言学习人物类型")
    target_level = int(target_level)
    if not 1 <= target_level <= 10:
        raise ValueError("语言熟练度目标必须在 1 到 10 之间")
    pid, path, layout = select_process_layout()
    if person_kind == "staff" and not bool(layout.staff_language_write_verified):
        raise RuntimeError("职员语言写入仍待 FM24/FM26 实机回滚验证")
    if layout.person_languages_offset is None:
        raise RuntimeError(f"{layout.display_name} 尚未适配人物语言结构")
    language_address, language_name = _resolve_language_address(
        int(pid), str(path), layout, int(language_id),
    )
    with open_process(pid, write_memory=True) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        reader = Reader(process, module.base_address, layout)
        # Shared-session Readers already expose the resolved module. Mirror
        # that contract for these local write-capable Readers because language
        # object validation needs the module bounds for its vtable check.
        reader.module = module
        player = _address(player_address)
        person = (
            _validated_player_person(reader, player, int(player_id))
            if person_kind == "player"
            else resolve_public_person_address(
                reader, {"id": player_id, "address": player_address}, "staff",
            )
        )
        language = _valid_language_object(reader, language_address, int(language_id))
        if not language:
            raise RuntimeError("所选语言对象校验失败")
        container, header, begin, end, capacity = _vector_header(reader, person)
        for slot in range(begin, end, 8) if begin else ():
            record = int(reader.ptr(slot) or 0)
            if not record or int(reader.ptr(record) or 0) != language_address:
                continue
            before = reader.u8(record + 0x08)
            if before is None or not 0 <= int(before) <= 10:
                raise RuntimeError("球员语言熟练度结构无效")
            if int(before) >= target_level:
                return {
                    "player_id": int(player_id), "person_id": int(player_id),
                    "person_kind": person_kind, "language_id": int(language_id),
                    "language_name": language_name, "before": int(before),
                    "after": int(before), "modified": False, "_rollback": {},
                }
            write_process_memory(process, record + 0x08, bytes((target_level,)))
            if reader.u8(record + 0x08) != target_level:
                write_process_memory(process, record + 0x08, bytes((int(before),)))
                if reader.u8(record + 0x08) != int(before):
                    raise RuntimeError("语言熟练度写入失败且原值回滚异常")
                raise RuntimeError("语言熟练度写入回读校验失败")
            return {
                "player_id": int(player_id), "person_id": int(player_id),
                "person_kind": person_kind, "language_id": int(language_id),
                "language_name": language_name, "before": int(before),
                "after": target_level, "modified": True,
                "_rollback": {
                    "kind": "value", "pid": int(pid), "layout_key": str(layout.key),
                    "module_base": int(module.base_address), "player_id": int(player_id),
                    "person_kind": person_kind,
                    "player_address": int(player), "language_address": int(language_address),
                    "record": int(record), "before": int(before), "after": target_level,
                },
            }

        template = bytearray(_record_template(reader, person, int(pid), str(path)))
        struct.pack_into("<Q", template, 0, int(language_address))
        template[0x08] = target_level
        allocations: list[tuple[int, int]] = []
        slot_before = b""
        header_after = b""
        record = 0
        try:
            record, record_free = _remote_malloc_block(process, LANGUAGE_RECORD_SIZE)
            allocations.append((int(record_free), int(record)))
            write_process_memory(process, record, bytes(template))
            pointer_raw = struct.pack("<Q", int(record))
            if begin and capacity - end >= 8:
                slot_before = reader.bytes(end, 8) or b""
                if len(slot_before) != 8:
                    raise RuntimeError("无法读取球员语言列表备用空间")
                write_process_memory(process, end, pointer_raw)
                header_after = struct.pack("<QQQ", begin, end + 8, capacity)
                write_process_memory(process, container, header_after)
                inserted_slot = end
            else:
                existing = reader.bytes(begin, end - begin) if begin and end > begin else b""
                if existing is None or len(existing) != max(0, end - begin):
                    raise RuntimeError("无法完整读取球员现有语言列表")
                vector, vector_free = _remote_malloc_block(process, len(existing) + 8)
                allocations.append((int(vector_free), int(vector)))
                write_process_memory(process, vector, bytes(existing) + pointer_raw)
                header_after = struct.pack(
                    "<QQQ", vector, vector + len(existing) + 8, vector + len(existing) + 8,
                )
                write_process_memory(process, container, header_after)
                inserted_slot = vector + len(existing)
            if (
                reader.bytes(container, 24) != header_after
                or int(reader.ptr(inserted_slot) or 0) != int(record)
                or int(reader.ptr(record) or 0) != int(language_address)
                or reader.u8(record + 0x08) != target_level
            ):
                raise RuntimeError("新增球员语言记录回读校验失败")
        except Exception as error:
            restored = False
            try:
                write_process_memory(process, container, header)
                if slot_before:
                    write_process_memory(process, end, slot_before)
                restored = reader.bytes(container, 24) == header
            finally:
                if restored:
                    for free_address, allocation in reversed(allocations):
                        _remote_free_block(process, free_address, allocation)
            if not restored:
                raise RuntimeError("新增语言记录失败且语言列表回滚异常") from error
            raise
        return {
            "player_id": int(player_id), "person_id": int(player_id),
            "person_kind": person_kind, "language_id": int(language_id),
            "language_name": language_name, "before": 0, "after": target_level,
            "modified": True,
            "_rollback": {
                "kind": "insert", "pid": int(pid), "layout_key": str(layout.key),
                "module_base": int(module.base_address), "player_id": int(player_id),
                "person_kind": person_kind,
                "player_address": int(player), "container": int(container),
                "header_before": header, "header_after": header_after,
                "slot": int(end) if slot_before else 0, "slot_before": slot_before,
                "allocations": [
                    {"free_address": free_address, "address": allocation}
                    for free_address, allocation in allocations
                ],
            },
        }


def restore_person_language_level(result: dict[str, Any]) -> None:
    rollback = dict(result.get("_rollback") or {})
    if not rollback:
        return
    pid, _path, layout = select_process_layout()
    if int(pid) != int(rollback.get("pid") or 0) or str(layout.key) != rollback.get("layout_key"):
        raise RuntimeError("游戏进程已经变化，无法安全回滚语言学习")
    with open_process(pid, write_memory=True) as process:
        module = layout.module(process)
        if not module or int(module.base_address) != int(rollback.get("module_base") or 0):
            raise RuntimeError("游戏模块已经变化，无法安全回滚语言学习")
        reader = Reader(process, module.base_address, layout)
        reader.module = module
        player = int(rollback.get("player_address") or 0)
        person_kind = str(rollback.get("person_kind") or "player")
        if person_kind == "player":
            _validated_player_person(reader, player, int(rollback.get("player_id") or 0))
        else:
            resolve_public_person_address(reader, {
                "id": int(rollback.get("player_id") or 0), "address": player,
            }, "staff")
        if rollback.get("kind") == "value":
            record = int(rollback.get("record") or 0)
            if (
                int(reader.ptr(record) or 0) != int(rollback.get("language_address") or 0)
                or reader.u8(record + 0x08) != int(rollback.get("after") or 0)
            ):
                raise RuntimeError("语言记录已经变化，拒绝覆盖后续修改")
            before = int(rollback.get("before") or 0)
            write_process_memory(process, record + 0x08, bytes((before,)))
            if reader.u8(record + 0x08) != before:
                raise RuntimeError("语言熟练度回滚回读校验失败")
            return
        container = int(rollback.get("container") or 0)
        if reader.bytes(container, 24) != rollback.get("header_after"):
            raise RuntimeError("球员语言列表已经变化，拒绝覆盖后续修改")
        write_process_memory(process, container, bytes(rollback["header_before"]))
        slot = int(rollback.get("slot") or 0)
        if slot:
            write_process_memory(process, slot, bytes(rollback.get("slot_before") or b""))
        if reader.bytes(container, 24) != rollback.get("header_before"):
            raise RuntimeError("球员语言列表回滚回读校验失败")
        for allocation in reversed(list(rollback.get("allocations") or [])):
            _remote_free_block(
                process, int(allocation.get("free_address") or 0),
                int(allocation.get("address") or 0),
            )


def apply_player_language_level(
    player_id: int, player_address: Any, language_id: int, target_level: int,
) -> dict[str, Any]:
    """Backward-compatible player wrapper for v1 language-course state."""
    return apply_person_language_level(
        player_id, player_address, language_id, target_level, person_kind="player",
    )


def restore_player_language_level(result: dict[str, Any]) -> None:
    restore_person_language_level(result)


__all__ = [
    "apply_person_language_level", "apply_player_language_level", "native_language_catalog",
    "restore_person_language_level", "restore_player_language_level",
]
