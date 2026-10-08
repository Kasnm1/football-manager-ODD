# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

root = Path(SPECPATH).resolve().parent

a = Analysis(
    [str(root / "fmodd_desktop.py")], pathex=[str(root)], binaries=[],
    datas=[
        (str(root / "web"), "web"),
        (str(root / "assets" / "nation_mappings"), r"assets\nation_mappings"),
        (str(root / "AI_USAGE_NOTICE.txt"), "."),
        (str(root / "build" / "desktop_host_v201"), "desktop_host"),
        (str(root / "build" / "FMODD-V1.1.ico"), "."),
    ],
    hiddenimports=[], hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=[],
    noarchive=False, optimize=2,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas,
    [('O', None, 'OPTION'), ('O', None, 'OPTION')],
    name="FMODD-V2.0.1", debug=False, bootloader_ignore_signals=False,
    strip=False, upx=False, upx_exclude=[], runtime_tmpdir=None,
    console=False, disable_windowed_traceback=False, argv_emulation=False,
    target_arch="x86_64", codesign_identity=None, entitlements_file=None,
    icon=str(root / "build" / "FMODD-V1.1.ico"),
    version=str(root / "FMODD-V2.0.1.version.txt"),
)
