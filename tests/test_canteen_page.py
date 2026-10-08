from pathlib import Path


ROOT = (Path(__file__).resolve().parents[1] / "src")


def test_canteen_text_view_keeps_plans_and_roster_actions_together() -> None:
    index = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    styles = (ROOT / "web" / "app.css").read_text(encoding="utf-8")

    page = index.split('id="page-canteen"', 1)[1].split('</section>', 1)[0]
    header = page.split('</header>', 1)[0]
    assert '<label class="facility-view-toggle"><input type="checkbox" data-facility-view-page="canteen"' in header
    assert header.index('class="canteen-header-status"') < header.index('class="facility-view-toggle"')
    assert all(f'class="canteen-window-hit {plan}' in page for plan in ("basic", "nutrition", "elite", "peak"))
    for action in ('data-canteen-ban=', 'data-canteen-ban-all', 'data-canteen-sea-cucumber=', 'data-canteen-sea-cucumber-all'):
        assert action in script
    assert 'app.canteenDock, app.canteenPlan, facilityViewMode("canteen")' in script
    assert 'viewMode === "text"\n    ? "none"' in script
    assert '.canteen-page.facility-text-view .canteen-stage-windows' in styles
    assert '.canteen-page.facility-text-view .canteen-window-hit' in styles


def test_canteen_roster_adapts_to_narrow_content_width() -> None:
    styles = (ROOT / "web" / "app.css").read_text(encoding="utf-8")

    assert ".canteen-page { container-type:inline-size;" in styles
    assert "grid-template-columns:minmax(5rem,1fr) minmax(0,60%)" in styles
    assert ".canteen-roster-actions { min-width:0; display:grid; grid-template-columns:repeat(2,minmax(0,1fr));" in styles
    assert "overflow-wrap:anywhere; white-space:normal" in styles
    narrow_layout = styles.split("@container (max-width:760px)", 1)[1]
    assert ".canteen-page.facility-text-view .canteen-layout" in narrow_layout
    assert "grid-template-columns:minmax(0,1fr)" in narrow_layout


def test_canteen_page_uses_scene_and_two_panel_dock() -> None:
    index = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    styles = (ROOT / "web" / "app.css").read_text(encoding="utf-8")

    assert 'data-page="canteen"' in index
    assert 'id="facilities-toggle"' in index
    assert 'id="facilities-subnav"' in index
    assert '<span>球队设施</span>' in index
    assert '<span>训练场</span>' in index
    assert '<span>食堂</span>' in index
    assert '<span>健身房</span>' not in index
    assert '<span>球队食堂</span>' not in index
    home_apps = index.split('class="home-app-grid"', 1)[1].split("</div>", 1)[0]
    assert '<strong>球队设施</strong>' in home_apps
    assert '<strong>医院</strong>' not in home_apps
    assert '<strong>训练场</strong>' not in home_apps
    assert '<strong>食堂</strong>' not in home_apps
    assert 'id="page-canteen"' in index
    assert 'data-canteen-tab="roster"' in index
    assert 'data-canteen-tab="meal"' in index
    assert 'data-canteen-plan="basic"' in index
    assert 'data-canteen-plan="nutrition"' in index
    assert 'data-canteen-plan="elite"' in index
    assert 'data-canteen-plan="peak"' in index
    assert "grid-template-columns:minmax(620px,1fr) 330px" in styles
    assert "url('/assets/canteen/canteen-closed.png')" in styles
    assert "background-size:cover" in styles
    for asset in ("canteen-closed.png", "canteen-basic.png", "canteen-nutrition.png", "canteen-elite.png", "canteen-peak.png"):
        assert (ROOT / "web" / "assets" / "canteen" / asset).exists()


def test_canteen_preview_has_roster_and_meal_interactions() -> None:
    index = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    assert 'canteenDock: "roster"' in script
    assert 'basic: {name:"球队普通餐", multiplier:1, daily_cost:0' in script
    assert 'nutrition: {name:"专业营养餐", multiplier:1.5, daily_cost:10_000' in script
    assert 'elite: {name:"精英定制餐", multiplier:2, daily_cost:50_000' in script
    assert 'peak: {name:"巅峰冲刺餐", multiplier:3, daily_cost:300_000' in script
    assert 'data-canteen-player=' not in script
    assert 'data-canteen-ban=' in script
    assert 'data-canteen-sea-cucumber=' in script
    assert 'data-canteen-ban-all' in script
    assert 'data-canteen-sea-cucumber-all' in script
    assert '"canteen.deny_all"' in script
    assert '"canteen.reward_all"' in script
    assert 'app.canteenBans[id] = addCanteenDays(canteenDate(), 3)' in script
    assert '"world.editor.ca"' in script
    assert '"world.editor.ca"' in script
    assert '"world.editor.ca"' in script
    assert 'request("/api/canteen/sea-cucumbers"' in script
    assert "player_ids:players.map" in script
    assert "await submitCanteenSeaCucumbers(players)" in script
    assert "return !canteenDaysRemaining(String(app.canteenBans[id] || \"\"), currentDate)" in script
    assert '"canteen.deny"' in script
    assert '"canteen.all_banned"' in script
    assert 'sharpness:Number(response.sharpness?.after ?? 100)' in script
    assert 'overflow-y:auto' in (ROOT / "web" / "app.css").read_text(encoding="utf-8")
    assert '.canteen-meal-list { min-height:0; flex:1 1 auto;' in (ROOT / "web" / "app.css").read_text(encoding="utf-8")
    assert 'function selectCanteenPlan(plan)' in script
    assert '"world.editor.ca"' in script
    assert 'canteenPlan: "basic"' in script
    assert '"world.editor.ca"' in script
    assert '"world.editor.ca"' in script
    assert 'canteen-window-hit basic active' in index
    assert 'canteen-window-hit nutrition active' not in index
    assert 'canteenSyncSequence: 0' in script
    assert 'const previousSync = app.canteenSyncPromise' in script
    assert 'const queued = (previousSync ? previousSync.catch(() => null) : Promise.resolve()).then(run)' in script
    assert 'app.canteenConfirmedPlan || previousPlan' in script
    assert 'player.player_contract?.team_id' in script
    assert 'contractTeamId === teamId' in script
    assert 'plan !== "basic" && !response.ca_growth?.installed' in script
    assert 'selected.multiplier > 1' in script
    assert 'selected.multiplier >= 2' not in script


def test_canteen_index_and_render_signature_avoid_repeated_roster_work() -> None:
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    canteen = script.split("function canteenDate()", 1)[1].split(
        "function interactiveSurfaceBusy", 1,
    )[0]
    index = canteen.split("function canteenDataIndex()", 1)[1].split(
        "function canteenRoster()", 1,
    )[0]
    renderer = canteen.split("function renderCanteen()", 1)[1].split(
        "function selectCanteenPlan", 1,
    )[0]

    assert "sources?.profiles === profiles" in index
    assert "sources.club === club" in index
    assert "sources.output === stateOutput" in index
    assert "playersById" in index
    assert "return canteenDataIndex().players;" in canteen
    assert "canteenDataIndex().playersById.get(Number(playerId))" in canteen
    assert "body:JSON.stringify({player_id:Number(player.id),team_id:index.teamId})" in canteen
    assert "managedPlayerTeamId" not in canteen
    assert "const signature = canteenRenderStateSignature(index, players);" in renderer
    assert "if (app.canteenRenderSignature === signature) return;" in renderer
    assert renderer.index("app.canteenRenderSignature === signature") < renderer.index("dock.innerHTML =")
    for field in ("funding.bank", "funding.wallet", "funding.total", "canteenDate()"):
        assert field in canteen


def test_canteen_exposes_busy_keyboard_and_accessibility_feedback() -> None:
    index = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    styles = (ROOT / "web" / "app.css").read_text(encoding="utf-8")

    assert 'aria-labelledby="canteen-title" aria-busy="false"' in index
    assert 'id="canteen-sync-status" role="status" aria-live="polite" hidden' in index
    assert '"canteen.panel"' in index
    assert 'role="tabpanel" aria-busy="false"' in index
    assert '"canteen.windows"' in index
    assert 'aria-pressed="true"' in index
    assert 'root.setAttribute("aria-busy", String(Boolean(busyLabel)))' in script
    assert 'button.setAttribute("aria-selected", String(active))' in script
    assert 'app.canteenActionLabel = uiText("canteen.all_rewarding", {completed, total:players.length})' in script
    assert ".canteen-window-hit:focus-visible" in styles
    assert ".canteen-dock-tabs button:focus-visible" in styles
    assert ".canteen-meal-option:focus-visible" in styles
    reduced_motion = styles.split("@media (prefers-reduced-motion: reduce)", 1)[1]
    assert ".canteen-sync-status > i" in reduced_motion
    assert ".canteen-meal-option" in reduced_motion
