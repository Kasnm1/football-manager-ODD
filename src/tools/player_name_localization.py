from __future__ import annotations

import os
import json
import re
import unicodedata
import xml.etree.ElementTree as ET
from functools import lru_cache
from pathlib import Path
from threading import RLock
from typing import Any


_CHANGE_PLAYER_NAME = re.compile(
    r'^\s*"CHANGE_PLAYER_NAME"\s+(\d+)\s+"([^"]*)"\s+"([^"]*)"\s+"([^"]*)"',
    re.IGNORECASE | re.MULTILINE,
)
_CACHE_LOCK = RLock()
_CACHE: dict[tuple[str, str, tuple[tuple[str, int, int], ...]], dict[int, str]] = {}
_XML_CACHE: dict[tuple[str, int, int], dict[int, str]] = {}
_XML_PART_CACHE: dict[tuple[str, int, int], dict[str, dict[str, str]]] = {}
_BUNDLED_CACHE: dict[tuple[str, int, int], dict[int, str]] = {}
_BUNDLED_PART_CACHE: dict[tuple[str, int, int], dict[str, dict[str, str]]] = {}
_GUESS_CACHE: dict[tuple[str, int, int], dict[str, str]] = {}

# FM Editor XML database-change property IDs.  The first property is the
# player's common/display name; the second is the full name used as a
# fallback when a common name is absent.
_XML_COMMON_NAME_PROPERTY = "1348693601"
_XML_FULL_NAME_PROPERTY = "1348889710"
_XML_SURNAME_PROPERTY = "1349742177"
_XML_FIRST_NAME_PROPERTY = "1348890209"

# This is deliberately a small, dependency-free fallback.  It is not an
# official name database: it only keeps an otherwise untranslated Latin name
# readable in Chinese when UID/XML/known-part data is unavailable.  Longer
# graphemes are checked first, then common consonant+vowel pairs, and finally
# individual letters.  The authoritative dictionaries always run before it.
_PHONETIC_CHUNKS: tuple[tuple[str, str], ...] = (
    ("sch", "施"), ("tch", "奇"), ("cia", "恰"), ("cio", "乔"),
    ("eau", "欧"), ("augh", "奥"), ("ough", "奥"), ("tion", "申"),
    ("sion", "申"), ("ph", "夫"), ("th", "斯"), ("sh", "什"),
    ("ch", "奇"), ("ck", "克"), ("qu", "夸"), ("wh", "沃"),
    ("wr", "若"), ("kn", "恩"), ("gn", "尼"), ("ng", "恩"),
    ("nk", "恩克"), ("ts", "茨"), ("tz", "茨"), ("x", "克斯"),
    ("aa", "阿"), ("ae", "埃"), ("ai", "艾"), ("ay", "艾"),
    ("au", "奥"), ("aw", "奥"), ("ea", "伊"), ("ee", "伊"),
    ("ei", "艾"), ("ey", "艾"), ("ie", "伊"), ("oa", "欧"),
    ("oe", "欧"), ("oi", "瓦"), ("oo", "乌"), ("ou", "欧"),
    ("ow", "欧"), ("ue", "韦"), ("ui", "韦"), ("ya", "亚"),
    ("ye", "耶"), ("yo", "约"), ("yu", "尤"),
    ("ba", "巴"), ("be", "贝"), ("bi", "比"), ("bo", "博"), ("bu", "布"),
    ("ca", "卡"), ("ce", "塞"), ("ci", "西"), ("co", "科"), ("cu", "库"),
    ("da", "达"), ("de", "德"), ("di", "迪"), ("do", "多"), ("du", "杜"),
    ("fa", "法"), ("fe", "费"), ("fi", "菲"), ("fo", "福"), ("fu", "富"),
    ("ga", "加"), ("ge", "盖"), ("gi", "吉"), ("go", "戈"), ("gu", "古"),
    ("ha", "哈"), ("he", "赫"), ("hi", "希"), ("ho", "霍"), ("hu", "胡"),
    ("ja", "贾"), ("je", "杰"), ("ji", "吉"), ("jo", "乔"), ("ju", "朱"),
    ("ka", "卡"), ("ke", "凯"), ("ki", "基"), ("ko", "科"), ("ku", "库"),
    ("la", "拉"), ("le", "莱"), ("li", "利"), ("lo", "洛"), ("lu", "卢"),
    ("ma", "马"), ("me", "梅"), ("mi", "米"), ("mo", "莫"), ("mu", "穆"),
    ("na", "纳"), ("ne", "内"), ("ni", "尼"), ("no", "诺"), ("nu", "努"),
    ("pa", "帕"), ("pe", "佩"), ("pi", "皮"), ("po", "波"), ("pu", "普"),
    ("ra", "拉"), ("re", "雷"), ("ri", "里"), ("ro", "罗"), ("ru", "鲁"),
    ("sa", "萨"), ("se", "塞"), ("si", "西"), ("so", "索"), ("su", "苏"),
    ("ta", "塔"), ("te", "特"), ("ti", "蒂"), ("to", "托"), ("tu", "图"),
    ("va", "瓦"), ("ve", "维"), ("vi", "维"), ("vo", "沃"), ("vu", "武"),
    ("wa", "瓦"), ("we", "韦"), ("wi", "威"), ("wo", "沃"), ("wu", "乌"),
    ("za", "扎"), ("ze", "泽"), ("zi", "齐"), ("zo", "佐"), ("zu", "祖"),
)
_PHONETIC_CHUNKS = tuple(sorted(_PHONETIC_CHUNKS, key=lambda item: len(item[0]), reverse=True))
_PHONETIC_LETTERS = {
    "a": "阿", "b": "布", "c": "克", "d": "德", "e": "伊", "f": "弗",
    "g": "格", "h": "赫", "i": "伊", "j": "杰", "k": "克", "l": "尔",
    "m": "姆", "n": "恩", "o": "奥", "p": "普", "q": "夸", "r": "尔",
    "s": "斯", "t": "特", "u": "乌", "v": "维", "w": "沃", "y": "伊",
    "z": "泽",
}
_PHONETIC_PREFIXES = {
    "al": "阿尔", "el": "埃尔", "van": "范", "von": "冯", "de": "德",
    "da": "达", "del": "德尔", "di": "迪", "dos": "多斯", "das": "达斯",
    "du": "杜", "bin": "本", "ben": "本", "ibn": "伊本", "la": "拉",
    "le": "勒", "ter": "特尔",
}


def _contains_cjk(value: str) -> bool:
    return any(
        "\u3400" <= char <= "\u4dbf"
        or "\u4e00" <= char <= "\u9fff"
        or "\uf900" <= char <= "\ufaff"
        for char in value
    )


def _read_lnc(path: Path) -> str:
    raw = path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return raw.decode("utf-16")
        except UnicodeDecodeError:
            return ""
    for encoding in ("utf-8-sig", "utf-16", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return ""


def parse_player_name_lnc(path: Path) -> dict[int, str]:
    """Read CJK display names from FM's UID-scoped CHANGE_PLAYER_NAME rows."""
    try:
        text = _read_lnc(Path(path))
    except OSError:
        return {}
    names: dict[int, str] = {}
    for match in _CHANGE_PLAYER_NAME.finditer(text):
        uid = int(match.group(1))
        first_name, common_name, second_name = (
            match.group(index).strip() for index in (2, 3, 4)
        )
        chinese_name = next(
            (
                value for value in (common_name, first_name, second_name)
                if value and _contains_cjk(value)
            ),
            "",
        )
        if uid > 0 and chinese_name and len(chinese_name) <= 40:
            names[uid] = chinese_name
    return names


def parse_player_name_xml(path: Path) -> dict[int, str]:
    """Read player UID/name changes from an FM Editor XML export.

    The exported ``数据库人员汉化.xml`` is a database-change document rather
    than an ``.lnc`` file.  Player rows use table type ``1`` and encode the
    stable FM UID in the low 32 bits of ``db_unique_id``.  Parsing is streamed
    so the 35 MB export does not remain resident as an XML tree.
    """
    try:
        rows: dict[int, dict[str, str]] = {}
        for _event, element in ET.iterparse(Path(path), events=("end",)):
            if element.tag != "record":
                continue
            fields = {
                child.attrib.get("id", ""): child.attrib.get("value", "")
                for child in element
                if child.tag in {"integer", "unsigned", "large", "string"}
            }
            if fields.get("database_table_type") != "1":
                element.clear()
                continue
            raw_uid = fields.get("db_unique_id", "")
            property_id = fields.get("property", "")
            value = str(fields.get("new_value", "") or "").strip()
            if not raw_uid or not property_id or not value:
                element.clear()
                continue
            if property_id not in {
                _XML_COMMON_NAME_PROPERTY,
                _XML_FULL_NAME_PROPERTY,
                _XML_SURNAME_PROPERTY,
            } or not _contains_cjk(value) or len(value) > 40:
                element.clear()
                continue
            try:
                uid = int(raw_uid) & 0xFFFFFFFF
            except (TypeError, ValueError):
                element.clear()
                continue
            if uid > 0:
                rows.setdefault(uid, {})[property_id] = value
            element.clear()
        return {
            uid: values.get(_XML_COMMON_NAME_PROPERTY)
            or values.get(_XML_FULL_NAME_PROPERTY)
            or values.get(_XML_SURNAME_PROPERTY)
            for uid, values in rows.items()
            if values.get(_XML_COMMON_NAME_PROPERTY)
            or values.get(_XML_FULL_NAME_PROPERTY)
            or values.get(_XML_SURNAME_PROPERTY)
        }
    except (ET.ParseError, OSError, ValueError, TypeError):
        return {}


def _normalise_english_name(value: str) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _latin_name_token(value: str) -> str:
    """Fold accents and punctuation for the offline phonetic fallback."""
    folded = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(char for char in folded if not unicodedata.combining(char)).casefold()


@lru_cache(maxsize=16384)
def _phonetic_token(value: str) -> str:
    """Return a conservative Chinese reading for one Latin name token.

    The output is intentionally a best-effort reading rather than a claim of
    official localization.  Callers only use it after all UID/XML mappings and
    unambiguous name-part mappings have been exhausted.
    """
    token = _latin_name_token(value).strip()
    if not token or not re.fullmatch(r"[a-z]+(?:[-'][a-z]+)*", token):
        return ""
    if token in _PHONETIC_PREFIXES:
        return _PHONETIC_PREFIXES[token]
    result: list[str] = []
    for part_index, part in enumerate(re.split(r"[-']+", token)):
        if not part:
            continue
        chars: list[str] = []
        index = 0
        while index < len(part):
            matched = False
            for source, target in _PHONETIC_CHUNKS:
                if part.startswith(source, index):
                    chars.append(target)
                    index += len(source)
                    matched = True
                    break
            if not matched:
                chars.append(_PHONETIC_LETTERS.get(part[index], ""))
                index += 1
        if chars:
            result.append("".join(chars))
    translated = "-".join(result)
    return translated if translated and _contains_cjk(translated) else ""


def _name_tokens(value: str) -> list[str]:
    return re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ'’\-]*", value)


def _is_japanese_player(player: dict[str, Any]) -> bool:
    """Identify Japan from the stable nation UID or public nation fields."""
    try:
        if int(player.get("nationality_id") or 0) == 116:
            return True
    except (TypeError, ValueError):
        pass
    values = {
        str(player.get(key) or "").strip().casefold()
        for key in ("nationality", "nationality_code", "nation", "nation_name")
    }
    return bool(values & {"japan", "jpn", "日本"})


def _translate_japanese_name(
    original: str, parts: dict[str, dict[str, str]],
) -> str:
    """Restore authoritative Japanese kanji; never invent them phonetically."""
    full = parts.get("full", {}).get(_normalise_english_name(original))
    if full:
        return full
    tokens = _name_tokens(original)
    if len(tokens) != 2:
        return ""
    given = parts.get("first", {}).get(_normalise_english_name(tokens[0]))
    surname = parts.get("surname", {}).get(_normalise_english_name(tokens[1]))
    if not given or not surname:
        return ""
    # FM's Latin display is normally given-name first; Japanese display is
    # surname first and does not use the western-name middle dot.
    return f"{surname}{given}"


def _translate_name_with_parts(
    original: str, parts: dict[str, dict[str, str]], *, allow_fallback: bool,
) -> str:
    key = _normalise_english_name(original)
    translated = parts.get("full", {}).get(key)
    if translated:
        return translated
    tokens = _name_tokens(original)
    if not tokens:
        return ""
    translated_tokens: list[str] = []
    for index, token in enumerate(tokens):
        token_key = _normalise_english_name(token)
        if index == 0:
            candidate = parts.get("first", {}).get(token_key) or parts.get("surname", {}).get(token_key)
        elif index == len(tokens) - 1:
            candidate = parts.get("surname", {}).get(token_key) or parts.get("first", {}).get(token_key)
        else:
            candidate = (
                parts.get("first", {}).get(token_key)
                or parts.get("surname", {}).get(token_key)
                or _PHONETIC_PREFIXES.get(_latin_name_token(token), "")
            )
        if not candidate and allow_fallback:
            candidate = _phonetic_token(token)
        if not candidate:
            return ""
        translated_tokens.append(candidate)
    # Keep hyphenated surnames readable while using the same separator as the
    # existing FM Chinese name dictionaries between separate name tokens.
    return "·".join(translated_tokens)


def parse_player_name_xml_parts(path: Path) -> dict[str, dict[str, str]]:
    """Read unambiguous English name-part translations from an FM XML export."""
    try:
        candidates: dict[str, dict[str, set[str]]] = {
            "full": {}, "first": {}, "surname": {},
        }
        property_groups = {
            _XML_FULL_NAME_PROPERTY: "full",
            _XML_FIRST_NAME_PROPERTY: "first",
            _XML_SURNAME_PROPERTY: "surname",
        }
        for _event, element in ET.iterparse(Path(path), events=("end",)):
            if element.tag != "record":
                continue
            fields = {
                child.attrib.get("id", ""): child.attrib.get("value", "")
                for child in element
                if child.tag in {"integer", "unsigned", "large", "string"}
            }
            group = property_groups.get(fields.get("property", ""))
            english = _normalise_english_name(fields.get("odvl", ""))
            chinese = str(fields.get("new_value", "") or "").strip()
            if (
                fields.get("database_table_type") == "1"
                and group
                and english
                and chinese
                and len(chinese) <= 40
                and _contains_cjk(chinese)
            ):
                candidates[group].setdefault(english, set()).add(chinese)
            element.clear()
        return {
            group: {
                english: next(iter(values))
                for english, values in entries.items()
                if len(values) == 1
            }
            for group, entries in candidates.items()
        }
    except (ET.ParseError, OSError, ValueError, TypeError):
        return {"full": {}, "first": {}, "surname": {}}


def _player_name_xml_paths() -> tuple[Path, ...]:
    configured = str(os.environ.get("FMODD_PLAYER_NAME_XML", "") or "").strip()
    candidates = [Path(configured)] if configured else []
    candidates.append(Path.home() / "Documents" / "数据库人员汉化.xml")
    return tuple(dict.fromkeys(path for path in candidates if path.is_file()))


def _load_player_name_xml(path: Path) -> dict[int, str]:
    try:
        stat = path.stat()
    except OSError:
        return {}
    key = (str(path.resolve()).casefold(), int(stat.st_mtime_ns), int(stat.st_size))
    with _CACHE_LOCK:
        cached = _XML_CACHE.get(key)
        if cached is not None:
            return cached
    names = parse_player_name_xml(path)
    with _CACHE_LOCK:
        existing = _XML_CACHE.setdefault(key, names)
        return existing


def _load_bundled_player_names() -> dict[int, str]:
    """Load the compact dictionary shipped inside a frozen ODD build."""
    try:
        from tools.app_paths import ASSET_ROOT, FROZEN

        if not FROZEN:
            return {}
        path = ASSET_ROOT / "assets" / "player_names" / "uid_names.json"
        stat = path.stat()
    except (ImportError, OSError, TypeError):
        return {}
    key = (str(path.resolve()).casefold(), int(stat.st_mtime_ns), int(stat.st_size))
    with _CACHE_LOCK:
        cached = _BUNDLED_CACHE.get(key)
        if cached is not None:
            return cached
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return {}
        names = {
            int(uid): str(name).strip()
            for uid, name in payload.items()
            if str(uid).isdigit()
            and int(uid) > 0
            and str(name).strip()
            and len(str(name).strip()) <= 40
            and _contains_cjk(str(name))
        }
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return {}
    with _CACHE_LOCK:
        existing = _BUNDLED_CACHE.setdefault(key, names)
        return existing


def _load_bundled_player_name_parts() -> dict[str, dict[str, str]]:
    """Load the compact, ambiguity-filtered name-part dictionary."""
    try:
        from tools.app_paths import ASSET_ROOT, FROZEN

        if not FROZEN:
            return {"full": {}, "first": {}, "surname": {}}
        path = ASSET_ROOT / "assets" / "player_names" / "name_parts.json"
        stat = path.stat()
    except (ImportError, OSError, TypeError):
        return {"full": {}, "first": {}, "surname": {}}
    key = (str(path.resolve()).casefold(), int(stat.st_mtime_ns), int(stat.st_size))
    with _CACHE_LOCK:
        cached = _BUNDLED_PART_CACHE.get(key)
        if cached is not None:
            return cached
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return {"full": {}, "first": {}, "surname": {}}
        parts = {
            group: {
                _normalise_english_name(english): str(chinese).strip()
                for english, chinese in (payload.get(group) or {}).items()
                if _normalise_english_name(english)
                and _contains_cjk(str(chinese))
                and len(str(chinese).strip()) <= 40
            }
            for group in ("full", "first", "surname")
        }
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return {"full": {}, "first": {}, "surname": {}}
    with _CACHE_LOCK:
        existing = _BUNDLED_PART_CACHE.setdefault(key, parts)
        return existing


def _load_name_guesses() -> dict[str, str]:
    """Load exact full-name phonetic guesses as the final fallback."""
    try:
        from tools.app_paths import ASSET_ROOT

        path = ASSET_ROOT / "assets" / "player_names" / "name_guesses.json"
        stat = path.stat()
    except (ImportError, OSError, TypeError):
        return {}
    key = (str(path.resolve()).casefold(), int(stat.st_mtime_ns), int(stat.st_size))
    with _CACHE_LOCK:
        cached = _GUESS_CACHE.get(key)
        if cached is not None:
            return cached
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return {}
        guesses = {
            _normalise_english_name(english): str(chinese).strip()
            for english, chinese in payload.items()
            if _normalise_english_name(english)
            and _contains_cjk(str(chinese))
            and len(str(chinese).strip()) <= 40
        }
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return {}
    with _CACHE_LOCK:
        existing = _GUESS_CACHE.setdefault(key, guesses)
        return existing


def _load_player_name_parts(paths: tuple[Path, ...]) -> dict[str, dict[str, str]]:
    merged: dict[str, dict[str, str]] = {"full": {}, "first": {}, "surname": {}}
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            continue
        key = (str(path.resolve()).casefold(), int(stat.st_mtime_ns), int(stat.st_size))
        with _CACHE_LOCK:
            parts = _XML_PART_CACHE.get(key)
        if parts is None:
            parts = parse_player_name_xml_parts(path)
            with _CACHE_LOCK:
                parts = _XML_PART_CACHE.setdefault(key, parts)
        for group in merged:
            for english, chinese in parts.get(group, {}).items():
                merged[group].setdefault(english, chinese)
    return merged


def _apply_player_name_parts(
    profile: dict[str, Any], parts: dict[str, dict[str, str]],
) -> None:
    groups = [profile.get("players", [])]
    groups.extend(
        squad.get("players", [])
        for squad in profile.get("squads", [])
        if isinstance(squad, dict)
    )
    for group in groups:
        for player in group:
            if not isinstance(player, dict) or player.get("name") is None:
                continue
            original = str(player.get("name") or "").strip()
            if not original or _contains_cjk(original):
                continue
            translated = (
                _translate_japanese_name(original, parts)
                if _is_japanese_player(player)
                else _translate_name_with_parts(
                    original, parts, allow_fallback=False,
                )
            )
            if not translated:
                continue
            player.setdefault("game_name", original)
            player["name"] = translated


def _apply_name_guesses(
    profile: dict[str, Any], guesses: dict[str, str],
) -> None:
    """Apply only exact full-name guesses after authoritative mappings."""
    groups = [profile.get("players", [])]
    groups.extend(
        squad.get("players", [])
        for squad in profile.get("squads", [])
        if isinstance(squad, dict)
    )
    for group in groups:
        for player in group:
            if not isinstance(player, dict):
                continue
            original = str(player.get("name") or "").strip()
            if not original or _contains_cjk(original):
                continue
            if _is_japanese_player(player):
                continue
            translated = guesses.get(_normalise_english_name(original))
            if translated:
                player.setdefault("game_name", original)
                player["name"] = translated


def _apply_phonetic_fallback(
    profile: dict[str, Any], parts: dict[str, dict[str, str]],
) -> None:
    """Translate still-unmatched Latin names with the offline fallback."""
    groups = [profile.get("players", [])]
    groups.extend(
        squad.get("players", [])
        for squad in profile.get("squads", [])
        if isinstance(squad, dict)
    )
    for group in groups:
        for player in group:
            if not isinstance(player, dict):
                continue
            original = str(player.get("name") or "").strip()
            if not original or _contains_cjk(original):
                continue
            if _is_japanese_player(player):
                continue
            translated = _translate_name_with_parts(
                original, parts, allow_fallback=True,
            )
            if not translated or not _contains_cjk(translated):
                continue
            player.setdefault("game_name", original)
            player["name"] = translated


def _xml_paths_signature(paths: tuple[Path, ...]) -> tuple[tuple[str, int, int], ...]:
    signature: list[tuple[str, int, int]] = []
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            continue
        signature.append(
            (str(path.resolve()).casefold(), int(stat.st_mtime_ns), int(stat.st_size))
        )
    return tuple(signature)


def _active_game_identity() -> tuple[str, str] | None:
    try:
        from tools.game_session import active_game_read_selection

        active = active_game_read_selection()
    except (ImportError, OSError, RuntimeError, TypeError, ValueError):
        return None
    if active is None:
        return None
    _pid, executable_path, layout = active
    path = str(executable_path or "").strip()
    layout_key = str(getattr(layout, "key", "") or "").strip()
    return (path, layout_key) if path else None


def _database_roots(executable_path: str) -> tuple[Path, ...]:
    game_root = Path(executable_path).resolve().parent
    roots = (
        game_root / "shared" / "data" / "database" / "db",
        game_root / "data" / "database" / "db",
    )
    return tuple(dict.fromkeys(root for root in roots if root.is_dir()))


def _generation_prefix(layout_key: str) -> str:
    key = str(layout_key or "").casefold()
    if key.startswith("fm26"):
        return "26"
    if key.startswith("fm24"):
        return "24"
    return ""


def load_installed_player_names(
    executable_path: str | None = None, layout_key: str | None = None,
) -> dict[int, str]:
    """Load installed FM name-fix files once and return their UID dictionary.

    Older database folders remain useful after an FM data update because stable
    person UIDs continue to identify the same player. Later database folders
    are parsed last, so a matching-version fix overrides an older translation.
    """
    if not executable_path:
        active = _active_game_identity()
        if active is None:
            return {}
        executable_path, active_layout_key = active
        layout_key = layout_key or active_layout_key
    resolved_path = str(Path(executable_path).resolve())
    generation = _generation_prefix(str(layout_key or ""))
    xml_paths = _player_name_xml_paths()
    cache_key = (resolved_path.casefold(), generation, _xml_paths_signature(xml_paths))
    with _CACHE_LOCK:
        cached = _CACHE.get(cache_key)
        if cached is not None:
            return cached

    files: list[tuple[int, str, Path]] = []
    for database_root in _database_roots(resolved_path):
        for version_root in database_root.iterdir():
            if not version_root.is_dir() or not version_root.name.isdigit():
                continue
            if generation and not version_root.name.startswith(generation):
                continue
            lnc_root = version_root / "lnc"
            if not lnc_root.is_dir():
                continue
            for path in lnc_root.rglob("*.lnc"):
                files.append((int(version_root.name), str(path).casefold(), path))

    # The XML export covers the broad database population.  Installed .lnc
    # rows are applied afterwards because they are the user's exact FM
    # language/database revision and therefore remain the preferred spelling.
    names: dict[int, str] = {}
    for path in xml_paths:
        names.update(_load_player_name_xml(path))
    # A frozen build carries a compact union of the known FM24/FM26 exports;
    # it fills gaps for users who do not have the source XML locally.  The
    # installed .lnc files remain the final, exact-version override.
    names.update(_load_bundled_player_names())
    for _version, _sort_path, path in sorted(files):
        names.update(parse_player_name_lnc(path))
    with _CACHE_LOCK:
        existing = _CACHE.setdefault(cache_key, names)
        return existing


def apply_installed_player_names(
    profile: dict[str, Any], names: dict[int, str] | None = None,
) -> dict[str, Any]:
    """Replace player display names by UID without exposing source metadata."""
    installed = load_installed_player_names() if names is None else names
    parts = _load_bundled_player_name_parts() if names is None else {
        "full": {}, "first": {}, "surname": {},
    }
    if names is None:
        xml_paths = _player_name_xml_paths()
        external_parts = _load_player_name_parts(xml_paths)
        for group in parts:
            for key, value in external_parts[group].items():
                parts[group].setdefault(key, value)
    if names is not None and not installed:
        return profile
    player_groups = [profile.get("players", [])]
    player_groups.extend(
        squad.get("players", [])
        for squad in profile.get("squads", [])
        if isinstance(squad, dict)
    )
    seen_objects: set[int] = set()
    for player in (row for group in player_groups for row in group):
        if not isinstance(player, dict) or id(player) in seen_objects:
            continue
        seen_objects.add(id(player))
        uid = int(player.get("id") or 0)
        translated = installed.get(uid)
        if not translated and player.get("legacy_id"):
            translated = installed.get(int(player["legacy_id"]))
        if not translated:
            continue
        player.setdefault("game_name", str(player.get("name") or ""))
        player["name"] = translated
    if names is None:
        _apply_player_name_parts(profile, parts)
        _apply_name_guesses(profile, _load_name_guesses())
        _apply_phonetic_fallback(profile, parts)
    return profile


def clear_installed_player_name_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()
        _XML_CACHE.clear()
        _XML_PART_CACHE.clear()
        _BUNDLED_CACHE.clear()
        _BUNDLED_PART_CACHE.clear()
        _GUESS_CACHE.clear()
