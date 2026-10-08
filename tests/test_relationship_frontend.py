from pathlib import Path


ROOT = (Path(__file__).parents[1] / "src")
JS = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
HTML = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
CSS = (ROOT / "web" / "app.css").read_text(encoding="utf-8")


def test_relationship_page_and_navigation_are_registered():
    assert 'data-page="relations"' in HTML
    assert 'id="page-relations"' in HTML
    assert "relations: () => renderRelationsPage()" in JS


def test_relationship_ecosystem_has_three_immersive_workspaces():
    assert 'data-relations-view="compare"' in HTML
    assert 'data-relations-view="timeline"' in HTML
    assert "双向关系对照页" in HTML
    assert "关系变化时间线" in HTML
    assert "/api/relations/timeline" in JS
    assert "relationship-compare-stage" in JS
    assert "relationship-timeline-track" in JS
    assert "training-relationship-archive" in JS
    assert "relationship-pulse" in CSS
    assert "relationship-time-marker" in CSS
    assert "training-bond-stage" in CSS


def test_relationship_comparison_deduplicates_pair_loading_and_timeline_has_modes():
    assert "pairResolvedKey" in JS
    comparison = JS.split("function renderRelationshipComparison", 1)[1].split(
        "function timelineDirectionHtml", 1,
    )[0]
    assert "state.pairResolvedKey!==selectionKey" in comparison
    assert "pair?.person_a!==state.compareA" not in comparison
    assert 'data-relationship-timeline-mode="${value}"' in JS
    assert '"relations.timeline.mode.time"' in JS
    assert "relationshipTimelinePeopleHtml" in JS


def test_relationship_ecosystem_uses_role_sources_and_side_switchers():
    assert 'include_relationships:true' in JS
    assert "function relationshipEligiblePeople(anchorKey,directory)" in JS
    assert ").sort((a,b)=>b.score-a.score" in JS
    assert 'select data-relationship-compare="${side}"' in JS
    assert "event_role_label" in JS
    assert "state.timeline?.sources" in JS
    assert 'row.source?.label||trainingArchiveFacilityLabel' in JS
    timeline_tab = HTML.index('data-relations-view="timeline"')
    compare_tab = HTML.index('data-relations-view="compare"')
    assert timeline_tab < compare_tab


def test_training_archive_has_first_level_guidance_lanes():
    assert '"training.archive.lane.guider"' in JS
    assert '"training.archive.lane.trainee"' in JS
    assert "training-archive-role-lane" in JS
    assert "training-archive-role-toggle" in JS
    assert "data-training-archive-group=\"${escapeHtml(laneKey)}\"" in JS
    assert ".training-archive-role-content[hidden]" in CSS
    assert "archive.role_categories||[]" in JS


def test_timeline_and_comparison_use_resolved_identity_instead_of_generic_membership():
    assert "person.timeline_category_key" in JS
    assert '"relations.timeline.category.unclassified"' in JS
    assert '"relations3d.free_agent"' in JS
    assert '"relations3d.free_agent"' in JS
    reason = JS.split("function relationDirectionReason", 1)[1].split(
        "function latestRelationshipEvent", 1,
    )[0]
    assert "return relationReasonText(reason);" in reason
    assert 'fromKind==="staff"&&toKind==="player"' in reason
    assert 'fromKind==="player"&&toKind==="staff"' in reason


def test_training_relationship_summary_is_a_separate_neutral_dialog():
    assert 'id="training-relationship-summary-dialog"' in HTML
    assert "trainingRelationshipArchiveButtonHtml(state)+trainingArchiveInsights" in JS
    assert 'data-training-relationship-summary-open' in JS
    assert 'id="training-relationship-summary-dialog"' in HTML
    summary = JS.split("function trainingRelationshipArchiveHtml", 1)[1].split(
        "function trainingArchiveRecordHtml", 1,
    )[0]
    assert "heart-handshake" not in summary
    assert ".training-relationship-summary-dialog" in CSS


def test_permanent_dislike_uses_skull_globally():
    helper = JS.split("function relationshipPermanentIcon", 1)[1].split("}", 1)[0]
    assert '"💀"' in helper
    assert "relationshipPermanentIcon" in JS


def test_relationship_frontend_uses_scoped_single_page_apis():
    for endpoint in (
        "/api/relations/scopes", "/api/relations/network",
        "/api/relations/entities",
    ):
        assert endpoint in JS
    assert "page:1, all:true" in JS
    current_renderer = JS.split("function renderRelationsPage()", 1)[1].split(
        "function relationGraphData", 1,
    )[0]
    assert "relations-pagination" not in current_renderer
    assert "squad_team_id:Number(state.squadTeamId" in JS


def test_3d_workspace_keeps_strict_212_engine_and_six_themes():
    for theme in (
        "璀璨银河", "绿茵球场", "战术演练", "极光天穹", "赛博矩阵", "荣誉陈列室",
    ):
        assert theme in JS
    for contract in (
        "const FOCAL = 900", "const NEAR = 50", "step < 120",
        "computeVisibility()", "data-rel3d-search", "data-rel3d-shot-go",
        'data-rel3d-tab="players"', 'data-rel3d-tab="staff"',
        "/api/rel3d/screenshot", 'e.key === "r" || e.key === "R"',
    ):
        assert contract in JS
    assert ".relations3d-overlay" in CSS
    assert "const _bgStarCache = new Map()" in JS
    assert "function _getBgStarData(starR, baseR)" in JS
    assert 'typeof overlay._disposeRelations3D === "function"' in JS
    assert "overlay._disposeRelations3D = disposeRelations3D" in JS
    assert "relationsCloseAdjuster" not in JS
    assert "st.running = false" in JS


def test_trophy_room_adds_museum_detail_without_replacing_graph_engine():
    trophy = JS.split("function drawTrophyBg", 1)[1].split(
        "function drawBg", 1,
    )[0]
    for detail in (
        "大理石地坪分缝", "后墙壁柱", "博物馆级玻璃展柜",
        "奖牌绶带", "中央分层黑曜石奖台", "drawTierRings",
    ):
        assert detail in trophy
    assert "cabinetPositions" in trophy
    assert "exhibits.sort" in trophy
    assert "st.trophyKits[k]" in trophy


def test_3d_adapter_reuses_current_page_and_external_second_source():
    assert "loadCompleteRelationshipGraph" not in JS
    assert "result.scope_people || result.people" in JS
    assert "Number(detail?.object_type) === 3" in JS
    assert "offset += 256" in JS
    assert "include_relationships:true" in JS
    assert "state.graphKey !== graphKey" in JS
    assert "state.graphEntities = entities" in JS
    assert "out_relations:entity.person_relations || {}" in JS


def test_2d_adapter_uses_scope_roster_for_exact_current_former_labels():
    assert "relationshipPageProjection(" in JS
    assert "result.scope_people || []" in JS
    assert '"relations.reason.former_teammate"' in JS
    assert '"relations.reason.former_manager"' in JS


def test_2d_header_uses_full_scope_counts_instead_of_current_page_length():
    assert "const scopePeopleCount = Array.isArray(page?.scope_people)" in JS
    assert "page.scope_people.length" in JS
    assert '${playerCount}</span>' in JS
    assert '${staffCount}</span>' in JS
    assert "enriched.filter((entry) => entry.count).length" not in JS


def test_3d_adapter_uses_full_scope_for_current_former_labels():
    assert "function relationshipGraphProjection(people, entities, personKind, scopePeople = [])" in JS
    assert "const internalKeys = new Set([...sourceByKey.keys()].filter(Boolean))" in JS
    assert "relationshipDisplayLabel(detail.reason, targetOf(key), internalKeys)" in JS
    assert "state.page?.scope_people || []" in JS


def test_3d_category_and_relationship_filters_keep_all_direction_labels():
    assert 'forwards:"relations.timeline.category.player_forwards"' in JS
    assert 'defenders:"relations.timeline.category.player_defenders"' in JS
    assert "existing.labels.add(label)" in JS
    assert "labelsOf(e).some((label) => st.activeLabels.has(label))" in JS
    assert "st.revealIdx >= 0 && base[st.revealIdx]" in JS
    assert "if (catPass(nodes[other])) base[other] = 1" in JS


def test_3d_category_filter_opens_concrete_positions_with_fixed_checkboxes():
    assert 'class="relations3d-cat-subs open"' in JS
    assert 'class="relations3d-cat-toggle open"' in JS
    assert 'aria-expanded="true"' in JS
    assert '.relations3d-filter-panel .relations3d-filter-item > input[type="checkbox"]' in CSS
    assert 'min-width: 14px !important' in CSS
    assert 'max-height: 14px !important' in CSS


def test_galaxy_constellations_have_valid_three_dimensional_points():
    assert JS.count('{ name:constellationLabel(') >= 20
    assert "const constellationR = starR * 0.78" in JS
    assert "x:cx*constellationR+(tx-mcx)*group.k*starR*0.45" in JS
    assert "y:cy*constellationR+(ty-mcy)*group.k*starR*0.45" in JS
    assert "constellations.forEach" in JS


def test_relationship_adjustment_ui_and_endpoint_are_removed():
    assert "/api/relations/adjust-bidirectional" not in JS
    assert "data-rel3d-adjust" not in JS
    assert "data-relations-adjust" not in JS
    assert "relationsOpenAdjuster" not in JS


def test_2d_more_button_expands_in_the_same_scope_key():
    assert 'class="relations-more" data-relations-expand="${entry.pid}"' in JS
    scope_key = '${state.scopeKind}:${state.teamId}:${state.squadTeamId}:${state.personKind}'
    assert JS.count(scope_key) >= 2


def test_relationship_room_rename_matches_212_storage_and_icon_contract():
    assert 'localStorage.getItem("fmodd.trainingRoomNames")' in JS
    assert 'id="training-room-rename-dialog"' in HTML
    assert 'id="training-room-rename-form"' in HTML
    assert 'localStorage.setItem("fmodd.trainingRoomNames",JSON.stringify(names))' in JS
    assert 'data-relationship-room-rename' in JS
    assert 'data-lucide="pencil"' in JS
    assert '${room.name} ${roomNumber} 号间' in JS


def test_relationship_training_separates_club_and_national_candidates():
    assert 'function relationshipTrainingManagedPeople(kind)' in JS
    assert 'rows.set(`${candidate.training_team_type}:${teamId}:${personId}`,candidate)' in JS
    assert '[["club","俱乐部"],["national","国家队"]]' in JS
    assert 'class="training-player-group team-group"' in JS
    assert '.training-player-group.team-group' in CSS
    assert "function relationshipTrainingGuidanceAllowed" in JS
    assert "training_squad_team_id" in JS
    assert "一线队可向下指导青训" in JS


def test_training_archive_groups_people_by_position_and_staff_team():
    assert '"training.archive.category.player_forwards"' in JS
    assert '"training.archive.category.player_goalkeepers"' in JS
    assert '`staff:${group.label}`,`training.archive.category.staff_${index}`' in JS
    assert 'data-training-archive-group=' in JS
    assert '"training.archive.facility.mentoring_room"' in JS
    assert ".training-archive-category>div[hidden],.training-archive-person>div[hidden]{display:none!important}" in CSS


def test_relationship_workspaces_switch_without_full_page_rebuild_and_prewarm_data():
    assert "function switchRelationshipWorkspace(view)" in JS
    assert "function scheduleRelationshipPrewarm()" in JS
    assert 'data-relations-workspace-content="network"' in JS
    assert 'data-relations-workspace-content="timeline"' in JS
    assert 'data-relations-workspace-content="compare"' in JS
    assert 'state.workspaceNodes?.delete("compare")' in JS


def test_relationship_comparison_degrades_cleanly_without_timeline_events():
    assert '.catch(()=>({events:[]}))' in JS
    assert '.relations-workspace-content { height:clamp(560px,calc(100vh - 260px),780px);' in CSS
    assert '.relations-workspace-content>.relationship-compare-stage,.relations-workspace-content>.relationship-timeline-stage { min-height:100%;' in CSS


def test_training_archive_shows_single_session_duration():
    assert '<small>单次训练</small><b>${actualDays}<em>游戏日</em></b>' in JS
    assert '<time><span>${escapeHtml(started)}</span><i data-lucide="arrow-right"></i><span>${escapeHtml(completed)}</span></time>' in JS
    assert '.training-archive-session-time{grid-template-columns:auto minmax(0,1fr)!important;' in CSS


def test_training_archive_uses_compact_attribute_change_axes():
    assert "function trainingArchiveAttributeAxisHtml(change)" in JS
    assert 'class="training-archive-change-axis"' in JS
    assert 'class="training-archive-axis"' in JS
    assert "trainingArchiveChangesHtml(event.attribute_changes||[])" in JS
    assert ".training-archive-changes>div{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));" in CSS
    assert ".training-archive-person>div{display:grid;grid-template-columns:1fr;" in CSS


def test_training_archive_projects_room_and_desk_usage_statistics():
    insights = JS.split("function trainingArchiveInsights", 1)[1].split(
        "function trainingRelationshipArchiveHtml", 1,
    )[0]
    assert "statistics.by_room" in insights
    assert "statistics.by_desk" in insights
    assert "综合训练房间使用统计" in insights
    assert "教练进修桌位使用统计" in insights
    assert "best_partners" not in insights
    assert "function trainingArchiveUsageHtml" in insights
    assert ".training-archive-usage" in CSS


def test_coaching_cards_follow_legacy_position_template_and_staff_relationship_fallback():
    assert 'class="player-card-position"' not in JS
    assert '<h3 class="${loanedOut ? "loaned-out" : ""}">${badgePrefix}<span class="player-card-name">${escapeHtml(player.name)}</span>${injuryCross}</h3>' in JS
    assert 'class="position-badge player-number-cycle player-card-portrait-frame"' in JS
    assert 'staffJobCategory(staff) === STAFF_JOB_CATEGORY_GROUPS[0].label' in JS
    assert ".staff-card .person-card-main>strong{display:flex;align-items:baseline;flex-wrap:wrap;gap:0}" in CSS


def test_relationship_dislike_filter_uses_legacy_red_palette():
    assert '.relations-filter-btn[data-relations-filter="厌恶"]' in CSS
    assert 'background:#f5dcdc' in CSS
    assert 'background:#e8c0c0' in CSS


def test_3d_custom_selects_keep_their_full_available_width():
    assert '.relations3d-bg-row > span:not(.fmodd-select)' in CSS
    assert '.relations3d-tune-styles label > span:not(.fmodd-select)' in CSS
    assert '.relations3d-tune-list label > span:not(.fmodd-select)' in CSS
    assert '.relations3d-overlay .fmodd-select { flex: 1 1 0; width: auto; min-width: 8em;' in CSS


def test_training_room_unlock_scope_heading_matches_legacy_copy():
    assert '"relationship.room.scope_card"' in JS
    assert '"relationship.room.focus_hint"' in JS


def test_relationship_training_height_uses_chinese_label_everywhere():
    assert '=== "physical:height") return "身高"' in JS
    assert "relationshipTrainingAttributeLabel({key:value})" in JS
    assert "relationshipTrainingAttributeLabel(attr)" in JS
    assert "身高增长（≤24岁）" not in JS


def test_relationship_controls_have_responsive_layouts():
    assert ".relations-page.active { display:grid; }" in CSS
    assert ".relations-page { display:grid;" not in CSS
    assert "@media(max-width:1100px)" in CSS
    assert "@media(max-width:680px)" in CSS
    assert "@media (max-width: 1500px)" in CSS
    assert "flex:0 0 auto; white-space:nowrap" in CSS
    assert ".relations3d-actions { flex: 1 1 100%; min-width: 0; justify-content: space-between; gap: 4px; }" in CSS
    assert ".relations3d-actions .relations3d-btn { width: 33px; padding: 0; justify-content: center; }" in CSS
    assert "html.rel3d-open, body.rel3d-open { overflow: hidden; }" in CSS
    assert 'document.documentElement.classList.add("rel3d-open")' in JS
    assert 'document.documentElement.classList.remove("rel3d-open")' in JS
