from __future__ import annotations

import ast
import re
from pathlib import Path

from tools.player_rca import FM24_RCA_MODEL, FM26_RCA_MODEL


ROOT = (Path(__file__).resolve().parents[1] / "src")
PUBLIC_RUNTIME_FILES = (
    ROOT / "fm_odds_web.py",
    ROOT / "tools" / "club_reader.py",
    ROOT / "tools" / "player_details_fm24.py",
    ROOT / "tools" / "player_details_fm26.py",
    ROOT / "tools" / "player_movement.py",
)
PUBLIC_PAYLOAD_KEYS = {
    "compatibility_mode",
    "error",
    "market_value_note",
    "message",
    "note",
    "source",
    "warning",
}
RESEARCH_SOURCE_PATTERN = re.compile(
    r"fmrte|cheat\s*(?:engine|table)|cetrainer|tdg6661|\btrainer\b",
    re.IGNORECASE,
)


def _literal_text(node: ast.AST | None) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            value.value
            for value in node.values
            if isinstance(value, ast.Constant) and isinstance(value.value, str)
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _literal_text(node.left) + _literal_text(node.right)
    return ""


def test_public_errors_and_payloads_do_not_name_research_sources() -> None:
    leaks: list[str] = []
    for path in PUBLIC_RUNTIME_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Raise):
                call = node.exc
                if isinstance(call, ast.Call) and call.args:
                    text = _literal_text(call.args[0])
                    if RESEARCH_SOURCE_PATTERN.search(text):
                        leaks.append(f"{path.relative_to(ROOT)}:{node.lineno}: {text}")
            elif isinstance(node, ast.Dict):
                for key_node, value_node in zip(node.keys, node.values):
                    key = _literal_text(key_node)
                    if key not in PUBLIC_PAYLOAD_KEYS:
                        continue
                    text = _literal_text(value_node)
                    if RESEARCH_SOURCE_PATTERN.search(text):
                        leaks.append(f"{path.relative_to(ROOT)}:{node.lineno}: {key}={text}")
    assert not leaks, "Research-source names leaked into public copy:\n" + "\n".join(leaks)


def test_public_rca_model_ids_do_not_name_research_sources() -> None:
    assert not RESEARCH_SOURCE_PATTERN.search(FM24_RCA_MODEL)
    assert not RESEARCH_SOURCE_PATTERN.search(FM26_RCA_MODEL)
