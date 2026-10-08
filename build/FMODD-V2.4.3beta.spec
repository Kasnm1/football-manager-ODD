# -*- mode: python ; coding: utf-8 -*-
# name="FMODD-V2.4.3beta"
from pathlib import Path
root = Path(SPECPATH).resolve().parent
source = (root / "build" / "FMODD-V2.4.0.spec").read_text(encoding="utf-8")
exec(source.replace("V2.4.0", "V2.4.3beta").replace("v240", "v243beta"), globals())
