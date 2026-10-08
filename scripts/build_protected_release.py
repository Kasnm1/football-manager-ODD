from __future__ import annotations

import argparse
import hashlib
import json
import locale
import os
import shutil
import subprocess
import sys
import sysconfig
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 compatibility.
    import tomli as tomllib
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "build" / "protection.toml"
VSWHERE = Path(r"C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe")


def run(command: list[str]) -> None:
    print("+", subprocess.list2cmdline(command), flush=True)
    subprocess.run(command, cwd=ROOT, check=True)


def visual_studio_build_environment() -> dict[str, str]:
    """Return an x64 MSVC environment suitable for in-process setuptools."""
    if not VSWHERE.is_file():
        raise FileNotFoundError("Visual Studio Build Tools vswhere.exe is missing")
    payload = json.loads(subprocess.check_output([
        str(VSWHERE), "-latest", "-products", "*", "-requires",
        "Microsoft.VisualStudio.Component.VC.Tools.x86.x64", "-format", "json",
    ], text=True, encoding=locale.getpreferredencoding(False), errors="replace"))
    if not payload:
        raise FileNotFoundError("Visual Studio C++ x64 build tools are required")
    devcmd = Path(payload[0]["installationPath"]) / "Common7" / "Tools" / "VsDevCmd.bat"
    environment_text = subprocess.check_output(
        f'call "{devcmd}" -arch=x64 -host_arch=x64 >nul && set',
        cwd=ROOT, shell=True, text=True,
        encoding=locale.getpreferredencoding(False), errors="replace",
    )
    environment = os.environ.copy()
    for line in environment_text.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            existing_key = next(
                (candidate for candidate in environment if candidate.casefold() == key.casefold()),
                None,
            )
            if existing_key is not None and existing_key != key:
                del environment[existing_key]
            environment[key] = value
    path_value = next(
        (value for key, value in environment.items() if key.casefold() == "path"),
        None,
    )
    compiler = shutil.which("cl.exe", path=path_value)
    if not compiler:
        raise FileNotFoundError("Visual Studio x64 cl.exe is missing after VsDevCmd")
    return environment


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _packaged_path(value: object) -> Path:
    path = Path(str(value))
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError(f"完整性校验打包路径无效：{value}")
    return path


def _register_integrity_file(
    expected: dict[str, str], packaged_name: object, source: Path,
) -> None:
    relative = _packaged_path(packaged_name)
    if not source.is_file():
        raise FileNotFoundError(f"完整性校验文件不存在：{source}")
    key = relative.as_posix()
    if key in expected:
        raise ValueError(f"完整性校验文件重复登记：{key}")
    expected[key] = sha256(source)


def collect_integrity_files(
    config: dict[str, object], protected_root: Path | None = None,
) -> dict[str, str]:
    host_output = ROOT / str(config["host_output"])
    expected: dict[str, str] = {}
    for packaged_name in config["integrity_files"]:
        relative = _packaged_path(packaged_name)
        if relative.parts[0] != "desktop_host" or len(relative.parts) < 2:
            raise ValueError(f"桌面宿主完整性路径无效：{relative}")
        _register_integrity_file(
            expected, relative, host_output.joinpath(*relative.parts[1:]),
        )
    for tree in config.get("integrity_trees", []):
        tree_config = dict(tree)
        source_name = (
            config[str(tree_config["source_config"])]
            if "source_config" in tree_config
            else tree_config["source"]
        )
        source_root = ROOT / str(source_name)
        packaged_root = _packaged_path(tree_config["packaged"])
        if not source_root.is_dir():
            raise FileNotFoundError(f"完整性校验目录不存在：{source_root}")
        for source in sorted(path for path in source_root.rglob("*") if path.is_file()):
            _register_integrity_file(
                expected,
                packaged_root / source.relative_to(source_root),
                source,
            )
    for packaged_name, source_name in sorted(
        dict(config.get("integrity_file_sources", {})).items()
    ):
        _register_integrity_file(expected, packaged_name, ROOT / str(source_name))
    native_files = {
        f"tools/{config['rust_native_dll']}": (
            ROOT / str(config["rust_native_output"])
            / str(config["rust_native_dll"])
        ),
        f"tools/{config['cpp_hook_dll']}": (
            ROOT / str(config["cpp_native_output"])
            / str(config["cpp_hook_dll"])
        ),
    }
    for packaged_name, source in native_files.items():
        _register_integrity_file(expected, packaged_name, source)
    if protected_root is not None:
        for module_name in config["protected_modules"]:
            module_path = Path(*str(module_name).split("."))
            candidates = list(
                (protected_root / module_path.parent).glob(
                    module_path.name + ".*.pyd"
                )
            )
            if len(candidates) != 1:
                raise RuntimeError(
                    f"受保护模块输出缺失或不唯一：{module_name} ({candidates})"
                )
            source = candidates[0]
            _register_integrity_file(
                expected, source.relative_to(protected_root), source,
            )
    return expected


def build_integrity_source(
    config: dict[str, object], generated_dir: Path,
    protected_root: Path | None = None,
) -> Path:
    expected = collect_integrity_files(config, protected_root)
    strict_roots = tuple(
        _packaged_path(value).as_posix()
        for value in config.get("integrity_strict_roots", [])
    )
    generated_dir.mkdir(parents=True, exist_ok=True)
    source_path = generated_dir / "_release_integrity.py"
    notice_path = ROOT / str(config["ai_usage_notice"])
    notice = notice_path.read_text(encoding="utf-8")
    content = (
        "from __future__ import annotations\n\n"
        "import hashlib\nimport hmac\nfrom pathlib import Path\n\n"
        f"EXPECTED = {expected!r}\n\n"
        f"STRICT_ROOTS = {strict_roots!r}\n\n"
        f"AI_USAGE_NOTICE = {notice!r}\n\n"
        "def _sha256(path: Path) -> str:\n"
        "    digest = hashlib.sha256()\n"
        "    with path.open('rb') as handle:\n"
        "        for chunk in iter(lambda: handle.read(1024 * 1024), b''):\n"
        "            digest.update(chunk)\n"
        "    return digest.hexdigest()\n\n"
        "def verify_packaged_files(asset_root: str) -> list[str]:\n"
        "    root = Path(asset_root)\n"
        "    errors: list[str] = []\n"
        "    for relative, expected in EXPECTED.items():\n"
        "        path = root / Path(relative)\n"
        "        try:\n"
        "            actual = _sha256(path)\n"
        "        except OSError:\n"
        "            errors.append(f'{relative} 缺失')\n"
        "            continue\n"
        "        if not hmac.compare_digest(actual, expected):\n"
        "            errors.append(f'{relative} 已被修改')\n"
        "    for packaged_root in STRICT_ROOTS:\n"
        "        directory = root / Path(packaged_root)\n"
        "        if not directory.is_dir():\n"
        "            continue\n"
        "        expected_names = {\n"
        "            name for name in EXPECTED\n"
        "            if name == packaged_root or name.startswith(packaged_root + '/')\n"
        "        }\n"
        "        actual_names = {\n"
        "            path.relative_to(root).as_posix()\n"
        "            for path in directory.rglob('*') if path.is_file()\n"
        "        }\n"
        "        for relative in sorted(actual_names - expected_names):\n"
        "            errors.append(f'{relative} 未登记')\n"
        "    return errors\n\n"
        "def verify_native_library(path: str, packaged_name: str) -> list[str]:\n"
        "    expected = EXPECTED.get(packaged_name)\n"
        "    if not expected:\n"
        "        return [f'{packaged_name} 未登记完整性摘要']\n"
        "    try:\n"
        "        actual = _sha256(Path(path))\n"
        "    except OSError:\n"
        "        return [f'{packaged_name} 缺失或不可读']\n"
        "    if not hmac.compare_digest(actual, expected):\n"
        "        return [f'{packaged_name} 已被修改']\n"
        "    return []\n"
    )
    if not source_path.is_file() or source_path.read_text(encoding="utf-8") != content:
        source_path.write_text(content, encoding="utf-8")
    return source_path


def compile_protected_modules(config: dict[str, object]) -> list[str]:
    try:
        from Cython.Build import cythonize
        from setuptools import Distribution, Extension
        from setuptools.command.build_ext import build_ext
    except ImportError as error:
        raise RuntimeError("缺少 Cython；请运行 python -m pip install Cython") from error

    output = ROOT / str(config["protected_output"])
    build_key = output.name.removeprefix("protected_runtime_")
    if not build_key or build_key == output.name:
        raise ValueError("protected_output 必须使用 protected_runtime_<版本> 目录名")
    generated_dir = output.parent / f"protected_generated_{build_key}"
    cython_dir = output.parent / f"cython_{build_key}"
    cython_build_dir = cython_dir.relative_to(ROOT)
    modules = list(config["protected_modules"])
    directives = {
        "language_level": 3,
        "binding": True,
        "embedsignature": False,
        "emit_code_comments": False,
        "profile": False,
        "linetrace": False,
        "annotation_typing": False,
        "infer_types": False,
        "boundscheck": True,
        "wraparound": True,
        "initializedcheck": True,
        "nonecheck": True,
        "overflowcheck": True,
        "cdivision": False,
    }
    compile_arguments = (
        ["/experimental:deterministic", f"/pathmap:{ROOT}=."]
        if os.name == "nt" else []
    )
    state_path = output.parent / f"protected_build_state_{build_key}.json"
    state = {
        "python": sys.version,
        "extension_suffix": sysconfig.get_config_var("EXT_SUFFIX"),
        "cython": __import__("Cython").__version__,
        "modules": modules,
        "directives": directives,
        "compile_arguments": compile_arguments,
    }
    previous_state = None
    try:
        previous_state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    if previous_state != state:
        for path in (output, cython_dir):
            if path.exists():
                shutil.rmtree(path)
    output.mkdir(parents=True, exist_ok=True)

    sources: list[Path] = []
    for name in modules:
        base = Path(*name.split("."))
        pyx_source = ROOT / base.with_suffix(".pyx")
        sources.append(base.with_suffix(".pyx") if pyx_source.is_file() else base.with_suffix(".py"))
    extensions = [
        Extension(name, [str(source)], extra_compile_args=compile_arguments)
        for name, source in zip(modules, sources)
    ]

    def compile_extensions(pending: list[Extension]) -> None:
        compiled = cythonize(
            pending,
            build_dir=str(cython_build_dir),
            compiler_directives=directives,
            nthreads=0,
        )
        distribution = Distribution({"ext_modules": compiled})
        command = build_ext(distribution)
        command.build_lib = str(output)
        command.build_temp = str(cython_build_dir / "temp")
        command.ensure_finalized()
        command.run()

    previous_cwd = Path.cwd()
    previous_environment = os.environ.copy()
    build_environment = visual_studio_build_environment()
    try:
        os.environ.clear()
        os.environ.update(build_environment)
        os.chdir(ROOT)
        compile_extensions(extensions)
        integrity_source = build_integrity_source(
            config, generated_dir, output,
        ).relative_to(ROOT)
        compile_extensions([
            Extension(
                "tools._release_integrity", [str(integrity_source)],
                extra_compile_args=compile_arguments,
            )
        ])
    finally:
        os.chdir(previous_cwd)
        os.environ.clear()
        os.environ.update(previous_environment)
    extension_names = [*modules, "tools._release_integrity"]
    missing = [
        name for name in extension_names
        if len(list((output / "tools").glob(name.rsplit(".", 1)[-1] + ".*.pyd"))) != 1
    ]
    if missing:
        raise RuntimeError("原生模块输出不完整：" + ", ".join(missing))
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return extension_names


def sign_executable(executable: Path, thumbprint: str | None) -> bool:
    if not thumbprint:
        return False
    candidates = sorted(Path(r"C:\Program Files (x86)\Windows Kits\10\bin").glob("*\\x64\\signtool.exe"))
    if not candidates:
        raise FileNotFoundError("找不到 Windows signtool.exe")
    run([
        str(candidates[-1]), "sign", "/sha1", thumbprint, "/fd", "SHA256", "/td", "SHA256",
        "/tr", "http://timestamp.digicert.com", str(executable),
    ])
    run([str(candidates[-1]), "verify", "/pa", "/v", str(executable)])
    return True


def write_release_manifest(
    executable: Path, version: str, protected_modules: list[str],
    integrity_files: list[str], signed: bool,
) -> Path:
    manifest = executable.with_suffix(".sha256.json")
    payload = {
        "version": version,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "artifact": executable.name,
        "size": executable.stat().st_size,
        "sha256": sha256(executable),
        "protected_modules": protected_modules,
        "integrity_files": integrity_files,
        "integrity_file_count": len(integrity_files),
        "ai_usage_notice": "AI_USAGE_NOTICE.txt",
        "ai_usage_notice_sha256": sha256(ROOT / "AI_USAGE_NOTICE.txt"),
        "authenticode_signed": signed,
    }
    manifest.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a protected FMODD release.")
    parser.add_argument("--version", required=True)
    parser.add_argument("--skip-tests", action="store_true")
    parser.add_argument("--clean", action="store_true")
    parser.add_argument("--certificate-thumbprint")
    args = parser.parse_args()
    version = args.version.removeprefix("V")
    config = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if config.get("version") != version:
        raise SystemExit(f"保护配置版本为 {config.get('version')}，不是 {version}")

    run([sys.executable, "scripts/release_preflight.py", "--repo", str(ROOT), "--version", version])
    run([
        sys.executable, "scripts/build_rust_native.py",
        "--output", str(config["rust_native_output"]),
    ])
    run([
        sys.executable, "scripts/build_cpp_hook_core.py",
        "--output", str(config["cpp_native_output"]),
    ])
    if not args.skip_tests:
        test_temp = ROOT / "build" / f"pytest-temp-v{version.replace('.', '')}"
        run([
            sys.executable, "-m", "pytest", "tests", "-q",
            "--basetemp", str(test_temp), "-p", "no:cacheprovider",
        ])
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if not npm:
        raise FileNotFoundError("npm is required to build protected frontend assets")
    run([npm, "ci", "--no-audit", "--no-fund"])
    run([
        npm, "run", "build:web:release", "--", "--version", version,
        "--output", str(config["release_web_output"]),
    ])
    run([
        sys.executable, "tools/build_embedded_web_assets.py",
        "--web-root", str(config["release_web_output"]),
    ])
    host_output = ROOT / str(config["host_output"])
    if host_output.exists():
        shutil.rmtree(host_output)
    run([
        "dotnet", "build", "desktop/WebViewHost.csproj", "-c", "Release",
        "-o", str(config["host_output"]),
        "-p:DebugType=None", "-p:DebugSymbols=false",
    ])
    protected_modules = compile_protected_modules(config)
    run([
        sys.executable, "scripts/verify_protected_core_parity.py",
        "--protected-root", str(config["protected_output"]),
    ])
    run([
        sys.executable, "scripts/verify_release_surface.py",
        "--web-root", str(config["release_web_output"]),
        "--protected-root", str(config["protected_output"]),
        "--host-root", str(config["host_output"]),
        "--native-root", str(config["rust_native_output"]),
        "--native-root", str(config["cpp_native_output"]),
    ])
    pyinstaller_command = ["pyinstaller", "--noconfirm"]
    if args.clean:
        pyinstaller_command.append("--clean")
    pyinstaller_command.append(str(config["spec"]))
    run(pyinstaller_command)

    executable = ROOT / "dist" / f"FMODD-V{version}.exe"
    if not executable.is_file():
        raise FileNotFoundError(f"封装产物不存在：{executable}")
    run([
        sys.executable, "scripts/verify_release_surface.py",
        "--web-root", str(config["release_web_output"]),
        "--protected-root", str(config["protected_output"]),
        "--executable", str(executable),
        "--host-root", str(config["host_output"]),
        "--native-root", str(config["rust_native_output"]),
        "--native-root", str(config["cpp_native_output"]),
    ])
    signed = sign_executable(executable, args.certificate_thumbprint)
    integrity_files = sorted(collect_integrity_files(config))
    manifest = write_release_manifest(
        executable, version, protected_modules, integrity_files, signed,
    )
    print(f"Protected release: {executable}")
    print(f"SHA-256 manifest: {manifest}")
    print(f"Authenticode: {'signed' if signed else 'not signed (no certificate thumbprint)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
