# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import tomllib

root = Path(SPECPATH).resolve().parent
protection = tomllib.loads((root / "build" / "protection.toml").read_text(encoding="utf-8"))
protected_root = root / protection["protected_output"]
rust_native_root = root / protection["rust_native_output"]
cpp_native_root = root / protection["cpp_native_output"]
release_web_root = root / protection["release_web_output"]
host_output = root / protection["host_output"]
protected_modules = ["tools._release_integrity", *protection["protected_modules"]]
protected_binaries = []
for module_name in protected_modules:
    module_path = Path(*module_name.split("."))
    candidates = list((protected_root / module_path.parent).glob(module_path.name + ".*.pyd"))
    if len(candidates) != 1:
        raise SystemExit(f"Protected module missing or ambiguous: {module_name} ({candidates})")
    protected_binaries.append((str(candidates[0]), str(module_path.parent)))
rust_native_dll = rust_native_root / protection["rust_native_dll"]
if not rust_native_dll.is_file():
    raise SystemExit(f"Rust native core missing: {rust_native_dll}")
protected_binaries.append((str(rust_native_dll), "tools"))
cpp_hook_dll = cpp_native_root / protection["cpp_hook_dll"]
if not cpp_hook_dll.is_file():
    raise SystemExit(f"C++ Hook core missing: {cpp_hook_dll}")
protected_binaries.append((str(cpp_hook_dll), "tools"))

host_datas = []
for packaged_name in protection["integrity_files"]:
    relative = Path(packaged_name)
    if relative.parts[0] != "desktop_host" or len(relative.parts) < 2:
        raise SystemExit(f"Invalid desktop host package path: {relative}")
    source = host_output.joinpath(*relative.parts[1:])
    if not source.is_file():
        raise SystemExit(f"Desktop host file missing: {source}")
    host_datas.append((str(source), str(relative.parent)))

static_datas = []
for packaged_name, source_name in protection["integrity_file_sources"].items():
    relative = Path(packaged_name)
    source = root / source_name
    if not source.is_file():
        raise SystemExit(f"Static release file missing: {source}")
    static_datas.append((str(source), str(relative.parent)))

a = Analysis(
    [str(root / "fmodd_desktop.py")],
    pathex=[str(protected_root), str(root)], binaries=protected_binaries,
    datas=[
        (str(release_web_root), "web"),
        (str(root / "assets" / "nation_mappings"), r"assets\nation_mappings"),
        *host_datas,
        *static_datas,
    ],
    hiddenimports=[], hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=[],
    noarchive=False, optimize=2,
)
a.pure = [entry for entry in a.pure if entry[0] not in protected_modules]
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas,
    [('O', None, 'OPTION'), ('O', None, 'OPTION')],
    name="FMODD-V2.3.3", debug=False, bootloader_ignore_signals=False,
    strip=False, upx=False, upx_exclude=[], runtime_tmpdir=None,
    console=False, disable_windowed_traceback=False, argv_emulation=False,
    target_arch="x86_64", codesign_identity=None, entitlements_file=None,
    icon=str(root / "build" / "FMODD-V1.1.ico"),
    version=str(root / "FMODD-V2.3.3.version.txt"),
)
