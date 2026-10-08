# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

root = Path(SPECPATH).resolve().parent
protected_root = root / "build" / "protected_runtime_v191d"
protected_modules = [
    "tools._release_integrity",
    "tools.game_layout",
    "tools.goalkeeper_bribe",
    "tools.native_layout_core",
    "tools.odds_math_core",
    "tools.redbull_hook",
]
protected_binaries = []
for module_name in protected_modules:
    module_path = Path(*module_name.split("."))
    candidates = list((protected_root / module_path.parent).glob(module_path.name + ".*.pyd"))
    if len(candidates) != 1:
        raise SystemExit(f"Protected module missing or ambiguous: {module_name} ({candidates})")
    protected_binaries.append((str(candidates[0]), str(module_path.parent)))

a = Analysis(
    [str(root / "fmodd_desktop.py")],
    pathex=[str(protected_root), str(root)],
    binaries=protected_binaries,
    datas=[
        (str(root / "web"), "web"),
        (str(root / "assets" / "nation_mappings"), r"assets\nation_mappings"),
        (str(root / "AI_USAGE_NOTICE.txt"), "."),
        (str(root / "build" / "desktop_host_v191d"), "desktop_host"),
        (str(root / "build" / "FMODD-V1.1.ico"), "."),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=2,
)
a.pure = [entry for entry in a.pure if entry[0] not in protected_modules]
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [('O', None, 'OPTION'), ('O', None, 'OPTION')],
    name="FMODD-V1.9.1d",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch="x86_64",
    codesign_identity=None,
    entitlements_file=None,
    icon=str(root / "build" / "FMODD-V1.1.ico"),
    version=str(root / "FMODD-V1.9.1d.version.txt"),
)
