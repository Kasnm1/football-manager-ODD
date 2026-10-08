# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

root = Path(r"U:\Work\FM")

a = Analysis(
    [str(root / "fmodd_desktop.py")],
    pathex=[str(root)],
    binaries=[],
    datas=[
        (str(root / "web"), "web"),
        (str(root / "assets" / "nation_mappings"), r"assets\nation_mappings"),
        (str(root / "build" / "desktop_host_v16"), "desktop_host"),
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
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [('O', None, 'OPTION'), ('O', None, 'OPTION')],
    name="FMODD-V1.6",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch="x86_64",
    codesign_identity=None,
    entitlements_file=None,
    icon=str(root / "build" / "FMODD-V1.1.ico"),
    version=str(root / "FMODD.version.txt"),
)
