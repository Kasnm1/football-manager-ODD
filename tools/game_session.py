"""Long-lived read-only sessions for the selected Football Manager process.

The process handle and resolved module identity are stable for the lifetime of a
running FM process.  Business readers stay short-lived so mutable fields are
never accidentally served from a global object cache, while repeated API calls
avoid reopening the process and enumerating its modules.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import atexit
import threading
from time import monotonic
from typing import Any, Iterator

from fm_collector.win32 import ProcessHandle, open_process, read_process_memory


class ForegroundPriorityLock:
    """A ``threading.Lock``-compatible gate that lets queued user work go first.

    The current holder is never interrupted. Once released, ordinary refresh
    work waits while at least one foreground transaction is queued, reducing
    user-visible tail latency without changing transaction serialization.
    """

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._locked = False
        self._foreground_waiters = 0
        self._request_context = threading.local()

    @property
    def foreground_waiters(self) -> int:
        with self._condition:
            return self._foreground_waiters

    def _acquire(
        self, *, foreground: bool, blocking: bool = True, timeout: float = -1,
    ) -> bool:
        if not blocking and timeout != -1:
            raise ValueError("can't specify a timeout for a non-blocking call")
        deadline = (
            monotonic() + float(timeout)
            if blocking and float(timeout) >= 0 else None
        )
        with self._condition:
            if foreground:
                self._foreground_waiters += 1
            try:
                while self._locked or (
                    not foreground and self._foreground_waiters > 0
                ):
                    if not blocking:
                        return False
                    if deadline is None:
                        self._condition.wait()
                        continue
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        return False
                    self._condition.wait(remaining)
                self._locked = True
                return True
            finally:
                if foreground:
                    self._foreground_waiters -= 1
                    if not self._locked:
                        self._condition.notify_all()

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        return self._acquire(
            foreground=bool(getattr(self._request_context, "foreground_depth", 0)),
            blocking=blocking, timeout=timeout,
        )

    @contextmanager
    def foreground_request(self) -> Iterator[None]:
        """Mark ordinary acquisitions in this request thread as foreground."""
        depth = int(getattr(self._request_context, "foreground_depth", 0))
        self._request_context.foreground_depth = depth + 1
        try:
            yield
        finally:
            if depth:
                self._request_context.foreground_depth = depth
            else:
                try:
                    del self._request_context.foreground_depth
                except AttributeError:
                    pass

    def acquire_foreground(
        self, blocking: bool = True, timeout: float = -1,
    ) -> bool:
        return self._acquire(
            foreground=True, blocking=blocking, timeout=timeout,
        )

    def release(self) -> None:
        with self._condition:
            if not self._locked:
                raise RuntimeError("release unlocked lock")
            self._locked = False
            self._condition.notify_all()

    def locked(self) -> bool:
        with self._condition:
            return self._locked

    def __enter__(self) -> ForegroundPriorityLock:
        self.acquire()
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.release()


def _layout_identity(layout: Any) -> tuple[Any, ...]:
    return (
        str(getattr(layout, "key", "")),
        str(getattr(layout, "distribution", "")),
        str(getattr(layout, "executable_sha256", "")),
        str(getattr(layout, "module_name", "")),
    )


@dataclass
class GameReadSession:
    pid: int
    process_path: str
    layout: Any
    process: ProcessHandle
    module: Any
    generation: int
    created_at: float = field(default_factory=monotonic)
    last_used_at: float = field(default_factory=monotonic)
    leases: int = 0
    retired: bool = False
    database_index: Any | None = None
    database_index_error: str = ""
    database_index_attempted_at: float = 0.0
    database_index_lock: threading.RLock = field(default_factory=threading.RLock)
    database_index_warming: bool = False
    database_index_cancel: threading.Event = field(default_factory=threading.Event)
    database_index_thread: threading.Thread | None = None
    close_pending: bool = False
    closed: bool = False

    @property
    def key(self) -> tuple[Any, ...]:
        return (self.pid, *_layout_identity(self.layout))

    def alive(self) -> bool:
        if not int(getattr(self.process, "handle", 0) or 0):
            return False
        # Every supported layout resolves to a loaded PE module.  Reading its
        # DOS signature is a cheap liveness/module-identity check and also
        # catches a PID whose former process has exited.
        return read_process_memory(
            self.process, int(self.module.base_address), 2,
        ) == b"MZ"

    def new_reader(self):
        # Import lazily so initial_data_audit can remain the owner of Reader
        # without introducing an import cycle.
        from tools.initial_data_audit import Reader

        self.last_used_at = monotonic()
        reader = Reader(self.process, int(self.module.base_address), self.layout)
        reader.process_path = self.process_path
        reader.module = self.module
        reader.session_generation = self.generation
        reader.database_index_provider = self.get_database_index
        reader.database_index_error_provider = self.get_database_index_error
        return reader

    def get_database_index_error(self) -> str:
        with self.database_index_lock:
            return str(self.database_index_error or "")

    def get_database_index(self, *, force_retry: bool = False):
        """Lazily build the non-odds native object directory for this session."""
        with self.database_index_lock:
            if self.closed or self.close_pending:
                return None
            if self.database_index is not None:
                return self.database_index
            now = monotonic()
            if (
                not force_retry and self.database_index_error
                and now - self.database_index_attempted_at < 5.0
            ):
                return None
            self.database_index_attempted_at = now
            try:
                from tools.database_index import DatabaseIndex
                from tools.initial_data_audit import Reader

                reader = Reader(self.process, int(self.module.base_address), self.layout)
                reader.process_path = self.process_path
                reader.module = self.module
                reader.session_generation = self.generation
                self.database_index = DatabaseIndex(reader)
                self.database_index_error = ""
                self.database_index_warming = True
                self.database_index_cancel.clear()
                index = self.database_index

                def warm() -> None:
                    try:
                        index.warm(self.database_index_cancel)
                    finally:
                        with self.database_index_lock:
                            self.database_index_warming = False
                            self.database_index_thread = None
                            if self.close_pending:
                                self._close_process_locked()

                self.database_index_thread = threading.Thread(
                    target=warm,
                    name=f"FMODDDatabaseIndex-{self.pid}",
                    daemon=True,
                )
                self.database_index_thread.start()
            except Exception as error:
                self.database_index = None
                self.database_index_error = str(error)
            return self.database_index

    def _close_process_locked(self) -> None:
        if self.closed:
            return
        self.database_index = None
        self.process.close()
        self.close_pending = False
        self.closed = True

    def close(self) -> None:
        with self.database_index_lock:
            if self.closed:
                return
            self.database_index_cancel.set()
            thread = self.database_index_thread
            if (
                thread is not None and thread.is_alive()
                and thread is not threading.current_thread()
            ):
                self.close_pending = True
                return
            self._close_process_locked()


@dataclass
class GameOperationSession:
    """One request's Reader paired with the least-privileged process handle."""

    reader: Any
    process: ProcessHandle
    module: Any
    generation: int
    writable: bool = False

    @property
    def pid(self) -> int:
        return int(self.reader.process.pid)

    @property
    def layout(self) -> Any:
        return self.reader.layout


class GameSessionRegistry:
    """Keep one reusable read handle for each running FM version/process."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sessions: dict[tuple[Any, ...], GameReadSession] = {}
        self._retired: list[GameReadSession] = []
        self._generation = 0
        self._active_key: tuple[Any, ...] | None = None

    def _retire(self, session: GameReadSession) -> None:
        session.retired = True
        if session.leases:
            self._retired.append(session)
        else:
            session.close()

    def acquire(
        self, selected: tuple[int, str, Any] | None = None,
    ) -> GameReadSession:
        if selected is None:
            with self._lock:
                active = self._sessions.get(self._active_key)
                if active is not None and active.alive():
                    active.leases += 1
                    active.last_used_at = monotonic()
                    return active
                if active is not None:
                    self._sessions.pop(active.key, None)
                    self._retire(active)
                    self._active_key = None
            from tools.initial_data_audit import (
                invalidate_process_layout_cache, select_process_layout,
            )

            invalidate_process_layout_cache()
            selected = select_process_layout()
        pid, process_path, layout = selected
        key = (int(pid), *_layout_identity(layout))
        with self._lock:
            # One Windows PID can only represent one live FM layout identity.
            # If a module/build identity changes in place, retire the former
            # session instead of leaving a stale handle and database index in
            # the registry beside the new selection.
            for existing_key, existing in list(self._sessions.items()):
                if int(existing.pid) != int(pid) or existing_key == key:
                    continue
                self._sessions.pop(existing_key, None)
                self._retire(existing)
                if self._active_key == existing_key:
                    self._active_key = None
            session = self._sessions.get(key)
            if session is not None and not session.alive():
                self._sessions.pop(key, None)
                self._retire(session)
                session = None
            if session is None:
                process = open_process(int(pid))
                try:
                    module = layout.module(process)
                    if not module:
                        raise RuntimeError(f"{layout.module_name} not loaded")
                    if read_process_memory(
                        process, int(module.base_address), 2,
                    ) != b"MZ":
                        raise RuntimeError(f"{layout.module_name} identity check failed")
                except Exception:
                    process.close()
                    raise
                self._generation += 1
                session = GameReadSession(
                    pid=int(pid), process_path=str(process_path), layout=layout,
                    process=process, module=module, generation=self._generation,
                )
                self._sessions[key] = session
            self._active_key = key
            session.leases += 1
            session.last_used_at = monotonic()
            return session

    def release(self, session: GameReadSession) -> None:
        with self._lock:
            session.leases = max(0, session.leases - 1)
            if session.retired and not session.leases:
                session.close()
                try:
                    self._retired.remove(session)
                except ValueError:
                    pass

    def active_selection(self) -> tuple[int, str, Any] | None:
        """Return the still-verified active process without taking a lease.

        Legacy readers and writers still call ``select_process_layout`` before
        opening their request-local handle.  Once the connection owns a live
        read session, its process/layout identity is stronger and cheaper than
        repeating a Toolhelp process scan and executable-layout resolution.
        """
        with self._lock:
            session = self._sessions.get(self._active_key)
            if session is None:
                return None
            try:
                alive = session.alive()
            except OSError:
                alive = False
            if not alive:
                self._sessions.pop(session.key, None)
                self._retire(session)
                self._active_key = None
                return None
            session.last_used_at = monotonic()
            return session.pid, session.process_path, session.layout

    def active_module(self, pid: int, layout: Any) -> Any | None:
        """Reuse the verified module snapshot for the same live generation."""
        key = (int(pid), *_layout_identity(layout))
        with self._lock:
            session = self._sessions.get(self._active_key)
            if session is None or session.key != key:
                return None
            try:
                alive = session.alive()
            except OSError:
                alive = False
            if not alive:
                self._sessions.pop(session.key, None)
                self._retire(session)
                self._active_key = None
                return None
            session.last_used_at = monotonic()
            return session.module

    def invalidate(self, *, pid: int | None = None) -> None:
        with self._lock:
            for key, session in list(self._sessions.items()):
                if pid is not None and int(session.pid) != int(pid):
                    continue
                self._sessions.pop(key, None)
                self._retire(session)
                if self._active_key == key:
                    self._active_key = None

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "pid": session.pid,
                    "game_key": str(getattr(session.layout, "key", "")),
                    "module_base": int(session.module.base_address),
                    "generation": session.generation,
                    "leases": session.leases,
                    "database_index": (
                        session.database_index.snapshot()
                        if session.database_index is not None else None
                    ),
                    "database_index_error": session.database_index_error or None,
                    "database_index_warming": session.database_index_warming,
                }
                for session in self._sessions.values()
            ]


_REGISTRY = GameSessionRegistry()


@contextmanager
def borrow_game_reader(
    selected: tuple[int, str, Any] | None = None,
) -> Iterator[Any]:
    session = _REGISTRY.acquire(selected)
    try:
        yield session.new_reader()
    finally:
        _REGISTRY.release(session)


def warm_database_index(
    selected: tuple[int, str, Any] | None = None,
) -> bool:
    """Trigger the current read session's background native directory warmup.

    The helper intentionally waits only for a short-lived reader lease and the
    creation of the index.  ``GameReadSession.get_database_index`` owns the
    actual scan in its daemon worker, so connecting a save is not blocked by
    the world-player traversal.
    """
    with borrow_game_reader(selected) as reader:
        provider = getattr(reader, "database_index_provider", None)
        if not callable(provider):
            return False
        return provider() is not None


@contextmanager
def borrow_game_operation(
    selected: tuple[int, str, Any] | None = None, *,
    write_memory: bool = False, create_thread: bool = False,
) -> Iterator[GameOperationSession]:
    """Reuse the live read session and open write rights only for this request."""
    borrowed = borrow_game_reader(selected) if selected is not None else borrow_game_reader()
    with borrowed as reader:
        module = getattr(reader, "module", None)
        if module is None:
            module = reader.layout.module(reader.process)
        if not module:
            raise RuntimeError(f"{reader.layout.module_name} not loaded")
        generation = int(getattr(reader, "session_generation", 0) or 0)
        if not write_memory:
            yield GameOperationSession(
                reader=reader, process=reader.process, module=module,
                generation=generation, writable=False,
            )
            return

        # A request-local Reader must never serve a prefetched page after an
        # in-process write. The long-lived session owns only the OS handle and
        # database directory; each borrow receives a fresh Reader instance.
        if hasattr(reader, "_page_cache"):
            reader._page_cache = None
        open_kwargs: dict[str, Any] = {"write_memory": True}
        if create_thread:
            open_kwargs["create_thread"] = True
        with open_process(int(reader.process.pid), **open_kwargs) as writable_process:
            yield GameOperationSession(
                reader=reader, process=writable_process, module=module,
                generation=generation, writable=True,
            )


def invalidate_game_read_sessions(*, pid: int | None = None) -> None:
    _REGISTRY.invalidate(pid=pid)


def active_game_read_selection() -> tuple[int, str, Any] | None:
    return _REGISTRY.active_selection()


def active_game_module(process: Any, layout: Any) -> Any | None:
    pid = int(getattr(process, "pid", 0) or 0)
    if pid <= 0:
        return None
    return _REGISTRY.active_module(pid, layout)


def game_read_session_snapshot() -> list[dict[str, Any]]:
    return _REGISTRY.snapshot()


atexit.register(invalidate_game_read_sessions)
