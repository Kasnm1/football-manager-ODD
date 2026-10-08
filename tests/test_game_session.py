from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace
import threading
import time
from unittest.mock import Mock, patch

from tools import club_reader, game_session, preview_cup_odds, world_clubs
from tools.game_layout import FM24_XGP_LAYOUT, FM26_LAYOUT, FM26_XGP_TEMPLATE


class FakeProcess:
    def __init__(self, pid: int):
        self.pid = pid
        self.handle = pid + 1000
        self.close_count = 0

    def close(self) -> None:
        self.close_count += 1
        self.handle = 0


class FakeLayout:
    def __init__(self, key: str, base: int):
        self.key = key
        self.distribution = "steam"
        self.executable_sha256 = key
        self.module_name = "game_plugin.dll"
        self._module = SimpleNamespace(base_address=base)
        self.module_calls = 0

    def module(self, _process):
        self.module_calls += 1
        return self._module


def test_read_session_reuses_handle_and_module_for_same_process():
    registry = game_session.GameSessionRegistry()
    layout = FakeLayout("fm26", 0x100000)
    process = FakeProcess(26)
    with (
        patch.object(game_session, "open_process", return_value=process) as opened,
        patch.object(game_session, "read_process_memory", return_value=b"MZ"),
    ):
        first = registry.acquire((26, "fm26.exe", layout))
        registry.release(first)
        second = registry.acquire((26, "fm26.exe", layout))
        registry.release(second)

    assert first is second
    opened.assert_called_once_with(26)
    assert layout.module_calls == 1


def test_active_session_skips_process_selection_on_followup_reads():
    registry = game_session.GameSessionRegistry()
    layout = FakeLayout("fm26", 0x100000)
    process = FakeProcess(26)
    with (
        patch.object(game_session, "open_process", return_value=process),
        patch.object(game_session, "read_process_memory", return_value=b"MZ"),
        patch(
            "tools.initial_data_audit.select_process_layout",
            return_value=(26, "fm26.exe", layout),
        ) as selected,
    ):
        first = registry.acquire()
        registry.release(first)
        second = registry.acquire()
        registry.release(second)

    assert first is second
    selected.assert_called_once_with()


def test_active_selection_reuses_verified_session_without_new_lease():
    registry = game_session.GameSessionRegistry()
    layout = FakeLayout("fm26", 0x100000)
    process = FakeProcess(26)
    with (
        patch.object(game_session, "open_process", return_value=process),
        patch.object(game_session, "read_process_memory", return_value=b"MZ"),
    ):
        session = registry.acquire((26, "fm26.exe", layout))
        registry.release(session)
        selected = registry.active_selection()

    assert selected == (26, "fm26.exe", layout)
    assert session.leases == 0
    assert process.close_count == 0


def test_active_selection_retires_dead_session():
    registry = game_session.GameSessionRegistry()
    layout = FakeLayout("fm26", 0x100000)
    process = FakeProcess(26)
    with (
        patch.object(game_session, "open_process", return_value=process),
        patch.object(
            game_session, "read_process_memory", side_effect=[b"MZ", b""],
        ),
    ):
        session = registry.acquire((26, "fm26.exe", layout))
        registry.release(session)
        assert registry.active_selection() is None

    assert process.close_count == 1
    assert registry.snapshot() == []


def test_game_layout_reuses_active_session_module_before_enumeration():
    process = FakeProcess(26)
    module = SimpleNamespace(base_address=0x260000)
    with (
        patch.object(game_session, "active_game_module", return_value=module) as active,
        patch("fm_collector.win32.find_module") as find,
    ):
        assert FM26_LAYOUT.module(process) is module

    active.assert_called_once_with(process, FM26_LAYOUT)
    find.assert_not_called()


def test_active_module_requires_same_pid_and_layout_identity():
    registry = game_session.GameSessionRegistry()
    layout = FakeLayout("fm26", 0x100000)
    process = FakeProcess(26)
    with (
        patch.object(game_session, "open_process", return_value=process),
        patch.object(game_session, "read_process_memory", return_value=b"MZ"),
    ):
        session = registry.acquire((26, "fm26.exe", layout))
        registry.release(session)
        assert registry.active_module(26, layout) is layout._module
        assert registry.active_module(24, layout) is None
        assert registry.active_module(26, FakeLayout("fm24", 0x200000)) is None


def test_foreground_priority_lock_serves_queued_user_before_background():
    lock = game_session.ForegroundPriorityLock()
    order: list[str] = []
    background_started = threading.Event()
    foreground_started = threading.Event()
    foreground_acquired = threading.Event()
    background_acquired = threading.Event()
    release_foreground = threading.Event()

    def background() -> None:
        background_started.set()
        with lock:
            order.append("background")
            background_acquired.set()

    def foreground() -> None:
        foreground_started.set()
        lock.acquire_foreground()
        try:
            order.append("foreground")
            foreground_acquired.set()
            release_foreground.wait(2)
        finally:
            lock.release()

    lock.acquire()
    background_thread = threading.Thread(target=background)
    foreground_thread = threading.Thread(target=foreground)
    background_thread.start()
    assert background_started.wait(1)
    foreground_thread.start()
    assert foreground_started.wait(1)
    deadline = time.monotonic() + 1
    while lock.foreground_waiters != 1 and time.monotonic() < deadline:
        time.sleep(0.001)
    assert lock.foreground_waiters == 1

    lock.release()
    assert foreground_acquired.wait(1)
    assert order == ["foreground"]
    assert not background_acquired.is_set()
    release_foreground.set()
    assert background_acquired.wait(1)
    foreground_thread.join(1)
    background_thread.join(1)
    assert order == ["foreground", "background"]


def test_foreground_priority_lock_keeps_standard_timeout_contract():
    lock = game_session.ForegroundPriorityLock()
    assert lock.acquire(blocking=False)
    assert lock.locked()
    assert not lock.acquire(timeout=0.001)
    lock.release()
    assert not lock.locked()


def test_foreground_request_marks_standard_context_acquire_as_priority():
    lock = game_session.ForegroundPriorityLock()
    order: list[str] = []
    background_acquired = threading.Event()
    foreground_acquired = threading.Event()

    def background() -> None:
        with lock:
            order.append("background")
            background_acquired.set()

    def foreground() -> None:
        with lock.foreground_request():
            with lock:
                order.append("foreground")
                foreground_acquired.set()

    lock.acquire()
    background_thread = threading.Thread(target=background)
    foreground_thread = threading.Thread(target=foreground)
    background_thread.start()
    foreground_thread.start()
    deadline = time.monotonic() + 1
    while lock.foreground_waiters != 1 and time.monotonic() < deadline:
        time.sleep(0.001)
    assert lock.foreground_waiters == 1
    lock.release()
    assert foreground_acquired.wait(1)
    assert background_acquired.wait(1)
    foreground_thread.join(1)
    background_thread.join(1)
    assert order == ["foreground", "background"]


def test_supported_reader_forwards_verified_process_selection():
    layout = FakeLayout("fm26", 0x100000)
    selected = (26, "fm26.exe", layout)
    reader = SimpleNamespace(
        process_path="fm26.exe", layout=layout,
        module=layout._module, process=FakeProcess(26),
    )
    with patch.object(
        preview_cup_odds, "borrow_game_reader", return_value=nullcontext(reader),
    ) as borrowed:
        with preview_cup_odds.open_supported_reader(selected) as opened:
            assert opened == ("fm26.exe", layout, layout._module, reader)

    borrowed.assert_called_once_with(selected)


def test_operation_session_reuses_reader_and_limits_write_handle_lifetime():
    layout = FakeLayout("fm26", 0x100000)
    read_process = FakeProcess(26)
    writable_process = FakeProcess(26)
    reader = SimpleNamespace(
        process=read_process, layout=layout, module=layout._module,
        session_generation=7, _page_cache={0x1000: b"cached"},
    )
    with (
        patch.object(
            game_session, "borrow_game_reader", return_value=nullcontext(reader),
        ) as borrowed,
        patch.object(
            game_session, "open_process", return_value=nullcontext(writable_process),
        ) as opened,
    ):
        with game_session.borrow_game_operation(write_memory=True) as operation:
            assert operation.reader is reader
            assert operation.process is writable_process
            assert operation.module is layout._module
            assert operation.generation == 7
            assert operation.writable is True
            assert operation.pid == 26
            assert reader._page_cache is None

    borrowed.assert_called_once_with()
    opened.assert_called_once_with(26, write_memory=True)


def test_read_sessions_keep_fm24_and_fm26_separate():
    registry = game_session.GameSessionRegistry()
    layouts = [FakeLayout("fm24", 0x240000), FakeLayout("fm26", 0x260000)]
    processes = [FakeProcess(24), FakeProcess(26)]
    with (
        patch.object(game_session, "open_process", side_effect=processes),
        patch.object(game_session, "read_process_memory", return_value=b"MZ"),
    ):
        fm24 = registry.acquire((24, "fm24.exe", layouts[0]))
        registry.release(fm24)
        fm26 = registry.acquire((26, "fm26.exe", layouts[1]))
        registry.release(fm26)

    assert fm24 is not fm26
    assert {row["game_key"] for row in registry.snapshot()} == {"fm24", "fm26"}


def test_same_pid_layout_change_retires_stale_session():
    registry = game_session.GameSessionRegistry()
    first_layout = FakeLayout("fm26", 0x260000)
    second_layout = FakeLayout("fm26", 0x270000)
    first_layout.executable_sha256 = "first-build"
    second_layout.executable_sha256 = "second-build"
    processes = [FakeProcess(26), FakeProcess(26)]
    with (
        patch.object(game_session, "open_process", side_effect=processes),
        patch.object(game_session, "read_process_memory", return_value=b"MZ"),
    ):
        first = registry.acquire((26, "fm26.exe", first_layout))
        registry.release(first)
        second = registry.acquire((26, "fm26.exe", second_layout))
        registry.release(second)

    assert first is not second
    assert processes[0].close_count == 1
    assert processes[1].close_count == 0
    assert len(registry.snapshot()) == 1


def test_invalidation_waits_for_active_reader_before_closing_handle():
    registry = game_session.GameSessionRegistry()
    layout = FakeLayout("fm24", 0x240000)
    process = FakeProcess(24)
    with (
        patch.object(game_session, "open_process", return_value=process),
        patch.object(game_session, "read_process_memory", return_value=b"MZ"),
    ):
        session = registry.acquire((24, "fm24.exe", layout))
        registry.invalidate(pid=24)
        assert process.close_count == 0
        registry.release(session)

    assert process.close_count == 1


def test_close_waits_for_background_database_warm_before_closing_handle():
    started = threading.Event()
    release = threading.Event()
    process = FakeProcess(26)
    index = SimpleNamespace()

    def warm(cancel_event):
        assert cancel_event is not None
        started.set()
        release.wait(2)

    index.warm = warm
    session = game_session.GameReadSession(
        pid=26,
        process_path="fm26.exe",
        layout=FakeLayout("fm26", 0x260000),
        process=process,
        module=SimpleNamespace(base_address=0x260000),
        generation=1,
    )
    try:
        with (
            patch("tools.initial_data_audit.Reader", return_value=SimpleNamespace()),
            patch("tools.database_index.DatabaseIndex", return_value=index),
        ):
            assert session.get_database_index() is index
            assert started.wait(1)
            session.close()

        assert session.database_index_cancel.is_set()
        assert session.close_pending is True
        assert process.close_count == 0
    finally:
        release.set()
        thread = session.database_index_thread
        if thread is not None:
            thread.join(2)

    assert process.close_count == 1
    assert session.closed is True


def test_failed_database_index_exposes_error_and_allows_forced_retry():
    process = FakeProcess(26)
    index = SimpleNamespace(warm=lambda _cancel: None)
    session = game_session.GameReadSession(
        pid=26,
        process_path="fm26.exe",
        layout=FakeLayout("fm26", 0x260000),
        process=process,
        module=SimpleNamespace(base_address=0x260000),
        generation=1,
    )
    reader = SimpleNamespace()
    with (
        patch("tools.initial_data_audit.Reader", return_value=reader),
        patch(
            "tools.database_index.DatabaseIndex",
            side_effect=[RuntimeError("native person table header is invalid"), index],
        ) as build,
    ):
        assert session.get_database_index() is None
        assert session.get_database_index_error() == "native person table header is invalid"
        assert session.get_database_index() is None
        assert build.call_count == 1
        assert session.get_database_index(force_retry=True) is index
        assert build.call_count == 2

    session.close()


def test_warm_database_index_triggers_reader_provider_without_waiting_for_scan():
    selected = (26, "fm26.exe", FakeLayout("fm26", 0x260000))
    provider = Mock(return_value=SimpleNamespace())
    reader = SimpleNamespace(database_index_provider=provider)
    with patch.object(
        game_session, "borrow_game_reader", return_value=nullcontext(reader),
    ) as borrowed:
        assert game_session.warm_database_index(selected) is True

    borrowed.assert_called_once_with(selected)
    provider.assert_called_once_with()


def test_lightweight_club_snapshot_does_not_expand_roster_or_staff():
    reader = SimpleNamespace(
        process=SimpleNamespace(pid=26),
        layout=SimpleNamespace(key="fm26"),
        module_base=0x100000,
        team=Mock(return_value={
            "id": 42, "name": "Full", "short_name": "Short",
            "team_type": "club", "reputation": 8000,
        }),
    )
    information = {"facilities": {"training": 17}, "finances": {"debts": []}}
    with (
        patch.object(world_clubs, "borrow_game_reader", return_value=nullcontext(reader)),
        patch.object(club_reader, "_club_information", return_value=information) as read_info,
    ):
        result = world_clubs.read_native_world_club_snapshot({
            "id": 42, "name": "Cached", "address": "0x500000",
        })

    assert result["club"]["reputation"] == 8000
    assert result["club_information"] is information
    read_info.assert_called_once_with(reader, 0x500000)


def test_native_writes_patch_only_the_matching_club_profile_cache():
    keys = [(90001, 1, 42), (90001, 2, 43)]
    first = {
        "team": {"id": 42, "name": "Old"},
        "club_information": {
            "stadium": {"name": "Old Ground", "capacity": 1000},
            "facilities": {"training": 10},
            "finances": {"debts": [{"original_debt": 500}]},
        },
    }
    second = {"team": {"id": 43, "name": "Other"}}
    try:
        with club_reader._CACHE_LOCK:
            club_reader._CACHE[keys[0]] = (1.0, first)
            club_reader._CACHE[keys[1]] = (1.0, second)
        patched = club_reader.patch_club_profile_cache(
            42, short_name="New", stadium={"capacity": 2000},
            facilities={"training": 11}, debts_repaid=True,
        )

        assert patched == 1
        assert first["team"]["name"] == "New"
        assert first["club_information"]["stadium"] == {
            "name": "Old Ground", "capacity": 2000,
        }
        assert first["club_information"]["facilities"]["training"] == 11
        assert first["club_information"]["finances"]["debts"] == []
        assert second["team"]["name"] == "Other"
    finally:
        with club_reader._CACHE_LOCK:
            for key in keys:
                club_reader._CACHE.pop(key, None)


def test_connection_context_reads_manager_without_fixture_or_odds_scan():
    layout = SimpleNamespace(
        key="fm26", game_version="26.3.2", display_name="FM26",
        manager_person_offset=0x450, staff_coaching_license_offset=None,
    )
    team = {
        "id": 42, "name": "Manchester United", "short_name": "Man Utd",
        "team_type": "club", "address": "0x6000",
        "club_address": "0x7000", "manager_address": "0x5000",
    }
    reader = SimpleNamespace(layout=layout, team=lambda address: team if address == 0x6000 else None)
    session = {
        "manager_id": 77, "manager_name": "Manager", "manager_address": "0x5000",
        "team_id": 42, "team_type": "club", "address": "0x6000",
        "club_address": "0x7000",
        "managed_team_refs": [{
            "manager_id": 77, "manager_address": "0x5000",
            "team_id": 42, "team_type": "club", "address": "0x6000",
            "club_address": "0x7000",
        }],
    }
    opened = nullcontext(("fm.exe", layout, SimpleNamespace(), reader))
    with (
        patch.object(preview_cup_odds, "open_supported_reader", return_value=opened),
        patch.object(
            preview_cup_odds, "read_game_clock_from_reader",
            return_value={"date": "2026-07-23", "time": "12:00", "minutes": 720},
        ),
        patch.object(
            preview_cup_odds, "read_savegame_identity",
            return_value={"savegame_id": "123", "session_nonce": 9},
        ),
        patch.object(
            preview_cup_odds, "discover_human_managers", return_value=[session],
        ) as discover,
        patch.object(
            preview_cup_odds, "resolve_save_identity", return_value="career-test",
        ) as resolve,
        patch.object(preview_cup_odds, "stored_selected_manager_id", return_value=0),
        patch.object(preview_cup_odds, "remember_save_name"),
        patch.object(preview_cup_odds, "confirmed_save_name", return_value="Test Save"),
    ):
        context = preview_cup_odds.read_connection_context()

    discover.assert_called_once_with(
        reader, [], preferred_manager_id=None, known_manager_sessions=None,
        allow_expensive_direct_scan=False,
    )
    assert context["connection_context_verified"] is True
    assert context["save_instance_id"] == "career-test"
    assert resolve.call_args.args[1] == 77
    assert context["manager"]["id"] == 77
    assert context["managed_team"]["id"] == 42
    assert context["managed_team"]["name"] == "Man Utd"


def test_connection_context_recovers_when_indexed_manager_lookup_is_empty():
    layout = SimpleNamespace(
        key="fm26", game_version="26.3.2", display_name="FM26",
        manager_person_offset=0x450, staff_coaching_license_offset=None,
    )
    team = {
        "id": 42, "name": "Manchester United", "short_name": "Man Utd",
        "team_type": "club", "address": "0x6000",
        "club_address": "0x7000", "manager_address": "0x5000",
    }
    reader = SimpleNamespace(
        layout=layout,
        team=lambda address: team if address == 0x6000 else None,
    )
    session = {
        "manager_id": 77, "manager_name": "Manager", "manager_address": "0x5000",
        "team_id": 42, "team_type": "club", "address": "0x6000",
        "club_address": "0x7000",
        "managed_team_refs": [{
            "manager_id": 77, "manager_address": "0x5000",
            "team_id": 42, "team_type": "club", "address": "0x6000",
            "club_address": "0x7000",
        }],
    }
    opened = nullcontext(("fm.exe", layout, SimpleNamespace(), reader))
    with (
        patch.object(preview_cup_odds, "open_supported_reader", return_value=opened),
        patch.object(
            preview_cup_odds, "read_game_clock_from_reader",
            return_value={"date": "2026-07-23", "time": "12:00", "minutes": 720},
        ),
        patch.object(
            preview_cup_odds, "read_savegame_identity",
            return_value={"savegame_id": "123", "session_nonce": 9},
        ),
        patch.object(
            preview_cup_odds, "discover_human_managers",
            side_effect=[[], [session]],
        ) as discover,
        patch.object(preview_cup_odds, "resolve_save_identity", return_value="career-test"),
        patch.object(preview_cup_odds, "stored_selected_manager_id", return_value=0),
        patch.object(preview_cup_odds, "remember_save_name"),
        patch.object(preview_cup_odds, "confirmed_save_name", return_value="Test Save"),
    ):
        context = preview_cup_odds.read_connection_context()

    assert [call.kwargs["allow_expensive_direct_scan"] for call in discover.call_args_list] == [
        False, True,
    ]
    assert context["connection_context_verified"] is True
    assert context["manager"]["id"] == 77
    assert context["managed_team"]["id"] == 42


def test_xgp_connection_context_recovers_manager_only_partial_lookup():
    partial = {
        "manager_id": 77,
        "manager_name": "Manager",
        "manager_address": "0x5000",
        "managed_team_refs": [],
    }
    team = {
        "id": 42, "name": "Manchester United", "short_name": "Man Utd",
        "team_type": "club", "address": "0x6000",
        "club_address": "0x7000", "manager_address": "0x5000",
    }
    recovered = {
        **partial,
        "team_id": 42,
        "team_type": "club",
        "address": "0x6000",
        "club_address": "0x7000",
        "managed_team_refs": [{
            "manager_id": 77, "manager_address": "0x5000",
            "team_id": 42, "team_type": "club", "address": "0x6000",
            "club_address": "0x7000",
        }],
    }

    for layout in (FM24_XGP_LAYOUT, FM26_XGP_TEMPLATE):
        reader = SimpleNamespace(
            layout=layout,
            team=lambda address: team if address == 0x6000 else None,
            u8=lambda _address: None,
        )
        opened = nullcontext(("fm.exe", layout, SimpleNamespace(), reader))
        with (
            patch.object(
                preview_cup_odds, "open_supported_reader", return_value=opened,
            ),
            patch.object(
                preview_cup_odds, "read_game_clock_from_reader",
                return_value={
                    "date": "2026-07-23", "time": "12:00", "minutes": 720,
                },
            ),
            patch.object(
                preview_cup_odds, "read_savegame_identity",
                return_value={"savegame_id": "123", "session_nonce": 9},
            ),
            patch.object(
                preview_cup_odds, "read_cached_save_name",
                return_value=(None, []),
            ),
            patch.object(
                preview_cup_odds, "discover_human_managers",
                side_effect=[[partial], [recovered]],
            ) as discover,
            patch.object(
                preview_cup_odds, "resolve_save_identity",
                return_value=f"career-{layout.key}-xgp",
            ),
            patch.object(
                preview_cup_odds, "stored_selected_manager_id", return_value=0,
            ),
            patch.object(preview_cup_odds, "remember_save_name"),
            patch.object(
                preview_cup_odds, "confirmed_save_name", return_value=None,
            ),
        ):
            context = preview_cup_odds.read_connection_context()

        assert [
            call.kwargs["allow_expensive_direct_scan"]
            for call in discover.call_args_list
        ] == [False, True]
        assert context["connection_context_verified"] is True
        assert context["manager"]["id"] == 77
        assert context["managed_team"]["id"] == 42


def test_connection_context_does_not_verify_an_empty_manager_lookup():
    layout = SimpleNamespace(
        key="fm26", game_version="26.3.2", display_name="FM26",
        manager_person_offset=0x450, staff_coaching_license_offset=None,
    )
    reader = SimpleNamespace(layout=layout)
    opened = nullcontext(("fm.exe", layout, SimpleNamespace(), reader))
    with (
        patch.object(preview_cup_odds, "open_supported_reader", return_value=opened),
        patch.object(
            preview_cup_odds, "read_game_clock_from_reader",
            return_value={"date": "2026-07-23", "time": "12:00", "minutes": 720},
        ),
        patch.object(
            preview_cup_odds, "read_savegame_identity",
            return_value={"savegame_id": "123", "session_nonce": 9},
        ),
        patch.object(preview_cup_odds, "discover_human_managers", return_value=[]),
        patch.object(preview_cup_odds, "resolve_save_identity", return_value="career-test"),
        patch.object(preview_cup_odds, "stored_selected_manager_id", return_value=0),
        patch.object(preview_cup_odds, "remember_save_name"),
        patch.object(preview_cup_odds, "confirmed_save_name", return_value="Test Save"),
    ):
        context = preview_cup_odds.read_connection_context()

    assert context["connection_context_verified"] is False
    assert context["manager"] is None


def test_fm24_verified_preferred_identity_does_not_defer_without_save_name():
    layout = SimpleNamespace(
        key="fm24", game_version="24.4.2", display_name="FM24",
        manager_person_offset=0x450, staff_coaching_license_offset=None,
    )
    team = {
        "id": 42, "name": "Manchester United", "short_name": "Man Utd",
        "team_type": "club", "address": "0x6000",
        "club_address": "0x7000", "manager_address": "0x5000",
    }
    reader = SimpleNamespace(
        layout=layout,
        team=lambda address: team if address == 0x6000 else None,
    )
    session = {
        "manager_id": 77, "manager_name": "Manager", "manager_address": "0x5000",
        "team_id": 42, "team_type": "club", "address": "0x6000",
        "club_address": "0x7000",
        "managed_team_refs": [{
            "manager_id": 77, "manager_address": "0x5000",
            "team_id": 42, "team_type": "club", "address": "0x6000",
            "club_address": "0x7000",
        }],
    }
    opened = nullcontext(("fm.exe", layout, SimpleNamespace(), reader))
    with (
        patch.object(preview_cup_odds, "open_supported_reader", return_value=opened),
        patch.object(
            preview_cup_odds, "read_game_clock_from_reader",
            return_value={"date": "2026-07-23", "time": "12:00", "minutes": 720},
        ),
        patch.object(preview_cup_odds, "read_savegame_identity", return_value=None),
        patch.object(
            preview_cup_odds, "read_cached_save_name", return_value=(None, []),
        ),
        patch.object(
            preview_cup_odds, "discover_human_managers", return_value=[session],
        ),
        patch.object(preview_cup_odds, "resolve_save_identity", return_value="career-test"),
        patch.object(preview_cup_odds, "stored_selected_manager_id", return_value=0),
        patch.object(preview_cup_odds, "remember_save_name"),
        patch.object(preview_cup_odds, "confirmed_save_name", return_value="Test Save"),
    ):
        context = preview_cup_odds.read_connection_context(
            preferred_save_id="career-test",
        )

    assert context["save_instance_id"] == "career-test"
    assert context["save_identity_discovery_deferred"] is False


def test_fm24_renamed_save_keeps_preferred_career_when_manager_survives():
    layout = SimpleNamespace(
        key="fm24", game_version="24.4.2", display_name="FM24",
        manager_person_offset=0x450, staff_coaching_license_offset=None,
    )
    team = {
        "id": 42, "name": "New Club", "short_name": "New Club",
        "team_type": "club", "address": "0x6000",
        "club_address": "0x7000", "manager_address": "0x5000",
    }
    session = {
        "manager_id": 77, "manager_name": "Manager", "manager_address": "0x5000",
        "managed_team_refs": [{
            "manager_id": 77, "manager_address": "0x5000",
            "team_id": 42, "team_type": "club", "address": "0x6000",
            "club_address": "0x7000",
        }],
    }
    reader = SimpleNamespace(
        layout=layout,
        team=lambda address: team if address == 0x6000 else None,
    )
    opened = nullcontext(("fm.exe", layout, SimpleNamespace(), reader))
    with (
        patch.object(preview_cup_odds, "open_supported_reader", return_value=opened),
        patch.object(
            preview_cup_odds, "read_game_clock_from_reader",
            return_value={"date": "2026-07-23", "time": "12:00", "minutes": 720},
        ),
        patch.object(preview_cup_odds, "read_savegame_identity", return_value=None),
        patch.object(preview_cup_odds, "discover_human_managers", return_value=[session]),
        patch.object(
            preview_cup_odds, "read_cached_save_name",
            return_value=("New Club Save", [object()]),
        ),
        patch.object(preview_cup_odds, "confirmed_save_name", return_value="Old Club Save"),
        patch.object(preview_cup_odds, "resolve_save_identity", return_value="career-current") as resolve,
        patch.object(preview_cup_odds, "stored_selected_manager_id", return_value=0),
        patch.object(preview_cup_odds, "remember_save_name"),
    ):
        context = preview_cup_odds.read_connection_context(
            preferred_save_id="career-current",
            preferred_manager_id=77,
            known_manager_sessions=[session],
        )

    assert context["save_instance_id"] == "career-current"
    assert context["manager"]["id"] == 77
    assert resolve.call_args.kwargs["preferred_id"] == "career-current"
    assert resolve.call_args.kwargs["stable_id"] is None
