# -*- mode: python ; coding: utf-8 -*-
# name="FMODD-V2.6.9beta"
# protection["protected_modules"] protection["release_web_output"] protection["rust_native_dll"] protection["cpp_hook_dll"] protection["integrity_files"]
from pathlib import Path
root = Path(SPECPATH).resolve().parent
source = (root / "build" / "FMODD-V2.4.0-protected.spec").read_text(encoding="utf-8")
exec(source.replace("V2.4.0", "V2.6.9beta").replace("v240", "v269beta"), globals())
