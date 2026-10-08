from __future__ import annotations

import multiprocessing
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from tools.storage_io import atomic_write_text, storage_lock


def _increment_storage_value(
    raw_path: str, iterations: int, ready: object, start: object,
) -> None:
    path = Path(raw_path)
    ready.put(True)
    start.wait()
    for _index in range(iterations):
        with storage_lock(path):
            value = int(path.read_text(encoding="utf-8"))
            time.sleep(0.002)
            atomic_write_text(path, str(value + 1))


def test_storage_lock_serializes_two_process_read_modify_write_cycles():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "counter.txt"
        path.write_text("0", encoding="utf-8")
        context = multiprocessing.get_context("spawn")
        ready = context.Queue()
        start = context.Event()
        processes = [
            context.Process(
                target=_increment_storage_value,
                args=(str(path), 20, ready, start),
            )
            for _index in range(2)
        ]
        for process in processes:
            process.start()
        for _process in processes:
            ready.get(timeout=10)
        start.set()
        for process in processes:
            process.join(timeout=20)

        assert [process.exitcode for process in processes] == [0, 0]
        assert path.read_text(encoding="utf-8") == "40"


def test_atomic_write_does_not_leave_fixed_temporary_file():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "settings.json"

        atomic_write_text(path, "ready")

        assert path.read_text(encoding="utf-8") == "ready"
        assert list(path.parent.glob("*.tmp")) == []


def test_atomic_write_retries_windows_file_contention():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "settings.json"
        path.write_text("old", encoding="utf-8")
        from os import replace as real_replace
        attempts = 0

        def flaky_replace(source: str | Path, target: str | Path) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise PermissionError(5, "access denied")
            real_replace(source, target)

        with (
            patch("tools.storage_io.os.replace", side_effect=flaky_replace),
            patch("tools.storage_io.time.sleep"),
        ):
            atomic_write_text(path, "ready")

        assert attempts == 2
        assert path.read_text(encoding="utf-8") == "ready"
        assert list(path.parent.glob("*.tmp")) == []
