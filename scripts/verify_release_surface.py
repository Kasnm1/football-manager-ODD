from __future__ import annotations

import argparse
import os
import tempfile
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 compatibility.
    import tomli as tomllib
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "build" / "protection.toml"


def _normalized(value: str) -> str:
    return PurePosixPath(value.replace("\\", "/")).as_posix().lstrip("/")


def _protected_binary_matches(root: Path, module_name: str) -> list[Path]:
    module_path = Path(*module_name.split("."))
    return list((root / module_path.parent).glob(module_path.name + ".*.pyd"))


def _audit_build_tree(
    root: Path, label: str, forbidden_suffixes: set[str], forbidden_markers: list[str],
) -> list[str]:
    errors: list[str] = []
    if not root.is_dir():
        errors.append(f"{label} directory is missing: {root}")
        return errors
    binary_suffixes = {".dll", ".exe", ".pyd"}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = _normalized(path.relative_to(root).as_posix())
        if path.suffix.lower() in forbidden_suffixes:
            errors.append(f"forbidden {label} suffix: {relative}")
        if path.suffix.lower() not in binary_suffixes:
            continue
        try:
            payload = path.read_bytes()
        except OSError as error:
            errors.append(f"cannot read {label} binary {relative}: {error}")
            continue
        for marker in forbidden_markers:
            encoded = marker.encode("utf-8")
            wide = marker.encode("utf-16le")
            if encoded in payload or wide in payload:
                errors.append(f"forbidden {label} binary marker {marker!r}: {relative}")
    return errors


def _inspect_executable(executable: Path) -> tuple[set[str], set[str]]:
    try:
        from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader
    except ImportError as error:
        raise RuntimeError("PyInstaller is required to inspect the release executable") from error

    archive = CArchiveReader(str(executable))
    package_names = {_normalized(name) for name in archive.toc}
    pyz_name = next((name for name in archive.toc if name.endswith("PYZ.pyz")), None)
    if pyz_name is None:
        raise RuntimeError("The executable does not contain PYZ.pyz")
    handle, temporary_name = tempfile.mkstemp(suffix=".pyz")
    try:
        os.close(handle)
        Path(temporary_name).write_bytes(archive.extract(pyz_name))
        pure_names = set(ZlibArchiveReader(temporary_name).toc)
    finally:
        Path(temporary_name).unlink(missing_ok=True)
    return package_names, pure_names


def validate(
    *, web_root: Path, protected_root: Path | None = None,
    executable: Path | None = None, host_root: Path | None = None,
    native_roots: tuple[Path, ...] = (),
) -> list[str]:
    config = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    protected_modules = [str(name) for name in config.get("protected_modules", [])]
    rust_native_dll = str(config.get("rust_native_dll", ""))
    cpp_hook_dll = str(config.get("cpp_hook_dll", ""))
    forbidden_paths = {str(value).lower() for value in config.get("release_forbidden_paths", [])}
    forbidden_suffixes = {str(value).lower() for value in config.get("release_forbidden_suffixes", [])}
    forbidden_markers = [str(value) for value in config.get("release_forbidden_markers", [])]
    forbidden_binary_markers = [
        str(value) for value in config.get("release_forbidden_binary_markers", [])
    ]
    errors: list[str] = []

    if protected_modules != sorted(set(protected_modules)):
        errors.append("protected_modules must be unique and sorted")

    for relative in ("index.html", "app.css", "app.js"):
        if not (web_root / relative).is_file():
            errors.append(f"release web asset is missing: {relative}")
    for path in web_root.rglob("*") if web_root.is_dir() else ():
        if not path.is_file():
            continue
        relative = _normalized(path.relative_to(web_root).as_posix())
        if path.suffix.lower() in forbidden_suffixes:
            errors.append(f"forbidden release web suffix: {relative}")
        if path.suffix.lower() in {".html", ".css", ".js", ".json", ".txt"}:
            content = path.read_text(encoding="utf-8", errors="replace")
            for marker in forbidden_markers:
                if marker in content:
                    errors.append(f"forbidden release marker {marker!r}: {relative}")

    app_js = web_root / "app.js"
    source_app_js = ROOT / "web" / "app.js"
    if (
        app_js.is_file() and source_app_js.is_file()
        and app_js.stat().st_size >= source_app_js.stat().st_size * 0.85
    ):
        errors.append("release app.js is not smaller than its source")

    spec_path = ROOT / str(config.get("spec", ""))
    if not spec_path.is_file():
        errors.append(f"protected spec is missing: {spec_path}")
    else:
        spec = spec_path.read_text(encoding="utf-8")
        if 'protection["release_web_output"]' not in spec or '(str(root / "web"), "web")' in spec:
            errors.append("protected spec does not package build/web_release")
        if 'protection["protected_modules"]' not in spec:
            errors.append("protected spec does not consume the configured protected module list")
        if 'protection["rust_native_dll"]' not in spec:
            errors.append("protected spec does not package the configured Rust native core")
        if 'protection["cpp_hook_dll"]' not in spec:
            errors.append("protected spec does not package the configured C++ Hook core")

    if protected_root is not None:
        for module_name in ["tools._release_integrity", *protected_modules]:
            if len(_protected_binary_matches(protected_root, module_name)) != 1:
                errors.append(f"protected binary missing or ambiguous: {module_name}")
        for path in protected_root.rglob("*"):
            if path.is_file() and path.suffix.lower() in {".py", ".pyc", ".pyo"}:
                errors.append(f"Python source leaked into protected runtime: {path.relative_to(protected_root)}")
        errors.extend(_audit_build_tree(
            protected_root, "protected runtime", forbidden_suffixes,
            forbidden_binary_markers,
        ))

    if host_root is not None:
        errors.extend(_audit_build_tree(host_root, "desktop host", forbidden_suffixes, forbidden_binary_markers))
    for native_root in native_roots:
        errors.extend(_audit_build_tree(native_root, "native output", forbidden_suffixes, forbidden_binary_markers))

    if executable is not None:
        try:
            package_names, pure_names = _inspect_executable(executable)
        except (OSError, RuntimeError, ValueError) as error:
            errors.append(f"cannot inspect release executable: {error}")
        else:
            for module_name in ["tools._release_integrity", *protected_modules]:
                if module_name in pure_names:
                    errors.append(f"protected module leaked into PYZ: {module_name}")
                module_path = module_name.replace(".", "/")
                if not any(
                    name.startswith(module_path + ".") and name.endswith(".pyd")
                    for name in package_names
                ):
                    errors.append(f"protected native module is missing from executable: {module_name}")
            if rust_native_dll and f"tools/{rust_native_dll}" not in package_names:
                errors.append("Rust native core is missing from executable")
            if cpp_hook_dll and f"tools/{cpp_hook_dll}" not in package_names:
                errors.append("C++ Hook core is missing from executable")
            for name in package_names:
                parts = {part.lower() for part in PurePosixPath(name).parts}
                if parts & forbidden_paths:
                    errors.append(f"forbidden packaged path: {name}")
                if PurePosixPath(name).suffix.lower() in forbidden_suffixes:
                    errors.append(f"forbidden packaged suffix: {name}")
            for required in ("web/app.js", "web/app.css", "web/index.html"):
                if required not in package_names:
                    errors.append(f"packaged web asset is missing: {required}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit FMODD protected release contents.")
    parser.add_argument("--web-root", type=Path, required=True)
    parser.add_argument("--protected-root", type=Path)
    parser.add_argument("--executable", type=Path)
    parser.add_argument("--host-root", type=Path)
    parser.add_argument("--native-root", type=Path, action="append", default=[])
    args = parser.parse_args()
    errors = validate(
        web_root=args.web_root.resolve(),
        protected_root=args.protected_root.resolve() if args.protected_root else None,
        executable=args.executable.resolve() if args.executable else None,
        host_root=args.host_root.resolve() if args.host_root else None,
        native_roots=tuple(path.resolve() for path in args.native_root),
    )
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print("Protected release surface: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
