from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any, Callable

from tools.app_paths import cache_data_root, career_data_root
from tools.result_evidence import (
    RESULT_DETAIL_FIELDS,
    _result_identity,
    discard_result_details,
    sanitize_result_details,
)
from tools.storage_io import atomic_write_bytes, storage_lock
from tools.storage_retention import (
    decode_result_history_bytes,
    encode_result_history_payload,
    enforce_storage_budget,
    result_retention_start,
)


RESULT_KICKOFF_FIELDS = ("kickoff_minutes", "kickoff_time")
_RESULT_HISTORY_LOCK = RLock()

HistoryLoader = Callable[[str | None], list[dict[str, Any]]]
HistoryMerger = Callable[[list[dict[str, Any]], str | None], list[dict[str, Any]]]


def result_history_path(save_instance_id: str | None) -> Path | None:
    if not save_instance_id:
        return None
    return career_data_root(save_instance_id) / "result_history.json"


def legacy_result_history_path(save_instance_id: str | None) -> Path | None:
    if not save_instance_id:
        return None
    return cache_data_root(save_instance_id) / "results" / "fm26_result_history.json"


@dataclass(frozen=True)
class ResultHistoryOwner:
    """Own one career's durable result-history read/merge/write lifecycle."""

    save_instance_id: str | None
    durable_path: Path | None
    legacy_path: Path | None

    @staticmethod
    def _read_file(path: Path | None) -> tuple[list[dict[str, Any]], bool]:
        if not path or not path.exists():
            return [], False
        with storage_lock(path):
            try:
                payload = decode_result_history_bytes(path.read_bytes())
            except (OSError, EOFError, ValueError):
                backup = path.with_suffix(path.suffix + ".backup")
                try:
                    payload = decode_result_history_bytes(backup.read_bytes())
                except (OSError, EOFError, ValueError):
                    return [], False
                try:
                    atomic_write_bytes(path, backup.read_bytes())
                except OSError:
                    pass
        rows = [item for item in payload["results"] if isinstance(item, dict)]
        return rows, bool(payload.get("legacy_history_imported"))

    def load(self) -> list[dict[str, Any]]:
        merged: dict[tuple[str, int, int, int], dict[str, Any]] = {}
        durable_rows, legacy_imported = self._read_file(self.durable_path)
        row_groups = [durable_rows]
        if not legacy_imported:
            legacy_rows, _ignored = self._read_file(self.legacy_path)
            row_groups.insert(0, legacy_rows)
        for rows in row_groups:
            for item in rows:
                try:
                    merged[_result_identity(item)] = item
                except (KeyError, TypeError, ValueError):
                    continue
        return sorted(merged.values(), key=_result_identity)

    def merge(
        self,
        results: list[dict[str, Any]],
        *,
        load_existing: HistoryLoader | None = None,
    ) -> list[dict[str, Any]]:
        existing = (
            load_existing(self.save_instance_id)
            if load_existing is not None
            else self.load()
        )
        merged: dict[tuple[str, int, int, int], dict[str, Any]] = {}
        for item in [*existing, *results]:
            try:
                item = sanitize_result_details(item)
                if self.save_instance_id:
                    item = {**item, "save_instance_id": self.save_instance_id}
                identity = _result_identity(item)
                previous = merged.get(identity)
                if previous:
                    for field in RESULT_KICKOFF_FIELDS:
                        if item.get(field) is None and previous.get(field) is not None:
                            item = {**item, field: previous[field]}
                previous_score = (
                    int(previous.get("home_goals", -1)),
                    int(previous.get("away_goals", -1)),
                ) if previous else None
                current_score = (
                    int(item.get("home_goals", -2)),
                    int(item.get("away_goals", -2)),
                )
                if previous and (
                    previous.get("result_conflict")
                    or previous_score != current_score
                ):
                    previous_verified = (
                        previous.get("settlement_verified") is True
                        or any(
                            candidate.get("settlement_verified") is True
                            for candidate in previous.get("result_conflict_candidates") or []
                            if isinstance(candidate, dict)
                        )
                    )
                    current_verified = item.get("settlement_verified") is True
                    if current_verified and not previous_verified:
                        merged[identity] = item
                        continue
                    if previous_verified and not current_verified:
                        continue
                    candidates = list(previous.get("result_conflict_candidates") or [])
                    if not candidates:
                        candidates.append({
                            "home_goals": previous_score[0],
                            "away_goals": previous_score[1],
                            "result_source": previous.get("result_source") or previous.get("source"),
                            "result_address": previous.get("result_address"),
                            "settlement_verified": previous_verified,
                        })
                    candidates.append({
                        "home_goals": current_score[0],
                        "away_goals": current_score[1],
                        "result_source": item.get("result_source") or item.get("source"),
                        "result_address": item.get("result_address"),
                        "settlement_verified": current_verified,
                    })
                    unique_candidates = {
                        (
                            int(candidate.get("home_goals", -1)),
                            int(candidate.get("away_goals", -1)),
                            str(candidate.get("result_source") or ""),
                            str(candidate.get("result_address") or ""),
                            bool(candidate.get("settlement_verified")),
                        ): candidate
                        for candidate in candidates
                        if isinstance(candidate, dict)
                    }
                    merged[identity] = {
                        **discard_result_details(previous),
                        "result_conflict": True,
                        "settlement_verified": False,
                        "result_conflict_candidates": list(unique_candidates.values()),
                    }
                    continue
                same_final_score = bool(
                    previous
                    and previous_score == current_score
                )
                if same_final_score and previous:
                    previous = sanitize_result_details(previous)
                    for field in RESULT_DETAIL_FIELDS:
                        if item.get(field) is None and previous.get(field) is not None:
                            item = {**item, field: previous[field]}
                    if (
                        not isinstance(item.get("goal_events"), list)
                        and isinstance(previous.get("goal_events"), list)
                    ):
                        item = {**item, "goal_events": deepcopy(previous["goal_events"])}
                merged[identity] = item
            except (KeyError, TypeError, ValueError):
                continue
        return sorted(merged.values(), key=_result_identity)

    def save(
        self,
        results: list[dict[str, Any]],
        game_date_text: str | None = None,
        protected_result_keys: set[tuple[str, int, int, int]] | None = None,
        *,
        merge_results: HistoryMerger | None = None,
    ) -> None:
        with _RESULT_HISTORY_LOCK:
            if not self.durable_path:
                return
            with storage_lock(self.durable_path):
                results = (
                    merge_results(results, self.save_instance_id)
                    if merge_results is not None
                    else self.merge(results)
                )
                if game_date_text:
                    current_date = datetime.fromisoformat(game_date_text).date()
                    retention_start = result_retention_start(current_date)
                    protected = set(protected_result_keys or ())
                    retained = []
                    for item in results:
                        try:
                            identity = _result_identity(item)
                        except (KeyError, TypeError, ValueError):
                            identity = None
                        if (
                            retention_start is None
                            or str(item.get("date", "")) >= retention_start.isoformat()
                            or identity in protected
                        ):
                            retained.append(item)
                    results = retained
                payload = {
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                    "source": "cumulative read-only FM process memory captures",
                    "legacy_history_imported": True,
                    "results": results,
                }
                if self.durable_path.is_file():
                    try:
                        current = self.durable_path.read_bytes()
                        decode_result_history_bytes(current)
                        valid_current = True
                    except (OSError, EOFError, ValueError):
                        valid_current = False
                    if valid_current:
                        atomic_write_bytes(
                            self.durable_path.with_suffix(
                                self.durable_path.suffix + ".backup"
                            ),
                            current,
                        )
                atomic_write_bytes(
                    self.durable_path,
                    encode_result_history_payload(payload),
                )
            enforce_storage_budget(root=self.durable_path.parents[2])
