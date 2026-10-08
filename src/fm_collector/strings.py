from __future__ import annotations

import re
from dataclasses import dataclass


ASCII_RUN = re.compile(rb"[\x20-\x7e]{4,}")


@dataclass(frozen=True)
class StringRecord:
    address: int
    encoding: str
    text: str


def _looks_useful(text: str, min_len: int) -> bool:
    text = text.strip()
    if len(text) < min_len:
        return False
    if not any(ch.isalpha() for ch in text):
        return False
    printable = sum(1 for ch in text if ch.isprintable())
    if printable / max(1, len(text)) < 0.95:
        return False
    symbolic = sum(1 for ch in text if not (ch.isalnum() or ch.isspace() or ch in "-_.'(),:/+&[]"))
    if symbolic / max(1, len(text)) > 0.35:
        return False
    return True


def _yield_text_runs(text: str, base_address: int, byte_stride: int, min_len: int, encoding: str):
    start = None
    chars: list[str] = []

    def flush(index: int):
        nonlocal start, chars
        if start is None:
            return None
        candidate = "".join(chars).strip()
        addr = base_address + start * byte_stride
        start = None
        chars = []
        if _looks_useful(candidate, min_len):
            return StringRecord(addr, encoding, candidate)
        return None

    for index, ch in enumerate(text):
        if ch.isprintable() and (ch == " " or not ch.isspace()):
            if start is None:
                start = index
            chars.append(ch)
        else:
            record = flush(index)
            if record is not None:
                yield record
    record = flush(len(text))
    if record is not None:
        yield record


def extract_strings(data: bytes, base_address: int, min_len: int = 5):
    seen: set[tuple[str, str]] = set()

    for match in ASCII_RUN.finditer(data):
        raw = match.group(0)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("latin-1", errors="ignore")
        text = text.strip()
        key = ("ascii", text)
        if key not in seen and _looks_useful(text, min_len):
            seen.add(key)
            yield StringRecord(base_address + match.start(), "ascii", text)

    # UTF-16 text in process memory can start on either byte alignment.
    for align in (0, 1):
        raw = data[align:]
        if len(raw) < min_len * 2:
            continue
        if len(raw) % 2:
            raw = raw[:-1]
        try:
            decoded = raw.decode("utf-16le", errors="ignore")
        except UnicodeDecodeError:
            continue
        for record in _yield_text_runs(decoded, base_address + align, 2, min_len, "utf-16le"):
            key = ("utf-16le", record.text)
            if key not in seen:
                seen.add(key)
                yield record

