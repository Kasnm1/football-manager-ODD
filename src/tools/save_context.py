from __future__ import annotations

import json
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any

from tools.storage_io import atomic_write_bytes, atomic_write_json, storage_lock


SCHEMA_VERSION = 2


class UnsupportedSaveContextVersion(RuntimeError):
    pass


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _display_name_part(value: Any) -> str:
    return " ".join(str(value or "").split()).strip(" -")


def account_display_name(
    manager_name: str | None, team_name: str | None, manager_id: int | None,
    recognized_on: str | None,
) -> str | None:
    """Build the immutable human-facing label for one manager account."""
    manager = _display_name_part(manager_name)
    team = _display_name_part(team_name)
    manager_value = int(manager_id or 0)
    if not manager or not team or manager_value <= 0:
        return None
    try:
        recognized = date.fromisoformat(str(recognized_on or "")[:10]).isoformat()
    except ValueError:
        recognized = date.today().isoformat()
    return f"{manager}-{team}-{manager_value}-{recognized}"


class SaveContextRegistry:
    """Durable career/account identities separated from physical data folders.

    A career ID describes the loaded FM career. An account ID describes one
    human manager inside that career. Both IDs are stable UUIDs; legacy folder
    names remain storage aliases so upgrading never moves or deletes data.
    """

    def __init__(self, path: Path, data_root: Path, legacy_path: Path) -> None:
        self.path = path
        self.data_root = data_root
        self.legacy_path = legacy_path
        self.lock = storage_lock(path)

    @property
    def backup_path(self) -> Path:
        return self.path.with_name(f"{self.path.stem}.backup{self.path.suffix}")

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "updated_at": None,
            "last_active_scope_id": None,
            "careers": {},
            "evidence_index": {},
            "storage_index": {},
            "account_index": {},
            "conflicts": [],
        }

    @staticmethod
    def _read_registry(path: Path) -> dict[str, Any] | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        raw_version = payload.get("schema_version") or 0
        if isinstance(raw_version, bool):
            return None
        try:
            version = int(raw_version)
        except (TypeError, ValueError):
            return None
        if version > SCHEMA_VERSION:
            raise UnsupportedSaveContextVersion(
                f"FMODD 存档上下文由更新版本创建（上下文版本 {version}，"
                f"当前最高支持 {SCHEMA_VERSION}）：{path}"
            )
        if version != SCHEMA_VERSION:
            return None
        for key, default in (
            ("careers", {}), ("evidence_index", {}),
            ("storage_index", {}), ("account_index", {}), ("conflicts", []),
        ):
            if not isinstance(payload.get(key), type(default)):
                payload[key] = default
        return payload

    def _load(self) -> dict[str, Any]:
        return (
            self._read_registry(self.path)
            or self._read_registry(self.backup_path)
            or self._empty()
        )

    def _legacy(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.legacy_path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _write(self, payload: dict[str, Any]) -> None:
        payload["schema_version"] = SCHEMA_VERSION
        payload["updated_at"] = _now()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        backup = self.legacy_path.with_name("save_registry.v1.backup.json")
        if not self.path.exists() and self.legacy_path.is_file() and not backup.exists():
            try:
                atomic_write_bytes(backup, self.legacy_path.read_bytes())
            except OSError:
                pass
        if self._read_registry(self.path) is not None:
            try:
                atomic_write_bytes(self.backup_path, self.path.read_bytes())
            except OSError:
                pass
        atomic_write_json(self.path, payload, indent=2)

    @staticmethod
    def evidence_key(namespace: str, kind: str, value: str | None) -> str | None:
        text = str(value or "").strip()
        return f"{namespace}:{kind}:{text}" if text else None

    def _legacy_scope_hint(
        self, namespace: str, stable_key: str | None, telemetry_key: str | None,
        manager_id: int | None,
    ) -> str | None:
        legacy = self._legacy()
        stable = str(stable_key or "").strip()
        telemetry = str(telemetry_key or "").strip()
        manager = f"human-{int(manager_id or 0)}" if int(manager_id or 0) > 0 else ""
        aliases = legacy.get("savegame_aliases") or {}
        telemetry_aliases = legacy.get("telemetry_aliases") or {}
        manager_aliases = legacy.get("manager_aliases") or {}
        resolved = (
            aliases.get(stable)
            or telemetry_aliases.get(stable)
            or telemetry_aliases.get(telemetry)
        )
        # A manager alias is weak evidence and must never override a newly
        # observed stable save identity. Use it only when no strong identity
        # exists (mainly old network-session migrations).
        if not resolved and not stable and not telemetry and manager:
            resolved = manager_aliases.get(manager)
        if not resolved:
            resolved = stable or telemetry or manager or None
        if not resolved:
            return None
        text = str(resolved)
        if namespace == "fm24" and not text.startswith("fm24-"):
            text = f"fm24-{text}"
        return text

    def lookup_career(
        self, *, namespace: str, stable_key: str | None = None,
        telemetry_key: str | None = None,
    ) -> str | None:
        namespace = str(namespace or "fm26").strip().lower()
        evidence = (
            self.evidence_key(namespace, "stable", stable_key),
            self.evidence_key(namespace, "telemetry", telemetry_key),
        )
        with self.lock:
            payload = self._load()
            for item in evidence:
                owner = str(payload["evidence_index"].get(item) or "") if item else ""
                career = payload["careers"].get(owner)
                if (
                    isinstance(career, dict)
                    and str(career.get("namespace") or namespace) == namespace
                ):
                    return owner
        return None

    def has_careers(self, namespace: str) -> bool:
        namespace = str(namespace or "fm26").strip().lower()
        with self.lock:
            return any(
                isinstance(career, dict)
                and str(career.get("namespace") or "").strip().lower() == namespace
                for career in self._load()["careers"].values()
            )

    def resolve_career(
        self, *, namespace: str, stable_key: str | None = None,
        equivalent_stable_key: str | None = None,
        telemetry_key: str | None = None, manager_id: int | None = None,
        preferred_career_id: str | None = None, display_name: str | None = None,
    ) -> str | None:
        namespace = str(namespace or "fm26").strip().lower()
        stable_evidence = self.evidence_key(namespace, "stable", stable_key)
        equivalent_stable_evidence = self.evidence_key(
            namespace, "stable", equivalent_stable_key,
        )
        telemetry_evidence = self.evidence_key(namespace, "telemetry", telemetry_key)
        evidence = [
            item for item in (
                stable_evidence, equivalent_stable_evidence, telemetry_evidence,
            ) if item
        ]
        if not evidence and not preferred_career_id and not manager_id:
            return None
        with self.lock:
            payload = self._load()
            careers = payload["careers"]
            index = payload["evidence_index"]

            def valid_owner(item: str | None) -> str | None:
                owner = str(index.get(item) or "") if item else ""
                career = careers.get(owner)
                if not isinstance(career, dict):
                    return None
                return owner if str(career.get("namespace") or namespace) == namespace else None

            stable_owner = valid_owner(stable_evidence)
            equivalent_stable_owner = valid_owner(equivalent_stable_evidence)
            telemetry_owner = valid_owner(telemetry_evidence)
            if equivalent_stable_owner and stable_evidence:
                equivalent_career = careers.get(equivalent_stable_owner) or {}
                direct_prefix = f"{namespace}:stable:savegame-id-"
                migration_prefix = (
                    direct_prefix if stable_evidence.startswith(direct_prefix) else None
                )
                already_migrated = any(
                    migration_prefix and str(item).startswith(migration_prefix)
                    for item in (equivalent_career.get("evidence") or [])
                )
                if migration_prefix and already_migrated:
                    equivalent_stable_owner = None
            # Callers may provide a second independently verified stable key
            # while migrating identity sources. It can claim an unknown new
            # key, but never overrides an owner already bound to that key.
            career_id = stable_owner or equivalent_stable_owner
            preferred = str(preferred_career_id or "")
            preferred_career = careers.get(preferred)
            preferred_valid = bool(
                isinstance(preferred_career, dict)
                and str(preferred_career.get("namespace") or namespace) == namespace
            )

            def provisional_manager_account(candidate_id: str) -> str | None:
                """Return the current manager account owned by an unproven career."""
                if int(manager_id or 0) <= 0:
                    return None
                candidate = careers.get(str(candidate_id))
                if not isinstance(candidate, dict):
                    return None
                stable_prefix = f"{namespace}:stable:"
                if any(
                    str(item).startswith(stable_prefix)
                    for item in (candidate.get("evidence") or [])
                ):
                    return None
                account_id = str(
                    (candidate.get("managers") or {}).get(str(int(manager_id))) or ""
                )
                account = payload["account_index"].get(account_id)
                if not isinstance(account, dict):
                    return None
                if (
                    str(account.get("career_id") or "") != str(candidate_id)
                    or int(account.get("manager_id") or 0) != int(manager_id)
                ):
                    return None
                return account_id

            preferred_provisional_account = (
                provisional_manager_account(preferred) if preferred_valid else None
            )
            if (
                not career_id and stable_evidence and preferred_valid
                and preferred_provisional_account
            ):
                # FM24 can expose the human manager before its stable save
                # provider becomes readable. Upgrade that exact active context
                # in place so the later stable identity cannot create an empty
                # wallet/inventory account for the same manager.
                career_id = preferred
            legacy_scope = self._legacy_scope_hint(
                namespace, equivalent_stable_key or stable_key,
                telemetry_key, manager_id,
            )
            storage_conflict = False
            if not career_id and legacy_scope:
                stored = str(payload["storage_index"].get(legacy_scope) or "")
                stored_career = careers.get(stored)
                if (
                    isinstance(stored_career, dict)
                    and str(stored_career.get("namespace") or namespace) == namespace
                ):
                    stored_stable = {
                        str(item) for item in (stored_career.get("evidence") or [])
                        if str(item).startswith(f"{namespace}:stable:")
                    }
                    if stable_evidence and stored_stable and stable_evidence not in stored_stable:
                        storage_conflict = True
                    else:
                        career_id = stored
            if not career_id and stable_evidence and telemetry_owner:
                # A weak process/session identity may be created before the
                # save provider becomes readable. It may accept the first
                # stable identity, but it must never absorb a second one.
                provisional = careers.get(telemetry_owner) or {}
                stable_prefix = f"{namespace}:stable:"
                if not any(
                    str(item).startswith(stable_prefix)
                    for item in (provisional.get("evidence") or [])
                ):
                    career_id = telemetry_owner
            if not career_id and not stable_evidence:
                career_id = telemetry_owner
            if not career_id and not stable_evidence and preferred_valid:
                # Missing/weak identity is allowed to retain the last
                # confirmed career. A new stable save fingerprint is not.
                career_id = preferred
            if not career_id and not evidence and int(manager_id or 0) > 0:
                # FM can briefly lose its save/session evidence while the
                # already confirmed human manager remains readable. Retain
                # the career only when that manager belongs to exactly one
                # career in this game generation. Ambiguous managers across
                # separate stable saves must remain isolated.
                manager_key = str(int(manager_id or 0))
                matching_careers = []
                for candidate_id, candidate in careers.items():
                    if not isinstance(candidate, dict):
                        continue
                    if str(candidate.get("namespace") or namespace) != namespace:
                        continue
                    account_id = str((candidate.get("managers") or {}).get(manager_key) or "")
                    account = payload["account_index"].get(account_id)
                    if (
                        isinstance(account, dict)
                        and str(account.get("career_id") or "") == str(candidate_id)
                        and int(account.get("manager_id") or 0) == int(manager_id or 0)
                    ):
                        matching_careers.append(str(candidate_id))
                if len(matching_careers) == 1:
                    career_id = matching_careers[0]
            created = False
            if not career_id:
                career_id = f"career-{uuid.uuid4().hex}"
                legacy_name = str(
                    (self._legacy().get("save_names") or {}).get(legacy_scope or "")
                    or ""
                ).strip()
                storage_scope = legacy_scope or career_id
                if storage_conflict:
                    stable_scope = str(stable_key or "").strip()
                    if namespace == "fm24" and stable_scope and not stable_scope.startswith("fm24-"):
                        stable_scope = f"fm24-{stable_scope}"
                    storage_scope = stable_scope or career_id
                    if str(payload["storage_index"].get(storage_scope) or "") not in {"", career_id}:
                        storage_scope = career_id
                careers[career_id] = {
                    "career_id": career_id,
                    "namespace": namespace,
                    "display_name": str(display_name or legacy_name or legacy_scope or career_id),
                    "shared_storage_scope": storage_scope,
                    "evidence": [],
                    "managers": {},
                    "selected_manager_id": None,
                    "created_at": _now(),
                    "last_seen_at": _now(),
                }
                created = True
            career = careers[career_id]
            changed = created
            if (
                stable_evidence and preferred_valid and preferred != career_id
                and preferred_provisional_account
            ):
                manager_key = str(int(manager_id or 0))
                target_account_id = str(
                    (career.get("managers") or {}).get(manager_key) or ""
                )
                if not target_account_id:
                    provisional = careers.get(preferred) or {}
                    provisional_managers = provisional.get("managers")
                    if isinstance(provisional_managers, dict):
                        provisional_managers.pop(manager_key, None)
                    if int(provisional.get("selected_manager_id") or 0) == int(
                        manager_id
                    ):
                        provisional["selected_manager_id"] = None
                    account = payload["account_index"][preferred_provisional_account]
                    account["career_id"] = career_id
                    career.setdefault("managers", {})[manager_key] = (
                        preferred_provisional_account
                    )
                    career["selected_manager_id"] = int(manager_id)
                    changed = True
            if display_name and str(career.get("display_name") or "") != str(display_name):
                career["display_name"] = str(display_name)
                changed = True
            if legacy_scope and not career.get("shared_storage_scope"):
                career["shared_storage_scope"] = legacy_scope
                changed = True
            shared_scope = str(career.get("shared_storage_scope") or career_id)
            if payload["storage_index"].get(shared_scope) != career_id:
                if shared_scope not in payload["storage_index"]:
                    payload["storage_index"][shared_scope] = career_id
                    changed = True
            known_evidence = set(career.get("evidence") or [])
            for item in evidence:
                owner = str(index.get(item) or "")
                if owner and owner != career_id:
                    conflict = {
                        "evidence": item,
                        "kept_career_id": owner,
                        "rejected_career_id": career_id,
                    }
                    if not any(
                        all(existing.get(key) == value for key, value in conflict.items())
                        for existing in payload["conflicts"]
                        if isinstance(existing, dict)
                    ):
                        payload["conflicts"].append({"at": _now(), **conflict})
                        changed = True
                    continue
                if item not in known_evidence:
                    career.setdefault("evidence", []).append(item)
                    known_evidence.add(item)
                    changed = True
                if index.get(item) != career_id:
                    index[item] = career_id
                    changed = True
            if changed:
                career["last_seen_at"] = _now()
                self._write(payload)
            return career_id

    def _legacy_account_scope(self, shared_scope: str, manager_id: int) -> str | None:
        legacy = self._legacy()
        value = str(
            (legacy.get("account_scopes") or {}).get(
                f"{shared_scope}|manager-{manager_id}",
            )
            or ""
        ).strip()
        return value or None

    def _legacy_selected_manager(self, shared_scope: str) -> int:
        try:
            return int(
                (self._legacy().get("selected_managers") or {}).get(shared_scope)
                or 0
            )
        except (TypeError, ValueError):
            return 0

    def resolve_storage_scope(self, identity: str | None) -> str | None:
        value = str(identity or "").strip()
        if not value:
            return None
        with self.lock:
            payload = self._load()
            career = payload["careers"].get(value)
            if isinstance(career, dict):
                return str(career.get("shared_storage_scope") or value)
            account = payload["account_index"].get(value)
            if isinstance(account, dict):
                return str(account.get("storage_scope") or value)
        return value

    def career_storage_scopes(self) -> set[str]:
        """Return physical scopes reserved for career-shared documents.

        A career container may coexist with one or more manager account
        containers.  It is durable data, but it is not itself selectable as a
        wallet/account unless an account row explicitly claims the same
        physical scope during legacy migration.
        """
        with self.lock:
            return {
                str(career.get("shared_storage_scope") or career_id)
                for career_id, career in self._load()["careers"].items()
                if isinstance(career, dict)
            }

    def _storage_scope_exists(self, scope: str) -> bool:
        saves_root = self.data_root / "saves"
        return bool(
            (saves_root / scope).exists()
            or (saves_root / f"{scope}.fmodd").exists()
        )

    def last_active_scope(self) -> str | None:
        with self.lock:
            payload = self._load()
            value = str(payload.get("last_active_scope_id") or "").strip()
            if not value:
                return None
            for account_id, account in payload["account_index"].items():
                if not isinstance(account, dict):
                    continue
                if value in {
                    str(account_id),
                    str(account.get("storage_scope") or account_id),
                }:
                    return str(account_id)
            career_scopes = {
                str(career.get("shared_storage_scope") or career_id)
                for career_id, career in payload["careers"].items()
                if isinstance(career, dict)
            }
            if value in payload["careers"] or value in career_scopes:
                return None
            # Retain unresolved legacy account aliases until their first
            # successful v2 account binding claims them.
            return value

    def remember_active_scope(self, scope_id: str | None) -> str | None:
        value = str(scope_id or "").strip()
        if not value:
            return None
        with self.lock:
            payload = self._load()
            if str(payload.get("last_active_scope_id") or "") == value:
                return value
            payload["last_active_scope_id"] = value
            self._write(payload)
        return value

    def resolve_account(
        self, career_id: str, manager_id: int | None,
        *, preferred_scope: str | None = None,
        manager_name: str | None = None, team_name: str | None = None,
    ) -> str | None:
        manager_value = int(manager_id or 0)
        with self.lock:
            payload = self._load()
            career = payload["careers"].get(str(career_id))
            if not isinstance(career, dict):
                return None
            if manager_value <= 0:
                return str(career_id)
            managers = career.setdefault("managers", {})
            existing = managers.get(str(manager_value))
            account = payload["account_index"].get(str(existing))
            if existing and isinstance(account, dict):
                if self._ensure_account_display_name(
                    account, manager_name, team_name, manager_value,
                ):
                    self._write(payload)
                return str(existing)
            preferred = str(preferred_scope or "")
            if preferred in payload["account_index"]:
                account = payload["account_index"][preferred]
                if (
                    isinstance(account, dict)
                    and str(account.get("career_id")) == str(career_id)
                    and int(account.get("manager_id") or 0) == manager_value
                ):
                    managers[str(manager_value)] = preferred
                    self._ensure_account_display_name(
                        account, manager_name, team_name, manager_value,
                    )
                    self._write(payload)
                    return preferred
            shared = str(career.get("shared_storage_scope") or career_id)
            legacy_manager = f"{shared}.manager-{manager_value}"
            migrated_storage = self._legacy_account_scope(shared, manager_value)
            legacy_selected_manager = self._legacy_selected_manager(shared)
            preferred_storage = self.resolve_storage_scope(preferred) if preferred else None
            assigned_storage = {
                str(item.get("storage_scope") or "")
                for item in payload["account_index"].values()
                if isinstance(item, dict)
            }
            if migrated_storage:
                storage_scope = migrated_storage
            elif preferred_storage and (
                preferred_storage == shared or preferred_storage.startswith(f"{shared}.manager-")
            ):
                storage_scope = preferred_storage
            elif self._storage_scope_exists(legacy_manager):
                storage_scope = legacy_manager
            elif (
                self._storage_scope_exists(shared)
                and shared not in assigned_storage
                and legacy_selected_manager in {0, manager_value}
            ):
                storage_scope = shared
            else:
                storage_scope = legacy_manager
            account_id = f"account-{uuid.uuid4().hex}"
            payload["account_index"][account_id] = {
                "account_id": account_id,
                "career_id": str(career_id),
                "manager_id": manager_value,
                "storage_scope": storage_scope,
                "created_at": _now(),
                "last_seen_at": _now(),
            }
            self._ensure_account_display_name(
                payload["account_index"][account_id],
                manager_name, team_name, manager_value,
            )
            managers[str(manager_value)] = account_id
            career["selected_manager_id"] = manager_value
            self._write(payload)
            return account_id

    def create_merged_account(
        self, career_id: str, manager_id: int,
        source_account_ids: list[str], *,
        manager_name: str | None = None, team_name: str | None = None,
    ) -> dict[str, Any]:
        """Create a fresh account containing the selected same-manager sources.

        Source account rows remain in the registry for recovery, but are marked
        as merged so normal account pickers do not offer them again.  The
        physical containers are merged by ``account_store`` after this method
        returns; the returned previous mapping allows the caller to roll back
        the registry if that write fails.
        """
        career_key = str(career_id or "").strip()
        manager_value = int(manager_id or 0)
        requested = [str(item or "").strip() for item in source_account_ids]
        requested = list(dict.fromkeys(item for item in requested if item))
        if not career_key or manager_value <= 0 or len(requested) < 2:
            raise ValueError("至少需要两个同经理账户才能合并")
        with self.lock:
            payload = self._load()
            career = payload["careers"].get(career_key)
            if not isinstance(career, dict):
                raise ValueError("当前生涯身份不存在")
            namespace = str(career.get("namespace") or "").strip().lower()
            valid_sources: list[str] = []
            for account_id in requested:
                account = payload["account_index"].get(account_id)
                if not isinstance(account, dict):
                    continue
                if int(account.get("manager_id") or 0) != manager_value:
                    continue
                source_career = payload["careers"].get(str(account.get("career_id") or ""))
                if (
                    not isinstance(source_career, dict)
                    or str(source_career.get("namespace") or "").strip().lower() != namespace
                ):
                    continue
                if account.get("merged_into"):
                    continue
                valid_sources.append(account_id)
            if len(valid_sources) < 2:
                raise ValueError("当前生涯下没有两个可合并的同经理账户")
            previous_account_id = str(
                (career.get("managers") or {}).get(str(manager_value)) or ""
            ) or None
            account_id = f"account-{uuid.uuid4().hex}"
            shared = str(career.get("shared_storage_scope") or career_key)
            storage_scope = f"{shared}.manager-{manager_value}.merged-{uuid.uuid4().hex[:12]}"
            now = _now()
            account = {
                "account_id": account_id,
                "career_id": career_key,
                "manager_id": manager_value,
                "storage_scope": storage_scope,
                "created_at": now,
                "last_seen_at": now,
                "merged_from": valid_sources,
            }
            self._ensure_account_display_name(account, manager_name, team_name, manager_value)
            payload["account_index"][account_id] = account
            managers = career.setdefault("managers", {})
            managers[str(manager_value)] = account_id
            career["selected_manager_id"] = manager_value
            for source_id in valid_sources:
                source = payload["account_index"].get(source_id)
                if isinstance(source, dict):
                    source["merged_into"] = account_id
                    source["merged_at"] = now
            self._write(payload)
            return {
                "account_id": account_id,
                "storage_scope": storage_scope,
                "source_account_ids": valid_sources,
                "previous_account_id": previous_account_id,
                "namespace": namespace,
            }

    def rollback_merged_account(
        self, career_id: str, manager_id: int, merge: dict[str, Any],
    ) -> None:
        """Undo a just-created merge when its physical container cannot write."""
        account_id = str(merge.get("account_id") or "").strip()
        source_ids = [str(item) for item in (merge.get("source_account_ids") or [])]
        previous = str(merge.get("previous_account_id") or "").strip()
        if not account_id:
            return
        with self.lock:
            payload = self._load()
            payload["account_index"].pop(account_id, None)
            career = payload["careers"].get(str(career_id or ""))
            if isinstance(career, dict):
                managers = career.get("managers")
                if isinstance(managers, dict) and str(managers.get(str(int(manager_id or 0))) or "") == account_id:
                    if previous:
                        managers[str(int(manager_id or 0))] = previous
                    else:
                        managers.pop(str(int(manager_id or 0)), None)
            for source_id in source_ids:
                source = payload["account_index"].get(source_id)
                if isinstance(source, dict) and str(source.get("merged_into") or "") == account_id:
                    source.pop("merged_into", None)
                    source.pop("merged_at", None)
            self._write(payload)

    @staticmethod
    def _ensure_account_display_name(
        account: dict[str, Any], manager_name: str | None,
        team_name: str | None, manager_id: int,
    ) -> bool:
        if str(account.get("display_name") or "").strip():
            return False
        recognized_on = str(
            account.get("recognized_on") or account.get("created_at") or ""
        )[:10]
        try:
            recognized_on = date.fromisoformat(recognized_on).isoformat()
        except ValueError:
            recognized_on = date.today().isoformat()
        display_name = account_display_name(
            manager_name, team_name, manager_id, recognized_on,
        )
        if not display_name:
            return False
        account["display_name"] = display_name
        account["recognized_on"] = recognized_on
        return True

    def account_name(self, account_id: str) -> str | None:
        with self.lock:
            account = self._load()["account_index"].get(str(account_id))
            if isinstance(account, dict):
                value = str(account.get("display_name") or "").strip()
                return value or None
        return None

    def remember_name(self, career_id: str, value: str) -> str | None:
        cleaned = str(value or "").strip()
        if not cleaned:
            return None
        with self.lock:
            payload = self._load()
            career = payload["careers"].get(str(career_id))
            if not isinstance(career, dict):
                return None
            if str(career.get("display_name") or "") == cleaned:
                return cleaned
            career["display_name"] = cleaned
            self._write(payload)
        return cleaned

    def career_name(self, career_id: str) -> str | None:
        with self.lock:
            career = self._load()["careers"].get(str(career_id))
            if isinstance(career, dict):
                value = str(career.get("display_name") or "").strip()
                return value or None
        return None

    def selected_manager(self, career_id: str) -> int | None:
        with self.lock:
            career = self._load()["careers"].get(str(career_id))
            try:
                value = int((career or {}).get("selected_manager_id") or 0)
            except (TypeError, ValueError):
                value = 0
            return value or None

    def remember_selected_manager(self, career_id: str, manager_id: int) -> int | None:
        value = int(manager_id or 0)
        if value <= 0:
            return None
        with self.lock:
            payload = self._load()
            career = payload["careers"].get(str(career_id))
            if not isinstance(career, dict):
                return None
            if int(career.get("selected_manager_id") or 0) == value:
                return value
            career["selected_manager_id"] = value
            self._write(payload)
        return value

    def list_accounts(self) -> list[dict[str, Any]]:
        with self.lock:
            payload = self._load()
            rows = []
            for account_id, account in payload["account_index"].items():
                if not isinstance(account, dict):
                    continue
                career_id = str(account.get("career_id") or "")
                career = payload["careers"].get(career_id) or {}
                rows.append({
                    "scope_id": str(account_id),
                    "career_id": career_id,
                    "save_id": career_id,
                    "save_name": str(
                        account.get("display_name")
                        or career.get("display_name") or career_id
                    ),
                    "identity_display_name": bool(account.get("display_name")),
                    "namespace": str(career.get("namespace") or ""),
                    "manager_id": int(account.get("manager_id") or 0) or None,
                    "storage_scope": str(account.get("storage_scope") or account_id),
                    "created_at": str(account.get("created_at") or ""),
                    "merged_into": str(account.get("merged_into") or ""),
                    "context_version": SCHEMA_VERSION,
                })
            return rows

    def forget_storage_scopes(self, storage_scopes: set[str]) -> int:
        """Forget account rows whose durable storage files were explicitly deleted."""
        targets = {str(item or "").strip() for item in storage_scopes if str(item or "").strip()}
        if not targets:
            return 0
        with self.lock:
            payload = self._load()
            removed_ids = {
                str(account_id)
                for account_id, account in payload["account_index"].items()
                if isinstance(account, dict)
                and str(account.get("storage_scope") or account_id) in targets
            }
            if not removed_ids:
                return 0
            for account_id in removed_ids:
                payload["account_index"].pop(account_id, None)
            for career in payload["careers"].values():
                if not isinstance(career, dict):
                    continue
                managers = career.get("managers")
                if isinstance(managers, dict):
                    career["managers"] = {
                        manager_id: account_id
                        for manager_id, account_id in managers.items()
                        if str(account_id) not in removed_ids
                    }
            if str(payload.get("last_active_scope_id") or "") in removed_ids:
                payload["last_active_scope_id"] = None
            self._write(payload)
            return len(removed_ids)
