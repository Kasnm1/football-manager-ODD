from pathlib import Path


ROOT = (Path(__file__).resolve().parents[1] / "src")
SCRIPT = (ROOT / "web" / "app.js").read_text(encoding="utf-8")


def _function_source(name: str, next_name: str) -> str:
    return SCRIPT.split(f"function {name}", 1)[1].split(f"function {next_name}", 1)[0]


def test_inventory_item_dialogs_have_render_signatures_and_single_flight_dom_updates():
    manual = _function_source("renderManualReallocation", "async function submitManualReallocation")
    development = _function_source("renderPlayerDevelopment", "async function submitPlayerDevelopment")

    assert "manualReallocationRenderSignature" in SCRIPT
    assert "playerDevelopmentRenderSignature" in SCRIPT
    assert "if (app.manualReallocationRenderSignature === signature) return;" in manual
    assert "if (app.playerDevelopmentRenderSignature === signature) return;" in development
    assert 'querySelectorAll("[data-manual-key]").forEach((button) => button.addEventListener' not in manual
    assert 'querySelectorAll("[data-development-key]").forEach((button) => button.addEventListener' not in development


def test_inventory_item_dialogs_use_stable_delegated_handlers():
    bind = SCRIPT.split("function bindEvents()", 1)[1]
    assert '$("#manual-attribute-groups").addEventListener("click"' in bind
    assert '$("#development-attribute-groups").addEventListener("click"' in bind
    assert "button.dataset.manualDelta" in bind
    assert "button.dataset.developmentKey" in bind


def test_inventory_item_dialogs_expose_busy_status_to_assistive_technology():
    assert 'function setInventoryDialogBusy(selector, busy)' in SCRIPT
    assert 'querySelector(".attribute-reallocation-content")' in SCRIPT
    assert 'setAttribute("role", "status")' in SCRIPT
    assert 'setAttribute("aria-live", "polite")' in SCRIPT
    assert 'setAttribute("aria-busy", "true")' in SCRIPT
    assert 'setAttribute("aria-busy", "false")' in SCRIPT
