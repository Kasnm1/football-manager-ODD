from __future__ import annotations

import json
import math
from typing import Any

from tools.app_paths import DATA_ROOT
from tools.storage_io import atomic_write_json, storage_lock


SETTINGS_PATH = DATA_ROOT / "settings.json"
DEFAULT_RESULT_RETENTION_SEASONS = 2
ODDS_SCOPE_OPTIONS = {"favorite_schedule", "famous", "all"}
ODDS_DAYS_OPTIONS = {3, 7, 14}
ODDS_AUTO_REFRESH_OPTIONS = {0, 8, 30, 60}
PREFERRED_GAME_VERSION_OPTIONS = {"fm24", "fm26"}
UI_LOCALE_OPTIONS = {
    "en-GB", "zh-CN", "zh-TW", "ko-KR", "de-DE", "es-ES", "fr-FR", "ru-RU", "ja-JP",
    "pt-BR", "pt-PT",
}
DEFAULT_UI_LOCALE = "en-GB"
LEGACY_UI_LOCALE = "zh-CN"
DEFAULT_ODDS_AUTO_REFRESH_SECONDS = 8
_LOCK = storage_lock(SETTINGS_PATH)
DEFAULT_SINGLE_BETTING_LIMIT = 20_000_000.0
DEFAULT_PARLAY_BETTING_LIMIT = 100_000_000.0
MONEY_CURRENCIES = {
    "GBP": {"symbol": "£", "rate": 1.0, "display_scale": 1.0, "decimal_digits": 2},
    "USD": {"symbol": "$", "rate": 1.35082459495356, "display_scale": 1.0, "decimal_digits": 2},
    "EUR": {"symbol": "€", "rate": 1.1571304778043, "display_scale": 1.0, "decimal_digits": 2},
    "CNY": {"symbol": "¥", "rate": 9.7068147703991, "display_scale": 10.0, "decimal_digits": 2},
    # Frozen game-facing GBP conversion rates. They intentionally do not track
    # live FX, so prices and limits remain deterministic between sessions.
    "RUB": {"symbol": "₽", "rate": 100.0, "display_scale": 100.0, "decimal_digits": 2},
    "JPY": {"symbol": "¥", "rate": 200.0, "display_scale": 200.0, "decimal_digits": 0},
    "KRW": {"symbol": "₩", "rate": 2000.0, "display_scale": 2000.0, "decimal_digits": 0},
    "CAD": {"symbol": "$", "rate": 1.87, "display_scale": 1.0, "decimal_digits": 2},
    "AUD": {"symbol": "$", "rate": 1.88, "display_scale": 1.0, "decimal_digits": 2},
    "CHF": {"symbol": "CHF ", "rate": 1.095, "display_scale": 1.0, "decimal_digits": 2},
}


def _currency_payload(code: Any) -> dict[str, Any]:
    resolved = str(code or "GBP").upper()
    if resolved not in MONEY_CURRENCIES:
        resolved = "GBP"
    config = MONEY_CURRENCIES[resolved]
    scale = float(config["display_scale"])
    rate = float(config["rate"])
    return {
        "money_currency": resolved,
        "money_symbol": str(config["symbol"]),
        "money_rate": rate,
        "money_decimal_digits": int(config["decimal_digits"]),
        "purchase_money_scale": scale,
        "default_betting_limit_single": DEFAULT_SINGLE_BETTING_LIMIT * scale / rate,
        "default_betting_limit_parlay": DEFAULT_PARLAY_BETTING_LIMIT * scale / rate,
    }


def local_purchase_price(value: Any, settings: dict[str, Any] | None = None) -> float:
    """Convert an FMODD-owned nominal purchase price to the internal GBP ledger."""
    resolved_settings = load_settings() if settings is None else settings
    try:
        amount = float(value)
        rate = float(resolved_settings.get("money_rate") or 1.0)
        currency = str(resolved_settings.get("money_currency") or "GBP").upper()
        default_scale = float(
            MONEY_CURRENCIES.get(currency, MONEY_CURRENCIES["GBP"])["display_scale"]
        )
        scale = float(resolved_settings.get("purchase_money_scale") or default_scale)
    except (TypeError, ValueError) as error:
        raise ValueError("购买价格必须为有效金额") from error
    if not math.isfinite(amount) or amount < 0:
        raise ValueError("购买价格必须为非负有限金额")
    if not math.isfinite(rate) or rate <= 0:
        rate = 1.0
    if not math.isfinite(scale) or scale <= 0:
        scale = 1.0
    return round(amount * scale / rate + 1e-9, 2)


def _positive_amount(value: Any, default: float) -> float:
    try:
        resolved = float(value)
    except (TypeError, ValueError):
        return default
    return resolved if math.isfinite(resolved) and resolved > 0 else default


def load_settings() -> dict[str, Any]:
    with _LOCK:
        settings_existed = SETTINGS_PATH.exists()
        try:
            payload = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        odds_scope = str(payload.get("odds_scope") or "all")
        if odds_scope in {"managed_league", "managed_schedule"}:
            odds_scope = "favorite_schedule"
        if odds_scope not in ODDS_SCOPE_OPTIONS:
            odds_scope = "all"
        try:
            odds_days = int(payload.get("odds_days", 7))
        except (TypeError, ValueError):
            odds_days = 7
        if odds_days not in ODDS_DAYS_OPTIONS:
            odds_days = 7
        try:
            odds_auto_refresh_seconds = int(payload.get(
                "odds_auto_refresh_seconds", DEFAULT_ODDS_AUTO_REFRESH_SECONDS,
            ))
        except (TypeError, ValueError):
            odds_auto_refresh_seconds = DEFAULT_ODDS_AUTO_REFRESH_SECONDS
        if odds_auto_refresh_seconds not in ODDS_AUTO_REFRESH_OPTIONS:
            odds_auto_refresh_seconds = DEFAULT_ODDS_AUTO_REFRESH_SECONDS
        preferred_game_version = str(payload.get("preferred_game_version") or "")
        if preferred_game_version not in PREFERRED_GAME_VERSION_OPTIONS:
            preferred_game_version = ""
        # Existing installations predate the locale field and were Chinese.
        # Preserve that experience while making English the default for a
        # genuinely new installation.
        locale_default = LEGACY_UI_LOCALE if settings_existed else DEFAULT_UI_LOCALE
        ui_locale = str(payload.get("ui_locale") or locale_default)
        if ui_locale not in UI_LOCALE_OPTIONS:
            ui_locale = locale_default
        raw_portrait_xml_paths = payload.get("portrait_xml_paths")
        if isinstance(raw_portrait_xml_paths, (list, tuple)):
            portrait_xml_paths = list(dict.fromkeys(
                str(value).strip() for value in raw_portrait_xml_paths if str(value).strip()
            ))
        else:
            legacy_portrait_xml = str(payload.get("portrait_xml_path") or "").strip()
            portrait_xml_paths = [legacy_portrait_xml] if legacy_portrait_xml else []
        return {
            "result_retention_seasons": DEFAULT_RESULT_RETENTION_SEASONS,
            "odds_scope": odds_scope,
            "odds_days": odds_days,
            "odds_auto_refresh_seconds": odds_auto_refresh_seconds,
            "championship_enabled": bool(payload.get("championship_enabled", True)),
            "preferred_game_version": preferred_game_version,
            "ui_locale": ui_locale,
            "player_name_localization": (
                ui_locale == "zh-CN"
                and bool(payload.get("player_name_localization", True))
            ),
            "portrait_graphics_root": str(payload.get("portrait_graphics_root") or ""),
            "portrait_xml_path": portrait_xml_paths[-1] if portrait_xml_paths else "",
            "portrait_xml_paths": portrait_xml_paths,
            "unlimited_betting": bool(payload.get("unlimited_betting", False)),
            "betting_limits_enabled": bool(payload.get("betting_limits_enabled", False)),
            "disable_fa_penalties": bool(payload.get("disable_fa_penalties", False)),
            "god_mode": bool(payload.get("god_mode", False)),
            "betting_limit_single": _positive_amount(payload.get("betting_limit_single"), DEFAULT_SINGLE_BETTING_LIMIT),
            "betting_limit_parlay": _positive_amount(payload.get("betting_limit_parlay"), DEFAULT_PARLAY_BETTING_LIMIT),
            **_currency_payload(payload.get("money_currency")),
        }


def set_portrait_sources(
    graphics_root: Any = "", xml_path: Any = "", *, clear: bool = False,
) -> dict[str, Any]:
    """Persist optional Football Manager graphics/XML portrait sources."""
    from pathlib import Path

    root_text = str(graphics_root or "").strip()
    xml_text = str(xml_path or "").strip()
    if clear and (root_text or xml_text):
        raise ValueError("清除头像来源时不能同时选择新路径")
    if root_text and xml_text:
        raise ValueError("头像来源只能选择 graphics 文件夹或单个 XML 其中一种")
    root = Path(root_text).expanduser() if root_text else None
    xml = Path(xml_text).expanduser() if xml_text else None
    if root is not None:
        if not root.is_absolute() or not root.is_dir():
            raise ValueError("头像 graphics 路径必须是已存在的文件夹")
        root = root.resolve()
    if xml is not None:
        if not xml.is_absolute() or not xml.is_file() or xml.suffix.casefold() != ".xml":
            raise ValueError("头像 XML 必须是已存在的 XML 文件")
        xml = xml.resolve()
    with _LOCK:
        payload = load_settings()
        if clear:
            payload.update({
                "portrait_graphics_root": "",
                "portrait_xml_path": "",
                "portrait_xml_paths": [],
            })
        elif root is not None:
            payload.update({
                "portrait_graphics_root": str(root) if root else "",
                "portrait_xml_path": "",
                "portrait_xml_paths": [],
            })
        elif xml is not None:
            existing = list(payload.get("portrait_xml_paths") or [])
            xml_text_resolved = str(xml)
            if xml_text_resolved not in existing:
                existing.append(xml_text_resolved)
            payload.update({
                "portrait_graphics_root": "",
                "portrait_xml_path": existing[-1] if existing else "",
                "portrait_xml_paths": existing,
            })
        return _write_settings(payload)


def _write_settings(payload: dict[str, Any]) -> dict[str, Any]:
    atomic_write_json(SETTINGS_PATH, payload, indent=2)
    return payload


def set_odds_reading(
    scope: Any, days: Any, auto_refresh_seconds: Any = DEFAULT_ODDS_AUTO_REFRESH_SECONDS,
) -> dict[str, Any]:
    resolved_scope = str(scope or "")
    if resolved_scope in {"managed_league", "managed_schedule"}:
        resolved_scope = "favorite_schedule"
    if resolved_scope not in ODDS_SCOPE_OPTIONS:
        raise ValueError("盘口读取范围无效")
    try:
        resolved_days = int(days)
    except (TypeError, ValueError) as error:
        raise ValueError("盘口时间无效") from error
    if resolved_days not in ODDS_DAYS_OPTIONS:
        raise ValueError("盘口时间仅支持14天、7天或3天")
    if auto_refresh_seconds in {"manual", "manual_only", None}:
        resolved_auto_refresh = 0
    else:
        try:
            resolved_auto_refresh = int(auto_refresh_seconds)
        except (TypeError, ValueError) as error:
            raise ValueError("盘口自动刷新时间无效") from error
    if resolved_auto_refresh not in ODDS_AUTO_REFRESH_OPTIONS:
        raise ValueError("盘口自动刷新仅支持8秒、30秒、1分钟或仅手动刷新")
    with _LOCK:
        payload = load_settings()
        payload.update({
            "odds_scope": resolved_scope,
            "odds_days": resolved_days,
            "odds_auto_refresh_seconds": resolved_auto_refresh,
        })
        return _write_settings(payload)


def set_betting_limits(enabled: Any, single: Any, parlay: Any, unlimited: Any) -> dict[str, Any]:
    single_limit = _positive_amount(single, 0)
    parlay_limit = _positive_amount(parlay, 0)
    if single_limit <= 0 or parlay_limit <= 0:
        raise ValueError("单关和串关投注上限必须大于 0")
    with _LOCK:
        payload = load_settings()
        payload.update({
            "betting_limits_enabled": bool(enabled),
            "betting_limit_single": single_limit,
            "betting_limit_parlay": parlay_limit,
            "unlimited_betting": bool(unlimited),
        })
        return _write_settings(payload)


def set_money_currency(currency: Any) -> dict[str, Any]:
    resolved = str(currency or "").upper()
    if resolved not in MONEY_CURRENCIES:
        raise ValueError("不支持该金额单位")
    with _LOCK:
        payload = load_settings()
        payload.update(_currency_payload(resolved))
        return _write_settings(payload)


def set_disable_fa_penalties(enabled: Any) -> dict[str, Any]:
    with _LOCK:
        payload = load_settings()
        payload["disable_fa_penalties"] = bool(enabled)
        return _write_settings(payload)


def set_god_mode(enabled: Any) -> dict[str, Any]:
    with _LOCK:
        payload = load_settings()
        payload["god_mode"] = bool(enabled)
        return _write_settings(payload)


def set_championship_enabled(enabled: Any) -> dict[str, Any]:
    with _LOCK:
        payload = load_settings()
        payload["championship_enabled"] = bool(enabled)
        return _write_settings(payload)


def set_preferred_game_version(key: Any) -> dict[str, Any]:
    resolved = str(key or "")
    if resolved not in PREFERRED_GAME_VERSION_OPTIONS:
        raise ValueError("game version must be fm24 or fm26")
    with _LOCK:
        payload = load_settings()
        payload["preferred_game_version"] = resolved
        return _write_settings(payload)


def set_ui_locale(locale: Any) -> dict[str, Any]:
    """Persist the UI language without coupling it to a save or manager account."""
    resolved = str(locale or "").strip()
    if resolved not in UI_LOCALE_OPTIONS:
        raise ValueError(
            "ui locale must be en-GB, zh-CN, zh-TW, ko-KR, de-DE, es-ES, fr-FR, ru-RU, ja-JP, pt-BR or pt-PT"
        )
    with _LOCK:
        payload = load_settings()
        payload["ui_locale"] = resolved
        if resolved != "zh-CN":
            payload["player_name_localization"] = False
        return _write_settings(payload)


def set_player_name_localization(enabled: Any) -> dict[str, Any]:
    """Persist whether player names use the installed Chinese dictionary."""
    with _LOCK:
        payload = load_settings()
        payload["player_name_localization"] = (
            payload.get("ui_locale") == "zh-CN" and bool(enabled)
        )
        return _write_settings(payload)
