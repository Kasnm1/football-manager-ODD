from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

from tools.app_paths import cache_data_root, save_data_root


def _fixture_key(item: dict[str, Any]) -> str:
    return ":".join(map(str, (
        item["fixture_date"], item["competition_id"],
        item["home"]["id"], item["away"]["id"],
    )))


def _forecast(item: dict[str, Any], output: dict[str, Any]) -> dict[str, Any] | None:
    try:
        forecast = {
            "generated_at": output["generated_at"],
            "game_date": output["game_date"],
            "model_version": output["model_version"],
            "xg": item["xg"],
            "fair_1x2": item["fair_1x2"],
            "casino_1x2": item.get("casino_1x2"),
            "market_catalog_state": str(
                item.get("market_catalog_state") or "core"
            ),
        }
    except (KeyError, TypeError):
        return None
    for key in (
        "kickoff_minutes", "kickoff_time",
        "asian_handicap", "total_goals", "btts",
    ):
        if key in item:
            forecast[key] = item[key]
    try:
        kickoff_minutes = int(forecast.get("kickoff_minutes"))
    except (TypeError, ValueError):
        forecast.pop("kickoff_minutes", None)
    else:
        if 0 <= kickoff_minutes < 24 * 60:
            forecast["kickoff_minutes"] = kickoff_minutes
            if not forecast.get("kickoff_time"):
                forecast["kickoff_time"] = (
                    f"{kickoff_minutes // 60:02d}:{kickoff_minutes % 60:02d}"
                )
        else:
            forecast.pop("kickoff_minutes", None)
    return forecast


def load_season_forecasts(
    output: dict[str, Any], fixture_keys: set[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Load previously published forecasts for the active season and scope."""
    try:
        season_start = str(output["season_start"])
        season_end = str(output["season_end"])
        scope_id = str(
            output.get("account_scope_id")
            or output.get("data_scope_id")
            or output.get("save_instance_id")
            or ""
        )
        season_name = f"{season_start[:4]}-{season_end[:4]}"
        path = (
            cache_data_root(scope_id)
            / "model" / "seasons" / season_name / "forecast_ledger.json"
        )
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (KeyError, OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError):
        return {}
    if (
        str(payload.get("season_start") or "") != season_start
        or str(payload.get("season_end") or "") != season_end
    ):
        return {}
    requested = fixture_keys if fixture_keys is not None else None
    forecasts: dict[str, dict[str, Any]] = {}
    for key, record in (payload.get("fixtures") or {}).items():
        fixture_key = str(key)
        if requested is not None and fixture_key not in requested:
            continue
        forecast = record.get("latest_forecast") or record.get("first_forecast")
        if isinstance(forecast, dict):
            loaded = deepcopy(forecast)
            for metadata_key in ("kickoff_minutes", "kickoff_time"):
                if (
                    loaded.get(metadata_key) is None
                    and record.get(metadata_key) is not None
                ):
                    loaded[metadata_key] = deepcopy(record[metadata_key])
            forecasts[fixture_key] = loaded
    return forecasts


def archive_season_output(output: dict[str, Any]) -> Path:
    season_name = f"{output['season_start'][:4]}-{output['season_end'][:4]}"
    path = cache_data_root() / "model" / "seasons" / season_name / "forecast_ledger.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {
            "schema_version": 1,
            "season_start": output["season_start"],
            "season_end": output["season_end"],
            "policy": "Freeze each published model version; calibrate only after the season closes.",
            "model_versions": [],
            "fixtures": {},
        }

    if output["model_version"] not in payload["model_versions"]:
        payload["model_versions"].append(output["model_version"])
    for item in output.get("matches", []):
        key = _fixture_key(item)
        forecast = _forecast(item, output)
        if forecast is None:
            continue
        record = payload["fixtures"].setdefault(key, {
            "fixture_date": item["fixture_date"],
            "competition_id": item["competition_id"],
            "competition_name": item["competition_name"],
            "kickoff_minutes": forecast.get("kickoff_minutes"),
            "kickoff_time": forecast.get("kickoff_time"),
            "home": {"id": item["home"]["id"], "name": item["home"]["name"]},
            "away": {"id": item["away"]["id"], "name": item["away"]["name"]},
            "first_forecast": forecast,
            "refresh_count": 0,
            "result": None,
        })
        record["kickoff_minutes"] = forecast.get("kickoff_minutes")
        record["kickoff_time"] = forecast.get("kickoff_time")
        record["latest_forecast"] = forecast
        record["refresh_count"] = int(record.get("refresh_count", 0)) + 1

    for result in output.get("season_results", []):
        key = ":".join(map(str, (
            result["date"], result["competition_id"],
            result["home"]["id"], result["away"]["id"],
        )))
        if key in payload["fixtures"]:
            payload["fixtures"][key]["result"] = {
                "date": result["date"],
                "home_goals": result["home_goals"],
                "away_goals": result["away_goals"],
            }

    payload["updated_at"] = datetime.now().isoformat(timespec="seconds")
    payload["last_game_date"] = output["game_date"]
    payload["calibration_ready"] = output["game_date"] > output["season_end"]
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    temporary.replace(path)
    return path
