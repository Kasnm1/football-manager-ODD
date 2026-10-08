from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CRATE = ROOT / "native" / "fmodd_native_core"
DEFAULT_OUTPUT = ROOT / "build" / "rust_native"


def cargo_executable() -> str:
    found = shutil.which("cargo")
    if found:
        return found
    candidate = Path.home() / ".cargo" / "bin" / "cargo.exe"
    if candidate.is_file():
        return str(candidate)
    raise FileNotFoundError("Rust cargo is required; install it from https://rustup.rs/")


def build(output: Path = DEFAULT_OUTPUT, *, release: bool = True) -> Path:
    command = [cargo_executable(), "build", "--locked"]
    if release:
        command.append("--release")
    environment = os.environ.copy()
    subprocess.run(command, cwd=CRATE, env=environment, check=True)
    profile = "release" if release else "debug"
    source = CRATE / "target" / profile / "fmodd_native_core.dll"
    if not source.is_file():
        raise FileNotFoundError(f"Rust native DLL was not produced: {source}")
    output.mkdir(parents=True, exist_ok=True)
    destination = output / source.name
    shutil.copy2(source, destination)
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the FMODD Rust native core.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    print(build(args.output.resolve(), release=not args.debug))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
