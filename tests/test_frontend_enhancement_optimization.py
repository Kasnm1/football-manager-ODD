from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def sources() -> tuple[str, str]:
    return (
        (ROOT / "web" / "app.js").read_text(encoding="utf-8"),
        (ROOT / "web" / "app.css").read_text(encoding="utf-8"),
    )


def test_custom_selects_keep_per_node_render_state() -> None:
    script, _ = sources()
    state = script.split("const customSelectState", 1)[1].split(
        "function customSelectContextKey", 1
    )[0]
    renderer = script.split("function syncCustomSelect(select)", 1)[1].split(
        "function customSelectsWithin", 1
    )[0]

    assert "renderStates: new WeakMap()" in state
    assert "const previous = customSelectState.renderStates.get(select);" in renderer
    for field in ("previous.value", "previous.selectedText", "previous.disabled", "previous.open", "previous.ariaLabel"):
        assert field in renderer
    assert "customSelectState.renderStates.set(select" in renderer
    assert renderer.index("if (\n    previous") < renderer.index(
        'trigger.querySelector(".fmodd-select-value").textContent'
    )


def test_custom_select_signature_covers_visible_and_interactive_state() -> None:
    script, _ = sources()
    renderer = script.split("function syncCustomSelect(select)", 1)[1].split(
        "function customSelectsWithin", 1
    )[0]

    assert "String(select.value)" in renderer
    assert "selected?.textContent" in renderer
    assert "Boolean(select.disabled)" in renderer
    assert "customSelectState.openKey === customSelectKey(select)" in renderer
    assert 'select.getAttribute("aria-label")' in renderer


def test_repeated_data_attribute_selects_receive_distinct_keys() -> None:
    script, _ = sources()
    key_builder = script.split("function customSelectKey(select)", 1)[1].split(
        "function ensureCustomSelectMenu", 1
    )[0]

    assert 'const owner = select.closest("[id]");' in key_builder
    assert 'owner?.id || "document"' in key_builder
    assert '${index}:${identity}' in key_builder
    assert 'return `${customSelectContextKey()}:attrs:${identity}`;' not in key_builder


def test_lucide_rendering_removes_processed_markers() -> None:
    script, _ = sources()
    icons = script.split("function finalizeLucideIcons", 1)[1].split(
        "async function copyText", 1
    )[0]

    assert 'scope?.querySelectorAll?.("svg[data-lucide]")' in icons
    assert 'icon.removeAttribute("data-lucide");' in icons
    assert 'icon.setAttribute("aria-hidden", "true");' in icons
    assert 'icon.setAttribute("focusable", "false");' in icons
    assert 'scope?.querySelector?.("[data-lucide]")' in icons
    assert "if (!hasPendingLucideIcons(scope)) return;" in icons
    assert icons.count("finalizeLucideIcons(scope);") == 1


def test_explicit_svg_icon_changes_remain_renderable() -> None:
    script, _ = sources()
    icon_update = 'icon.setAttribute("data-lucide", confirmed ? "lock-keyhole-open" : "lock-keyhole");'
    renderer = script.split("function renderIcons(root = null)", 1)[1].split(
        "async function copyText", 1
    )[0]

    assert icon_update in script
    assert 'scope?.querySelector?.("[data-lucide]")' in script
    assert renderer.index("window.lucide.createIcons") < renderer.index("finalizeLucideIcons(scope);")


def test_icon_and_select_enhancement_remains_root_scoped() -> None:
    script, _ = sources()
    renderer = script.split("function renderIcons(root = null)", 1)[1].split(
        "async function copyText", 1
    )[0]

    assert "const roots = iconRenderRoots(root)" in renderer
    assert "syncCustomSelects(scope)" in renderer
    assert 'root:scope' in renderer
    assert "window.lucide.createIcons" in renderer


def test_custom_select_keyboard_and_aria_contract_is_preserved() -> None:
    script, css = sources()
    enhancer = script.split("function enhanceCustomSelect(select)", 1)[1].split(
        "function syncCustomSelect(select)", 1
    )[0]
    menu = script.split("function ensureCustomSelectMenu()", 1)[1].split(
        "function mountCustomSelectMenu", 1
    )[0]

    assert 'menu.setAttribute("role", "listbox");' in menu
    assert 'role="option" aria-selected="${selected}"' in script
    assert 'trigger.setAttribute("aria-haspopup", "listbox");' in enhancer
    assert 'trigger.setAttribute("aria-controls", "fmodd-select-menu");' in enhancer
    for key in ("Enter", "ArrowDown", "ArrowUp", "Home", "End", "Escape"):
        assert f'"{key}"' in enhancer
    assert ".fmodd-select-trigger:focus-visible" in css


def test_custom_select_preserves_value_across_synchronous_rerender() -> None:
    script, _ = sources()
    state = script.split("const customSelectState", 1)[1].split(
        "function customSelectContextKey", 1
    )[0]
    enhancer = script.split("function enhanceCustomSelect(select)", 1)[1].split(
        "function syncCustomSelect(select)", 1
    )[0]
    renderer = script.split("function syncCustomSelect(select)", 1)[1].split(
        "function customSelectsWithin", 1
    )[0]

    assert "pendingValues: new Map()" in state
    assert "const key = customSelectKey(select);" in enhancer
    assert "customSelectState.pendingValues.set(key, value);" in enhancer
    assert "const pendingValue = customSelectState.pendingValues.get(pendingKey);" in renderer
    assert "select.value = pendingValue;" in renderer
    assert "customSelectState.pendingValues.delete(pendingKey);" in renderer
