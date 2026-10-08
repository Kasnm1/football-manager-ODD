from __future__ import annotations

import threading
from copy import deepcopy
from time import perf_counter
from typing import Any

# Compatibility export; account use cases no longer depend on a mutable host.
from tools.account_application import AccountApplicationService
from tools.refresh_status import RefreshStatusSnapshot


class RefreshCoordinator:
    """Owns refresh admission, thread dispatch, and public progress state."""

    def __init__(self, host: Any) -> None:
        self.host = host
        self._last_status_snapshot: RefreshStatusSnapshot | None = None

    def _status_context_key(self) -> tuple | None:
        """Reuse the runtime's identity fingerprint without account I/O or locks."""
        key = getattr(self.host, "_public_state_context_key", None)
        if not callable(key):
            return None
        try:
            context = key()
            if context is None:
                return None
            pending = getattr(self.host, "pending_manager_club_change", None) or {}
            return (
                context, pending.get("request_id"),
                bool(getattr(self.host, "manager_context_absence_pending", False)),
            )
        except (AttributeError, RuntimeError, TypeError, ValueError):
            # A context being replaced is not permission to reuse old fields.
            return None

    def _cached_status(self, context_key: tuple | None) -> dict[str, Any]:
        snapshot = self._last_status_snapshot
        return snapshot.read(context_key) if snapshot is not None else {}

    @staticmethod
    def _progress_snapshot(host: Any, now: float) -> dict[str, Any]:
        try:
            refresh_progress = dict(getattr(host, "refresh_progress", {}) or {})
        except (RuntimeError, TypeError, ValueError):
            refresh_progress = {}
        progress_updated_at = float(
            getattr(host, "_refresh_progress_updated_at", 0.0) or 0.0
        )
        if refresh_progress and progress_updated_at:
            refresh_progress["heartbeat_age_ms"] = round(
                max(0.0, now - progress_updated_at) * 1000, 1,
            )
        stage_started_at = float(
            getattr(host, "_refresh_stage_started_at", 0.0) or 0.0
        )
        if stage_started_at:
            refresh_progress["stage_elapsed_ms"] = round(
                max(0.0, now - stage_started_at) * 1000, 1,
            )
        return refresh_progress

    def _contended_status(self, now: float) -> dict[str, Any]:
        """Return live progress without waiting behind a heavyweight state build."""
        host = self.host
        try:
            payload = self._cached_status(self._status_context_key())
        except (RuntimeError, TypeError, ValueError):
            payload = {}
        try:
            has_output = bool(getattr(host, "output", {}).get("save_instance_id"))
        except (AttributeError, RuntimeError):
            has_output = False
        refreshing = bool(getattr(host, "refreshing", False))
        reconciling = bool(getattr(host, "reconciling", False))
        payload.update({
            "refreshing": refreshing,
            "reconciling": reconciling,
            "background_reconcile_ready": bool(
                reconciling
                and getattr(host, "cache_verified", False)
                and has_output
                and not getattr(host, "save_change_pending", False)
            ),
            "refresh_mode": getattr(host, "refresh_mode", None),
            "cache_verified": bool(getattr(host, "cache_verified", False)),
            "data_version": int(getattr(host, "data_version", 0) or 0),
            "status": str(getattr(host, "status", "") or ""),
            "error": getattr(host, "error", None),
            "error_stage": getattr(host, "last_refresh_error_stage", None),
            "refresh_reason": getattr(host, "refresh_reason", None),
            "refresh_stage": getattr(host, "refresh_stage", None),
            "refresh_progress": self._progress_snapshot(host, now),
            "has_output": has_output,
            "connection_pending": bool(
                getattr(host, "connection_requested", False)
                and refreshing and not has_output
            ),
            "status_snapshot_delayed": True,
            "save_change_pending": bool(getattr(host, "save_change_pending", False)),
        })
        payload.setdefault("betting_ready", False)
        if payload["save_change_pending"]:
            payload["betting_ready"] = False
        payload.setdefault("manager_club_change_confirmation", None)
        return payload

    def start(
        self, reason: str = "manual", *, full: bool = False,
        cancel_on_clock_change: bool = False,
    ) -> bool:
        host = self.host
        with host.lock:
            if host.refreshing or host.reconciling:
                return False
            if getattr(host, "club_refreshing", False):
                if reason in getattr(host, "club_deferred_refresh_reasons", ()):
                    host.deferred_memory_job_after_club = {
                        "kind": "refresh", "reason": reason,
                        "full": bool(full),
                        "cancel_on_clock_change": bool(cancel_on_clock_change),
                    }
                    host.status = "俱乐部资料读取完成后将继续刷新盘口"
                else:
                    host.status = "俱乐部资料正在读取，请完成后再刷新"
                return False
            if not str(host.output.get("save_instance_id") or ""):
                host.status = "请先连接 Football Manager 存档"
                return False
            if reason in {
                "game_clock_stable", "schedule_change", "schedule_rollover",
                "travel_complete",
            } and not host._has_verified_save():
                return False
            host.refreshing = True
            result_only = reason in {"game_clock_stable", "manual_results"}
            host.refresh_mode = "results" if result_only else ("full" if full else "fast")
            host.refresh_reason = reason
            cancel_event = threading.Event()
            host.refresh_cancel_event = cancel_event
            host.refresh_cancel_on_clock_change = bool(cancel_on_clock_change or not full)
            host.refresh_cancel_reason = None
            host.error = None
            if full and reason in {"save_change_full", "connect_save", "current_save_deleted"}:
                host.cache_verified = False
            host.status = (
                "正在更新赛果并结算注单..." if result_only
                else (
                    "正在完整重建存档数据..." if full
                    else "正在刷新积分榜..."
                    if reason == "manual_standings"
                    else "正在刷新盘口..."
                )
            )
            if full and reason not in {"startup", "identity_change_full"}:
                host.club_profile = None
                host.club_context = None
                host.club_profiles = {}
                host.club_contexts = {}
                host.club_last_updated = None
                host.club_error = None
        if result_only:
            target = host._result_refresh_worker
            args = (cancel_event, reason)
        else:
            target = host._refresh_worker
            args = (reason, "full" if full else "fast", False, cancel_event)
        threading.Thread(target=target, args=args, daemon=True).start()
        return True

    def status(self) -> dict[str, Any]:
        host = self.host
        acquired = host.lock.acquire(timeout=0.05)
        if not acquired:
            return self._contended_status(perf_counter())
        try:
            now = perf_counter()
            has_verified_save = host._has_verified_save()
            connection_pending = bool(
                getattr(host, "connection_requested", False)
                and host.refreshing and not has_verified_save
            )
            refresh_progress = self._progress_snapshot(host, now)
            payload = {
                **host._refresh_state(),
                "refresh_reason": host.refresh_reason,
                "refresh_stage": getattr(host, "refresh_stage", None),
                "refresh_progress": refresh_progress,
                "error_traceback": getattr(host, "last_refresh_traceback", None),
                "partial_result": deepcopy(
                    getattr(host, "last_refresh_partial_result", None),
                ),
                "operation_error": getattr(host, "last_operation_error", None),
                "operation_error_type": getattr(host, "last_operation_error_type", None),
                "operation_error_stage": getattr(host, "last_operation_error_stage", None),
                "operation_error_code": getattr(host, "last_operation_error_code", None),
                "operation_error_phase": getattr(host, "last_operation_error_phase", None),
                "operation_error_retryable": bool(
                    getattr(host, "last_operation_retryable", False)
                ),
                "operation_error_traceback": getattr(host, "last_operation_traceback", None),
                "operation_performance": dict(
                    getattr(host, "last_operation_performance", {}) or {}
                ),
                "refresh_trace": host._refresh_trace_snapshot(
                    getattr(host, "refresh_trace", {}),
                ),
                "last_refresh_trace": host._refresh_trace_snapshot(
                    getattr(host, "last_refresh_trace", {}),
                ),
                "state_build_ms": float(
                    getattr(host, "last_public_state_ms", 0.0) or 0.0
                ),
                "state_build_metrics": dict(
                    getattr(host, "last_public_state_metrics", {}) or {}
                ),
                "has_output": bool(host.output),
                "connection_pending": connection_pending,
            }
            verified_scope_id = (
                host._data_scope_id(host.output) if has_verified_save else ""
            )
            connection_scope_id = str(
                getattr(host, "connection_scope_id", "") or ""
            )
            scope_id = verified_scope_id or connection_scope_id
            wallet_ready = bool(
                verified_scope_id
                or (getattr(host, "wallet_ready", False) and connection_scope_id)
            )
            context_key = self._status_context_key()
        finally:
            host.lock.release()
        payload["wallet_ready"] = wallet_ready
        if wallet_ready:
            payload["wallet_scope_id"] = scope_id
            cached = self._cached_status(context_key)
            if cached.get("wallet_scope_id") == scope_id and cached.get("wallet_balance") is not None:
                payload["wallet_balance"] = cached["wallet_balance"]
        payload["status_snapshot_delayed"] = False
        try:
            self._last_status_snapshot = RefreshStatusSnapshot(context_key, payload)
        except (RuntimeError, TypeError, ValueError):
            self._last_status_snapshot = None
        return payload


class ClubLegacyRefreshCoordinator:
    """Drain one active and one latest archive job on existing club workers."""

    def __init__(self, host: Any) -> None:
        self.host = host
        self.pending: dict[str, Any] | None = None
        self.active: dict[str, Any] | None = None
        self.revision = 0
        self.completed_scope = ""
        self.error: str | None = None

    def enqueue(self, job: dict[str, Any]) -> None:
        with self.host.lock:
            self.pending = job
            self.error = None

    def busy(self) -> bool:
        with self.host.lock:
            return self.active is not None or self.pending is not None

    def current(self, job: dict[str, Any]) -> bool:
        with self.host.lock:
            return job["key"] == self.host._club_legacy_context_key()

    def snapshot(self, scope_id: str) -> dict[str, Any]:
        with self.host.lock:
            return {
                "data_scope_id": scope_id,
                "refreshing": any(
                    job is not None and job["scope_id"] == scope_id and self.current(job)
                    for job in (self.active, self.pending)
                ),
                "revision": self.revision,
                "error": self.error if self.completed_scope == scope_id else None,
            }

    def drain(self) -> None:
        with self.host.lock:
            if self.active is not None or self.pending is None:
                return
            self.active, self.pending = self.pending, None
        while True:
            job = self.active
            errors = []
            try:
                if self.current(job):
                    errors = self.host._sync_club_legacy_job(job, lambda: self.current(job))
            except Exception as error:
                errors = [str(error)]
            with self.host.lock:
                if self.current(job):
                    self.revision += 1
                    self.completed_scope = job["scope_id"]
                    self.error = "；".join(errors) if errors else None
                    self.host.club_legacy_error = self.error
                self.active, self.pending = self.pending, None
                if self.active is None:
                    return
