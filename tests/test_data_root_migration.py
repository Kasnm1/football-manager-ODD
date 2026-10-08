from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools import app_paths
from tools.domain_errors import ValidationError


def _write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def test_configure_data_root_migrates_durable_data_and_keeps_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    config = tmp_path / "config" / "data-root.json"
    durable = {
        Path("saves/account-1.fmodd"): b"account",
        Path("careers/career-1/result_history.json"): b"history",
        Path("settings.json"): b'{"ui_locale":"zh-CN"}',
        Path("global_statistics.json"): b'{"total_seconds":60}',
        Path("screenshots/shot.png"): b"png",
    }
    for relative, payload in durable.items():
        _write(source / relative, payload)
    for relative in (
        Path("cache/account-1/odds.json"),
        Path("model/model.json"),
        Path("odds/snapshot.json"),
        Path("results/verified_results.json"),
        Path("runtime/save-name-addresses.v1.json"),
        Path("world/clubs.json"),
    ):
        _write(source / relative, b"rebuildable")
    temporary = Path(f".settings.json.1.2.{'a' * 32}.tmp")
    _write(source / temporary, b"temporary")

    monkeypatch.setattr(app_paths, "DATA_ROOT", source)
    monkeypatch.setattr(app_paths, "DATA_ROOT_CONFIG_PATH", config)

    result = app_paths.configure_data_root(str(target))

    for relative, payload in durable.items():
        assert (target / relative).read_bytes() == payload
        assert (source / relative).read_bytes() == payload
    for name in ("cache", "model", "odds", "results", "runtime", "world"):
        assert not (target / name).exists()
    assert not (target / temporary).exists()
    assert (target / app_paths.DATA_ROOT_MARKER).is_file()
    assert json.loads(config.read_text(encoding="utf-8")) == {"path": str(target)}
    assert result["restart_required"] is True
    assert result["migrated_files"] == len(durable)


def test_configure_data_root_accepts_identical_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    config = tmp_path / "config" / "data-root.json"
    _write(source / "saves" / "account-1.fmodd", b"account")
    _write(target / "saves" / "account-1.fmodd", b"account")
    monkeypatch.setattr(app_paths, "DATA_ROOT", source)
    monkeypatch.setattr(app_paths, "DATA_ROOT_CONFIG_PATH", config)

    result = app_paths.configure_data_root(str(target))

    assert result["migrated_files"] == 0
    assert result["preserved_files"] == 1
    assert json.loads(config.read_text(encoding="utf-8"))["path"] == str(target)


def test_configure_data_root_rejects_conflicting_destination_without_switching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    config = tmp_path / "config" / "data-root.json"
    _write(source / "saves" / "account-1.fmodd", b"new")
    _write(target / "saves" / "account-1.fmodd", b"old")
    monkeypatch.setattr(app_paths, "DATA_ROOT", source)
    monkeypatch.setattr(app_paths, "DATA_ROOT_CONFIG_PATH", config)

    with pytest.raises(ValidationError) as captured:
        app_paths.configure_data_root(str(target))

    assert captured.value.message_key == "storage.migration.invalid_target"
    assert (target / "saves" / "account-1.fmodd").read_bytes() == b"old"
    assert (source / "saves" / "account-1.fmodd").read_bytes() == b"new"
    assert not config.exists()


def test_configure_data_root_rejects_nested_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    config = tmp_path / "config" / "data-root.json"
    monkeypatch.setattr(app_paths, "DATA_ROOT", source)
    monkeypatch.setattr(app_paths, "DATA_ROOT_CONFIG_PATH", config)

    with pytest.raises(ValidationError) as captured:
        app_paths.configure_data_root(str(source / "nested"))

    assert captured.value.message_key == "storage.migration.invalid_target"
    assert not config.exists()
