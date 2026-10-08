from __future__ import annotations

import argparse
import json
import locale
import os
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "native" / "fmodd_hook_core" / "fmodd_hook_core.cpp"
DEFAULT_OUTPUT = ROOT / "build" / "cpp_native"
VSWHERE = Path(r"C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe")


def visual_studio_path() -> Path:
    if not VSWHERE.is_file():
        raise FileNotFoundError("Visual Studio Build Tools vswhere.exe is missing")
    payload = json.loads(subprocess.check_output([
        str(VSWHERE), "-latest", "-products", "*", "-requires",
        "Microsoft.VisualStudio.Component.VC.Tools.x86.x64", "-format", "json",
    ], text=True, encoding=locale.getpreferredencoding(False), errors="replace"))
    if not payload:
        raise FileNotFoundError("Visual Studio C++ x64 build tools are required")
    return Path(payload[0]["installationPath"])


def build(output: Path = DEFAULT_OUTPUT) -> Path:
    output.mkdir(parents=True, exist_ok=True)
    intermediate = output / "obj"
    intermediate.mkdir(parents=True, exist_ok=True)
    destination = output / "fmodd_hook_core.dll"
    devcmd = visual_studio_path() / "Common7" / "Tools" / "VsDevCmd.bat"
    environment_text = subprocess.check_output(
        f'call "{devcmd}" -arch=x64 -host_arch=x64 >nul && set',
        cwd=ROOT, shell=True, text=True,
        encoding=locale.getpreferredencoding(False), errors="replace",
    )
    environment = os.environ.copy()
    for line in environment_text.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            environment[key] = value
    tools_dir = next(
        (Path(value) for key, value in environment.items() if key.casefold() == "vctoolsinstalldir"),
        None,
    )
    compiler = tools_dir / "bin" / "Hostx64" / "x64" / "cl.exe" if tools_dir else None
    if compiler is None or not compiler.is_file():
        raise FileNotFoundError("Visual Studio x64 cl.exe is missing after VsDevCmd")
    subprocess.run([
        str(compiler), "/nologo", "/std:c++20", "/O2", "/GL", "/EHsc", "/W4", "/WX", "/LD",
        f"/Fo{intermediate}\\", f"/Fe:{destination}", str(SOURCE),
        "/link", "/LTCG", "/INCREMENTAL:NO",
    ], cwd=ROOT, env=environment, check=True)
    if not destination.is_file():
        raise FileNotFoundError(f"C++ Hook DLL was not produced: {destination}")
    for suffix in (".exp", ".lib"):
        generated = destination.with_suffix(suffix)
        if generated.exists():
            shutil.move(str(generated), intermediate / generated.name)
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the FMODD C++ Hook core.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    print(build(args.output.resolve()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
