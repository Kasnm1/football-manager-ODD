from __future__ import annotations

from collections.abc import Callable
from typing import Any


class TrainingRosterIndex:
    """Resolve persisted training subjects against cached and live team rosters."""

    def __init__(
        self,
        profiles: list[dict[str, Any]],
        managed_teams: list[dict[str, Any]],
        *,
        read_roster: Callable[[Any], list[dict[str, Any]]],
        resolve_player_address: Callable[[int, Any, int, Any], str | None] | None = None,
        player_manager_candidate: Callable[[int], dict[str, Any] | None],
        rebind_player: Callable[[str, str], dict[str, Any]],
        rebind_staff: Callable[[str, str], dict[str, Any]],
    ) -> None:
        self.profiles = profiles
        self.managed_teams = managed_teams
        self._read_roster = read_roster
        self._resolve_player_address = resolve_player_address
        self._player_manager_candidate = player_manager_candidate
        self._rebind_player = rebind_player
        self._rebind_staff = rebind_staff
        self._live_rosters: dict[int, list[dict[str, Any]] | None] = {}

        self.profiles_by_team = {
            int((profile.get("team") or {}).get("id") or 0): profile
            for profile in profiles
            if int((profile.get("team") or {}).get("id") or 0) > 0
        }
        self.managed_teams_by_id = {
            int(row.get("id") or 0): row
            for row in managed_teams
            if int(row.get("id") or 0) > 0
        }
        self.player_candidates = self._index_people("players")
        self.staff_candidates = self._index_people("staff", club_only=True)

    def _index_people(
        self, collection: str, *, club_only: bool = False,
    ) -> dict[int, list[tuple[dict[str, Any], dict[str, Any]]]]:
        candidates: dict[int, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
        for profile in self.profiles:
            team_type = str(
                (profile.get("team") or {}).get("team_type") or "club"
            )
            if club_only and team_type != "club":
                continue
            for person in profile.get(collection, []):
                person_id = int(person.get("id") or 0)
                if person_id > 0:
                    candidates.setdefault(person_id, []).append((profile, person))
        return candidates

    def _live_roster(self, team_id: int) -> list[dict[str, Any]] | None:
        if team_id in self._live_rosters:
            return self._live_rosters[team_id]
        team = self.managed_teams_by_id.get(team_id) or {}
        profile_team = (self.profiles_by_team.get(team_id) or {}).get("team") or {}
        team_address = team.get("address") or profile_team.get("address")
        if not team_address:
            self._live_rosters[team_id] = None
            return None
        try:
            roster = self._read_roster(team_address)
        except Exception:
            roster = None
        self._live_rosters[team_id] = roster
        return roster

    def _team_address(self, team_id: int) -> Any:
        team = self.managed_teams_by_id.get(int(team_id)) or {}
        profile_team = (
            self.profiles_by_team.get(int(team_id)) or {}
        ).get("team") or {}
        return team.get("address") or profile_team.get("address")

    def _validated_player_address(
        self, team_id: int, player_id: int, address_hint: Any,
    ) -> str | None:
        if self._resolve_player_address is None:
            return None
        team_address = self._team_address(team_id)
        if not team_address:
            return None
        try:
            return self._resolve_player_address(
                int(team_id), team_address, int(player_id), address_hint,
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            return None

    def scan_player(self, focus: dict[str, Any]) -> dict[str, Any] | None:
        """Resolve a due event from fresh rosters, then a validated saved address."""
        player_id = int(focus.get("player_id") or 0)
        preferred_team_id = int(focus.get("player_team_id") or 0)
        candidate_ids = list(dict.fromkeys([
            preferred_team_id,
            *(int(row.get("id") or 0) for row in self.managed_teams),
        ]))
        for team_id in candidate_ids:
            if team_id <= 0:
                continue
            cached = next((
                player for _profile, player in self.player_candidates.get(player_id, [])
                if int((_profile.get("team") or {}).get("id") or 0) == team_id
            ), None)
            address = self._validated_player_address(
                team_id, player_id,
                focus.get("player_address") or (cached or {}).get("address"),
            )
            if address:
                player = cached or {
                    "id": player_id,
                    "name": focus.get("player_name") or str(player_id),
                }
                player["address"] = address
                return player
            player = next((
                row for row in (self._live_roster(team_id) or [])
                if int(row.get("id") or 0) == player_id
            ), None)
            if player:
                return player

        cached = self.player_candidates.get(player_id) or []
        cached_player = cached[0][1] if cached else None
        fallback_address = (
            focus.get("player_address") or (cached_player or {}).get("address")
        )
        if player_id > 0 and fallback_address:
            return {
                "id": player_id,
                "address": fallback_address,
                "name": focus.get("player_name") or str(player_id),
            }
        return None

    def resolve_player(
        self, focus: dict[str, Any], *, force_live: bool = False,
    ) -> dict[str, Any] | None:
        if force_live:
            return self.scan_player(focus)
        player_id = int(focus.get("player_id") or 0)
        team_id = int(focus.get("player_team_id") or 0)
        if team_id:
            profile = self.profiles_by_team.get(team_id)
            player = next((
                row for row in (profile or {}).get("players", [])
                if int(row.get("id") or 0) == player_id
            ), None)
            if player:
                return player

        candidates = self.player_candidates.get(player_id) or []
        if candidates:
            club_player = next((
                player for profile, player in candidates
                if str(
                    (profile.get("team") or {}).get("team_type") or "club"
                ) == "club"
            ), None)
            return club_player or candidates[0][1]

        # Cached profiles prove that an unmatched address is stale. Without a
        # current profile, retain the saved address and let the memory writer
        # perform the final UID/object validation.
        if self.profiles:
            return None
        address = focus.get("player_address")
        if player_id > 0 and address:
            return {
                "id": player_id,
                "address": address,
                "name": focus.get("player_name") or str(player_id),
            }
        return None

    def resolve_staff(
        self, focus: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        staff_id = int(focus.get("staff_id") or 0)
        team_id = int(focus.get("staff_team_id") or 0)
        if focus.get("subject_type") == "player_manager":
            manager = self._player_manager_candidate(team_id)
            if manager and int(manager.get("id") or 0) == staff_id:
                return {
                    "team": {
                        "id": int(manager.get("training_team_id") or 0),
                        "name": str(manager.get("training_team_name") or ""),
                        "address": manager.get("team_address"),
                        "team_type": "club",
                    },
                }, manager
            return None

        if team_id:
            profile = self.profiles_by_team.get(team_id)
            staff = next((
                row for row in (profile or {}).get("staff", [])
                if int(row.get("id") or 0) == staff_id
            ), None)
            if staff:
                return profile, staff
        candidates = self.staff_candidates.get(staff_id) or []
        return candidates[0] if candidates else None

    def bind_player_address(
        self, focus: dict[str, Any], player: dict[str, Any] | None,
    ) -> str:
        address = str((player or {}).get("address") or "")
        if address and address != str(focus.get("player_address") or ""):
            try:
                self._rebind_player(str(focus.get("id") or ""), address)
                focus["player_address"] = address
            except (OSError, RuntimeError, ValueError):
                pass
        return address

    def bind_staff_address(
        self, focus: dict[str, Any], staff: dict[str, Any] | None,
    ) -> str:
        address = str((staff or {}).get("address") or "")
        if address and address != str(focus.get("staff_address") or ""):
            try:
                self._rebind_staff(str(focus.get("id") or ""), address)
                focus["staff_address"] = address
            except (OSError, RuntimeError, ValueError):
                pass
        return address

    def sync_rows(self, focuses: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
        rows: list[tuple[Any, ...]] = []
        for focus in focuses:
            if focus.get("focus_type") == "coaching_license":
                resolved = self.resolve_staff(focus)
                profile, staff = resolved or ({}, {})
                address = self.bind_staff_address(focus, staff)
                license_code = int(
                    (staff.get("coaching_license") or {}).get("code", -1)
                )
                rows.append((
                    str(focus.get("id") or ""), "coaching_license",
                    str(focus.get("subject_type") or "staff"),
                    int(focus.get("initial_license_code") or 0),
                    int(focus.get("target_license_code") or 0),
                    str(focus.get("due_on") or ""), address,
                    int((profile.get("team") or {}).get("id") or 0), license_code,
                ))
                continue

            player = self.resolve_player(focus)
            address = self.bind_player_address(focus, player)
            cached_attribute = None
            if player and focus.get("focus_type") == "attribute":
                group, _, name = str(focus.get("attribute_key") or "").partition(":")
                if focus.get("attribute_kind") == "hidden":
                    hidden_name = "争论" if name == "争议性" else name
                    cached_attribute = (
                        player.get("hidden_attributes") or {}
                    ).get(hidden_name)
                else:
                    cached_attribute = (
                        (player.get("attributes") or {}).get(group) or {}
                    ).get(name)
            rows.append((
                str(focus.get("id") or ""), str(focus.get("focus_type") or ""),
                int(focus.get("initial_attribute") or 0),
                int(focus.get("progress_points") or 0),
                int(focus.get("required_points") or 0),
                str(focus.get("expires_on") or ""), address, cached_attribute,
            ))
        return rows
