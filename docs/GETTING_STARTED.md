[English](GETTING_STARTED.md) · [简体中文](GETTING_STARTED.zh-CN.md) · [한국어](GETTING_STARTED.ko-KR.md)

# Run the development version

[← Project homepage](../README.md) · [Contributing](../CONTRIBUTING.md)

## Prepare your environment

Use Windows with Python 3.10 or newer. Python 3.10 also needs `tomli`. The native cores require the Rust MSVC toolchain, Visual C++ Build Tools and the Windows SDK.

Clone or download the repository and open a terminal in its root directory. An isolated Python environment is recommended for development:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

For Python 3.10:

```powershell
python -m pip install tomli
```

## Build the native cores and start the local service

```powershell
python scripts\build_rust_native.py
python scripts\build_cpp_hook_core.py
python fm_odds_web.py --port 7857 --no-browser --keep-alive
```

Open **http://127.0.0.1:7857** in your browser. Development reads the HTML, CSS, JavaScript and assets directly from `web/`.

Start Football Manager, load a save, then connect through FMODD. Available operations depend on the exact game build and distribution platform. Back up your save before first using operations that modify native game data.

## Make a change

Read [the contributor rules](../AGENTS.md), the [documentation map](README.md) and the relevant architecture or feature-index entry. Keep changes focused and run the checks that cover your change.

Refresh the browser after editing frontend files. Restart the local service when server code changes; stop only the service instance you started. Keep personal saves, account data, credentials and local outputs outside your commits.

Detailed development and troubleshooting notes are available in the [development manual (Chinese)](../DEVELOPMENT.md).
