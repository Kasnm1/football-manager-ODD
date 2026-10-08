from __future__ import annotations

import hashlib
import os
import re
import shutil
import uuid
from contextlib import ExitStack
from pathlib import Path
from typing import Iterable

from tools.domain_errors import UnavailableError, ValidationError
from tools.storage_io import atomic_write_text, storage_lock


REBUILDABLE_ROOT_NAMES = frozenset({
    "cache", "model", "odds", "results", "runtime", "world",
})
_ATOMIC_TEMP_PATTERN = re.compile(r"^\..+\.\d+\.\d+\.[0-9a-f]{32}\.tmp$")
_MIGRATION_STAGE_PREFIX = ".fmodd-migration-"


def _is_reparse_point(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(callable(is_junction) and is_junction())


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _invalid_target(message: str, *, path: Path | None = None) -> ValidationError:
    details = {"path": str(path)} if path is not None else {}
    return ValidationError(
        message,
        code="storage_migration_invalid_target",
        phase="validate_storage_root",
        details=details,
        message_key="storage.migration.invalid_target",
    )


def _migration_failed(message: str, *, path: Path | None = None) -> UnavailableError:
    details = {"path": str(path)} if path is not None else {}
    return UnavailableError(
        message,
        code="storage_migration_failed",
        phase="migrate_storage_root",
        retryable=True,
        details=details,
        message_key="storage.migration.failed",
    )


def _transient_file(path: Path) -> bool:
    return bool(
        _ATOMIC_TEMP_PATTERN.match(path.name)
        or path.name.startswith(_MIGRATION_STAGE_PREFIX)
    )


def _inventory(source: Path, marker_name: str) -> tuple[list[Path], list[Path]]:
    directories: list[Path] = []
    files: list[Path] = []

    def visit(directory: Path, relative: Path) -> None:
        try:
            children = sorted(directory.iterdir(), key=lambda item: item.name.casefold())
        except OSError as error:
            raise _migration_failed(f"无法读取原数据目录：{directory}", path=directory) from error
        for child in children:
            child_relative = relative / child.name
            if not relative.parts and (
                child.name.casefold() in REBUILDABLE_ROOT_NAMES
                or child.name == marker_name
                or child.name.startswith(_MIGRATION_STAGE_PREFIX)
            ):
                continue
            try:
                if _is_reparse_point(child):
                    raise _invalid_target(
                        f"数据目录包含不支持迁移的链接：{child}", path=child,
                    )
                if child.is_dir():
                    directories.append(child_relative)
                    visit(child, child_relative)
                elif child.is_file():
                    if not _transient_file(child):
                        files.append(child_relative)
                else:
                    raise _invalid_target(
                        f"数据目录包含不支持迁移的文件类型：{child}", path=child,
                    )
            except OSError as error:
                raise _migration_failed(f"无法检查原数据：{child}", path=child) from error

    if source.exists():
        if _is_reparse_point(source) or not source.is_dir():
            raise _invalid_target(f"当前数据目录无效：{source}", path=source)
        visit(source, Path())
    return directories, files


def _digest(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _copy_to_stage(source: Path, staged: Path) -> tuple[str, int]:
    staged.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    with source.open("rb") as input_handle, staged.open("xb") as output_handle:
        while chunk := input_handle.read(1024 * 1024):
            output_handle.write(chunk)
            digest.update(chunk)
            size += len(chunk)
        output_handle.flush()
        os.fsync(output_handle.fileno())
    shutil.copystat(source, staged, follow_symlinks=False)
    staged_digest, staged_size = _digest(staged)
    if staged_digest != digest.hexdigest() or staged_size != size:
        raise _migration_failed(f"迁移副本校验失败：{source}", path=source)
    return digest.hexdigest(), size


def _conflicting_destination(destination: Path, expected_digest: str) -> bool:
    if not destination.exists():
        return False
    if _is_reparse_point(destination) or not destination.is_file():
        return True
    actual_digest, _size = _digest(destination)
    return actual_digest != expected_digest


def _acquire_file_locks(stack: ExitStack, paths: Iterable[Path]) -> None:
    for path in paths:
        stack.enter_context(storage_lock(path))


def migrate_data_root(source: Path, target: Path, *, marker_name: str) -> dict[str, int]:
    """Copy durable FMODD data to *target* and leave *source* untouched.

    Rebuildable top-level data and orphaned atomic-write files are intentionally
    excluded. Existing identical files are accepted so an interrupted migration
    can be retried; different destination data is never overwritten.
    """
    source = Path(source).resolve()
    target = Path(target).resolve()
    if source == target:
        target.mkdir(parents=True, exist_ok=True)
        atomic_write_text(target / marker_name, "FMODD data root\n")
        return {"migrated_files": 0, "migrated_bytes": 0, "preserved_files": 0}
    if _is_within(target, source) or _is_within(source, target):
        raise _invalid_target(
            "新旧数据目录不能互相包含，请选择其他目录", path=target,
        )

    try:
        target.mkdir(parents=True, exist_ok=True)
        if _is_reparse_point(target) or not target.is_dir():
            raise _invalid_target(f"所选数据目录无效：{target}", path=target)
        probe = target / f".fmodd-write-test-{uuid.uuid4().hex}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except ValidationError:
        raise
    except OSError as error:
        raise _migration_failed(f"所选目录不可写：{target}", path=target) from error

    stage = target / f"{_MIGRATION_STAGE_PREFIX}{uuid.uuid4().hex}"
    migrated_files = 0
    migrated_bytes = 0
    preserved_files = 0
    try:
        directories, relative_files = _inventory(source, marker_name)
        source_files = [source / relative for relative in relative_files]
        destination_files = [target / relative for relative in relative_files]
        with ExitStack() as locks:
            _acquire_file_locks(
                locks,
                sorted([*source_files, *destination_files], key=lambda path: str(path).casefold()),
            )
            stable_directories, stable_files = _inventory(source, marker_name)
            if stable_directories != directories or stable_files != relative_files:
                raise _migration_failed("迁移期间原数据目录发生变化，请重试")

            stage.mkdir(parents=True, exist_ok=False)
            for relative in directories:
                destination = target / relative
                if destination.exists() and (
                    _is_reparse_point(destination) or not destination.is_dir()
                ):
                    raise _invalid_target(
                        f"目标目录包含冲突的 FMODD 数据：{destination}",
                        path=destination,
                    )
                (stage / relative).mkdir(parents=True, exist_ok=True)

            staged_files: list[tuple[Path, Path, int]] = []
            for relative, source_file in zip(relative_files, source_files):
                try:
                    source_digest, source_size = _digest(source_file)
                    destination = target / relative
                    if destination.exists():
                        if _conflicting_destination(destination, source_digest):
                            raise _invalid_target(
                                f"目标目录包含不同的 FMODD 数据：{destination}",
                                path=destination,
                            )
                        preserved_files += 1
                        continue
                    staged = stage / relative
                    copied_digest, copied_size = _copy_to_stage(source_file, staged)
                    if copied_digest != source_digest or copied_size != source_size:
                        raise _migration_failed(
                            f"迁移副本与原数据不一致：{source_file}", path=source_file,
                        )
                    staged_files.append((staged, destination, copied_size))
                    migrated_files += 1
                    migrated_bytes += copied_size
                except (ValidationError, UnavailableError):
                    raise
                except OSError as error:
                    raise _migration_failed(
                        f"无法迁移 FMODD 数据：{source_file}", path=source_file,
                    ) from error

            for relative in directories:
                (target / relative).mkdir(parents=True, exist_ok=True)
            for staged, destination, copied_size in staged_files:
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    staged_digest, _size = _digest(staged)
                    if _conflicting_destination(destination, staged_digest):
                        raise _invalid_target(
                            f"目标目录包含不同的 FMODD 数据：{destination}",
                            path=destination,
                        )
                    staged.unlink()
                    preserved_files += 1
                    migrated_files -= 1
                    migrated_bytes -= copied_size
                    continue
                try:
                    os.rename(staged, destination)
                except FileExistsError:
                    staged_digest, _size = _digest(staged)
                    if _conflicting_destination(destination, staged_digest):
                        raise _invalid_target(
                            f"目标目录包含不同的 FMODD 数据：{destination}",
                            path=destination,
                        )
                    staged.unlink()
                    preserved_files += 1
                    migrated_files -= 1
                    migrated_bytes -= copied_size

        atomic_write_text(target / marker_name, "FMODD data root\n")
        return {
            "migrated_files": migrated_files,
            "migrated_bytes": migrated_bytes,
            "preserved_files": preserved_files,
        }
    except (ValidationError, UnavailableError):
        raise
    except OSError as error:
        raise _migration_failed(
            f"无法迁移 FMODD 数据到目标目录：{target}", path=target,
        ) from error
    finally:
        try:
            if stage.is_dir() and not _is_reparse_point(stage):
                shutil.rmtree(stage)
        except OSError:
            pass


__all__ = ["REBUILDABLE_ROOT_NAMES", "migrate_data_root"]
