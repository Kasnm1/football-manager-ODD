from __future__ import annotations

import json
from pathlib import Path
from threading import RLock
from typing import Any

from tools.account_store import load_document, save_document
from tools.app_paths import save_data_root
from tools.app_settings import load_settings
from tools.player_name_localization import apply_installed_player_names
from tools.player_name_localization import load_installed_player_names


_LOCK = RLock()


def _path(save_id: str) -> Path:
    return save_data_root(save_id) / "club" / "player_names.json"


def _load(save_id: str) -> dict[str, str]:
    payload = load_document(
        "player_aliases", {}, save_id, legacy_path=_path(save_id),
    )
    return {
        str(key): str(value).strip()
        for key, value in payload.items()
        if str(key).isdigit() and str(value).strip()
    } if isinstance(payload, dict) else {}


def _original_name_display(player: dict[str, Any]) -> str:
    """Return FM's original display name without reordering or punctuation."""
    editor_names = (player.get("editor") or {}).get("names") or {}
    original = str(
        player.get("game_name")
        or player.get("name")
        or player.get("full_name")
        or editor_names.get("full_name")
        or ""
    ).strip()
    if original:
        return original
    first = str(
        player.get("first_name") or editor_names.get("first_name") or ""
    ).strip()
    last = str(
        player.get("last_name") or editor_names.get("last_name") or ""
    ).strip()
    return " ".join(part for part in (first, last) if part)


def apply_player_aliases(profile: dict[str, Any], save_id: str) -> dict[str, Any]:
    localization_enabled = bool(load_settings().get("player_name_localization", True))
    player_groups = [profile.get("players", [])]
    player_groups.extend(
        squad.get("players", [])
        for squad in profile.get("squads", [])
        if isinstance(squad, dict)
    )
    if localization_enabled:
        # A profile may be re-applied after the user toggles the display mode.
        # Restore the factual FM spelling before looking up UID translations.
        for group in player_groups:
            for player in group:
                if isinstance(player, dict) and player.get("game_name") and not player.get("custom_name"):
                    player["name"] = str(player["game_name"])
        apply_installed_player_names(profile)
    with _LOCK:
        aliases = _load(save_id)
    seen_objects: set[int] = set()
    for player in (row for group in player_groups for row in group):
        object_id = id(player)
        if object_id in seen_objects:
            continue
        seen_objects.add(object_id)
        player.setdefault("game_name", str(player.get("name") or ""))
        alias = aliases.get(str(int(player.get("id") or 0)))
        if not alias and player.get("legacy_id"):
            alias = aliases.get(str(int(player["legacy_id"])))
        if alias:
            player["name"] = alias
            player["custom_name"] = True
        elif not localization_enabled:
            player["name"] = _original_name_display(player)
    return profile


def player_alias_uids_for_query(save_id: str, query: str) -> tuple[int, ...]:
    """Resolve an installed or user-defined localized name to stable UIDs."""
    normalized = " ".join(str(query or "").strip().casefold().split())
    if not normalized:
        return ()
    names = dict(load_installed_player_names())
    with _LOCK:
        names.update({int(uid): name for uid, name in _load(save_id).items()})
    return tuple(sorted(
        int(uid) for uid, name in names.items()
        if normalized in " ".join(str(name or "").casefold().split())
    ))


def set_player_alias(save_id: str, player_id: int, value: str) -> str | None:
    name = str(value or "").strip()
    if len(name) > 40 or any(ord(char) < 32 for char in name):
        raise ValueError("球员名称长度不能超过40个字符")
    with _LOCK:
        aliases = _load(save_id)
        key = str(int(player_id))
        if name:
            aliases[key] = name
        else:
            aliases.pop(key, None)
        save_document(
            "player_aliases", aliases, save_id, legacy_path=_path(save_id),
        )
    return name or None
