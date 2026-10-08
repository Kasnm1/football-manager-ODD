from pathlib import Path


ROOT = (Path(__file__).resolve().parents[1] / "src")


def test_activity_text_view_is_a_header_level_service_directory() -> None:
    index = open(ROOT / "web" / "index.html", encoding="utf-8").read()
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    styles = (ROOT / "web" / "app.css").read_text(encoding="utf-8")

    page = index.split('id="page-activity"', 1)[1].split('</section>', 1)[0]
    header = page.split('</header>', 1)[0]
    assert '<label class="facility-view-toggle"><input type="checkbox" data-facility-view-page="activity"' in header
    assert header.index('class="activity-header-actions"') < header.index('class="facility-view-toggle"')
    assert 'data-i18n-aria-label="facilities.view.compact"' in header
    assert 'Object.fromEntries(FACILITY_VIEW_PAGES.map((page) => [page, storedFacilityViewMode(page)]))' in script
    assert 'localStorage.setItem(`${FACILITY_VIEW_STORAGE_PREFIX}${page}`, mode)' in script
    assert 'checkbox.checked = mode === "text";' in script
    assert 'checkbox.addEventListener("change"' in script
    assert 'facilityViewMode("activity")' in script
    assert 'app.activityFloor, app.activityNode, app.activityRosterTab' in script
    assert 'data-activity-floor="${key}"' in script
    assert 'data-activity-node="${escapeHtml(node.key)}"' in script
    assert 'data-activity-submit' in script
    assert '.activity-page.facility-text-view .activity-scene' in styles
    assert '.activity-page.facility-text-view .activity-scene-node' in styles
    assert '.activity-page.facility-text-view .activity-scene-labels{position:static;width:100%;min-width:0;display:grid;grid-template-columns:minmax(0,1fr);justify-self:stretch;justify-items:stretch' in styles
    assert 'box-sizing:border-box;width:100%;max-width:none;min-height:58px;height:auto;justify-self:stretch;flex:none' in styles
    assert 'grid-template-columns:minmax(360px,420px) minmax(520px,1fr)' in styles
    assert 'background:#eef3f0!important' in styles


def test_facility_page_header_controls_share_the_right_aligned_tool_group() -> None:
    index = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    styles = (ROOT / "web" / "app.css").read_text(encoding="utf-8")
    expected_controls = {
        "activity": ("activity-header-actions", "activity-balance"),
        "training": ("training-balance", "training-settle-button"),
        "canteen": ("canteen-header-status", "canteen-balance"),
    }

    for page_id, control_ids in expected_controls.items():
        page = index.split(f'id="page-{page_id}"', 1)[1].split("</section>", 1)[0]
        header = page.split("</header>", 1)[0]
        tools_at = header.index('class="facility-page-header-tools"')
        assert header.index("<h1") < tools_at
        tools = header[tools_at:]
        assert all(control_id in tools for control_id in control_ids)
    assert f'data-facility-view-page="{page_id}"' in tools

    assert ".facility-page-header-tools{min-width:0;flex:1 1 auto;display:flex;align-items:center;justify-content:flex-end" in styles
    assert ".canteen-header { min-height:74px; display:flex; align-items:center; justify-content:space-between" in styles


def test_activity_centre_uses_floor_scene_layout():
    index = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    styles = (ROOT / "web" / "app.css").read_text(encoding="utf-8")

    assert 'class="app-page activity-page" id="page-activity"' in index
    assert 'data-activity-floor="${key}"' in script
    assert '"activity.floor_entertainment"' in script
    assert '"activity.floor_talk_room"' in script
    assert '"activity.node_salary"' in script
    assert 'name:"媒体中心"' in script
    assert '"activity.floor_international"' in script
    assert 'request("/api/activity/centre/purchase"' in script
    assert 'request("/api/activity/media"' in script
    assert 'request("/api/activity/batch"' in script
    assert 'selectedActivity.kind === "player"' in script
    assert 'request("/api/activity/international"' in script
    assert 'request("/api/activity/international/nations"' in script
    assert '"activity.media_interview"' in script
    assert '"activity.club_press_conference"' in script
    assert '"activity.media_player"' in script
    assert '"activity.node_naturalization"' in script
    assert 'description:"",kind:"naturalization"' in script
    assert '"activity.club_players"' in script
    assert '"activity.world_players"' in script
    assert '"activity.nationality_slot_title"' in script
    assert '"activity.change_primary_nationality"' in script
    assert '"activity.change_secondary_nationality"' in script
    assert 'data-nationality-scope="club"' in script
    assert 'data-nationality-scope="world"' in script
    assert 'data-activity-nation-open' in script
    assert 'data-activity-nation-select=' in script
    assert 'data-activity-nation-open ${app.internationalNationsLoading ? "disabled" : ""}' in script
    assert '"activity.nations_loading"' in script
    assert 'data-activity-submit' in script
    assert 'id="naturalization-player"' in script
    assert 'id="naturalization-nation"' in script
    assert ".activity-workspace" in styles
    assert "/assets/activity/activity-centre-v1.png" in styles
    assert (ROOT / "web" / "assets" / "activity" / "activity-centre-v1.png").is_file()
    assert "/assets/activity/activity-centre-sprite-v2.png" in styles
    assert (ROOT / "web" / "assets" / "activity" / "activity-centre-sprite-v2.png").is_file()
    assert "data-activity-node" in script
    assert "activity-scene-node" in styles


def test_international_nations_auto_scans_and_retries_after_missing_cache():
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    scan_block = script.split("async function ensureWorldDataScan", 1)[1].split(
        "async function loadInternationalNations", 1,
    )[0]
    load_block = script.split("async function loadInternationalNations", 1)[1].split(
        "async function loadActivityNationalityWorldPlayers", 1,
    )[0]
    assert 'request("/api/world-clubs/scan", {' in scan_block
    assert 'request("/api/world-nations?page=1&page_size=15")' in scan_block
    assert "await ensureInternationalNationScan();" in load_block
    assert "app.internationalNations = null;" in load_block
    assert "scanAttempted" in load_block


def test_refresh_required_requests_are_recovered_once_without_repeating_stale_writes():
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    request_block = script.split("function refreshRequiredMessage", 1)[1].split(
        "function localizeServerError", 1,
    )[0]
    assert "refreshForRequestError" in request_block
    assert "__autoRefreshRetry" in request_block
    assert "!autoRefreshRetry" in request_block
    assert "refreshIsSafeToRetry" in request_block
    assert "相关数据已自动刷新，请重新执行刚才的操作" in request_block
    assert "ensureManagedRostersLoaded(true, true)" in request_block
    assert "ensureOddsDataRefresh" in request_block
    assert "request(\"/api/refresh\", {" in request_block


def test_activity_centre_responds_to_its_available_width():
    styles = (ROOT / "web" / "app.css").read_text(encoding="utf-8")

    assert "container-name: activity-building" in styles
    assert "@container activity-building (max-width: 1040px)" in styles
    assert "grid-template-columns: minmax(0,1fr)" in styles
    assert "container-name: activity-scene" in styles
    assert "@container activity-scene (max-width: 1380px)" in styles
    assert "flex-wrap: wrap" in styles
    assert "overflow: hidden" in styles
    assert "flex-shrink: 0" in styles
    assert ".activity-scene-node>svg{grid-column:1;grid-row:1 / span 2}" in styles
    assert ".activity-scene-node>span{grid-column:2;grid-row:1}" in styles
    assert ".activity-scene-node>b{grid-column:2;grid-row:2}" in styles
    assert ".activity-scene-node>span,.activity-scene-node>b{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}" in styles


def test_intimacy_ranking_has_player_and_staff_tabs():
    index = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    assert 'data-ranking-tab="players"' in index
    assert 'data-ranking-tab="staff"' in index
    assert "function renderIntimacyRanking()" in script
    assert "app.rankingTab" in script
    assert "profile.staff" in script


def test_floor_and_room_unlock_rules_match_activity_centre_contract():
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    assert 'entertainment:{floor:"1F"' in script
    assert "?? 2000000" in script
    assert '"activity.node_social_bar"' in script
    assert 'talk_room:{floor:"2F"' in script
    assert script.count("?? 2000000") >= 2
    assert '"activity.node_counselling"' in script
    assert '"activity.node_retirement"' in script
    assert '"activity.node_black_room"' in script
    assert '"activity.node_language"' in script
    assert "activityMeta.departure_mediation =" not in script
    assert 'facilityNode("departure_mediation"' not in script
    assert "data-departure-mediation=" not in script
    assert 'request("/api/activity/departure-mediation"' not in script
    assert '"world.editor.languages"' in script
    assert 'request("/api/activity/languages"' in script
    assert 'request("/api/activity/language-learning"' in script
    assert 'data-language-manual' in script
    assert 'data-language-auto' in script
    assert 'data-language-cancel' in script
    assert 'data-language-instant' in script
    assert 'data-language-person-kind="player"' in script
    assert 'data-language-person-kind="staff"' in script
    assert 'request(`/api/activity/staff-languages?${query}`)' in script
    assert "person_kind:languagePersonKind" in script
    assert "targets:languageRequestTargets(targets)" in script
    assert '"activity.staff_write_reason"' in script
    assert '"activity.manual_learning"' in script
    assert '"activity.auto_learning"' in script
    assert '"activity.cancel_learning"' in script
    assert "立刻 +1" in script
    assert '"activity.node_black_room"' in script
    assert 'request("/api/activity/black-room"' in script
    assert '"activity.node_retirement"' in script
    assert 'media:{floor:"3F"' in script
    assert '"relations3d.tune.on"' in script
    assert 'international:{floor:"4F"' in script
    assert '"activity.floor_international"' in script
    assert "?? 10000000" in script
    assert '"activity.node_naturalization"' in script
    assert '"activity.node_naturalization"' in script
    assert '"activity.node_review"' in script
    assert 'club_price ?? toPurchaseInternalMoney(1000000)' in script
    assert 'world_price ?? toPurchaseInternalMoney(100000000)' in script
    assert '"activity.per_person"' in script
    assert '"activity.per_person"' in script
    assert '"activity.nationality_processing"' in script
    assert 'data-activity-cancel-secondary' in script
    assert 'action:"remove_secondary"' in script
    assert 'data-lucide="file-pen-line"' not in script
    assert script.count("const nationalityUnitPrice =") == 1
    assert script.index("const nationalityUnitPrice =") < script.index("const selectedPanel = () =>")
    assert 'request("/api/activity/facility/purchase"' in script
    assert '"activity.room_buy_room"' in script


def test_activity_actions_show_no_price_and_use_shared_floor_cooldown():
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    assert "activity_floor_cooldowns" in script
    assert "`${usageKey}:${meta.centre}`" in script
    assert "冷却至 ${escapeHtml(cooldownUntil)}" in script
    assert '<footer class="activity-roster-footer"><button' in script
    assert '<footer class="activity-roster-footer"><strong>' not in script
    assert 'if (meta.kind === "language") return ""' in script


def test_dual_coaching_filters_club_only_activity_targets():
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    assert "function managedClubPlayers()" in script
    assert '["player_feature", "naturalization"].includes(activity) ? clubPlayers : players' in script
    assert "const naturalizationPlayer = activityIndex.clubPlayersById.get(naturalizationPlayerId) || null;" in script
    assert "function managedRostersAreCurrent()" in script
    assert "ensureManagedRostersLoaded(false)" in script


def test_activity_scene_opens_multi_player_roster_instead_of_cards():
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    styles = (ROOT / "web" / "app.css").read_text(encoding="utf-8")

    assert "activityNode: null" in script
    assert "请选择活动" in script
    assert 'data-activity-roster-player="${id}"' in script
    assert 'id="activity-select-all"' in script
    assert "app.activitySelections" in script
    assert 'request("/api/activity"' in script
    assert 'request("/api/activity/counseling"' in script
    assert 'request("/api/activity/retirement"' in script
    assert "activity-player-row" in styles
    assert "activity-select-prompt" in styles
    assert "interactiveSurfaceBusy(container)" in script
    assert '"#training-dock-content,#activity-content"' in script


def test_activity_roster_is_classified_and_cards_show_complete_person_information():
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    styles = (ROOT / "web" / "app.css").read_text(encoding="utf-8")
    renderer = script.split("function renderActivity()", 1)[1].split(
        "function remainingInjuryDays", 1,
    )[0]

    assert "const rosterGroups = isRosterStaff" in renderer
    assert '[["forwards","前场"],["midfielders","中场"],["defenders","后场"],["goalkeepers","门将"]]' in renderer
    assert "STAFF_JOB_CATEGORY_GROUPS.map" in renderer
    assert 'class="activity-person-facts"' in renderer
    assert 'class="activity-person-abilities"' in renderer
    assert 'class="activity-person-relation"' not in renderer
    assert 'class="activity-person-nationality"' in renderer
    assert "activityPlayerNationalityText(player)" in renderer
    assert "function activityPlayerSecondaryNationalities(person)" in script
    assert "function activityPlayerHasPrimaryNationality(person, nationId)" in script
    assert "已是第一国籍" in renderer
    assert "activityPlayerHasSecondaryNationality(player, app.internationalNationId)" in renderer
    assert 'data-activity-roster-refresh' in renderer
    assert 'ensureManagedRostersLoaded(true, true)' in script
    assert 'toast("球员资料已刷新")' in script
    assert 'data-activity-cancel-secondary ${app.activityActionBusy || !removableNationalityPlayers.length ? "disabled" : ""}' in renderer
    assert "按每名球员当前记录的第二国籍执行" in script
    assert "managerRelationshipText(player, isRosterStaff)" not in renderer
    assert ".activity-player-identity .activity-person-abilities{flex-wrap:nowrap" in styles
    assert ': resolveIntimacyLabel(reason, true, true).label;' in script
    assert '? `<span><b>岗位</b>${escapeHtml(personDetail)}</span>${teams' in renderer
    assert '? `<span><b>岗位</b>${escapeHtml(personDetail)}</span><span><b>能力</b>' not in renderer
    assert "职员\" : \"球员\"} → 主教练" not in renderer
    assert "activity-roster-group-section" in styles
    assert "white-space:normal" in styles


def test_activity_player_requests_include_managed_team_identity():
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    renderer = script.split("function renderActivity()", 1)[1].split(
        "function remainingInjuryDays", 1,
    )[0]
    assert 'selectedActivity.kind === "staff_entertainment" ? player.training_team_id : activityIndex.teamIdByPlayerId.get(playerId)' in renderer
    assert 'request("/api/activity/batch"' in renderer
    assert 'team_id:Number(activityIndex.teamIdByPlayerId.get(Number(player.id)) || 0)' in renderer
    assert 'activity:selectedActivity.key,' in renderer
    assert "player_id:playerId,team_id:teamId" in script
    assert "target_id:playerId,team_id:teamId" in script
    assert "player_id:playerId,team_id:teamId,nation_id:nationId" in script
    assert "player_id:Number(player.id),\n    team_id:managedPlayerTeamId(player.id)," in script
    assert "player_id:playerId,team_id:index.teamIdByPlayerId.get(playerId) || 0,mode" in script


def test_retirement_activity_includes_related_club_squads():
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    assert "const retirementPlayersById = new Map(playersById);" in script
    assert "for (const player of clubProfileSquadPlayers(profile))" in script
    assert "retirementPlayers:[...retirementPlayersById.values()]" in script
    assert "const retirementPlayers = activityIndex.retirementPlayers.filter" in script


def test_activity_index_is_bound_to_all_projection_sources():
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    index = script.split("function activityDataIndex()", 1)[1].split(
        "function activityRenderStateSignature", 1,
    )[0]

    assert "sources?.profiles === profiles" in index
    for source in ("languages", "nations", "intelligence"):
        assert f"sources.{source} === {source}" in index
    assert "if (!languagesById.has(languageId))" in index
    assert "if (!nationsById.has(nationId))" in index
    assert "if (!candidatesByFixtureId.has(fixtureId))" in index
    assert "teamNamesByPlayerId" in index
    assert "teamIdByPlayerId" in index
    assert "orderedLanguagesById" in index
    assert "availableByCompetition" in index
    assert "randomAvailableTiers" in index


def test_activity_render_skips_unchanged_dom_and_coalesces_search():
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    renderer = script.split("function renderActivity()", 1)[1].split(
        "function remainingInjuryDays", 1,
    )[0]
    search = renderer.split('$("#intelligence-search")?.addEventListener("input"', 1)[1].split(
        '$("[data-intelligence-search-clear]")', 1,
    )[0]

    assert "const renderSignature = activityRenderStateSignature();" in renderer
    assert "if (app.activityRenderSignature === renderSignature) return;" in renderer
    assert renderer.index("app.activityRenderSignature === renderSignature") < renderer.index("const floors = {")
    assert renderer.index("app.activityRenderSignature === renderSignature") < renderer.index("container.innerHTML =")
    assert "app.activityRenderSignature = renderSignature;" in renderer
    assert "scheduleActivityRender();" in search
    assert "renderActivity();" not in search
    assert "requestAnimationFrame(() =>" in search
    signature = script.split("function activityRenderStateSignature()", 1)[1].split(
        "function scheduleActivityRender", 1,
    )[0]
    for field in ("bank_balance", "casino_balance", "total_balance"):
        assert f"app.state?.economy?.{field}" in signature


def test_retirement_search_preserves_ime_composition_before_rerender():
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    renderer = script.split("function renderActivity()", 1)[1].split(
        "function remainingInjuryDays", 1,
    )[0]
    search = renderer.split('const retirementSearchInput = $("#retirement-search");', 1)[1].split(
        '$("#activity-nation")', 1,
    )[0]

    assert 'addEventListener("compositionstart"' in search
    assert 'addEventListener("compositionend"' in search
    assert 'addEventListener("keydown"' in search
    assert "event.isComposing || app.retirementSearchComposing" in search
    assert "cancelAnimationFrame(app.activityRenderFrame)" in search
    assert "commitRetirementSearch(event.target.value)" in search
    assert 'event.key !== "Enter" || event.isComposing || app.retirementSearchComposing' in search
    assert "commitRetirementSearch(event.currentTarget.value)" in search
    assert "event.currentTarget.blur()" in search
    assert "if (app.retirementSearchComposing) return;" in renderer
    commit = script.split("function commitRetirementSearch(value)", 1)[1].split(
        "function normalizeActivityLanguageName", 1,
    )[0]
    assert 'const input = $("#retirement-search");' in commit
    assert "input.setSelectionRange(input.value.length, input.value.length);" in commit


def test_activity_roster_and_actions_use_indexed_player_context():
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    renderer = script.split("function renderActivity()", 1)[1].split(
        "function remainingInjuryDays", 1,
    )[0]

    assert "activityIndex.teamNamesByPlayerId.get(id)" in renderer
    assert "activityIndex.nationsById.get(nationId)" in renderer
    assert "selectedPlayersById.get(id)" in renderer
    assert "managedPlayerTeams(id)" not in renderer
    assert "managedPlayerTeamId(playerId)" not in renderer


def test_activity_workspace_exposes_busy_selection_and_motion_feedback():
    markup = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    styles = (ROOT / "web" / "app.css").read_text(encoding="utf-8")
    renderer = script.split("function renderActivity()", 1)[1].split(
        "function remainingInjuryDays", 1,
    )[0]

    assert 'id="activity-sync-status"' in markup
    assert 'role="status" aria-live="polite" hidden' in markup
    assert 'id="activity-content" class="activity-centre-content" aria-busy="false"' in markup
    assert 'container.setAttribute("aria-busy", String(Boolean(activityBusyLabel)))' in renderer
    assert 'class="activity-floor-tabs" role="tablist" aria-label="' in renderer
    assert 'role="tab" aria-selected="${key === app.activityFloor}"' in renderer
    assert 'aria-pressed="${app.intelligenceMode === mode}"' in renderer
    assert "node.key === app.activityNode" in renderer
    assert 'class="activity-roster-list" role="group" aria-label="' in renderer
    assert ".activity-floor-tabs button:focus-visible" in styles
    assert ".activity-scene-node:focus-visible" in styles
    reduced_motion = styles.split("@media (prefers-reduced-motion: reduce)", 1)[1]
    assert ".activity-sync-status > i" in reduced_motion
    assert ".activity-scene-node:hover { transform:none; }" in reduced_motion
    assert ".counseling-wheel.spinning" in reduced_motion


def test_activity_header_can_force_refresh_player_statuses_and_retirement_cache():
    markup = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    styles = (ROOT / "web" / "app.css").read_text(encoding="utf-8")

    assert 'id="activity-player-refresh-button"' in markup
    assert '"activity.refresh_players"' in markup
    assert '$("#activity-player-refresh-button").addEventListener("click", async () =>' in script
    assert "await ensureManagedRostersLoaded(true, true);" in script
    assert "app.retirementPlayersCache = {club:null, world:null};" in script
    assert 'if (app.activityNode === "retirement") await loadRetirementPlayers(true);' in script
    assert '"activity.refresh_players"' in script
    assert ".activity-player-refresh-button:focus-visible" in styles


def test_entertainment_roster_supports_staff_transaction_and_patch():
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    assert 'data-activity-roster-tab="players"' in script
    assert 'data-activity-roster-tab="staff"' in script
    assert 'kind:"staff_entertainment"' in script
    assert 'request("/api/activity/staff"' in script
    assert "staff_id:playerId,team_id:teamId" in script
    assert "data_version:Number(app.state?.data_version || 0)" in script
    assert "updateManagedStaff(playerId" in script
    assert "staff:${usageKey}:${meta.centre}" in script
    for label in ("职员小酌交流", "职员温泉交流", "职员电竞交流", "职员按摩放松"):
        assert label in script
    assert "双方关系值将同步增加" in script
    assert "无关系时建立朋友关系" in script
    assert "仅增加主教练到该职员方向" not in script
    assert "职员活动只增加职员对玩家经理方向" not in script


def test_activity_staff_cards_stay_compact_while_player_cards_show_status():
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    renderer = script.split("function renderActivity()", 1)[1].split(
        "function remainingInjuryDays", 1,
    )[0]

    assert "staffKeyAttributes(player)" not in renderer
    assert 'const abilityLine = isRosterStaff\n        ? ""' in renderer
    assert "<b>当前状态</b>" in renderer
    assert "<b>体能" not in renderer
    assert "体能 <strong>${Math.round(Number(player.fitness || 0))}" in renderer
    assert "士气 <strong>${Math.round(Number(player.morale || 0))}" in renderer
    assert "比赛状态 <strong>${Math.round(Number(player.sharpness || 0))}" in renderer


def test_intimacy_ranking_uses_strict_directional_tiers_and_212_table_ui():
    index = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    styles = (ROOT / "web" / "app.css").read_text(encoding="utf-8")

    assert 'class="intimacy-ranking-tabs"' in index
    assert 'id="intimacy-ranking-pair"' not in index
    assert "function intimacyRankingProjection(person)" in script
    assert "left.tier - right.tier || right.signed_score - left.signed_score" in script
    assert "playerFractionalIntimacy(person.id)" not in script
    for marker in ("lb-stats", "lb-table", "lb-head", "lb-row", "lb-rank-badge"):
        assert marker in script
        assert f".{marker}" in styles
    assert "平均亲密度" in script
    assert "最高亲密度" in script
    assert "永久羁绊" in script


def test_entertainment_staff_flag_is_available_to_roster_footer():
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    renderer = script.split("function renderActivity()", 1)[1].split(
        "function remainingInjuryDays", 1,
    )[0]
    declaration = renderer.index('const isEntertainmentStaff = selectedActivity.kind === "staff_entertainment";')
    row_map = renderer.index("const rows = targetPlayers.map", declaration)
    footer = renderer.index("const targetLabel =", row_map)
    assert declaration < row_map < footer
    assert 'isEntertainmentStaff ? "职员" : "球员"' in renderer[footer:]


def test_psychological_counseling_shows_one_result_wheel_per_player():
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    markup = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    styles = (ROOT / "web" / "app.css").read_text(encoding="utf-8")

    assert 'id="counseling-wheel-dialog"' in markup
    assert "openCounselingWheels(targetPlayers)" in script
    assert "settleCounselingWheel(id, result)" in script
    assert "players.filter((player) => player.has_unhappiness === true)" in script
    assert 'data-counseling-wheel-spin="${Number(player.id)}"' in script
    assert 'data-counseling-wheel-unlock="${Number(player.id)}"' in script
    assert "unlockCounselingWheel" in script
    assert "guaranteed" in script
    assert "100_000" in script
    assert "spinAllCounselingWheels" in script
    assert 'id="counseling-wheel-auto"' in markup
    assert "不满已清除" in script
    assert "不满未清除" in script
    assert "grid-template-columns:repeat(auto-fit,minmax(280px,1fr))" in styles
    assert "conic-gradient(#279b68 0 40%,#66b58d 40% 65%,#d2a844 65% 95%,#b54a52 95% 100%)" in styles


def test_language_classroom_uses_searchable_picker_with_twelve_common_languages():
    markup = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    styles = (ROOT / "web" / "app.css").read_text(encoding="utf-8")

    assert 'id="activity-language-dialog"' in markup
    assert 'id="activity-language-search"' in markup
    assert 'id="activity-language-picker-results"' in markup
    assert 'id="activity-language-instant-dialog"' in markup
    assert 'id="activity-language-instant-next-confirm"' in markup
    assert 'id="activity-language-instant-max-confirm"' in markup
    assert "立刻满级" in markup
    assert "费用将从银行账户与钱包合计扣除" not in markup
    assert "费用将从银行账户与钱包合计扣除" not in script
    assert 'id="activity-language-picker"' in script
    assert '<select id="activity-language"' not in script
    assert 'data-activity-language-choice' in script
    assert 'openActivityLanguagePicker' in script
    assert 'renderActivityLanguagePicker' in script
    assert "ACTIVITY_LANGUAGE_SCHEDULE" not in script
    assert "从当前熟练度持续提升至 10" not in script
    assert ".activity-language-schedule" not in styles
    priority_start = script.index("const COMMON_ACTIVITY_LANGUAGES = [")
    priority_end = script.index("];", priority_start)
    priority_block = script[priority_start:priority_end]
    assert priority_block.count("{label:") == 12
    for language in ("英语", "西班牙语", "葡萄牙语", "汉语", "韩语", "日语"):
        assert f'{{label:"{language}"' in priority_block
    assert "name.includes(normalizedAlias)" not in script
    assert "\\u200b-\\u200d\\ufeff" in script
    assert "function mostUsedActivityLanguage" in script
    assert "current.players += 1" in script
    assert '"fmodd-activity-language-user-selected"' in script
    assert "clubPlayers.length ? clubPlayers : players" in script
    assert "LANGUAGE_INSTANT_LEVEL_DAYS = [7,10,15,20,25,30,35,43,52,63]" in script
    assert "Math.round((200000 * Number(LANGUAGE_INSTANT_LEVEL_DAYS[level]" in script
    assert "instant_max_prices" in script
    assert "function ensureActivityLanguageInstantDialog" in script
    assert "const dialog = ensureActivityLanguageInstantDialog();" in script
    assert "if (Number.isFinite(quoted) && quoted > 0) return quoted;" in script
    assert script.count("const instantLanguagePrice =") == 1
    assert script.index("const instantLanguagePrice =") < script.index("const selectedPanel = () =>")
    instant_handler = script.index('$("[data-language-instant]")')
    auto_handler = script.index('$("[data-language-auto]")', instant_handler)
    assert "if (!dialog) return;" not in script[instant_handler:auto_handler]
    assert 'performInstantUpgrade("instant_max")' in script
    assert 'mode === "instant_max" ? "提升至满级"' in script
    assert 'commonChoice ? "常用语言" : "语言目录"' not in script
    assert ".activity-language-picker-dialog" in styles
    assert ".activity-language-instant-dialog" in styles
    assert ".activity-language-instant-options" in styles
    assert ".activity-language-choice-grid" in styles
    assert "min-height: 40px" in styles
    assert "grid-template-columns: repeat(2,minmax(0,1fr))" in styles
    assert "min-height: min(590px,calc(100vh - 246px))" in styles
    assert ".activity-language-roster-shell > .activity-roster-footer { grid-row: 6; }" in styles
    assert "padding: 7px 12px 18px" in styles
    assert "min-height: 34px" in styles
