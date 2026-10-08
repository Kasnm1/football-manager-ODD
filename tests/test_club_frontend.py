from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_club_header_omits_save_metadata_subtitle():
    markup = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    page = markup.split('id="page-club"', 1)[1].split("</section>", 1)[0]
    status_sync = script.split("function syncClubPageStatus(status, club)", 1)[1].split("function ", 1)[0]

    assert 'id="club-subtitle"' not in page
    assert '$("#club-subtitle")' not in status_sync
    assert "status.last_updated" not in status_sync


def _source(name: str) -> str:
    return (ROOT / "web" / name).read_text(encoding="utf-8")


def test_club_frontend_indexes_profiles_squads_and_position_groups() -> None:
    script = _source("app.js")
    index = script.split("function clubDataIndex", 1)[1].split(
        "function teamProfile", 1,
    )[0]
    render = script.split("function renderClub()", 1)[1].split(
        "async function updateSalarySchedule", 1,
    )[0]

    assert "app.clubIndexSource === profiles" in index
    assert "profilesByTeamId = new Map()" in index
    assert "profileViewsByTeamId = new Map()" in index
    assert "squadsByTeamId = new Map()" in index
    assert "playerGroups = {forwards:[],midfielders:[],defenders:[],goalkeepers:[]}" in index
    assert "playerGroups[playerPositionGroup(player)].push(player)" in index
    assert "Object.values(playerGroups).forEach((rows) => rows.sort(playerAbilitySort))" in index
    assert 'request("/api/club?compact=1")' in script
    assert "staffById:new Map" in index
    assert "clubRenderStateSignature(index, status, club)" in render
    assert "app.clubRenderSignature === signature" in render
    assert "selectedView?.playerGroups" in render
    assert "selectedPlayers.filter" not in render


def test_club_submenu_follows_its_primary_navigation_entry() -> None:
    markup = _source("index.html")
    club_entry = markup.index('data-page="club"')
    club_subnav = markup.index('id="club-subnav"')
    next_primary_group = markup.index('id="relations-toggle"')
    assert club_entry < club_subnav < next_primary_group


def test_club_players_sort_by_ca_then_pa_inside_each_position_group() -> None:
    script = _source("app.js")
    sorter = script.split("function playerAbilitySort", 1)[1].split(
        "function staffSortInCategory", 1,
    )[0]

    assert "Number(b?.ca||0)-Number(a?.ca||0)" in sorter
    assert "Number(b?.pa||0)-Number(a?.pa||0)" in sorter


def test_club_players_keep_212_collapsible_position_groups_inside_squad_tabs() -> None:
    script = _source("app.js")
    stylesheet = _source("app.css")
    render = script.split("function renderClub()", 1)[1].split(
        "async function updateSalarySchedule", 1,
    )[0]

    assert 'clubSquadTabs(squads, app.clubSquadTeamId, "data-club-squad")' in render
    assert 'const stateKey = `player:${Number(app.clubSquadTeamId || club.team?.id || 0)}:${key}`' in render
    assert 'class="activity-roster-group main-group" data-club-group=' in render
    assert 'aria-expanded="${expanded}"' in render
    assert 'expanded ? "chevron-down" : "chevron-right"' in render
    assert ".player-group > .activity-roster-group.main-group" in stylesheet


def test_club_frontend_keeps_busy_progress_and_accessibility_outside_content_signature() -> None:
    markup = _source("index.html")
    script = _source("app.js")
    stylesheet = _source("app.css")
    status = script.split("function syncClubPageStatus", 1)[1].split(
        "function renderClub()", 1,
    )[0]
    render = script.split("function renderClub()", 1)[1].split(
        "async function updateSalarySchedule", 1,
    )[0]

    assert 'id="page-club" aria-labelledby="club-title" aria-busy="false"' in markup
    assert 'id="club-sync-status"' in markup
    assert 'id="club-content" role="tabpanel" aria-busy="false"' in markup
    assert '"nav.club_sections"' in markup
    assert 'role="tab" aria-selected="true" aria-controls="club-content"' in markup
    assert 'setAttribute("aria-busy", String(busy))' in status
    assert "data-club-progress-label" in render
    assert "data-club-progress-value" in render
    assert "data-club-progress-fill" in render
    assert "data-club-progress-note" in render
    assert "syncClubPageStatus(status, club);" in render
    assert '"world.view_details"' in script
    assert 'data-staff-detail="${staff.id}" aria-label="' in render
    assert ".club-sync-status" in stylesheet
    assert "#club-refresh-button.is-refreshing svg" in stylesheet


def test_club_staff_uses_seven_212_groups_and_empty_mount_can_rerender() -> None:
    script = _source("app.js")
    render = script.split("function renderClub()", 1)[1].split(
        "async function updateSalarySchedule", 1,
    )[0]

    assert "app.clubRenderSignature === signature && node?.childElementCount" in render
    assert "staffKeyAttributes(staff)" in render
    assert "staffJobCategory({})" in render
    assert "staffJobCategory(staff) === label" in render
    assert ".sort(staffSortInCategory)" in render
    assert "data-club-group" in render


def test_club_staff_restores_212_key_attributes_and_relationship_summary() -> None:
    script = _source("app.js")
    stylesheet = _source("app.css")
    render = script.split("function renderClub()", 1)[1].split(
        "async function updateSalarySchedule", 1,
    )[0]

    assert '"world.player_detail.kicker_attributes"' in script
    assert '"world.player_detail.kicker_attributes"' in script
    assert "staffKeyAttributes(staff)" in render
    assert "chairman_attributes" in render
    assert 'class="staff-key-attr"' in render
    assert 'class="staff-card-relation"' in render
    assert ".staff-abilities" in stylesheet
    assert ".staff-key-attr" in stylesheet
    assert "keyPairs.slice" not in render
    assert "staff-ability-more" not in render
    assert "const hasManagerRelation" in render
    assert "managerRelationshipText(staff,true)" in render
    assert 'staffJobCategory(staff) === STAFF_JOB_CATEGORY_GROUPS[0].label' in render
    assert '"world.club.staff.leader" : "world.club.staff.colleague"' in render
    assert 'class="staff-card-relation"> · ${escapeHtml(relationText)}' in render
    assert "职员 → 主教练" not in render
    relationship = script.split("function managerRelationshipText", 1)[1].split(
        "function resolveIntimacyLabel", 1,
    )[0]
    assert "relationshipPermanentIcon(intimacy" in relationship
    assert 'return `${intimacyText} · ${label}`' in relationship


def test_club_frontend_prefers_dedicated_club_status_over_stale_state_snapshot() -> None:
    script = _source("app.js")
    load = script.split("async function loadClub()", 1)[1].split(
        "async function requestTrainingFloorUnlock", 1,
    )[0]
    render = script.split("function renderClub()", 1)[1].split(
        "async function updateSalarySchedule", 1,
    )[0]

    assert "app.clubStatus = clubStatusSnapshot(payload);" in load
    assert "app.state.club_status = {...app.clubStatus};" in load
    assert "const status = app.clubStatus || liveStatus;" in render
    assert "liveStatus.refreshing || liveStatus.error || liveStatus.ready" not in render


def test_club_navigation_does_not_treat_player_only_profile_as_current() -> None:
    script = _source("app.js")
    helper = script.split("function clubProfileIsCurrent()", 1)[1].split(
        "function clubLoadIsActive()", 1,
    )[0]

    assert "app.club.profile_stage === \"complete\"" in helper
    assert "Array.isArray(app.club.staff)" in helper
    assert "The refresh worker publishes a player-only intermediate profile first" in script


def test_club_navigation_starts_dedicated_load_despite_stale_refresh_snapshot() -> None:
    script = _source("app.js")
    helper = script.split("function clubLoadIsActive()", 1)[1].split(
        "async function ensureClubLoaded", 1,
    )[0]
    navigation = script.split('$("#shop-cart-button")', 1)[1].split(
        "renderCurrentPage(page);", 1,
    )[0]
    clock_refresh = script.split("if (previousDate !== clock.date)", 1)[1].split(
        "} catch (_error)", 1,
    )[0]

    assert "Boolean(app.clubLoadPromise || app.clubPoll)" in helper
    assert navigation.count("!clubLoadIsActive()") == 2
    assert "!clubLoadIsActive()" in clock_refresh
    assert "club_status)?.refreshing" not in navigation
    assert "club_status)?.refreshing" not in clock_refresh


def test_club_navigation_and_scope_resets_invalidate_frontend_indexes() -> None:
    script = _source("app.js")
    nav = script.split("function renderClubTeamNav", 1)[1].split(
        "async function loadWorldClubs", 1,
    )[0]
    reset = script.split("function resetAccountScopedWorldState", 1)[1].split(
        "async function loadStateOnce", 1,
    )[0]

    assert "app.clubTeamNavSignature === signature" in nav
    assert 'aria-current="${active ? "page" : "false"}"' in nav
    assert "renderIcons(node);" in nav
    assert "app.clubIndexSource = null" in reset
    assert "app.clubIndex = null" in reset
    assert "app.clubIndexVersion = 0" in reset
    assert "app.clubRenderSignature = null" in reset
    assert "app.clubTeamNavSignature = null" in reset
    assert script.count("invalidateClubDataIndex({resetVersion:true});") >= 3
