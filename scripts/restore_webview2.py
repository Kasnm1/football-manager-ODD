"""Restore the pinned desktop SDK without storing vendor DLLs in Git."""

from __future__ import annotations

import argparse
import hashlib
import io
from pathlib import Path
import urllib.request
import zipfile

VERSION = "1.0.4078.44"
PACKAGE_SHA256 = "dc4d1d9168df26b830398303e50210b6e1729f6ce5a7ac69d2c766852f489962"
PACKAGE_URL = (
    "https://api.nuget.org/v3-flatcontainer/microsoft.web.webview2/"
    f"{VERSION}/microsoft.web.webview2.{VERSION}.nupkg"
)
ENTRIES = {
    "lib/net462/Microsoft.Web.WebView2.Core.dll": "Microsoft.Web.WebView2.Core.dll",
    "lib/net462/Microsoft.Web.WebView2.WinForms.dll": "Microsoft.Web.WebView2.WinForms.dll",
    "runtimes/win-x64/native/WebView2Loader.dll": "WebView2Loader.dll",
    "LICENSE.txt": "WebView2-LICENSE.txt",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, help="Use an already downloaded NuGet package.")
    args = parser.parse_args()
    if args.package is not None:
        payload = args.package.read_bytes()
    else:
        with urllib.request.urlopen(PACKAGE_URL, timeout=60) as response:
            payload = response.read()
    if hashlib.sha256(payload).hexdigest() != PACKAGE_SHA256:
        raise RuntimeError("WebView2 package checksum mismatch; no files were written.")

    target = Path(__file__).resolve().parents[1] / "src" / "build" / "desktop_host"
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        files = [(target / name, archive.read(entry)) for entry, name in ENTRIES.items()]
    for path, content in files:
        if path.exists() and path.read_bytes() != content:
            raise RuntimeError(f"Existing SDK file differs; refusing to overwrite: {path.name}")
    target.mkdir(parents=True, exist_ok=True)
    for path, content in files:
        if not path.exists():
            with path.open("xb") as handle:
                handle.write(content)
    print(f"WebView2 SDK {VERSION} restored to {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
