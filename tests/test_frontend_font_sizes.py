from pathlib import Path
import re


ROOT = (Path(__file__).resolve().parents[1] / "src")
MINIMUM_FONT_SIZE_PX = 13
FONT_SIZE_PX = re.compile(r"font-size\s*:\s*(\d+(?:\.\d+)?)px", re.IGNORECASE)


def test_visible_web_text_uses_minimum_font_size():
    violations = []
    for stylesheet in sorted((ROOT / "web").glob("*.css")):
        text = stylesheet.read_text(encoding="utf-8")
        for match in FONT_SIZE_PX.finditer(text):
            size = float(match.group(1))
            if size < MINIMUM_FONT_SIZE_PX:
                line = text.count("\n", 0, match.start()) + 1
                violations.append(f"{stylesheet.relative_to(ROOT)}:{line} ({size:g}px)")

    assert not violations, (
        f"Visible web text must be at least {MINIMUM_FONT_SIZE_PX}px:\n"
        + "\n".join(violations)
    )
