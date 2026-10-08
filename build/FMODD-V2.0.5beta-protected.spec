# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import tomllib

root = Path(SPECPATH).resolve().parent
protection = tomllib.loads((root / "build" / "protection.toml").read_text(encoding="utf-8"))
protected_root = root / protection["protected_output"]
release_web_root = root / protection["release_web_output"]
protected_modules = ["tools._release_integrity", *protection["protected_modules"]]
protected_binaries = []
for module_name in protected_modules:
    module_path = Path(*module_name.split("."))
    candidates = list((protected_root / module_path.parent).glob(module_path.name + ".*.pyd"))
    if len(candidates) != 1:
        raise SystemExit(f"Protected module missing or ambiguous: {module_name} ({candidates})")
    protected_binaries.append((str(candidates[0]), str(module_path.parent)))

a = Analysis(
    [str(root / "fmodd_desktop.py")],
    pathex=[str(protected_root), str(root)], binaries=protected_binaries,
    datas=[
        (str(release_web_root), "web"),
        (str(root / "assets" / "nation_mappings"), r"assets\nation_mappings"),
        (str(root / "AI_USAGE_NOTICE.txt"), "."),
        (str(root / "build" / "desktop_host_v205beta"), "desktop_host"),
        (str(root / "build" / "FMODD-V1.1.ico"), "."),
    ],
    hiddenimports=[], hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=[],
    noarchive=False, optimize=2,
)
a.pure = [entry for entry in a.pure if entry[0] not in protected_modules]
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas,
    [('O', None, 'OPTION'), ('O', None, 'OPTION')],
    name="FMODD-V2.0.5beta", debug=False, bootloader_ignore_signals=False,
    strip=False, upx=False, upx_exclude=[], runtime_tmpdir=None,
    console=False, disable_windowed_traceback=False, argv_emulation=False,
    target_arch="x86_64", codesign_identity=None, entitlements_file=None,
    icon=str(root / "build" / "FMODD-V1.1.ico"),
    version=str(root / "FMODD-V2.0.5beta.version.txt"),
)
