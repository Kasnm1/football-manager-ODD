import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _script() -> str:
    return (ROOT / "web" / "app.js").read_text(encoding="utf-8")


def test_hall_of_fame_entry_is_enabled_and_page_is_retained() -> None:
    markup = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    assert 'data-page="hall-of-fame"' in markup
    assert 'id="page-hall-of-fame"' in markup
    assert "俱乐部名人堂" in markup


def test_hall_of_fame_best_team_has_season_switcher_pitch_and_data_table() -> None:
    script = _script()
    styles = (ROOT / "web" / "app.css").read_text(encoding="utf-8")
    for marker in (
        '["best", "最佳阵容"]',
        "function renderLegacyBestTeam(players)",
        "data-legacy-best-period=\"all_time\"",
        "data-legacy-best-formation=",
        "常见阵型",
        "data-legacy-best-slot=",
        "data-legacy-best-player=",
        "data-legacy-best-recommend",
        "function recommendLegacyBestTeam()",
        'document.querySelector("[data-legacy-best-recommend]")',
        "function saveLegacyBestTeam(formation, assignments",
        'request("/api/club-legacy/best-team"',
        "最佳 11 人数据",
        '["position", "player", "shirt_number", "appearances"',
        '["position", "player", "shirt_number", "appearances", "starts", "minutes", "goals", "assists", "rating", "ca"]',
        '"legacy.best.clean_sheets_value"',
        '"legacy.best.blocks_value"',
        "function renderLegacyBestTeamPitchStats(candidate, position, periodKey)",
        "function renderLegacyBestTeamPitchMeta(candidate, position, periodKey)",
        "function legacyBestTeamPitchY(position, rawY)",
        "function legacyBestTeamPitchX(rawX)",
        "const pitchX = legacyBestTeamPitchX(x)",
        'stats.cleanSheets, 0',
        'stats.blocks, 0',
        'stats.goals, 0',
        'stats.assists, 0',
        'class="legacy-best-pitch-stats"',
        'class="legacy-best-pitch-meta"',
        "const availableCandidates = sortedCandidates.filter((player) => !assignedIds.has(Number(player.uid)))",
        "候选位置 ",
        "season_snapshots",
    ):
        assert marker in script
    assert "cleanSheets:stats.clean_sheets" in script
    for selector in (
        ".legacy-best-team-pitch{",
        ".legacy-best-team-candidate{",
        ".legacy-best-team-table{",
        ".legacy-best-team-table table{",
        "clip-path:none!important",
        ".legacy-best-team{width:min(100%,1350px)!important;margin:0 auto}",
        "width:960px;min-width:960px",
        "aspect-ratio:4/5",
        ".legacy-best-pitch-meta{",
        ".legacy-best-pitch-stats{",
        "grid-template-columns:repeat(3,minmax(0,1fr))",
        "border-top:1px solid #ffffff20",
        "background:#020b08a8",
        ".legacy-best-team-slot{width:190px!important}",
        ".legacy-best-team-workspace aside{height:1200px;min-height:0;max-height:1200px!important;align-self:start;overflow:hidden}",
        ".legacy-best-team-candidate strong{font-size:16px!important}",
    ):
        assert selector in styles
    for removed_copy in (
        "输入至少 2 个字或球员 UID",
        "该赛季来自功能启用后的部分观察，不能视为完整历史名单或完整赛季统计。",
        "数据来自 FMODD 在该赛季最后一次成功观察；未读取到的字段显示为 —。",
        "门将行的“进球 / 扑救”和“助攻 / 封堵”列分别显示扑救数与封堵数。",
    ):
        assert removed_copy not in script


def test_hall_of_fame_loads_and_refreshes_legacy_data() -> None:
    script = _script()
    assert 'request(`/api/club-legacy?team_id=${teamId}`)' in script
    assert 'request("/api/club-legacy?team_id=0")' in script
    assert 'request("/api/club-legacy/preference"' in script
    assert "function renderClubLegacy()" in script
    assert "function renderHallOfFame()" in script
    assert "async function refreshHallOfFame()" in script
    assert "async function updateLegacyPreference(uid, changes)" in script


def test_hall_of_fame_uses_overlapping_views_and_no_mutually_exclusive_layers() -> None:
    script = _script()
    assert 'clubLegacyView: "auto"' in script
    assert "const LEGACY_VIEWS = [" in script
    for label in ('["roster", "当前阵容"', '["watch", "持续关注"', '["hall", "名人堂"'):
        assert label in script
    assert "function legacyViewPlayers(players, view)" in script
    assert "if (view === \"watch\") return player.favorite;" in script
    assert "if (view === \"hall\") return player.inducted;" in script
    assert "function legacyPlayerLayer" not in script
    assert "const LEGACY_LAYERS" not in script
    assert "data-legacy-layer" not in script
    assert "clubLegacyArchiveOpen" not in script


def test_hall_of_fame_players_are_ordered_by_shirt_number() -> None:
    script = _script()
    assert "const visible = players.filter((player) =>" in script
    assert "if (view !== \"hall\") return visible;" not in script
    assert "const leftShirt = legacyLatestShirt(left);" in script
    assert "const rightShirt = legacyLatestShirt(right);" in script
    assert "if (leftShirt == null) return 1;" in script
    assert "if (rightShirt == null) return -1;" in script
    assert "return leftShirt - rightShirt" in script
    assert "Number(left.uid || 0) - Number(right.uid || 0)" in script


def test_hall_of_fame_is_data_first_and_player_opens_full_profile() -> None:
    script = _script()
    for function_name in (
        "renderLegacyRecentChanges", "renderLegacyPlayerTable", "renderLegacyProfile",
        "renderLegacySeasonSection", "renderLegacyAttributeSection",
        "renderLegacyValueSection", "renderLegacyShirtSection",
        "renderLegacyInjurySection", "renderLegacyTransferSection",
        ):
            assert f"function {function_name}" in script
    for marker in (
        '"legacy.ca_recent"', '"legacy.profile.stats.season_title"',
        '"legacy.profile.attributes.title"', '"legacy.profile.value.title"',
        '"legacy.profile.transfer.title"', '"legacy.profile.back_players"',
        "data-legacy-select", "data-legacy-back",
    ):
        assert marker in script
    assert "const selected = players.find" in script
    assert "if (selected) return" in script


def test_hall_of_fame_profile_uses_archive_hero_glance_and_two_column_hierarchy() -> None:
    script = _script()
    styles = (ROOT / "web" / "app.css").read_text(encoding="utf-8")
    for marker in (
        "function renderLegacyProfileGlance(player)",
        'class="legacy-hof-profile-glance"',
        'class="legacy-hof-hero-metrics"',
        'class="legacy-hof-head-side"',
        'class="legacy-hof-profile-columns"',
        'class="legacy-hof-profile-primary"',
        'class="legacy-hof-profile-secondary"',
        'class="legacy-hof-profile-foot"',
    ):
        assert marker in script
    assert "PLAYER ARCHIVE" not in script
    for selector in (
        ".hall-of-fame-page .legacy-hof-recent header>div>small",
        ".hall-of-fame-page .legacy-hof-season-leaders header>div>small",
        ".hall-of-fame-page .legacy-hof-player-data header>div>small",
    ):
        assert selector in (ROOT / "web" / "app.css").read_text(encoding="utf-8")
    assert "function legacyClubTenure(player)" in script
    assert "function legacyClubTenureForClub(club, player)" in script
    assert "${joined}-${club?.active ? \"\"" in script
    assert '"legacy.active"' in script
    for selector in (
        ".legacy-hof-detail-view{width:100%;max-width:none;min-height:calc(100vh - 180px);margin:0}",
        ".legacy-hof-profile-head{position:relative;display:grid;grid-template-columns:90px minmax(0,1fr) auto",
        ".legacy-hof-profile-glance{display:grid;grid-template-columns:repeat(4,minmax(0,1fr))",
        ".legacy-hof-profile-columns{display:grid;grid-template-columns:minmax(0,1.65fr) minmax(420px,.85fr)",
        ".legacy-hof-profile-primary .legacy-hof-stat-grid{grid-template-columns:repeat(3,minmax(0,1fr))",
        ".legacy-hof-hero-metrics{display:grid;grid-template-columns:repeat(3,minmax(0,1fr))",
        ".legacy-hof-value-summary{display:grid;grid-template-columns:repeat(2,minmax(0,1fr))",
    ):
        assert selector in styles


def test_hall_of_fame_shirt_history_uses_season_instead_of_observation_dates() -> None:
    script = _script()
    assert "function legacySeasonLabelFromDate(raw)" in script
    assert "function legacyShirtSeasonLabel(row)" in script
    section = script.split("function renderLegacyShirtSection(player)", 1)[1].split("function renderLegacyInjurySection", 1)[0]
    assert "<th>赛季</th><th>号码</th><th>俱乐部</th>" in section
    assert "首次观察" not in section
    assert "最后观察" not in section


def test_hall_of_fame_recent_change_prioritizes_ca_over_attribute_detail() -> None:
    script = _script()
    assert "function legacyLatestCaTransition(player)" in script
    assert "function legacyCaChange(player)" in script
    transition = script.split("function legacyLatestCaTransition(player)", 1)[1].split("function legacyCaChange(player)", 1)[0]
    assert "for (let index = snapshots.length - 1; index > 0; index -= 1)" in transition
    assert "snapshots[index - 1]?.ca" in transition
    assert "snapshots[index]?.ca" in transition
    recent = script.split("function legacyPlayerRecentChanges(player)", 1)[1].split("function legacyChangeTone", 1)[0]
    assert "const ca = legacyCaChange(player)" in recent
    assert 'subject: "CA"' in script
    assert "priority: 0" in script
    assert "Number(a.priority ?? 9) - Number(b.priority ?? 9)" in recent


def test_hall_of_fame_value_history_hides_market_value() -> None:
    script = _script()
    section = script.split("function renderLegacyValueSection(player)", 1)[1].split("function renderLegacyShirtSection", 1)[0]
    assert "function legacyQuarterlyValueHistory(history)" in script
    assert "legacyQuarterlyValueHistory(history).slice().reverse()" in section
    assert "<th>记录日期</th><th>挂牌价</th>" in section
    assert "市场身价" not in section
    assert "market_value" not in section


def test_hall_of_fame_profile_hides_snapshot_and_mapping_notes() -> None:
    script = _script()
    season = script.split("function renderLegacySeasonSection(player)", 1)[1].split("function renderLegacyAttributeSection", 1)[0]
    attributes = script.split("function renderLegacyAttributeSection(player)", 1)[1].split("function renderLegacyValueSection", 1)[0]
    for section in (season, attributes):
        for note in (
            "FM26 当前赛季总计",
            "观测于",
            "分赛事和完整历史仍待映射",
            "逐赛季归档待后续接入",
            "月度快照记录",
            "首末快照对比",
            "每月保留最后一次成功读取",
        ):
            assert note not in section


def test_hall_of_fame_profile_places_match_moments_before_shirt_and_simplifies_injury_table() -> None:
    script = _script()
    profile = script.split("function renderLegacyProfile(player)", 1)[1].split("function renderClubLegacy", 1)[0]
    assert profile.index("renderLegacyMatchMomentsSection(player)") < profile.index("renderLegacyShirtSection(player)")
    injury = script.split("function renderLegacyInjurySection(player)", 1)[1].split("function renderLegacyTransferSection", 1)[0]
    assert "<th>具体伤病</th><th>开始</th><th>恢复</th><th>时长</th>" in injury
    assert "<th>俱乐部</th>" not in injury


def test_hall_of_fame_season_section_has_total_and_archived_season_switches() -> None:
    script = _script()
    styles = (ROOT / "web" / "app.css").read_text(encoding="utf-8")
    season_stats = script.split("function legacySeasonStats(player)", 1)[1].split("function legacyStatNumber", 1)[0]
    assert '"fm24_native_current_season"' in season_stats
    assert '"fm26_native_current_season"' in season_stats
    for marker in (
        'clubLegacyStatsScope: "season"',
        'clubLegacyStatsPeriod: "all_time"',
        'data-legacy-stats-scope="season"',
        'data-legacy-stats-scope="career"',
        'data-legacy-stats-period="${escapeHtml(key)}"',
        '"legacy.profile.stats.career_title"',
        '"legacy.profile.stats.total"',
        '"legacy.profile.stats.season_option"',
        "function legacyAggregateSeasonStats(seasonSnapshots)",
        "seasonSnapshots: new Map()",
        "app.clubLegacyStatsScope = button.dataset.legacyStatsScope === \"career\" ? \"career\" : \"season\"",
        'app.clubLegacyStatsPeriod = String(button.dataset.legacyStatsPeriod || "all_time")',
        "app.clubLegacyStatsScope",
    ):
        assert marker in script
    for selector in (
        ".legacy-hof-section-switch{display:grid;grid-template-columns:repeat(2,minmax(0,1fr))",
        ".legacy-hof-section-switch button.active{background:var(--hof-accent);color:#fff}",
        ".legacy-hof-period-switch{display:flex;flex-wrap:wrap",
        ".legacy-hof-period-switch button.active{border-color:var(--hof-accent);background:var(--hof-accent);color:#fff}",
    ):
        assert selector in styles


def test_hall_of_fame_total_aggregates_archived_seasons_without_faking_missing_values() -> None:
    source = _script()
    helpers = "function legacyVerifiedSeasonStats" + source.split(
        "function legacyVerifiedSeasonStats", 1,
    )[1].split("function legacyStatNumber", 1)[0]
    probe = r'''
const rows = [
  {season_stats:{source:"fm26_native_current_season",scope:"current_season_total",appearances:10,starts:8,sub_appearances:2,minutes:800,goals:3,average_rating:7}},
  {season_stats:{source:"fm24_native_current_season",scope:"current_season_total",appearances:20,starts:17,sub_appearances:3,minutes:1600,goals:5,assists:4,average_rating:8}},
];
console.log(JSON.stringify(legacyAggregateSeasonStats(rows)));
'''
    result = subprocess.run(
        ["node", "-e", helpers + probe], check=True, capture_output=True,
        text=True, encoding="utf-8",
    )
    total = json.loads(result.stdout)
    assert total["appearances"] == 30
    assert total["starts"] == 25
    assert total["goals"] == 8
    assert "assists" not in total
    assert total["minutes"] == 2400
    assert round(total["average_rating"], 2) == 7.67
    assert "xg" not in total


def test_hall_of_fame_transfer_section_shows_complete_current_club_context() -> None:
    script = _script()
    raw_script = open(ROOT / "web" / "app.js", encoding="utf-8").read()
    section = raw_script.split("function renderLegacyTransferSection(player)", 1)[1].split("function renderLegacyTimelineSection", 1)[0]
    details = script.split("function legacyTransferClubDetails(player)", 1)[1].split("function legacyPortraitMarkup", 1)[0]
    for label in (
        "legacy.profile.transfer.current_club", "legacy.profile.transfer.joined",
        "legacy.profile.transfer.previous_club", "legacy.profile.transfer.parent_club",
    ):
        assert label in section
    assert "transfer.isLoaned" in section
    assert "transfer.ownerClub" in section
    assert "nativeJoinedDate" in details
    assert "snapshot.player_contract?.team_name" in details
    assert "loan.team_name" in details
    assert "ownerClubId !== loanClubId" in details
    assert 'previousClubState === "none" ? "无" : "未读取"' in details
    assert "previous_club_name" in details
    assert "trackedPrevious" in details
    for obsolete in ("转入本队时间", "当前挂牌价", "转出本队", "仍在本队"):
        assert obsolete not in section


def test_hall_of_fame_profile_uses_live_current_club_and_inline_display_name_edit() -> None:
    script = _script()
    identity = script.split("function legacyIdentityLabel(player)", 1)[1].split("function legacyPreferenceBusy", 1)[0]
    club_line = script.split("function legacyClubLine(player)", 1)[1].split("function renderLegacyViewNav", 1)[0]
    header = script.split("function renderLegacyProfileHeader(player)", 1)[1].split("function legacyStatCards", 1)[0]
    glance = script.split("function renderLegacyProfileGlance(player)", 1)[1].split("function renderLegacySeasonSection", 1)[0]
    live_profile = script.split("async function loadLegacyPlayerLiveProfile", 1)[1].split("function beginLegacyPlayerNameEdit", 1)[0]
    name_edit = script.split("function beginLegacyPlayerNameEdit", 1)[1].split("function applyClubLegacyStatus", 1)[0]
    assert "已离队" not in identity
    assert "已离队" not in club_line
    assert 'legacyTransferClubDetails(player).currentClub' in club_line
    assert '"legacy.profile.current_affiliation"' in glance
    assert "transfer.ownerClub" in glance
    assert "`外租至 ${transfer.currentClub}`" in glance
    assert 'data-legacy-name-edit="${player.uid}"' in header
    assert 'data-lucide="pencil"' in header
    assert "/api/world-players/detail?player_id=" in live_profile
    assert "app.clubLegacyLivePlayers.set" in live_profile
    assert 'request("/api/world-players/name"' in name_edit
    assert 'class="legacy-hof-name-input"' in name_edit
    assert 'placeholder="' in name_edit


def test_hall_of_fame_timeline_hides_roster_sync_audit_events() -> None:
    script = _script()
    section = script.split("function renderLegacyTimelineSection(player)", 1)[1].split("function renderLegacyCurationSection", 1)[0]
    assert "returned_to_roster" in section
    assert "left_current_roster" in section
    assert "joined_current_club" not in section
    assert "visibleEvents" in section


def test_hall_of_fame_home_uses_compact_player_cards_without_the_summary_metrics_row() -> None:
    script = _script()
    assert "renderLegacyOverviewMetrics" not in script
    assert ".legacy-hof-metrics" not in script
    assert 'class="legacy-hof-player-grid"' in script
    assert 'class="legacy-hof-player-card${' in script
    assert 'class="legacy-hof-card-star' in script
    assert 'class="legacy-hof-card-mark"' in script
    assert 'class="legacy-hof-card-open"' not in script
    assert 'class="legacy-hof-roster-table"' not in script


def test_hall_of_fame_cards_use_local_fm_portraits_with_initial_fallback() -> None:
    script = _script()
    styles = (ROOT / "web" / "app.css").read_text(encoding="utf-8")
    for marker in (
        "function legacyPortraitMarkup(player, className = \"\", {defer = false, fallbackText = \"\"} = {})",
        "/api/player-avatar?player_id=",
        "onerror=\"this.hidden=true;this.nextElementSibling.hidden=false\"",
        'class="legacy-hof-portrait',
    ):
        assert marker in script
    for selector in (
        ".legacy-hof-portrait{position:absolute;inset:0",
        ".legacy-hof-portrait img{display:block;width:100%;height:100%;object-fit:cover}",
    ):
        assert selector in styles
    assert '${shirt ? `<b>#${shirt}</b>` : ""}' not in script
    assert ".legacy-hof-card-mark>b" not in styles
    assert ".legacy-hof-plaque>b" not in styles
    assert "if path == \"/api/player-avatar\"" in (ROOT / "fm_odds_web.py").read_text(encoding="utf-8")
    assert 'X-FMODD-Asset-Source' in (ROOT / "fm_odds_web.py").read_text(encoding="utf-8")


def test_hall_of_fame_home_uses_ca_only_panel_and_season_leaders_panel() -> None:
    script = _script()
    styles = (ROOT / "web" / "app.css").read_text(encoding="utf-8")
    recent = script.split("function renderLegacyRecentChanges(players)", 1)[1].split("function renderLegacySeasonLeaders", 1)[0]
    assert "filter((change) => change.subject === \"CA\" || change.category === \"能力\")" in recent
    assert "最近 CA 变化" in recent
    assert "function renderLegacySeasonLeaders(players)" in script
    assert 'class="legacy-hof-season-leaders' in script
    for marker in ("最佳评分", "射手王", "助攻王", "CA 提升最多", "出场最多", "出场时间最多"):
        assert marker in script
    assert "row.appearances >= 3 || row.minutes >= 180" in script
    for marker in (
        "data-legacy-leaders-scope", "legacy.leader.scope_all",
        "legacy.leader.scope_league", "legacy.leader.scope_cup",
        "legacy.leader.goal_contributions", "legacy.leader.pass_completion",
    ):
        assert marker in script
    assert "评分至少 3 次出场或 180 分钟" not in script
    assert 'class="legacy-hof-home-top"' in script
    home = script.split("function renderClubLegacy()", 1)[1].split("async function searchLegacyWorldPlayers", 1)[0]
    assert 'data-legacy-search' not in home
    assert ".legacy-hof-home-top{display:grid;grid-template-columns:minmax(0,1.2fr) minmax(300px,.8fr)" in styles
    assert ".legacy-hof-card-mark{width:36px;height:36px" in styles
    assert ".legacy-hof-leader-scope" in styles
    assert ".legacy-hof-leader-grid.tertiary" in styles


def test_hall_of_fame_recent_changes_show_dates_and_before_after_values() -> None:
    script = _script()
    recent = script.split("function renderLegacyRecentChanges(players)", 1)[1].split(
        "function renderLegacySeasonLeaders", 1,
    )[0]
    for marker in (
        "function legacyEventChangeParts(event)",
        "function legacyPlayerRecentChanges(player)",
        "function legacyValueChangeDetail(previous, latest)",
        'class="legacy-hof-change-table"',
        'class="legacy-hof-change-row"',
        "<span>原值</span>",
        "<span>新值</span>",
        "<span>幅度</span>",
        "legacyValueChangeDetail(previous, latest)",
    ):
        assert marker in script
    assert '<span>CA</span>' not in recent
    assert 'class="legacy-hof-change-subject"' not in recent
    styles = (ROOT / "web" / "app.css").read_text(encoding="utf-8")
    assert "grid-template-columns:88px minmax(130px,1.4fr) 58px 18px 58px 58px 16px" in styles


def test_hall_of_fame_cards_use_explicit_data_labels_and_compact_surface() -> None:
    script = _script()
    styles = (ROOT / "web" / "app.css").read_text(encoding="utf-8")
    for marker in (
        '<dt>本季表现</dt>', '<dt>本季评分</dt>', '<dt>当前身价</dt>',
        'class="legacy-hof-card-club"',
        'class="legacy-hof-card-head"', 'class="legacy-hof-card-change',
        'class="legacy-hof-card-mark"', 'class="legacy-hof-card-change-main"',
        'class="legacy-hof-card-ca-change"',
    ):
        assert marker in script
    for selector in (
        '.legacy-hof-toolbar{display:grid;grid-template-areas:"scope views"',
        ".legacy-hof-player-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr))",
        ".legacy-hof-player-card{position:relative;min-width:0;min-height:294px;display:grid;grid-template-rows:58px 44px auto minmax(90px,1fr)",
        ".legacy-hof-card-star{position:static",
        ".legacy-hof-card-change .legacy-hof-card-change-main{display:flex;align-items:baseline",
        ".legacy-hof-card-change .legacy-hof-card-ca-change{display:flex;align-items:center",
        ".legacy-hof-card-facts{display:grid;grid-template-columns:repeat(2,minmax(0,1fr))",
        ".legacy-hof-card-facts>div:first-child{grid-column:1/-1",
        ".legacy-hof-card-facts dd{overflow:visible",
        ".legacy-hof-card-change>div>span{grid-column:1/-1",
        ".legacy-hof-card-change em{grid-column:2;grid-row:1",
        ".legacy-hof-player-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:16px;padding:16px;background:var(--hof-grid-bg)}",
        ".legacy-hof-player-card{position:relative;min-width:0;min-height:294px;display:grid;grid-template-rows:58px 44px auto minmax(90px,1fr);overflow:hidden;border:1px solid var(--hof-line);border-radius:8px;background:var(--hof-surface);box-shadow:var(--hof-card-shadow);cursor:pointer}",
    ):
        assert selector in styles
    assert 'legacy.player_card.current_estimated_value' not in script


def test_hall_of_fame_card_empty_changes_use_clear_status_labels() -> None:
    script = _script()
    assert '"legacy.event.watch_status"' in script
    assert '"legacy.player_card.just_watched"' in script
    assert '<p>${changeEmptyLabel}</p>' in script
    assert '"legacy.no_changes"' in script


def test_hall_of_fame_watch_view_can_search_and_follow_world_players() -> None:
    script = _script()
    for marker in (
        "function renderLegacyWorldSearch(players)",
        "async function searchLegacyWorldPlayers(query)",
        "async function followLegacyWorldPlayer(uid)",
        "/api/world-players?",
        "data-legacy-world-search",
        "data-legacy-world-follow",
    ):
        assert marker in script
    for marker in (
        'addEventListener("compositionstart"',
        'addEventListener("compositionend"',
        "event.isComposing || app.clubLegacyWorldComposing",
        "function scheduleLegacyWorldPlayerSearch(query)",
        'document.activeElement?.matches?.("[data-legacy-world-search]")',
    ):
        assert marker in script


def test_hall_of_fame_exposes_loading_error_busy_and_focus_recovery_states() -> None:
    script = _script()
    for marker in (
        "clubLegacyError", "clubLegacyRevision", "clubLegacyRenderSignature",
        "clubLegacyPreferenceBusy", "legacy-hof-loading", "legacy-hof-error",
        "data-legacy-retry", 'aria-busy="${busy}"', "clubLegacyReturnUid",
    ):
        assert marker in script
    assert "if (signature !== app.clubLegacyRenderSignature)" in script
    assert 'document.querySelector(`[data-legacy-select="${returnUid}"]`)?.focus();' in script
    preference = script.split("async function updateLegacyPreference(uid, changes)", 1)[1].split("function renderHallOfFame", 1)[0]
    assert "app.clubLegacySelectedUid = Number(uid)" not in preference


def test_hall_of_fame_keeps_all_record_types_and_management_actions() -> None:
    script = _script()
    for label in (
        "legacy.profile.stats.season_title", "legacy.profile.attributes.title",
        "legacy.profile.value.title", "legacy.profile.shirt.title",
        "legacy.profile.injury.title", "legacy.profile.transfer.title",
    ):
        assert label in script
    for marker in ("data-legacy-favorite", "data-legacy-induct", "data-legacy-tier", "data-legacy-record-save"):
        assert marker in script
    assert "function legacyInjurySpells(player)" in script
    assert "function legacyAttributeDiff(player)" in script
    assert "function legacyInjuryName(spell)" in script
    assert "<th>具体伤病</th>" in script


def test_hall_of_fame_supports_quick_match_moment_capture() -> None:
    script = _script()
    for marker in (
        "function renderLegacyMatchMomentsSection(player)",
        "data-legacy-record-moment",
        "async function openLegacyMatchMomentDialog(uid)",
        "/api/team-results?team_id=",
        "/api/club-legacy/match-moment",
        "贡献类型",
        "比赛时刻",
        "legacy-match-moment-dialog",
    ):
        assert marker in script or marker in (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    styles = (ROOT / "web" / "app.css").read_text(encoding="utf-8")
    for selector in (".legacy-hof-moment", ".legacy-moment-tags", ".legacy-moment-tag-groups", ".legacy-moment-form-grid"):
        assert selector in styles
    for tag in ("游龙", "倒钩", "世界波", "关键解围", "绝妙助攻"):
        assert tag in script
    assert "保存时将按需核对原生进球事件" in script


def test_hall_of_fame_attribute_archive_does_not_render_training_ca_object() -> None:
    script = _script()
    section = script.split("function renderLegacyAttributeSection(player)", 1)[1].split("function renderLegacyValueSection", 1)[0]
    assert "训练 CA" not in section


def test_hall_of_fame_styles_cover_data_home_and_detail() -> None:
    styles = (ROOT / "web" / "app.css").read_text(encoding="utf-8")
    for class_name in (
        ".legacy-hof-home", ".legacy-hof-toolbar", ".legacy-hof-views",
        ".legacy-hof-recent", ".legacy-hof-change-table", ".legacy-hof-player-data",
        ".legacy-hof-player-grid", ".legacy-hof-player-card", ".legacy-hof-player-link",
        ".legacy-hof-card-change", ".legacy-hof-card-mark", ".legacy-hof-season-leaders", ".legacy-hof-back", ".legacy-hof-profile",
    ):
        assert class_name in styles
    legacy_start = styles.index("/* Hall of fame v9: single ODD layer")
    legacy_styles = styles[legacy_start:]
    for size in range(1, 13):
        assert f"font-size:{size}px" not in legacy_styles
    dark = (ROOT / "web" / "dark-theme.css").read_text(encoding="utf-8")
    assert "Hall of fame night: token swap for the v9 ODD layer" in dark
    assert ":root[data-theme=\"dark\"] .hall-of-fame-page{--hof-ink" in dark


def test_hall_of_fame_visual_iteration_covers_event_cards_detail_theme_and_motion() -> None:
    script = _script()
    styles = (ROOT / "web" / "app.css").read_text(encoding="utf-8")
    dark = (ROOT / "web" / "dark-theme.css").read_text(encoding="utf-8")
    assert "function legacyEventChangeParts(event)" in script
    assert 'class="legacy-hof-change-row"' in script
    for selector in (
        ".legacy-hof-profile-head", ".legacy-hof-section", ".legacy-hof-stat-grid",
        ".legacy-hof-transfer-cards", ".legacy-hof-curation",
        "@media(prefers-reduced-motion:reduce)",
    ):
        assert selector in styles
    assert ':root[data-theme="dark"] .hall-of-fame-page{--hof-' in dark
    assert '.legacy-hof-roster-table' not in dark


def test_hall_of_fame_v10_uses_command_deck_home_and_full_width_dossier() -> None:
    script = _script()
    styles = (ROOT / "web" / "app.css").read_text(encoding="utf-8")
    for marker in (
        'class="legacy-hof-section legacy-hof-shirt-section"',
        'class="legacy-hof-section legacy-hof-injury-section"',
        'class="legacy-hof-section legacy-hof-transfer-section"',
    ):
        assert marker in script
    for selector in (
        "/* Hall of fame v10: command-deck home and full-width player dossier. */",
        ".legacy-hof-toolbar{position:relative;grid-template-columns:minmax(250px,.72fr) minmax(620px,1.28fr)",
        ".legacy-hof-views button{min-height:58px",
        ".legacy-hof-player-card{min-height:308px",
        ".legacy-hof-detail-view,.legacy-hof-profile{width:100%;max-width:none}",
        ".legacy-hof-profile-head{isolation:isolate;min-height:216px",
        ".legacy-hof-section{border-top:4px solid var(--hof-green)",
        ".legacy-hof-injury-section{border-top-color:var(--hof-red)}",
    ):
        assert selector in styles


def test_hall_of_fame_view_navigation_is_one_row_large_and_icon_free() -> None:
    script = _script()
    styles = (ROOT / "web" / "app.css").read_text(encoding="utf-8")
    nav = script.split("function renderLegacyViewNav(players)", 1)[1].split("function renderLegacyRecentChanges", 1)[0]
    assert "data-lucide" not in nav
    assert "LEGACY_VIEWS.map(([key, label])" in nav
    assert ".legacy-hof-views{grid-area:views;display:grid;grid-template-columns:repeat(4,minmax(0,1fr))" in styles
    assert ".legacy-hof-views button{display:flex;align-items:center;justify-content:center;gap:9px;min-height:46px" in styles
    assert "font-size:15px" in styles
