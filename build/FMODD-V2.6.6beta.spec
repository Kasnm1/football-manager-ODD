# -*- mode: python ; coding: utf-8 -*-
# name="FMODD-V2.6.6beta"
from pathlib import Path
root = Path(SPECPATH).resolve().parent
source = (root / "build" / "FMODD-V2.4.0.spec").read_text(encoding="utf-8")
exec(source.replace("V2.4.0", "V2.6.6beta").replace("v240", "v266beta"), globals())
