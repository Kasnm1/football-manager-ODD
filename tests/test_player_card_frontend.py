from __future__ import annotations

import unittest
from pathlib import Path

from frontend_source import read_frontend_source


ROOT = (Path(__file__).resolve().parents[1] / "src")


class PlayerCardFrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        cls.styles = (ROOT / "web" / "app.css").read_text(encoding="utf-8")
        cls.html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")

    def test_card_shows_number_and_cycles_verified_positions(self):
        self.assertIn("function playerCardBadge(player)", self.script)
        self.assertIn("player.shirt_number ?? player.player_contract?.squad_number", self.script)
        self.assertIn('data-player-position-cycle="${player.id}"', self.script)
        self.assertIn("cyclePlayerCardPosition(Number(badge.dataset.playerPositionCycle))", self.script)
        self.assertIn("Number(value) > 1", self.script)

    def test_club_player_number_precedes_name_without_covering_portrait(self):
        self.assertIn('player-card-portrait-frame', self.script)
        self.assertIn(
            'legacyPortraitMarkup(player, "club-player-portrait", {defer:Boolean(app.clubStatus?.refreshing), fallbackText:portraitFallback})',
            self.script,
        )
        self.assertIn("const portraitFallback = playerCardPositions(player)[0] || primaryPlayerPosition(player)", self.script)
        self.assertIn("const fallback = String(fallbackText || initial)", self.script)
        self.assertIn("if (!uid || defer)", self.script)
        self.assertIn('class="player-card-identity-prefix"', self.script)
        self.assertIn('${escapeHtml(badge)}#', self.script)
        self.assertIn('class="player-card-name"', self.script)
        self.assertNotIn('class="player-card-badge"', self.script)
        self.assertIn('.player-card-portrait-frame .legacy-hof-portrait', self.styles)
        self.assertIn(
            '.player-card-portrait-frame .club-player-portrait>span { width:auto; height:auto; font-size:16px; font-weight:700;',
            self.styles,
        )
        self.assertIn('.player-card-identity-prefix {', self.styles)
        self.assertNotIn('.player-card-badge {', self.styles)

    def test_club_players_use_native_squad_tabs(self):
        self.assertIn("function ownedPlayerSquadPayload(player)", self.script)
        self.assertIn("source_squad_team_id:sourceSquadTeamId", self.script)
        self.assertIn("function clubProfileSquads(profile)", self.script)
        self.assertIn("function selectedClubSquad(profile, selectedTeamId = null)", self.script)
        self.assertIn("squad.type_code != null && Number(squad.type_code) === 0", self.script)
        self.assertIn('data-club-squad', self.script)
        self.assertIn('data-owned-squad', self.script)
        self.assertIn('class="club-squad-tabs"', self.script)
        self.assertIn("主教练与各级球队", self.script)
        self.assertIn(".club-squad-tabs", self.styles)
        self.assertIn("俱乐部球员", self.html)
        self.assertNotIn("<span>一线球员</span>", self.html)

        owned_renderer = self.script.split(
            "function renderOwnedClubDetailContent", 1,
        )[1].split("function startWorldClubAcquisitionProgress", 1)[0]
        regular_renderer = self.script.split(
            "function renderWorldClubDetailContent", 1,
        )[1].split("async function openWorldClubDetail", 1)[0]
        self.assertIn(
            'content.querySelectorAll("[data-owned-squad]")',
            owned_renderer,
        )
        self.assertNotIn(
            'content.querySelectorAll("[data-owned-squad]")',
            regular_renderer,
        )

    def test_card_uses_compact_weekly_wage_and_asking_price(self):
        script = open(ROOT / "web" / "app.js", encoding="utf-8").read()
        self.assertIn("const unit = moneyAbbreviationUnit(amount);", script)
        self.assertIn('if (unit) return `£${compact(amount / unit[0], unit[1])}`;', script)
        self.assertIn("player.player_contract?.wage_per_week", script)
        self.assertIn("player.transfer?.asking_price ?? player.asking_price", script)
        self.assertIn('uiText("owned.detail.weekly_wage")', script)
        self.assertIn('uiText("world.club.player.value")', script)

    def test_card_long_facts_keep_their_full_reading_order_in_two_columns(self):
        renderer = self.script.split("function renderPlayerCard(player)", 1)[1].split(
            "function renderClubTeamNav", 1,
        )[0]
        self.assertIn("const nationalityLabel = playerNationalityLabel(player);", renderer)
        self.assertIn('title="${escapeHtml(nationalityLabel)}"', renderer)
        self.assertIn('title="${escapeHtml(wageLabel)}"', renderer)
        self.assertIn('class="people-card-grid player-card-grid"', self.script)
        self.assertIn(".player-card-grid { grid-template-columns:repeat(auto-fill,minmax(340px,1fr)); }", self.styles)
        self.assertIn(".player-card-facts { grid-template-columns:1fr 1fr; }", self.styles)
        self.assertIn(".player-card-facts span { min-height:56px;", self.styles)
        self.assertIn(".player-card-facts .nationality-value { display:block;", self.styles)
        self.assertNotIn("@container player-card", self.styles)

    def test_owned_club_cards_sync_vertical_financial_metrics(self):
        self.assertIn("async function loadOwnedClubMetrics", self.script)
        self.assertIn('class="owned-club-row-metrics"', self.script)
        self.assertIn('class="owned-club-row-kicker"', self.script)
        portfolio_card = self.script.split("if (portfolio)", 1)[1].split(
            'return `<article class="world-club-card">', 1,
        )[0]
        self.assertNotIn("<span>已收购俱乐部</span>", portfolio_card)
        self.assertNotIn('data-lucide="arrow-up-right"', portfolio_card)
        self.assertNotIn('data-lucide="badge-pound-sterling"', portfolio_card)
        self.assertIn('class="owned-club-row-sell"', portfolio_card)
        self.assertIn("background:#b5453e;color:#fff", self.styles)
        for label in (
            "当前估值", "财政结余",
            "本赛季营收", "负债", "联赛排名",
        ):
            self.assertIn(f'ownedClubMetric("{label}"', self.script)
        reputation_metric = self.script.split(
            "function ownedClubReputationMetric", 1,
        )[1].split("function worldClubCard", 1)[0]
        self.assertIn('class="owned-reputation-rank-metric"', self.script)
        self.assertIn('`${row.label} ${row.rank}/${row.total}`', reputation_metric)
        self.assertIn('.join("\\n")', reputation_metric)
        self.assertNotIn('.join(" · ")', reputation_metric)
        self.assertNotIn('`${row.label}第 ${row.rank}/${row.total}`', self.script)
        raw_script = read_frontend_source(ROOT / "web" / "app.js", mode="raw")
        acquisition_metric = raw_script.split(
            "function ownedClubAcquisitionMetric", 1,
        )[1].split("function playerNationalityLabel", 1)[0]
        self.assertIn("metrics?.net_acquisition_cost ?? metrics?.acquisition_price", acquisition_metric)
        self.assertIn('uiText("owned.detail.acquisition_cost")', acquisition_metric)
        self.assertNotIn("acquisition_refund", acquisition_metric)
        self.assertIn('uiText("portfolio.card.relative_to_price")', acquisition_metric)
        self.assertIn('<b class="owned-acquisition-change ${tone}"', self.script)
        self.assertIn('class="owned-acquisition-change-stack"', self.script)
        self.assertIn('<b class="owned-acquisition-percent ${tone}"', self.script)
        self.assertIn("change.percent", self.script)
        self.assertIn(".owned-acquisition-change-stack{display:grid;justify-items:start}", self.styles)
        self.assertIn(".owned-acquisition-change.profit,.owned-acquisition-percent.profit{color:var(--profit-red)}", self.styles)
        self.assertIn(".owned-acquisition-change.loss,.owned-acquisition-percent.loss{color:var(--loss-green)}", self.styles)
        self.assertIn(
            ":is(.owned-acquisition-metric,.owned-history-metric){padding-top:7px;padding-bottom:7px}",
            self.styles,
        )
        self.assertIn(".owned-club-row-metrics{grid-area:metrics", self.styles)
        self.assertIn("grid-template-columns:repeat(4,minmax(0,1fr))", self.styles)
        self.assertIn('grid-template-areas:"identity metrics view" "form metrics view"', self.styles)
        self.assertIn(".owned-club-row-identity h2{overflow-wrap:anywhere;font-size:26px", self.styles)
        self.assertIn(".owned-club-row-metrics dd{margin-top:5px;color:#173f31;font-size:17px", self.styles)
        self.assertIn(".owned-reputation-rank-metric dd{overflow:visible", self.styles)
        self.assertIn("text-overflow:clip;white-space:pre-line", self.styles)
        self.assertIn(".owned-club-row-view{display:flex;align-self:center;align-items:center;justify-self:end", self.styles)
        self.assertIn("align-self:center", self.styles)
        self.assertIn("min-width:86px;min-height:44px", self.styles)
        self.assertIn("margin:-22px 0", self.styles)
        self.assertIn("border-radius:0;background:#fff", self.styles)

    def test_owned_club_card_metrics_have_separated_equal_height_headers(self):
        self.assertIn(
            ".owned-club-row-card > dl.owned-club-row-metrics > div {",
            self.styles,
        )
        self.assertIn("grid-auto-rows: 1fr;", self.styles)
        self.assertIn("grid-template-rows: minmax(36px, auto) auto;", self.styles)
        self.assertIn("border: 1px solid #d8e5de;", self.styles)
        self.assertIn(".owned-club-row-metrics dt {", self.styles)
        self.assertIn("min-height: 36px;", self.styles)
        self.assertIn(".owned-club-row-metrics dd { margin: 9px 0 0; }", self.styles)

    def test_owned_club_cards_default_collapsed_and_expand_in_requested_order(self):
        card = self.script.split("function worldClubCard", 1)[1].split(
            "function activeOwnedClubs", 1,
        )[0]
        expected = [
            'ownedClubMetric("联赛排名"',
            'ownedClubMetric("转会预算"',
            'ownedClubMetric("财政结余"',
            'ownedClubMetric("预计分红"',
            'ownedClubMetric("当前估值"',
            "ownedClubAcquisitionMetric(metrics)",
            'ownedClubMetric("持有天数"',
            "ownedClubDebtMetric(metrics, club.id)",
            "ownedClubReputationMetric(club)",
            "${reputationMetric}",
            "${fanCountMetric}",
            'ownedClubMetric("本赛季营收"',
        ]
        positions = [card.index(value) for value in expected]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("ownedClubExpandedIds: new Set()", self.script)
        self.assertIn('data-owned-club-toggle="${teamId}"', card)
        self.assertIn('aria-expanded="${expanded}"', card)
        self.assertIn(".owned-club-row-card:not(.is-expanded)", self.styles)
        self.assertIn("div:nth-child(n+5)", self.styles)

    def test_owned_club_reputation_and_followers_show_adjacent_history(self):
        renderer = self.script.split("function ownedClubHistoricalMetric", 1)[1].split(
            "function playerNationalityLabel", 1,
        )[0]
        card = self.script.split("function worldClubCard", 1)[1].split(
            "function activeOwnedClubs", 1,
        )[0]
        self.assertIn("comparison?.change", renderer)
        self.assertIn("comparison?.change_percent", renderer)
        self.assertIn("暂无记录", renderer)
        self.assertIn('"相比上个月"', card)
        self.assertIn("metrics.reputation_comparison", card)
        self.assertIn('"相比上赛季"', card)
        self.assertIn("metrics.social_media_followers_comparison", card)
        self.assertIn('metrics.reputation ?? club.reputation', card)
        self.assertIn('metrics.fan_count_source === "social_media_followers"', card)
        self.assertIn(".owned-history-metric", self.styles)

    def test_owned_club_card_order_uses_a_scoped_reorder_mode(self):
        interactions = self.script.split(
            "function initializeOwnedClubCardInteractions", 1,
        )[1].split("async function loadOwnedClubMetrics", 1)[0]
        self.assertIn("function ownedClubOrderStorageKey()", self.script)
        self.assertIn('`fmodd-owned-club-order:${String(app.state?.data_scope_id || "default")}`', self.script)
        self.assertIn("localStorage.getItem(ownedClubOrderStorageKey())", self.script)
        self.assertIn("localStorage.setItem(ownedClubOrderStorageKey(), JSON.stringify(teamIds))", self.script)
        self.assertIn('data-owned-club-order="up"', self.script)
        self.assertIn('data-owned-club-order="down"', self.script)
        self.assertIn('data-lucide="arrow-up"', self.script)
        self.assertIn('data-lucide="arrow-down"', self.script)
        self.assertIn("ownedClubReorderMode: false", self.script)
        self.assertIn("data-owned-club-reorder-toggle", self.script)
        self.assertIn('aria-pressed="${app.ownedClubReorderMode}"', self.script)
        self.assertIn('status.textContent =', interactions)
        self.assertIn('card.querySelector("h2")?.textContent', interactions)
        self.assertIn("grid.insertBefore(card, previous)", interactions)
        self.assertIn("grid.insertBefore(next, card)", interactions)
        self.assertIn("saveOwnedClubOrder(grid)", interactions)
        self.assertNotIn("pointerdown", interactions)
        self.assertNotIn("owned-club-drag-handle", self.styles)

    def test_owned_club_collapsed_card_uses_inline_disclosure_without_misclicks(self):
        self.assertIn(".owned-club-row-card:not(.is-expanded) > .owned-club-row-identity {", self.styles)
        self.assertIn("padding: 13px 16px;", self.styles)
        self.assertIn("grid-template-rows: minmax(20px,auto) auto;", self.styles)
        self.assertIn("height: 36px;", self.styles)
        self.assertNotIn("transform: translate(-50%,50%);", self.styles)
        self.assertIn("data-owned-club-disclosure", self.script)
        self.assertIn('event.target.closest("button, a, input, select, textarea, label")', self.script)
        self.assertIn("app.ownedClubReorderMode", self.script)
        self.assertIn(
            ".owned-club-row-card:not(.is-expanded) > .owned-club-row-actions "
            ".owned-club-row-sell { display: none; }",
            self.styles,
        )
        self.assertIn(
            ".owned-club-row-card > dl.owned-club-row-metrics > div:first-child dd "
            "{ color: #173f31; }",
            self.styles,
        )
        card = self.script.split("function worldClubCard", 1)[1].split(
            "function activeOwnedClubs", 1,
        )[0]
        toggle = card.split('class="owned-club-row-toggle"', 1)[1].split("</button>", 1)[0]
        self.assertIn('data-lucide="chevron-${expanded ? "up" : "down"}"', toggle)
        self.assertIn("更多", toggle)
        self.assertIn("收起", toggle)
        self.assertNotIn("更多数据", toggle)
        self.assertIn("<span>", toggle)

    def test_detail_keeps_full_money_and_combines_nationalities(self):
        self.assertIn(
            "rows.map(localizedNationName).filter(Boolean).join(\" / \")",
            self.script,
        )
        self.assertIn("value != null ? formatPounds(value) : \"未读取\"", self.script)
        self.assertIn('["周薪", `${contractMoney(playerContract.wage_per_week)} / 周`]', self.script)
        self.assertIn('["标价", transfer.asking_price != null ? formatPounds', self.script)

    def test_player_detail_hides_removed_identity_and_contract_fields(self):
        detail = self.script.split("function openClubDetail", 1)[1].split(
            "function beginPlayerNameEdit", 1
        )[0]
        self.assertNotIn("<small>球员 ID</small>", detail)
        self.assertNotIn("person.registrations", detail)
        self.assertNotIn("bonuses_and_clauses", detail)
        for field in ("签字费", "忠诚奖金", "满意度"):
            self.assertNotIn(f'["{field}",', detail)
        for heading in ("注册情况", "奖金", "合同条款"):
            self.assertNotIn(f'<h4 class="detail-subheading">{heading}</h4>', detail)

    def test_player_detail_uses_sectioned_responsive_dashboard(self):
        detail = self.script.split("function openClubDetail", 1)[1].split(
            "function beginPlayerNameEdit", 1
        )[0]
        self.assertIn(
            'person, "player-detail-portrait", {fallbackText:detailPortraitFallback}',
            detail,
        )
        self.assertIn('class="player-detail-portrait-frame">${detailPortrait}', detail)
        self.assertNotIn('class="detail-position"', detail)
        self.assertIn(
            ".player-detail-portrait-frame { position:relative; flex:0 0 72px; width:72px; height:72px;",
            self.styles,
        )
        self.assertIn('class="player-detail-overview"', detail)
        self.assertIn('class="player-detail-attribute-block"', detail)
        self.assertIn('class="player-detail-columns"', detail)
        for heading in ("能力与位置", "比赛状态", "身体资料", "一般资料", "转会资料"):
            self.assertIn(f'"{heading}"', detail)
        self.assertIn(".player-detail-overview{display:grid;grid-template-columns:repeat(3", self.styles)
        self.assertIn(".player-attribute-group{padding:12px;border:1px", self.styles)
        self.assertIn(".player-detail-columns{display:grid;grid-template-columns:repeat(2", self.styles)
        self.assertIn("@media(max-width:640px)", self.styles)

    def test_staff_hidden_values_follow_display_setting(self):
        detail = self.script.split("function openClubDetail", 1)[1].split(
            "function beginPlayerNameEdit", 1
        )[0]
        self.assertIn(
            '...(app.showHiddenAttributes ? [["CA / PA",', detail,
        )
        self.assertIn(
            '${app.showHiddenAttributes && staffHiddenAttributes.length ?', detail,
        )

    def test_preferred_moves_render_as_named_tags(self):
        self.assertIn('class="preferred-move-tag"', self.script)
        self.assertNotIn("row.bit + 1", self.script)
        self.assertIn(".preferred-move-tags { display:flex; flex-wrap:wrap;", self.styles)
        self.assertIn(".preferred-move-tag { display:inline-flex;", self.styles)

    def test_card_marks_injuries_loans_and_uses_larger_fact_text(self):
        self.assertIn("player.availability?.injury_count", self.script)
        self.assertIn("player.availability?.injuries", self.script)
        self.assertIn("player.is_loaned_out || player.loan?.is_loaned_out", self.script)
        self.assertIn('class="player-injury-cross"', self.script)
        self.assertIn('loanedOut ? "loaned-out"', self.script)
        self.assertIn(".player-card .person-card-main h3.loaned-out { color:#164f86; }", self.styles)
        self.assertIn(".player-injury-cross {", self.styles)
        self.assertIn("width:22px; height:22px; border:1px solid #d64b43", self.styles)
        self.assertIn("border-radius:4px; background:#fff3f2; color:#bd3029", self.styles)
        fact_style = self.styles.split(".player-card-facts span {", 1)[1].split("}", 1)[0]
        self.assertIn("font-size:15px;", fact_style)

    def test_owned_club_player_move_dialog_is_wired_to_real_api(self):
        self.assertIn('id="owned-player-move-dialog"', self.html)
        self.assertIn('data-player-id="${Number(player.id)}"', self.script)
        self.assertIn('"/api/world-clubs/player-move"', self.script)
        self.assertIn('["player-transfer","player-loan"]', self.script)
        self.assertIn("至少还需要收购一家俱乐部", self.script)
        self.assertNotIn("fm24LoanUnavailable", self.script)
        self.assertNotIn("fm26LoanUnavailable", self.script)
        self.assertIn(
            'ownedClubAction("租借","calendar-arrow-down","player-loan")',
            self.script,
        )
        self.assertNotIn('action === "player-loan" && selectedGameIsFm26()', self.script)
        self.assertNotIn("FM24 暂按兼容转移处理", self.script)
        self.assertIn("toast(result.warning ||", self.script)
        self.assertIn("applyPlayerMovementTargetRefresh(result);", self.script)
        self.assertIn("result?.refreshed_targets", self.script)
        self.assertIn("result?.affected_team_ids", self.script)
        self.assertIn("error.code = payload.error_code", self.script)
        self.assertIn("error.phase = payload.error_phase", self.script)
        self.assertIn("error.retryable = Boolean(payload.retryable)", self.script)
        self.assertIn('const submitButton = $("#owned-player-move-confirm");', self.script)
        self.assertIn("submitButton.disabled = false;", self.script)

    def test_owned_club_player_squad_controls_follow_current_team(self):
        self.assertIn('id="owned-player-move-target-label"', self.html)
        self.assertIn('ownedClubAction("下调","arrow-down-to-line","player-demote")', self.script)
        self.assertIn('ownedClubAction("上调","arrow-up-to-line","player-promote")', self.script)
        self.assertIn('["player-promote","player-demote"]', self.script)
        self.assertIn('request("/api/world-clubs/player-squad"', self.script)
        self.assertIn("source_squad_team_id:Number(sourceSquad.team_id)", self.script)
        self.assertIn("target_squad_team_id:targetSquadTeamId", self.script)
        self.assertIn('if (promote) {', self.script)
        self.assertIn(
            '是否将「${player.name || player.id}」上调到一线队？', self.script,
        )
        promote_branch = self.script.split("if (promote) {", 1)[1].split(
            '$("#owned-player-move-title").textContent = "下调球员";', 1,
        )[0]
        self.assertIn("await moveToSquad(targets[0]);", promote_branch)
        self.assertNotIn("target.innerHTML", promote_branch)
        self.assertNotIn("dialog.showModal()", promote_branch)
        self.assertIn('$("#owned-player-move-note").hidden = true;', self.script)
        self.assertNotIn("主合同与注册队保持不变", self.script)

    def test_owned_player_release_is_visible_and_wired_to_real_api(self):
        self.assertNotIn('fm24PlayerReleaseAvailable', self.script)
        self.assertIn('ownedClubAction("解约","user-round-x","player-release","danger")', self.script)
        self.assertIn('if (action === "player-release")', self.script)
        self.assertIn('"/api/world-clubs/player-release"', self.script)
        self.assertIn('确认与「${player.name || player.id}」解除合同', self.script)
        self.assertIn('await fmoddAlert(error.message || "球员解约失败")', self.script)

    def test_owned_staff_release_is_wired_to_real_api(self):
        self.assertNotIn("staffReleaseAvailable", self.script)
        self.assertIn('data-staff-id="${Number(row.id)}"', self.script)
        self.assertIn(
            'isClubControllerJobType(row.job_type) ? "" : `${ownedClubAction("转会"',
            self.script,
        )
        self.assertIn(
            'ownedClubAction("解雇","user-round-x","staff-dismiss","danger")',
            self.script,
        )
        self.assertIn(
            "const managerActions = detail.manager && !detail.managerIsPlayer",
            self.script,
        )
        self.assertIn(
            'ownedClubAction("主教练下课","user-round-x","manager-dismiss","danger")',
            self.script,
        )
        self.assertIn('["staff-dismiss","manager-dismiss"]', self.script)
        self.assertIn('"/api/world-clubs/staff-release"', self.script)
        self.assertIn("staff_id:Number(staffId)", self.script)
        self.assertIn("确认解雇“${staff.name || staff.id}”", self.script)
        self.assertIn("detail.managerIsPlayer = Boolean(staff.manager_is_player)", self.script)
        self.assertIn("detail.manager && !detail.managerIsPlayer", self.script)

    def test_owned_club_async_loads_keep_the_latest_selected_tab(self):
        self.assertIn("detail.activeOwnedTab = tab;", self.script)
        self.assertGreaterEqual(
            self.script.count("detail.activeOwnedTab || activeTab"),
            4,
        )
        self.assertGreaterEqual(
            self.script.count('detail.activeOwnedTab || "transfers"'),
            2,
        )

    def test_owned_staff_is_sorted_by_native_job_type(self):
        staff_markup = self.script.split("const staffRows =", 1)[1].split(
            "const futureTransferRows =", 1,
        )[0]
        self.assertIn("left.job_type == null", staff_markup)
        self.assertIn("right.job_type == null", staff_markup)
        self.assertIn("Number.MAX_SAFE_INTEGER", staff_markup)
        self.assertIn('localeCompare(String(right.name || ""), activeUiLocale())', staff_markup)

    def test_owned_player_listing_is_wired_to_real_api(self):
        self.assertIn('"/api/world-clubs/player-list"', self.script)
        self.assertIn('if (action === "player-list")', self.script)
        self.assertIn('player.transfer?.transfer_listed ? "取消" : "挂牌"', self.script)
        self.assertIn("transfer_status_after", self.script)
        self.assertIn("await fmoddAlert(error.message", self.script)

    def test_owned_staff_transfer_is_wired_to_real_api(self):
        self.assertIn("function openOwnedStaffMoveDialog(", self.script)
        self.assertIn('if (action === "staff-transfer")', self.script)
        self.assertIn('"/api/world-clubs/staff-move"', self.script)
        self.assertIn("staff_id:Number(staffId)", self.script)
        self.assertIn("至少还需要收购一家俱乐部才能转会职员", self.script)
        self.assertIn('await fmoddAlert(error.message || "职员转会失败")', self.script)

    def test_owned_club_finance_buttons_use_verified_transaction_api(self):
        self.assertIn("function openOwnedClubFinanceDialog(", self.script)
        self.assertIn('"/api/world-clubs/finance-transfer"', self.script)
        self.assertIn('"/api/bank/club-funds-quote"', self.script)
        quote = self.script.split('"/api/bank/club-funds-quote"', 1)[1].split(
            '$("#money-input-dialog").showModal()', 1,
        )[0]
        self.assertIn("liveClubFunds.amount", quote)
        self.assertIn('field:ownedClubFinance.field', self.script)
        self.assertIn('response.owned_club_finance.amount', self.script)
        self.assertIn('if (detail.league_available === false)', self.script)
        self.assertIn('toast("请开启此俱乐部联赛后再进行资金操作")', self.script)
        self.assertIn('activeTab, field, direction, button = null', self.script)
        self.assertIn('runOwnedClubOperation(button, "读取额度"', self.script)
        self.assertIn('timeoutMessage:"读取俱乐部资金超时，按钮已恢复，请刷新俱乐部后重试"', self.script)
        self.assertIn("timeoutMs:180000", quote)
        request = self.script.split("async function request", 1)[1].split(
            "function localizeServerError", 1,
        )[0]
        self.assertNotIn('/api/world-clubs/finance-transfer', request)

    def test_owned_operation_buttons_share_one_width(self):
        self.assertIn(
            ".owned-operation-card>.owned-upgrade-list .owned-club-action{"
            "flex:0 1 200px;width:200px;max-width:100%}",
            self.styles,
        )

    def test_owned_player_opens_shared_detail_card_and_supports_aliases(self):
        self.assertIn('if (action === "player-detail")', self.script)
        self.assertIn('openClubDetail("player", playerId, {players:detail.players || []}', self.script)
        self.assertIn('"/api/world-clubs/player-name"', self.script)
        self.assertIn('if (ownedClub) body.team_id = Number(options.teamId)', self.script)

    def test_owned_club_team_uses_season_rating_injury_and_manager_records(self):
        self.assertIn("function ownedPlayerSeasonRating(player)", self.script)
        player_rows = self.script.split("const playerRows =", 1)[1].split(
            "const staffRows =", 1,
        )[0]
        self.assertIn("const abilityMetrics = app.showHiddenAttributes", player_rows)
        self.assertIn('ownedClubMetric("CA", value(player.ca))', player_rows)
        self.assertIn('ownedClubMetric("PA", value(player.pa))', player_rows)
        self.assertIn("<dl>${abilityMetrics}", player_rows)
        self.assertIn('ownedClubMetric("本赛季评分", ownedPlayerSeasonRating(player))', self.script)
        self.assertNotIn('ownedClubMetric("体能", value(player.fitness_percent, "%"))', self.script)
        self.assertIn("function ownedPlayerInjuryStatus(player)", self.script)
        self.assertIn('ownedClubMetric("伤病情况", injury.label, injury.tone)', self.script)
        self.assertIn("availability.injuries", self.script)
        self.assertIn("function ownedManagerSummary(record)", self.script)
        self.assertIn("执教期间", self.script)
        self.assertIn(".owned-manager-summary{", self.styles)
        self.assertIn(".owned-player-row dl div.injured", self.styles)

    def test_owned_club_staff_cards_prioritize_role_and_wage(self):
        staff_markup = self.script.split("const staffRows =", 1)[1].split(
            "const futureTransferRows =", 1,
        )[0]
        self.assertNotIn('ownedClubMetric("CA"', staff_markup)
        self.assertNotIn('ownedClubMetric("PA"', staff_markup)
        self.assertIn('ownedClubMetric("职位", localizedStaffRole(row))', staff_markup)
        self.assertIn(
            'ownedClubMetric("周薪", row.wage_display == null ? "未读取" : '
            'formatPounds(row.wage_display))',
            staff_markup,
        )
        self.assertIn('class="owned-staff-facts"', staff_markup)
        self.assertIn(".owned-staff-row .owned-staff-facts dd{font-size:16px", self.styles)
        self.assertIn(".owned-staff-row .owned-row-actions{justify-content:start}", self.styles)

    def test_current_contract_card_shows_player_manager_record(self):
        self.assertIn("const managerRecord = club.manager_record?.total", self.script)
        self.assertIn("玩家主教练执教战绩", self.script)
        self.assertIn("managerRecord.win_rate", self.script)
        self.assertIn('class="record-win"', self.script)
        self.assertIn('class="record-draw"', self.script)
        self.assertIn('class="record-loss"', self.script)
        self.assertIn('class="record-rate"', self.script)
        self.assertIn(".contract-manager-record {", self.styles)

    def test_owned_player_and_staff_rows_align_metrics_and_actions(self):
        self.assertIn(".owned-player-row{--owned-row-action-width:88px}", self.styles)
        self.assertIn("--owned-row-action-width:104px", self.styles)
        self.assertIn(
            ".owned-player-row dl,.owned-staff-row dl{width:100%;justify-self:stretch}",
            self.styles,
        )
        self.assertIn(
            ".owned-player-row dt,.owned-player-row dd,.owned-staff-row dt,"
            ".owned-staff-row dd{text-align:left}",
            self.styles,
        )
        self.assertIn(
            ".owned-player-row{grid-template-columns:minmax(180px,1fr) "
            "minmax(260px,1fr) 464px}",
            self.styles,
        )
        self.assertIn(
            ".owned-staff-row{grid-template-columns:minmax(220px,1fr) "
            "minmax(300px,1.25fr) 214px}",
            self.styles,
        )

    def test_group_information_precedes_shareholder_report_and_starts_empty(self):
        deck = self.script.split("function portfolioControlDeck", 1)[1].split(
            "function portfolioToolShell", 1,
        )[0]
        self.assertLess(deck.index('"集团信息"'), deck.index('"股东报告"'))
        self.assertIn('information:["GROUP PROFILE", "集团信息", ""]', self.script)
        self.assertIn('if (kind === "information")', self.script)
        self.assertIn("function renderGroupInformation(content)", self.script)
        self.assertIn("portfolioSummaryMetrics(clubs, summary)", self.script)
        self.assertIn('ownedClubMetric("当前估值"', self.script)
        self.assertIn('ownedClubMetric("全部俱乐部结余"', self.script)
        self.assertIn('ownedClubMetric("累积收购成本"', self.script)
        self.assertIn('ownedClubMetric("本赛季营收"', self.script)
        self.assertIn('ownedClubMetric("本月预计分红"', self.script)
        self.assertIn('ownedClubMetric("已收购俱乐部数量"', self.script)
        self.assertIn("data-group-rename", self.script)
        self.assertIn('request("/api/world-clubs/group-name"', self.script)
        self.assertIn("group-rename-dialog", self.html)
        self.assertIn("我的集团", self.html)
        self.assertNotIn('"集团调配"', self.script)
        self.assertNotIn("function renderPortfolioPeople", self.script)


if __name__ == "__main__":
    unittest.main()
