from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]


def test_bottom_toast_uses_the_global_application_layer() -> None:
    css = (ROOT / "web" / "app.css").read_text(encoding="utf-8")
    toast_rule = re.search(r"\.toast\s*\{(?P<body>[^}]*)\}", css)

    assert toast_rule is not None
    assert "position:fixed" in toast_rule.group("body")
    assert "z-index:2147483647" in toast_rule.group("body")


def test_bottom_toast_enters_the_top_layer_above_modal_cards() -> None:
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    toast_markup = re.search(r'<div id="toast"[^>]*>', html)
    assert toast_markup is not None
    assert 'popover="manual"' in toast_markup.group(0)
    assert 'node.showPopover();' in script
    assert 'node.hidePopover();' in script
