from __future__ import annotations

import json
import re
from typing import Any, Callable
from urllib.parse import urlparse
from urllib.request import Request, urlopen


LATEST_VERSION_URL = "https://fmodd.com/version.json"
OFFICIAL_DOWNLOAD_PAGE = "https://fmodd.com/download"
UPDATE_CHECK_TIMEOUT_SECONDS = 5
MAX_MANIFEST_BYTES = 16 * 1024
_VERSION_PATTERN = re.compile(
    r"^[vV]?(\d+)\.(\d+)\.(\d+)(?:[-._]?([A-Za-z]+)(\d*))?$"
)
_PRERELEASE_ORDER = {
    "a": 0,
    "alpha": 0,
    "b": 1,
    "beta": 1,
    "rc": 2,
}


class UpdateCheckError(RuntimeError):
    """Raised when the official update manifest cannot be trusted or read."""


def _version_key(version: str) -> tuple[int, int, int, int, str, int]:
    resolved = str(version or "").strip()
    match = _VERSION_PATTERN.fullmatch(resolved)
    if not match:
        raise UpdateCheckError("invalid_version")
    major, minor, patch = (int(part) for part in match.group(1, 2, 3))
    label = str(match.group(4) or "").casefold()
    sequence = int(match.group(5) or 0)
    if not label:
        return major, minor, patch, 3, "", sequence
    stage = _PRERELEASE_ORDER.get(label, 1)
    label_key = "" if label in _PRERELEASE_ORDER else label
    return major, minor, patch, stage, label_key, sequence


def compare_versions(left: str, right: str) -> int:
    """Return -1, 0 or 1 using FMODD's release-version convention."""
    left_key = _version_key(left)
    right_key = _version_key(right)
    return (left_key > right_key) - (left_key < right_key)


def _official_download_page(value: Any) -> str:
    resolved = str(value or OFFICIAL_DOWNLOAD_PAGE).strip()
    parsed = urlparse(resolved)
    if parsed.scheme != "https" or parsed.hostname not in {"fmodd.com", "www.fmodd.com"}:
        raise UpdateCheckError("invalid_download_page")
    return resolved


def check_for_updates(
    current_version: str,
    *,
    opener: Callable[..., Any] = urlopen,
) -> dict[str, Any]:
    """Read the bounded official manifest and compare it with this FMODD build."""
    current = str(current_version or "").strip().removeprefix("V")
    _version_key(current)
    request = Request(
        LATEST_VERSION_URL,
        headers={
            "Accept": "application/json",
            "Cache-Control": "no-cache",
            "User-Agent": f"FMODD/{current}",
        },
    )
    try:
        with opener(request, timeout=UPDATE_CHECK_TIMEOUT_SECONDS) as response:
            raw = response.read(MAX_MANIFEST_BYTES + 1)
    except (OSError, TimeoutError) as error:
        raise UpdateCheckError("request_failed") from error
    if len(raw) > MAX_MANIFEST_BYTES:
        raise UpdateCheckError("manifest_too_large")
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise UpdateCheckError("invalid_manifest") from error
    if not isinstance(manifest, dict) or manifest.get("schemaVersion") != 1:
        raise UpdateCheckError("unsupported_manifest")
    latest = str(manifest.get("version") or "").strip().removeprefix("V")
    comparison = compare_versions(current, latest)
    return {
        "current_version": current,
        "latest_version": latest,
        "release_date": str(manifest.get("releaseDate") or "").strip(),
        "download_page": _official_download_page(manifest.get("downloadPage")),
        "update_available": comparison < 0,
    }
