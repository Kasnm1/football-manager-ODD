import json
from pathlib import Path
import subprocess


ROOT = (Path(__file__).resolve().parents[1] / "src")


def sources() -> tuple[str, str, str]:
    return (
        (ROOT / "web" / "app.js").read_text(encoding="utf-8"),
        (ROOT / "web" / "index.html").read_text(encoding="utf-8"),
        (ROOT / "web" / "app.css").read_text(encoding="utf-8"),
    )


def test_shell_uses_one_static_dom_index() -> None:
    script, _, _ = sources()
    index = script.split("function shellDomIndex()", 1)[1].split(
        "function shellRenderState", 1
    )[0]

    assert "if (app.shellDom) return app.shellDom;" in index
    assert 'pages: [...document.querySelectorAll(".app-page")]' in index
    assert 'pageButtons: [...document.querySelectorAll("[data-page]")]' in index
    assert 'sidebarPageButtons: [...document.querySelectorAll(".main-sidebar [data-page]")]' in index
    assert 'gameVersionRoots: [...document.querySelectorAll("[data-game-version-switch]")]' in index


def test_shell_signature_covers_every_visible_state_owner() -> None:
    script, _, _ = sources()
    state = script.split("function shellRenderState", 1)[1].split(
        "function renderShellControls", 1
    )[0]

    for field in (
        "data_scope_id",
        "app_version",
        "game_clock",
        "topSaveLabel(data)",
        "availableWalletBalance()",
        "match_intelligence_notice",
        "manager_options",
        "selected_manager_id",
        "saved_accounts",
        "manual_account_scope_id",
        "game_versions",
    ):
        assert field in state


def test_shell_skips_unchanged_render_and_updates_only_changed_part() -> None:
    script, _, _ = sources()
    renderer = script.split("function renderShellControls", 1)[1].split(
        "function render()", 1
    )[0]

    assert "if (!force && app.shellRenderSignature === view.signature) return;" in renderer
    assert "const parts = app.shellRenderParts;" in renderer
    for part in ("core", "versions", "connection", "managers", "accounts", "wallet", "intelligence"):
        assert f"parts.{part}" in renderer
    assert renderer.index("app.shellRenderSignature === view.signature") < renderer.index(
        '$("#game-date").textContent'
    )


def test_navigation_short_circuit_keeps_page_load_side_effects() -> None:
    script, _, _ = sources()
    navigation = script.split("function showPage(page", 1)[1].split(
        "function showInitialUsageNotice", 1
    )[0]

    assert 'const navigationSignature=page==="relations"' in navigation
    assert "if (app.navigationRenderSignature !== navigationSignature)" in navigation
    assert "dom.pages.forEach" in navigation
    assert "dom.pageButtons.forEach" in navigation
    assert "dom.sidebarPageButtons.forEach" in navigation
    assert "app.navigationRenderSignature = navigationSignature;" in navigation
    assert navigation.index("app.navigationRenderSignature = navigationSignature;") < navigation.index(
        "ensureManagedRostersLoaded"
    )
    assert navigation.index("app.navigationRenderSignature = navigationSignature;") < navigation.index(
        "loadWorldClubs"
    )
    setup = script.split('$("#world-toggle").addEventListener', 1)[1].split(
        '$("#club-subnav").addEventListener', 1
    )[0]
    assert setup.count("app.navigationRenderSignature = null;") == 3


def test_sidebar_group_open_state_keeps_only_the_latest_group_expanded() -> None:
    script, _, _ = sources()
    helper_start = script.index("const SIDEBAR_GROUP_IDS")
    helper_end = script.index("function showPage", helper_start)
    helper = script[helper_start:helper_end]
    runner = r"""
function createElement() {
  const element = {attributes:{"aria-expanded":"false"}, classes:new Set()};
  element.setAttribute = (name, value) => { element.attributes[name] = value; };
  element.getAttribute = (name) => element.attributes[name];
  element.classList = {
    toggle(name, enabled) {
      if (enabled) element.classes.add(name);
      else element.classes.delete(name);
    },
  };
  return element;
}
const elements = {};
for (const id of ["world", "facilities", "club", "relations"]) {
  elements[`#${id}-toggle`] = createElement();
  elements[`#${id}-subnav`] = createElement();
}
const $ = (selector) => elements[selector];
""" + helper + r"""
setSidebarGroupOpen("world", true, {exclusive:true});
setSidebarGroupOpen("facilities", true, {exclusive:true});
process.stdout.write(JSON.stringify(Object.fromEntries(SIDEBAR_GROUP_IDS.map((id) => [id, {
  expanded:elements[`#${id}-toggle`].getAttribute("aria-expanded"),
  active:elements[`#${id}-toggle`].classes.has("active"),
  subnavOpen:elements[`#${id}-subnav`].classes.has("open"),
}]))));
"""

    result = subprocess.run(
        ["node", "-e", runner], capture_output=True, check=True, text=True
    )
    state = json.loads(result.stdout)

    assert state["facilities"] == {
        "expanded": "true",
        "active": True,
        "subnavOpen": True,
    }
    for group_id in ("world", "club", "relations"):
        assert state[group_id] == {
            "expanded": "false",
            "active": False,
            "subnavOpen": False,
        }


def test_sidebar_toggle_and_page_navigation_use_the_exclusive_state_helper() -> None:
    script, _, _ = sources()
    navigation = script.split("function showPage(page", 1)[1].split(
        "function showInitialUsageNotice", 1
    )[0]
    setup = script.split('$("#world-toggle").addEventListener', 1)[1].split(
        '$("#club-subnav").addEventListener', 1
    )[0]

    assert 'setSidebarGroupOpen(activeSidebarGroup, true, {exclusive:true});' in navigation
    assert 'setSidebarGroupOpen("world", !open, {exclusive:!open});' in setup
    assert 'setSidebarGroupOpen("facilities", !open, {exclusive:!open});' in setup
    assert 'setSidebarGroupOpen("relations", !open, {exclusive:!open});' in setup


def test_refresh_feedback_runs_before_refresh_control_short_circuit() -> None:
    script, _, _ = sources()
    renderer = script.split("function renderRefreshControls()", 1)[1].split(
        "function syncBettingPageStatus", 1
    )[0]

    assert renderer.index("renderTopbarRefreshStatus();") < renderer.index(
        "app.refreshControlsRenderSignature === signature"
    )
    assert "if (app.refreshControlsRenderSignature === signature) return;" in renderer


def test_shell_navigation_accessibility_and_focus_contract() -> None:
    script, html, css = sources()

    assert 'id="primary-navigation" aria-label="主要应用导航"' in html
    assert 'aria-controls="primary-navigation" aria-expanded="false"' in html
    assert 'id="world-toggle" aria-controls="world-subnav" aria-expanded="false"' in html
    assert 'id="facilities-toggle" aria-controls="facilities-subnav" aria-expanded="false"' in html
    assert 'class="topbar-center" role="status" aria-live="polite"' in html
    assert 'id="manager-select" aria-label="切换当前经理"' in html
    assert '$("#sidebar-toggle").setAttribute("aria-expanded", String(!collapsed));' in script
    assert '$("#wallet-button").setAttribute("aria-label", `钱包，余额 ${view.fullBalanceLabel}`);' in script
    assert 'dom.activityNav.setAttribute(' in script
    assert ".top-action:focus-visible" in css
    assert ".sidebar-toggle:focus-visible" in css
    assert ".game-version-switch [data-game-version-button]:focus-visible" in css
