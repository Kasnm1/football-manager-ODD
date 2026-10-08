# -*- mode: python ; coding: utf-8 -*-
# name="FMODD-V2.4.2beta"
from pathlib import Path
root = Path(SPECPATH).resolve().parent
source = (root / "build" / "FMODD-V2.4.0.spec").read_text(encoding="utf-8")
exec(source.replace("V2.4.0", "V2.4.2beta").replace("v240", "v242beta"), globals())
