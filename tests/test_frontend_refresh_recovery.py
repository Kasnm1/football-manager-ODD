from __future__ import annotations

import unittest
from pathlib import Path


ROOT = (Path(__file__).resolve().parents[1] / "src")


class FrontendRefreshRecoveryTests(unittest.TestCase):
    def test_home_connection_progress_stops_after_save_is_connected(self):
        view_model = self.script.split("function homeViewModel()", 1)[1].split(
            "function homeRenderStateSignature", 1,
        )[0]
        home = self.script.split("function renderHome()", 1)[1].split(
            "async function connectCurrentSave()", 1,
        )[0]
        connect = self.script.split("async function connectCurrentSave()", 1)[1].split(
            "function renderRefreshControls()", 1,
        )[0]

        self.assertIn("syncHomeConnectionProgress(connecting);", home)
        self.assertNotIn("syncHomeConnectionProgress(connecting || (connected && oddsBusy));", home)
        self.assertIn("liveRefreshProgressText()", view_model)
        self.assertIn("detail,", home)
        self.assertIn("connectButton.disabled = connecting || refreshControlBusy", home)
        self.assertIn('response.refresh_mode || "startup"', connect)

    def test_odds_display_uses_price_ladder_precision_without_repricing(self):
        helper = self.script.split("const formatOdds", 1)[1].split(
            "const formatAsianLine", 1,
        )[0]

        self.assertIn("return odds.toFixed(2);", helper)
        self.assertIn("if (market && odds > 5) return odds.toFixed(1);", helper)
        self.assertNotIn("Math.round", helper)

    def test_server_errors_are_localized_before_user_display(self):
        request_block = self.script.split("async function request", 1)[1].split(
            "function statePatchMatchesCurrentState", 1,
        )[0]

        self.assertIn("payload.error_detail_safe && payload.error", request_block)
        self.assertIn("localizeServerError(payload.error)", request_block)
        self.assertIn('"insufficient available balance":"可用余额不足"', request_block)
        self.assertIn('return `操作失败：${text}`;', request_block)
        self.assertNotIn("服务端返回了未翻译的错误", request_block)

    def test_mutating_requests_have_shared_busy_and_duplicate_protection(self):
        request_block = self.script.split("async function request", 1)[1].split(
            "function statePatchMatchesCurrentState", 1,
        )[0]

        self.assertIn('!["GET", "HEAD", "OPTIONS"].includes(method)', request_block)
        self.assertIn("app.mutationRequestsInFlight.has(mutationKey)", request_block)
        self.assertIn("beginMutationRequestFeedback()", request_block)
        self.assertIn("app.mutationRequestsInFlight.delete(mutationKey)", request_block)
        self.assertIn("该操作正在处理中，请勿重复点击", request_block)
        self.assertIn('control.setAttribute("aria-busy", "true")', self.script)
        self.assertIn(".request-busy::after", self.css)

    def test_all_interactive_controls_receive_immediate_press_feedback(self):
        feedback = self.script.split("const INSTANT_CLICK_FEEDBACK_SELECTOR", 1)[1].split(
            "async function saveFavoriteTeamIds", 1,
        )[0]

        for selector in ("button", "a[href]", "[role=button]", "summary", "[tabindex]"):
            self.assertIn(selector, feedback)
        self.assertIn('document.addEventListener("pointerdown"', feedback)
        self.assertIn('document.addEventListener("keydown"', feedback)
        self.assertIn('control.classList.add("instant-click-feedback")', feedback)
        self.assertIn("initializeInstantClickFeedback();", self.script)
        self.assertIn(".instant-click-feedback{", self.css)

    def test_async_result_cards_open_before_their_requests_finish(self):
        team = self.script.split("async function openTeamFormCard", 1)[1].split(
            "function dayBefore", 1,
        )[0]
        results = self.script.split("async function openResultsDialog", 1)[1].split(
            "async function loadTeamResults", 1,
        )[0]
        world_player = self.script.split("async function openWorldPlayerDetail", 1)[1].split(
            "function renderWorldPlayers", 1,
        )[0]

        self.assertLess(team.index("dialog.showModal()"), team.index("await loadTeamResults(teamId)"))
        self.assertIn('contentNode.setAttribute("aria-busy", "true")', team)
        self.assertIn("app.teamFormRequestKey !== requestKey", team)
        self.assertIn("data-team-form-retry", team)

        self.assertLess(results.index("dialog.showModal()"), results.index("await loadSeasonResults()"))
        self.assertIn('content.setAttribute("aria-busy", "true")', results)
        self.assertIn("data-results-retry", results)

        self.assertLess(world_player.index("dialog.showModal()"), world_player.index("await request("))
        self.assertIn("app.worldPlayerDetailRequestKey !== requestKey", world_player)
        self.assertIn("data-world-player-detail-retry", world_player)
        self.assertIn(".detail-loading-state", self.css)
        self.assertIn("prefers-reduced-motion:reduce", self.css)
        self.assertNotIn("窗口已打开", self.script)
        self.assertNotIn("读取完成后自动显示", self.script)

    def test_favorite_competitions_scope_filters_matches_and_results(self):
        source = self.script

        self.assertIn('data-competition="favorite_competitions_all"', source)
        self.assertIn('app.favoriteCompetitionIds.has(Number(match.competition_id))', source)
        self.assertIn('app.favoriteCompetitionIds.has(Number(result.competition_id))', source)

    def test_unemployed_manager_requires_confirmation_before_odds_refresh(self):
        prompt = self.script.split("async function maybeShowUnemployedPrompt", 1)[1].split(
            "async function switchManager", 1,
        )[0]

        self.assertIn("unemployed_confirmation_required", prompt)
        self.assertIn('request("/api/manager/unemployed-confirm"', prompt)
        self.assertIn("app.unemployedPromptKey = null", prompt)
        self.assertIn("确认无业并继续", prompt)
        self.assertIn("入职俱乐部后会自动识别并沿用当前账户", prompt)

    def test_manager_club_change_requires_confirmation_before_switch(self):
        prompt = self.script.split(
            "async function maybeShowManagerClubChangePrompt", 1,
        )[1].split("async function maybeShowUnemployedPrompt", 1)[0]

        self.assertIn("manager_club_change_confirmation", prompt)
        self.assertIn('request("/api/manager/club-change-confirm"', prompt)
        self.assertIn("是否切换到新俱乐部", prompt)
        self.assertIn("确认切换", prompt)
        self.assertLess(prompt.index("fmoddConfirm("), prompt.index("request("))

    def test_page_data_is_prepared_after_state_publish_and_navigation_intent(self):
        coordinator = self.script.split(
            "function dataResourceVersionKey", 1,
        )[1].split("function applyEconomySnapshot", 1)[0]
        state_load = self.script.split("async function loadStateOnce", 1)[1].split(
            "function setStateSyncFeedback", 1,
        )[0]
        events = self.script.split("function bindEvents", 1)[1]

        self.assertIn("scheduleDataPrewarm();", state_load)
        self.assertIn('pointerenter", () => schedulePageIntentPrewarm', events)
        self.assertIn('focusin", () => schedulePageIntentPrewarm', events)
        for page in (
            '"club"', '"world-clubs"', '"world-nations"',
            '"world-players"', '"hall-of-fame"', '"relations"',
        ):
            self.assertIn(page, coordinator)
        self.assertNotIn("loadOwnedClubMetrics", coordinator)
        self.assertNotIn("loadTransferHistory", coordinator)

    def test_manager_switcher_is_centered_without_an_internal_status_separator(self):
        self.assertIn(".topbar-center { display:flex; align-items:center;", self.css)
        self.assertIn(".topbar-center > span + span::before", self.css)
        self.assertNotIn(".topbar-center span + span::before", self.css)
        self.assertIn(
            ".manager-switcher > span:first-child { display:flex; align-items:center; height:28px;",
            self.css,
        )
        self.assertIn(".manager-switcher .fmodd-select { align-self:center;", self.css)

    def test_page_prewarm_is_scope_versioned_single_flight_and_side_effect_free(self):
        coordinator = self.script.split(
            "function dataResourceVersionKey", 1,
        )[1].split("function applyEconomySnapshot", 1)[0]
        players = self.script.split("async function loadWorldPlayers", 1)[1].split(
            "function worldPlayerRow", 1,
        )[0]
        legacy = self.script.split("async function loadClubLegacyIndex", 1)[1].split(
            "async function refreshHallOfFame", 1,
        )[0]
        show_page = self.script.split("function showPage", 1)[1].split(
            "function showInitialUsageNotice", 1,
        )[0]

        self.assertIn("app.pageDataRequests.get(key)", coordinator)
        self.assertIn("requestedVersion !== Number(app.state?.data_version || 0)", players)
        self.assertIn('requestQuery.set("background", "1")', players)
        self.assertIn("hydrate:!background", coordinator)
        self.assertIn("__backgroundRead:background", self.script)
        self.assertIn("!readOnlyPreview && !backgroundRead", self.script)
        self.assertIn("if (!background) void loadTransferHistory", legacy)
        self.assertIn("if (!background) toast", legacy)
        self.assertIn("loadClubLegacyIndex();", show_page)
        self.assertNotIn("loadClubLegacyIndex({force:true});", show_page)

    def test_odds_version_change_does_not_replay_all_page_prewarm(self):
        coordinator = self.script.split(
            "function dataPrewarmCycleKey", 1,
        )[1].split("function pageDataVersionIsCurrent", 1)[0]
        state_load = self.script.split("async function loadStateOnce", 1)[1].split(
            "function setStateSyncFeedback", 1,
        )[0]

        self.assertNotIn("data_version", coordinator)
        self.assertIn("const scopeKey = dataPrewarmCycleKey();", self.script)
        self.assertNotIn(
            "if (dataVersionChanged && !accountScopeChanged) cancelDataPrewarm();",
            state_load,
        )
        self.assertIn("schedulePageIntentPrewarm(app.page);", state_load)

    def test_status_poll_keeps_applied_version_until_full_state_sync(self):
        polling = self.script.split("async function pollRefreshStatus", 1)[1].split(
            "function beginPolling", 1,
        )[0]

        self.assertIn("const statusPatch = {...status};", polling)
        self.assertIn("if (dataVersionChanged) delete statusPatch.data_version;", polling)
        self.assertIn("if (dataVersionChanged || (wasBusy && !busy))", polling)

    def test_page_prewarm_retries_only_failed_resources_and_stops_while_hidden(self):
        coordinator = self.script.split("async function runDataPrewarm", 1)[1].split(
            "function applyEconomySnapshot", 1,
        )[0]

        self.assertIn("for (const page of [...app.dataPrewarmPendingPages])", coordinator)
        self.assertIn("app.dataPrewarmPendingPages.delete(page);", coordinator)
        self.assertIn("app.dataPrewarmRetryCounts.set(page, attempts);", coordinator)
        self.assertIn('if (document.visibilityState === "hidden") return;', coordinator)
        self.assertIn("if (!dataPrewarmCanRun())", coordinator)
        self.assertIn("DATA_PREWARM_RETRY_MAX_MS", coordinator)
        self.assertIn("2 ** Math.max(0, nextAttempt - 1)", coordinator)
        self.assertIn("const DATA_PREWARM_MAX_RETRIES = 8;", self.script)

    def test_automatic_prewarm_is_shallow_but_navigation_intent_is_deep(self):
        coordinator = self.script.split("async function preparePageData", 1)[1].split(
            "function applyEconomySnapshot", 1,
        )[0]
        legacy = self.script.split("async function loadClubLegacyIndex", 1)[1].split(
            "async function refreshHallOfFame", 1,
        )[0]

        self.assertIn("{background:true, deep:false}", coordinator)
        self.assertIn("{background:true, deep:true}", coordinator)
        self.assertIn("if (!deep) return true;", coordinator)
        self.assertIn("if (!deep && app.clubLegacyLoadPromise) return true;", coordinator)
        self.assertIn("includeDetails:deep", coordinator)
        self.assertIn("const loadDetails = includeDetails == null ? !background", legacy)
        self.assertIn("let payloads = [];", legacy)
        self.assertIn('loadDetails ? "details" : "index"', legacy)

    def test_heavy_hover_intent_waits_but_keyboard_intent_stays_fast(self):
        coordinator = self.script.split("function schedulePageIntentPrewarm", 1)[1].split(
            "function applyEconomySnapshot", 1,
        )[0]
        events = self.script.split("function bindEvents", 1)[1]

        self.assertIn("DATA_PREWARM_HEAVY_PAGES.has(page)", coordinator)
        self.assertIn("DATA_PREWARM_HEAVY_INTENT_DELAY_MS", coordinator)
        self.assertIn("{strong:true}", events)

    def test_background_hall_of_fame_details_are_yielded(self):
        legacy = self.script.split("async function loadClubLegacyIndex", 1)[1].split(
            "async function refreshHallOfFame", 1,
        )[0]

        self.assertIn("if (loadDetails && background)", legacy)
        self.assertIn("for (const teamId of clubIds)", legacy)
        self.assertIn("await waitForDataPrewarmIdle();", legacy)
        self.assertIn("payloads = await Promise.all", legacy)

    def test_background_relationship_entity_resolution_is_serial(self):
        network = self.script.split("async function loadRelationNetwork", 1)[1].split(
            "async function loadRelationshipPair", 1,
        )[0]

        self.assertIn("if (background)", network)
        self.assertIn("for (const personKeys of chunks)", network)
        self.assertIn("await waitForDataPrewarmIdle();", network)
        self.assertIn("resolvedChunks = await Promise.all(chunks.map(resolveChunk));", network)

    @classmethod
    def setUpClass(cls):
        cls.script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        cls.html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        cls.css = (ROOT / "web" / "app.css").read_text(encoding="utf-8")

    def test_settings_offer_favorite_competitions_instead_of_player_matches(self):
        odds_scope = self.html.split('id="odds-scope"', 1)[1].split("</select>", 1)[0]

        self.assertIn('<option value="favorite_schedule">收藏赛事</option>', odds_scope)
        self.assertNotIn("managed_schedule", odds_scope)
        self.assertNotIn("玩家比赛", odds_scope)

    def test_championship_setting_is_available_without_a_loaded_save(self):
        self.assertIn('id="championship-enabled"', self.script)
        self.assertIn('request("/api/settings/championship"', self.script)
        self.assertIn("settings.championship_enabled !== false", self.script)
        self.assertIn("if (championshipAvailable)", self.script)

    def test_settings_are_grouped_into_horizontal_top_tabs(self):
        tabs = self.script.split("const SETTINGS_TAB_GROUPS", 1)[1].split(
            "function activateSettingsTab", 1,
        )[0]
        startup = self.script.rsplit("ensureMoneyCurrencyControl();", 1)[1]

        for label in ("常规", "盘口", "显示", "作弊"):
            self.assertIn(f'label:"{label}"', tabs)
        self.assertNotIn('label:"存储支持"', tabs)
        for selector in (
            ".money-currency-setting", ".save-account-setting",
            ".full-refresh-setting",
            ".odds-reading-setting", "#championship-enabled",
            ".compact-money-setting", ".profit-loss-color-setting", "#show-hidden-attributes",
            ".cheat-setting", ".storage-setting", ".support-setting",
        ):
            self.assertIn(f'"{selector}"', tabs)
        general = tabs.split('key:"general"', 1)[1].split('key:"markets"', 1)[0]
        markets = tabs.split('key:"markets"', 1)[1].split('key:"display"', 1)[0]
        self.assertIn('".full-refresh-setting"', general)
        self.assertNotIn('".full-refresh-setting"', markets)
        self.assertIn("ensureSettingsTabs();", startup)
        self.assertLess(startup.index("ensureSettingsTabs();"), startup.index("bindEvents();"))
        self.assertIn(".settings-tabs { position:sticky; top:0;", self.css)
        self.assertIn(".settings-tabs button {", self.css)
        self.assertIn("display:flex", self.css.split(".settings-tabs {", 1)[1].split("}", 1)[0])
        self.assertIn("background:transparent", self.css.split(".settings-tabs button {", 1)[1].split("}", 1)[0])
        self.assertIn("border-bottom-color:#3f8465", self.css)

    def test_settings_tabs_are_text_only(self):
        tabs = self.script.split("const SETTINGS_TAB_GROUPS", 1)[1].split(
            "function activateSettingsTab", 1,
        )[0]
        self.assertNotIn("icon:", tabs)
        helper = self.script.split("function ensureSettingsTabs()", 1)[1].split(
            "function bindEvents", 1,
        )[0]
        self.assertIn("tab.textContent = uiText(group.labelKey);", helper)
        self.assertNotIn("data-lucide=\"${group.icon}\"", helper)

    def test_paid_feature_pages_share_bank_and_wallet_funding_summary(self):
        for target in (
            "#shop-balance", "#activity-balance", "#hospital-balance",
            "#training-balance", "#canteen-balance",
        ):
            self.assertIn(f'renderFundingSummary("{target}"', self.script)
        self.assertIn("function economyFundingSnapshot()", self.script)
        self.assertNotIn("银行优先 · 钱包补足", self.script)
        self.assertIn("fundingPaymentSplit(total)", self.script)
        self.assertIn("fundingPaymentSplit(price)", self.script)
        self.assertIn(".funding-summary{", self.css)

    def test_settings_operation_log_tracks_mutation_results_without_payloads(self):
        request = self.script.split("async function request", 1)[1].split(
            "function localizeServerError", 1,
        )[0]
        tabs = self.script.split("const SETTINGS_TAB_GROUPS", 1)[1].split(
            "function activateSettingsTab", 1,
        )[0]

        self.assertIn('key:"operations", label:"记录"', tabs)
        self.assertIn('selectors:[".operation-log-setting"]', tabs)
        self.assertIn('id="operation-log-content"', self.html)
        self.assertIn("beginOperationRecord(path, method)", request)
        self.assertIn("finishAcceptedOperationRecord(operationRecordId, path, payload)", request)
        self.assertIn('finishOperationRecord(operationRecordId, "failed"', request)
        record_helper = self.script.split("function beginOperationRecord", 1)[1].split(
            "async function request", 1,
        )[0]
        self.assertNotIn("fetchOptions.body", record_helper)
        self.assertNotIn("record.body", record_helper)

    def test_connection_and_refresh_logs_wait_for_background_completion(self):
        self.assertIn('const DEFERRED_OPERATION_PATHS = new Set([', self.script)
        self.assertIn('"/api/connect"', self.script)
        self.assertIn('"/api/refresh/full"', self.script)
        accepted = self.script.split("function finishAcceptedOperationRecord", 1)[1].split(
            "function reconcileDeferredOperationRecords", 1,
        )[0]
        self.assertIn("if (payload?.started === true) return;", accepted)
        self.assertIn('"后台任务未启动，请查看当前状态后重试"', accepted)
        reconciler = self.script.split("function reconcileDeferredOperationRecords", 1)[1].split(
            "async function request", 1,
        )[0]
        self.assertIn("state.refreshing || state.reconciling || state.connection_pending", reconciler)
        self.assertIn("if (connection.connected)", reconciler)
        self.assertIn('finishOperationRecord(record.id, "failed"', reconciler)
        load_state = self.script.split("async function loadStateOnce", 1)[1].split(
            "function setStateSyncFeedback", 1,
        )[0]
        self.assertIn("reconcileDeferredOperationRecords(nextState);", load_state)

    def test_settings_operation_log_fills_panel_and_hides_api_routes(self):
        renderer = self.script.split("function renderOperationRecords", 1)[1].split(
            "const SETTINGS_TAB_GROUPS", 1,
        )[0]
        route_labels = self.script.split("const OPERATION_ROUTE_LABELS", 1)[1].split(
            "function operationRouteTitle", 1,
        )[0]

        helper = self.script.split("function ensureSettingsTabs()", 1)[1].split(
            "function bindEvents()", 1,
        )[0]
        self.assertIn('panelStack.className = "settings-panels";', helper)
        self.assertIn("panelStack.append(panel);", helper)
        self.assertIn("settings.replaceChildren(tabs, panelStack);", helper)
        self.assertIn(".settings-content>.settings-panels { width:100%;", self.css)
        self.assertIn(
            '#settings-dialog>.settings-content[data-active-tab="operations"]>'
            '.settings-panels { min-height:0; flex:1 1 auto;',
            self.css,
        )
        self.assertIn('[data-settings-panel="operations"]{height:100%;', self.css)
        self.assertIn(
            ".settings-panel>.operation-log-setting{width:100%;height:100%;"
            "min-width:0;min-height:0;box-sizing:border-box;display:grid;"
            "grid-template-columns:minmax(0,1fr);",
            self.css,
        )
        self.assertIn("justify-content:stretch;justify-items:stretch;", self.css)
        self.assertIn(".operation-log-content{width:100%;", self.css)
        self.assertIn('"operations.route.learn_player_language", "operations.location.activity_language"', route_labels)
        self.assertIn('class="operation-log-location"', renderer)
        self.assertIn("operationRouteLocation(record.path)", renderer)
        self.assertNotIn("record.method", renderer)
        self.assertNotIn('<code>', renderer)

    def test_hidden_operation_log_stays_out_of_mutation_render_path(self):
        record_helper = self.script.split("function beginOperationRecord", 1)[1].split(
            "async function request", 1,
        )[0]
        renderer = self.script.split("function operationLogIsVisible", 1)[1].split(
            "const SETTINGS_TAB_GROUPS", 1,
        )[0]

        self.assertEqual(record_helper.count("renderOperationRecords({visibleOnly:true});"), 2)
        self.assertIn("if (visibleOnly && !operationLogIsVisible()) return;", renderer)
        self.assertIn("renderIcons(root);", renderer)
        self.assertNotIn("renderIcons();", renderer)

    def test_state_updates_render_only_the_active_page(self):
        page_renderer = self.script.split("function renderCurrentPage", 1)[1].split(
            "function render()", 1,
        )[0]
        main_renderer = self.script.split("function render()", 1)[1].split(
            "async function maybeShowIntegrityNotice", 1,
        )[0]
        patch_renderer = self.script.split("async function applyStatePatchOrReload", 1)[1].split(
            "function fmoddConfirm", 1,
        )[0]

        for page in ("betting", "shop", "inventory", "bank", "activity", "training", "club"):
            self.assertIn(f"{page}:", page_renderer)
        self.assertIn("else renderCurrentPage();", main_renderer)
        self.assertNotIn("renderShop(); renderLottery(); renderInventory();", main_renderer)
        self.assertIn("renderCurrentPage();", patch_renderer)
        self.assertNotIn("\n  render();", patch_renderer)

    def test_dynamic_icons_and_selects_are_scoped_to_changed_roots(self):
        custom_selects = self.script.split("function customSelectsWithin", 1)[1].split(
            "const round", 1,
        )[0]
        icons = self.script.split("function iconRenderRoots", 1)[1].split(
            "async function copyText", 1,
        )[0]

        self.assertIn("function customSelectMutationRoots(mutations)", custom_selects)
        self.assertIn("queueCustomSelectSync(roots)", custom_selects)
        self.assertNotIn('document.querySelectorAll("select").forEach(syncCustomSelect)', custom_selects)
        self.assertIn("function renderIcons(root = null)", icons)
        self.assertIn("root:scope", icons)

    def test_storage_and_support_belong_to_general_settings(self):
        tabs = self.script.split("const SETTINGS_TAB_GROUPS", 1)[1].split(
            "function activateSettingsTab", 1,
        )[0]
        general = tabs.split('key:"general"', 1)[1].split('key:"markets"', 1)[0]

        for selector in (".storage-setting", ".support-setting", ".author-update-note"):
            self.assertIn(f'"{selector}"', general)
        self.assertNotIn('icon:"settings"', general)

    def test_settings_panels_do_not_repeat_tab_titles(self):
        helper = self.script.split("function ensureSettingsTabs()", 1)[1].split(
            "function bindEvents()", 1,
        )[0]

        self.assertNotIn("settings-panel-intro", helper)
        self.assertNotIn("group.description", helper)
        self.assertNotIn("description:", self.script.split(
            "const SETTINGS_TAB_GROUPS", 1,
        )[1].split("function activateSettingsTab", 1)[0])
        self.assertNotIn(".settings-panel-intro", self.css)

    def test_short_general_selects_do_not_squeeze_labels(self):
        self.assertIn(
            ".money-currency-setting > label { flex:0 0 auto; white-space:nowrap; }",
            self.css,
        )
        self.assertIn(
            ".money-currency-setting > .fmodd-select { width:180px; min-width:180px; max-width:180px; flex:0 0 180px; }",
            self.css,
        )
        self.assertIn(".save-account-setting select { width:100%;", self.css)

    def test_settings_tabs_preserve_unclassified_future_controls(self):
        helper = self.script.split("function ensureSettingsTabs()", 1)[1].split(
            "function bindEvents()", 1,
        )[0]

        self.assertIn('panels.get("general").append(item)', helper)
        self.assertIn('role", "tablist"', helper)
        self.assertIn('role", "tabpanel"', helper)
        self.assertIn('["ArrowLeft", "ArrowRight", "Home", "End"]', helper)

    def test_final_refresh_state_retries_until_full_state_load_succeeds(self):
        self.assertIn("app.stateSyncPending = true;", self.script)
        self.assertIn("const loaded = await loadState({fresh:true});", self.script)
        self.assertIn("if (loaded) app.stateSyncPending = false;", self.script)
        self.assertIn('else beginPolling(1000, "status");', self.script)

    def test_read_requests_have_a_default_timeout_without_changing_mutation_policy(self):
        request = self.script.split("async function request", 1)[1].split(
            "function localizeServerError", 1,
        )[0]

        self.assertIn("!mutating ? 30000 : 0", request)
        self.assertIn("读取本地服务超时，界面已恢复", request)
        self.assertIn("操作响应超时，界面已恢复", request)

    def test_connected_save_keeps_refresh_status_polling_across_pages(self):
        helper = self.script.split("function maintainRefreshPolling()", 1)[1].split(
            "function beginStartupLoading", 1,
        )[0]
        page_switch = self.script.split("function showPage", 1)[1].split(
            "function showInitialUsageNotice", 1,
        )[0]

        self.assertIn('else if (isSaveConnected()) beginPolling(2200, "status");', helper)
        self.assertIn("maintainRefreshPolling();", page_switch)
        self.assertNotIn("stopPolling();", page_switch)

    def test_page_navigation_exposes_the_current_destination(self):
        page_switch = self.script.split("function showPage", 1)[1].split(
            "function showInitialUsageNotice", 1,
        )[0]

        self.assertIn('button.setAttribute("aria-current", "page")', page_switch)
        self.assertIn('button.removeAttribute("aria-current")', page_switch)
        self.assertIn(".main-sidebar nav button:focus-visible", self.css)

    def test_refresh_buttons_start_lightweight_status_polling(self):
        helper = self.script.split("function beginRefreshStatusPolling", 1)[1].split(
            "function beginStartupLoading", 1,
        )[0]
        refresh_handler = self.script.split(
            '$("#refresh-button").addEventListener', 1,
        )[1].split(
            '$("#market-full-refresh-button")', 1,
        )[0]
        market_full_handler = self.script.split(
            '$("#market-full-refresh-button")?.addEventListener', 1,
        )[1].split(
            '$("#settle-button")', 1,
        )[0]
        full_handler = self.script.split(
            '$("#full-refresh-button")?.addEventListener', 1,
        )[1].split(
            '$("#club-refresh-button")', 1,
        )[0]

        self.assertIn('beginPolling(ACTIVE_REFRESH_POLL_MS, "status")', helper)
        self.assertIn("const ACTIVE_REFRESH_POLL_MS = 250;", self.script)
        self.assertIn('beginRefreshStatusPolling("fast"', refresh_handler)
        self.assertNotIn("loadState", refresh_handler)
        self.assertIn('request("/api/refresh/full"', market_full_handler)
        self.assertIn('beginRefreshStatusPolling("full"', market_full_handler)
        self.assertNotIn("loadState", market_full_handler)
        self.assertIn("if (!isSaveConnected() || !app.state?.connection?.live)", full_handler)
        self.assertIn("await connectCurrentSave();", full_handler)
        self.assertLess(
            full_handler.index("await connectCurrentSave();"),
            full_handler.index("beginStartupLoading("),
        )
        self.assertIn('beginRefreshStatusPolling("full"', full_handler)
        self.assertNotIn("loadState", full_handler)

    def test_topbar_uses_server_progress_instead_of_simulated_98_percent(self):
        topbar = self.script.split("function renderTopbarRefreshStatus()", 1)[1].split(
            "function showPage", 1,
        )[0]
        progress_helper = self.script.split("function liveRefreshProgressText(", 1)[1].split(
            "function updateHomeConnectionProgress", 1,
        )[0]
        club = self.script.split("function renderClub()", 1)[1].split(
            "async function updateSalarySchedule", 1,
        )[0]

        self.assertIn("liveRefreshProgressText()", topbar)
        self.assertIn("state?.refresh_progress", progress_helper)
        self.assertIn("progress.completed", progress_helper)
        self.assertIn("progress.total", progress_helper)
        self.assertIn("progress.heartbeat_age_ms", progress_helper)
        self.assertNotIn("elapsed / 60000", topbar)
        self.assertNotIn("Math.min(98", topbar)
        self.assertNotIn("timedProgress", club)
        self.assertNotIn("elapsed / 60000 * 97", club)
        self.assertNotIn("Math.min(98", club)
        self.assertIn("const liveStatus = app.state?.club_status || {};", club)
        self.assertIn("后台核对完成后将自动继续", club)
        self.assertNotIn("remaining = 95", club)

    def test_refresh_progress_uses_localized_stage_codes_and_slow_hint(self):
        progress_helper = self.script.split("function liveRefreshProgressText(", 1)[1].split(
            "function updateHomeConnectionProgress", 1,
        )[0]

        self.assertIn('generate_odds:"正在读取赛程并生成盘口"', progress_helper)
        self.assertIn('persist_snapshot:"正在保存盘口快照"', progress_helper)
        self.assertIn("fallbackLabels[state?.refresh_stage]", progress_helper)
        self.assertNotIn("progress.text", progress_helper)
        self.assertNotIn("state.status", progress_helper)
        self.assertIn('" · 当前步骤耗时较长"', progress_helper)
        self.assertIn("`${text}${counter}${slowHint}`", progress_helper)

    def test_state_loads_are_coalesced_and_report_failure(self):
        self.assertIn("stateLoadPromise: null", self.script)
        self.assertIn("if (app.stateLoadPromise)", self.script)
        self.assertIn("return true;", self.script)
        self.assertIn("return false;", self.script)

    def test_integrity_notice_is_marked_read_when_displayed(self):
        block = self.script.split("async function maybeShowIntegrityNotice()", 1)[1].split(
            "function maybeShowCreditDefaultNotice()", 1
        )[0]
        self.assertLess(
            block.index("const alertClosed = fmoddIntegrityAlert(notice);"),
            block.index('await request("/api/integrity/notice/acknowledge"'),
        )
        self.assertLess(
            block.index('await request("/api/integrity/notice/acknowledge"'),
            block.index("await alertClosed;"),
        )

    def test_referee_notices_are_marked_read_when_displayed(self):
        debt = self.script.split("function maybeShowRefereeDebtNotice()", 1)[1].split(
            "function maybeShowRefereeIntegrityNotice()", 1
        )[0]
        integrity = self.script.split("function maybeShowRefereeIntegrityNotice()", 1)[1].split(
            "function managerOptionLabel", 1
        )[0]
        self.assertIn('action:"read"', debt)
        self.assertIn("!item.read", debt)
        self.assertIn('request("/api/mail/referee-integrity/acknowledge"', integrity)
        self.assertIn("app.refereeIntegrityNoticeId", integrity)
        self.assertIn("!item.read", integrity)

    def test_mutation_patches_are_version_and_save_scoped_with_full_fallback(self):
        self.assertIn("function statePatchMatchesCurrentState(response)", self.script)
        self.assertIn(
            "Number(sync.data_version) === Number(app.state.data_version)",
            self.script,
        )
        self.assertIn(
            'String(sync.data_scope_id || "") === String(app.state.data_scope_id || "")',
            self.script,
        )
        self.assertIn("return loadState({fresh:true});", self.script)
        self.assertIn(
            "patch.economy = {...(app.state.economy || {}), ...patch.economy};",
            self.script,
        )

    def test_low_risk_economy_mutations_apply_patches(self):
        for endpoint in (
            "/api/shop/buy-batch",
            "/api/inventory/destroy",
            "/api/inventory/activate",
            "/api/inventory/cancel",
            "/api/inventory/use-league-points",
            "/api/inventory/use-reputation",
            "/api/inventory/heal-player",
            "/api/inventory/fake-marrow",
            "/api/inventory/reverse-age",
            "/api/bank/borrow",
            "/api/cheat-balance",
            "/api/cheat-credit/clear",
        ):
            operation = self.script.split(endpoint, 1)[1].split("catch", 1)[0]
            self.assertIn("applyStatePatchOrReload", operation, endpoint)

    def test_active_refresh_and_club_polling_use_short_bounded_intervals(self):
        self.assertIn("const ACTIVE_REFRESH_POLL_MS = 250;", self.script)
        self.assertIn("const CLUB_REFRESH_POLL_MS = 500;", self.script)
        self.assertIn("const CLUB_REFRESH_POLL_ATTEMPTS = 240;", self.script)
        self.assertIn("setInterval(loadClub, CLUB_REFRESH_POLL_MS)", self.script)
        self.assertEqual(
            self.script.count("setTimeout(resolve, CLUB_REFRESH_POLL_MS)"), 3,
        )

    def test_operation_log_persistence_is_deferred_off_the_click_path(self):
        helper = self.script.split("function scheduleOperationRecordPersistence", 1)[1].split(
            "function beginOperationRecord", 1,
        )[0]
        record_helper = self.script.split("function beginOperationRecord", 1)[1].split(
            "async function request", 1,
        )[0]
        self.assertIn("queueMicrotask", helper)
        self.assertEqual(record_helper.count("scheduleOperationRecordPersistence();"), 2)
        self.assertNotIn("persistOperationRecords();", record_helper)

    def test_wallet_transfer_submits_the_displayed_account_scope(self):
        submit = self.script.split('$("#money-input-form")', 1)[1].split(
            '$("#credit-max")', 1,
        )[0]
        self.assertIn(
            'data_scope_id:String(app.state?.data_scope_id || "")', submit,
        )

    def test_clear_cache_never_requests_saved_data_deletion(self):
        self.assertIn("CLEAR_REBUILDABLE_CACHE", self.script)
        self.assertNotIn("CLEAR_ALL_SAVED_DATA", self.script)
        self.assertIn("账户存档不会删除", self.script)

    def test_delete_other_saves_requires_confirmation_and_preserves_current(self):
        self.assertIn("DELETE_OTHER_SAVE_FILES", self.script)
        self.assertIn('"settings.storage.delete_other.confirm"', self.script)
        self.assertIn("/api/storage/delete-other-saves", self.script)

    def test_delete_current_save_requires_two_confirmations(self):
        handler = self.script.split('$("#delete-current-save-button")', 1)[1].split(
            '$("#support-author-button")', 1,
        )[0]
        self.assertEqual(handler.count("await fmoddConfirm("), 2)
        self.assertIn("DELETE_CURRENT_SAVE_FILE", handler)
        self.assertIn("/api/storage/delete-current-save", handler)
        self.assertIn("不会删除 Football Manager 的 .fm 游戏存档", handler)

    def test_live_settlement_state_refreshes_every_five_seconds(self):
        self.assertIn(
            'app.state?.live_settlement?.monitoring) beginPolling(5000, "state")',
            self.script,
        )

    def test_open_hospital_refreshes_injuries_when_game_date_changes(self):
        clock_loader = self.script.split("async function loadGameClock()", 1)[1].split(
            "async function pollRefreshStatus()", 1,
        )[0]
        self.assertIn('if (app.page === "hospital"', clock_loader)
        self.assertIn("ensureManagedRostersLoaded(false)", clock_loader)

    def test_live_market_repaints_when_quotes_change_without_a_clock_tick(self):
        clock_loader = self.script.split("async function loadGameClock()", 1)[1].split(
            "async function pollRefreshStatus()", 1,
        )[0]
        quote_applier = self.script.split("function applyLiveClockQuotes(clock)", 1)[1].split(
            "function syncSelectionOddsFromMatches()", 1,
        )[0]

        self.assertIn("const liveQuotesChanged = applyLiveClockQuotes(clock);", clock_loader)
        self.assertIn("previous !== current || liveQuotesChanged", clock_loader)
        self.assertIn("let changed = false;", quote_applier)
        self.assertIn("return changed;", quote_applier)
        self.assertIn(
            "applyLiveClockQuotes(app.gameClock || app.state?.game_clock);",
            self.script,
        )

    def test_live_quote_change_detection_reuses_one_signature_per_fixture(self):
        quote_applier = self.script.split("function applyLiveClockQuotes(clock)", 1)[1].split(
            "function syncSelectionOddsFromMatches()", 1,
        )[0]

        self.assertIn("app.liveQuoteSignatures.get(key) !== quoteSignature", quote_applier)
        self.assertIn("app.liveQuoteSignatures.set(key, quoteSignature)", quote_applier)
        self.assertIn("const matchMap = bettingDataIndex().matchesByKey;", quote_applier)
        self.assertIn("const match = matchMap.get(selectionMatchKey(selection));", quote_applier)
        self.assertNotIn("const previousQuote = JSON.stringify", quote_applier)
        self.assertEqual(quote_applier.count("JSON.stringify(["), 1)

    def test_fixture_list_skips_unchanged_dom_rebuilds_and_coalesces_search_input(self):
        renderer = self.script.split("function fixtureListRenderSignature", 1)[1].split(
            "function fixtureRow(match)", 1,
        )[0]
        search_binding = self.script.split(
            '$("#match-search").addEventListener("input"', 1,
        )[1].split('$("#date-filters")', 1)[0]

        self.assertIn("function scheduleFixtureRender()", renderer)
        self.assertIn("requestAnimationFrame(() =>", renderer)
        self.assertIn("app.fixtureRenderSignature === renderSignature", renderer)
        self.assertLess(renderer.index("app.fixtureRenderSignature === renderSignature"), renderer.index("const grouped = new Map();"))
        self.assertIn("scheduleFixtureRender();", search_binding)
        self.assertNotIn("renderFixtures();", search_binding)

    def test_fixture_list_renders_in_bounded_batches(self):
        renderer = self.script.split("function fixtureListRenderSignature", 1)[1].split(
            "function fixtureRow(match)", 1,
        )[0]
        click_handler = self.script.split("async function handleFixturesClick(event)", 1)[1].split(
            "function fixtureListRenderSignature", 1,
        )[0]

        self.assertIn("const FIXTURE_RENDER_BATCH = 180;", self.script)
        self.assertIn("const fullVisible = filteredMatches();", renderer)
        self.assertIn("const visible = fullVisible.slice(0, renderLimit);", renderer)
        self.assertIn("data-load-more-fixtures", renderer)
        self.assertIn('event.target.closest("[data-load-more-fixtures]")', click_handler)
        self.assertIn("app.fixtureRenderLimit += FIXTURE_RENDER_BATCH;", click_handler)
        self.assertIn(".fixture-load-more", self.css)
        self.assertIn('<div class="fixture-load-more"><span aria-live="polite">', renderer)
        self.assertNotIn('<div class="fixture-load-more" role="status">', renderer)

    def test_state_load_coalesces_fresh_follow_up_requests(self):
        loader = self.script.split("async function loadState({fresh = false} = {})", 1)[1].split(
            "function freeServicesEnabled", 1,
        )[0]

        self.assertIn("stateReloadRequested: false", self.script)
        self.assertIn("if (fresh) app.stateReloadRequested = true;", loader)
        self.assertIn("return app.stateLoadPromise;", loader)
        self.assertIn("loaded = await loadStateOnce();", loader)
        self.assertIn("while (app.stateReloadRequested);", loader)

    def test_startup_waits_for_refresh_before_loading_full_state(self):
        bootstrap = self.script.split("async function bootstrapState()", 1)[1].split(
            "function beginPolling", 1,
        )[0]
        polling = self.script.split("async function pollRefreshStatus()", 1)[1].split(
            "async function bootstrapState()", 1,
        )[0]

        self.assertIn('request("/api/refresh/status"', bootstrap)
        self.assertIn("!status.cache_verified", bootstrap)
        self.assertIn("status.refreshing || status.reconciling || status.connection_pending", bootstrap)
        self.assertIn("!status.cache_verified", polling)
        self.assertIn('beginPolling(ACTIVE_REFRESH_POLL_MS, "status")', bootstrap)
        self.assertNotIn("beginStartupLoading();", bootstrap)
        self.assertIn("return loadState();", polling)
        self.assertIn("if (startupBusy)", polling)
        self.assertIn("bootstrapState(); loadGameClock();", self.script)
        self.assertNotIn("initializeTrainingDrag(); loadState(); loadGameClock();", self.script)

    def test_state_read_timeout_is_longer_and_read_only_message_is_accurate(self):
        request_block = self.script.split("async function request", 1)[1].split(
            "function statePatchMatchesCurrentState", 1,
        )[0]

        self.assertIn('path === "/api/state"\n    ? 60000', request_block)
        self.assertIn("存档资料仍在后台读取，界面会自动继续同步，请勿重复点击刷新", request_block)
        self.assertNotIn("操作结果可能已在游戏中生效", request_block)

    def test_state_sync_feedback_is_delayed_and_respects_reduced_motion(self):
        feedback = self.script.split("function setStateSyncFeedback(active)", 1)[1].split(
            "async function loadState({fresh = false} = {})", 1,
        )[0]

        self.assertIn("setTimeout(() =>", feedback)
        self.assertIn('classList.add("state-syncing")', feedback)
        self.assertIn('setAttribute("aria-busy", "true")', feedback)
        self.assertIn("}, 160);", feedback)
        self.assertIn(".state-syncing .topbar-center::after", self.css)
        reduced_motion = self.css.split("@media (prefers-reduced-motion: reduce)", 1)[1]
        self.assertIn(".state-syncing .topbar-center::after { animation:none; }", reduced_motion)

    def test_betting_surface_has_focus_and_reduced_motion_feedback(self):
        self.assertIn("#page-betting .date-heading", self.css)
        self.assertIn("#page-betting .odds-button:focus-visible", self.css)
        self.assertIn("#page-betting .fixture-row:focus-within", self.css)
        self.assertIn("@media (prefers-reduced-motion: reduce)", self.css)
        self.assertIn("#page-betting .odds-button:hover:not(:disabled)", self.css)

    def test_relative_date_filters_prefer_the_live_game_clock(self):
        fixture_filter = self.script.split("function filteredMatches()", 1)[1].split(
            "function isoDayAfter", 1,
        )[0]
        self.assertIn(
            'String(app.gameClock?.date || app.state?.game_clock?.date || output().game_date || "")',
            fixture_filter,
        )
        self.assertIn('app.selectedDate === "today"', fixture_filter)
        self.assertIn('app.selectedDate === "tomorrow"', fixture_filter)

    def test_mail_uses_server_pagination(self):
        self.assertIn("/api/mail?page=", self.script)
        self.assertIn("data-mail-page", self.script)
        self.assertIn("mail-pagination", self.script)

    def test_fixture_controls_use_one_stable_delegated_handler(self):
        self.assertIn('$("#fixtures").addEventListener("click", handleFixturesClick);', self.script)
        self.assertNotIn(
            'document.querySelectorAll(".quick-choice").forEach((button) => button.addEventListener("click"',
            self.script,
        )
        self.assertNotIn('container.querySelectorAll("[data-selection]").forEach', self.script)

    def test_refresh_removes_selections_for_missing_matches(self):
        self.assertIn('if (selection.type !== "championship") return matchMap.has(selectionMatchKey(selection));', self.script)
        self.assertIn('selection.market_version = String(competition.market_version || "");', self.script)
        self.assertIn('selection.odds = Number(team.odds);', self.script)

    def test_bet_request_is_save_scoped_and_recovers_from_repricing(self):
        self.assertIn("save_instance_id:output().save_instance_id", self.script)
        self.assertIn("if (result.repriced) await loadState({fresh:true});", self.script)
        self.assertIn("盘口已自动更新", self.script)
        self.assertIn('if (/盘口|刷新|存档已变化/.test(message))', self.script)

    def test_refresh_removes_selections_for_changed_lines(self):
        self.assertIn('selection.market === "AH"', self.script)
        self.assertIn('selection.market === "OU"', self.script)
        self.assertIn('selection.market === "HT_OU"', self.script)

    def test_refresh_reprices_every_retained_market_selection(self):
        self.assertIn("const refreshed = selectionFor(", self.script)
        self.assertIn("Object.assign(selection, refreshed);", self.script)
        self.assertIn(
            "if (!Number.isFinite(refreshed.odds) || refreshed.odds < 1.01)",
            self.script,
        )

    def test_manager_switch_keeps_current_market_snapshot(self):
        self.assertIn("已切换经理账户，沿用当前盘口", self.script)
        self.assertNotIn("正在切换经理并完整刷新", self.script)

    def test_betting_keypad_has_zero_shortcuts_and_half_button(self):
        self.assertIn(
            '"00","0","×2","maximum","K","M","B","÷2"',
            self.script,
        )
        self.assertIn('key === "clear" ? "keypad.clear"', self.script)
        self.assertIn('key === "maximum" ? "keypad.maximum"', self.script)
        self.assertIn(
            'const MONEY_ZERO_SHORTCUTS = Object.freeze({K:"000", M:"000000", B:"000000000"});',
            self.script,
        )
        self.assertIn(
            'const stakeMultipliers = {"×2":2,"÷2":0.5};',
            self.script,
        )
        self.assertNotIn('"×100":100', self.script)
        self.assertNotIn('"×1000":1000', self.script)
        self.assertNotIn('"×10000":10000', self.script)
        self.assertIn("Number(value) * stakeMultipliers[key]", self.script)

    def test_maximum_stake_uses_visible_wallet_balance_without_rounding_up(self):
        self.assertIn("const balance = availableWalletBalance();", self.script)
        self.assertIn(
            "Math.floor((toDisplayMoney(value) + Number.EPSILON) * scale) / scale",
            self.script,
        )
        self.assertIn(
            "value = String(toSpendableDisplayMoney(maximumStakeForSlip()))",
            self.script,
        )

    def test_wallet_status_poll_keeps_economy_balance_in_sync(self):
        self.assertIn(
            "applyEconomySnapshot({casino_balance:walletBalance}, {merge:true});",
            self.script,
        )

    def test_club_refresh_does_not_discard_derived_economy_status(self):
        load_club = self.script.split("async function loadClub()", 1)[1].split(
            "function clubProfileIsCurrent", 1,
        )[0]
        self.assertIn(
            "applyEconomySnapshot(payload.economy, {merge:true});",
            load_club,
        )

    def test_club_refresh_rejects_another_account_scope(self):
        load_club = self.script.split("async function loadClub()", 1)[1].split(
            "function clubProfileIsCurrent", 1,
        )[0]
        self.assertIn(
            "if (app.state && !accountPayloadMatchesCurrentState(payload)) return false;",
            load_club,
        )

    def test_economy_updates_keep_all_wallet_surfaces_in_sync(self):
        helper = self.script.split("function applyEconomySnapshot", 1)[1].split(
            "async function applyStatePatchOrReload", 1,
        )[0]
        self.assertIn("app.state.balance = walletBalance;", helper)
        self.assertIn('$("#balance").textContent = formatMoney(walletBalance);', helper)
        self.assertIn(
            '$("#wallet-dialog-balance").textContent = formatFullMoney(walletBalance);',
            helper,
        )
        self.assertIn('if (refreshBank && app.page === "bank") renderBank();', helper)
        self.assertNotIn("app.state.economy = response.economy", self.script)

    def test_wallet_status_poll_rejects_another_account_scope(self):
        self.assertIn('const statusWalletScope = String(status.wallet_scope_id || "");', self.script)
        self.assertIn('const currentWalletScope = String(app.state.data_scope_id || "");', self.script)
        self.assertIn(
            "(!currentWalletScope || statusWalletScope === currentWalletScope)",
            self.script,
        )

    def test_detailed_market_uses_extended_catalog_without_main_line_banner(self):
        for market in (
            "DOUBLE_CHANCE", "HT_AH", "HIGHEST_SCORING_HALF",
            "WINNING_MARGIN", "CLEAN_SHEET", "WIN_TO_NIL",
            "SH_1X2", "SH_AH", "SH_OU",
        ):
            self.assertIn(f'"{market}"', self.script)
        self.assertIn('panel("handicap", "让球盘"', self.script)
        self.assertNotIn("亚洲让球盘", self.script)
        self.assertNotIn("handicapCaption", self.script)
        self.assertIn('max="4" value="1" data-slider="half-total"', self.script)
        self.assertIn('max="7" value="2" data-slider="total"', self.script)
        self.assertIn('code = plus ? "three_plus" : "exact"', self.script)
        self.assertIn('fullTotalPlus ? "seven_plus" : "exact"', self.script)
        self.assertIn('panel("half_totals", "上半场大小球", halfTotals', self.script)
        self.assertIn('home_away:"非平局"', self.script)
        self.assertNotIn('home_away:"主胜或客胜"', self.script)
        self.assertIn('class="market-grid detailed-market-grid sportsbook-market-list"', self.script)
        self.assertIn(
            'class="bet-market-panel ${item.wide ? "wide-market" : ""} ${layoutClass}"',
            self.script,
        )

    def test_market_tabs_and_favorites_are_persistent(self):
        for key, label in (
            ("favorites", "收藏"), ("popular", "常用"), ("all", "全部"),
            ("full", "全场"), ("half", "半场"),
            ("precision", "精确进球"), ("result", "赛果"),
        ):
            self.assertIn(f'["{key}", "{label}"]', self.script)
        self.assertIn('["advance", uiText(placementMatch ? "betting.tab_winner" : "betting.tab_advance")]', self.script)
        self.assertIn('...(advancePanels.length ? [["advance", uiText(placementMatch ? "betting.tab_winner" : "betting.tab_advance")]] : [])', self.script)
        self.assertIn('app.marketTab === "advance" && !advancePanels.length ? "popular"', self.script)
        advance = self.script.split("const advancePanels =", 1)[1].split("const resultPanels", 1)[0]
        self.assertIn('placementMatch ? "最终获胜" : "最终晋级"', advance)
        self.assertIn('placementMatch ? "球队获胜方式" : "球队晋级方式"', advance)
        result = self.script.split("const resultPanels = [", 1)[1].split("];", 1)[0]
        self.assertNotIn('panel("advance"', result)
        self.assertIn('localStorage.setItem("fm-odds-market-tab"', self.script)
        self.assertIn('request("/api/favorites/markets"', self.script)
        self.assertIn('favorite_market_keys_initialized', self.script)
        self.assertIn('localStorage.setItem("fm-odds-favorite-markets-migrated"', self.script)
        self.assertIn('data-favorite-market="${item.key}"', self.script)

    def test_popular_and_half_market_order_matches_requested_workflow(self):
        popular = self.script.split("const popularPanels = [", 1)[1].split("];", 1)[0]
        expected = [
            "mainPanels[0]", "precisionPanels[2]", "precisionPanels[3]",
            "mainPanels[2]", "mainPanels[3]", "goalPanels[0]",
            "halfPanels[1]", "halfPanels[2]", "goalPanels[1]",
        ]
        positions = [popular.index(value) for value in expected]
        self.assertEqual(positions, sorted(positions))

        half = self.script.split("const halfPanels = [", 1)[1].split("];", 1)[0]
        self.assertLess(half.index('panel("half_full"'), half.index('panel("half_1x2"'))

    def test_half_settlements_use_effective_odds_and_specific_ticket_labels(self):
        self.assertIn("const settledLegOdds = (result, leg)", self.script)
        self.assertIn("result.return_multiplier ?? ((Number(leg.odds || 1) + 1) / 2)", self.script)
        self.assertIn("result.return_multiplier ?? 0.5", self.script)
        self.assertIn('singleOutcome === "half_won" ? "赢一半"', self.script)
        self.assertIn('singleOutcome === "half_lost" ? "输一半"', self.script)
        self.assertNotIn("splitOutcomeBadge", self.script)
        self.assertIn('void:"走水"', self.script)
        self.assertNotIn('void:"已走水"', self.script)
        self.assertIn(': "走水";', self.script)
        self.assertIn(': "走";', self.script)

    def test_pending_parlay_renders_partial_leg_outcomes_without_settling_ticket(self):
        pending_ticket = self.script.split("const pendingTicket = (record)", 1)[1].split(
            "const settledTicket = (record)", 1,
        )[0]
        self.assertIn("record.partial_settlement || []", pending_ticket)
        self.assertIn("pendingLegStatus(result)", pending_ticket)
        self.assertIn('won:"赢"', self.script)
        self.assertIn('half_won:"赢一半"', self.script)
        self.assertIn("${pendingStatus(record)}", pending_ticket)

    def test_shop_quantity_can_be_typed_on_product_and_cart(self):
        self.assertIn('data-quantity-input aria-label=', self.script)
        self.assertIn('data-cart-quantity aria-label=', self.script)
        self.assertIn(
            'setShopCartQuantity(control.dataset.shopQuantity, event.target.value)',
            self.script,
        )
        self.assertIn(
            'setShopCartQuantity(row.dataset.cartSku, input.value)',
            self.script,
        )
        self.assertIn('Math.floor(Number(quantity) || 0)', self.script)

    def test_economy_index_is_snapshot_scoped_and_preserves_first_duplicates(self):
        index = self.script.split("function economyDataIndex()", 1)[1].split(
            "function shopProduct", 1,
        )[0]

        self.assertIn(
            "app.economyIndexSource === economy && app.economyIndex", index,
        )
        self.assertIn("app.economyIndexSource = economy;", index)
        self.assertIn("if (!catalogBySku.has(product.sku))", index)
        self.assertIn("if (!descriptionsBySku.has(product.sku))", index)
        self.assertIn("if (!inventoryById.has(item.id))", index)
        self.assertIn("availableBySku.set(item.sku, skuRows)", index)

    def test_economy_surfaces_skip_unchanged_dom_rebuilds(self):
        shop = self.script.split("function renderShop()", 1)[1].split(
            "function renderBank()", 1,
        )[0]
        cart = self.script.split("function renderShopCart()", 1)[1].split(
            "function renderShop()", 1,
        )[0]
        inventory = self.script.split("function renderInventory()", 1)[1].split(
            "function currentManagedLeaguePointsTarget", 1,
        )[0]
        bank = self.script.split("function renderBank()", 1)[1].split(
            "function openMoneyDialog", 1,
        )[0]

        self.assertIn("app.shopContentSignature !== contentSignature", shop)
        self.assertIn("app.shopCartRenderSignature === signature", cart)
        self.assertIn("app.inventoryRenderSignature === signature", inventory)
        self.assertIn("app.bankRenderSignature === signature", bank)
        self.assertLess(
            cart.index("app.shopCartRenderSignature === signature"),
            cart.index('$("#shop-cart-items").innerHTML'),
        )
        self.assertLess(
            inventory.index("app.inventoryRenderSignature === signature"),
            inventory.index('$("#inventory-content").innerHTML'),
        )
        self.assertLess(
            bank.index("app.bankRenderSignature === signature"),
            bank.index('$("#bank-content").innerHTML'),
        )

    def test_inventory_operations_reuse_economy_indexes(self):
        inventory = self.script.split("function renderInventory()", 1)[1].split(
            "function currentManagedLeaguePointsTarget", 1,
        )[0]
        operations = self.script.split("function currentManagedLeaguePointsTarget", 1)[1].split(
            "function renderOwnedClubs", 1,
        )[0]

        self.assertIn("economyItem(button.dataset.activateItem)", inventory)
        self.assertIn("economyItem(button.dataset.cancelItem)", inventory)
        self.assertIn("economyItem(itemId)", operations)
        self.assertIn("availableEconomyItems(LEAGUE_POINTS_SKU)", operations)
        self.assertIn("availableEconomyItems(sku)", operations)

    def test_inventory_tabs_and_economy_motion_preferences_are_accessible(self):
        inventory = self.script.split("function renderInventory()", 1)[1].split(
            "function currentManagedLeaguePointsTarget", 1,
        )[0]
        reduced_motion = self.css.split("@media (prefers-reduced-motion: reduce)", 1)[1]

        self.assertIn('id="inventory-tabs" role="tablist"', self.html)
        self.assertEqual(self.html.count('data-inventory-tab='), 2)
        self.assertIn('button.setAttribute("aria-selected", String(selected))', inventory)
        for selector in ("#page-shop", "#page-inventory", "#page-bank", ".shop-cart-button"):
            self.assertIn(selector, reduced_motion)

    def test_network_error_never_mentions_development_service(self):
        self.assertIn("无法连接本地服务", self.script)
        self.assertNotIn("无法连接开发服务", self.script)

    def test_parlay_cards_do_not_claim_one_competition_for_the_whole_ticket(self):
        self.assertEqual(
            self.script.count('const competitionDetail = record.type === "parlay" || system'),
            2,
        )
        self.assertEqual(self.script.count("${competitionDetail}"), 2)

    def test_parlays_support_ten_matches_with_only_straight_nine_and_ten_folds(self):
        self.assertIn("selectedMatches.size >= 10", self.script)
        self.assertIn("串关和复式最多选择 10 场比赛", self.script)
        self.assertIn('9: {"9X1":[9]}', self.script)
        self.assertIn('10: {"10X1":[10]}', self.script)

    def test_second_half_exact_and_team_total_markets_are_rendered(self):
        for market in ("SH_TEAM_GOALS", "SH_TOTAL_GOALS", "SH_SCORE", "TEAM_OU"):
            self.assertIn(market, self.script)
        self.assertIn('class="market-average-ca"', self.script)

    def test_exact_score_and_goal_markets_unify_zero_zero_only(self):
        self.assertIn(
            'function specialMarketOdds(probability, pricingMode = "casino", divisor = 1.17)',
            self.script,
        )
        self.assertEqual(
            self.script.count(
                "app.pricingMode, homeGoals === 0 && awayGoals === 0 ? 1.17 : 1.26"
            ),
            2,
        )
        self.assertIn(
            "function exactScoreDivisor(matrix, targetReturnRate = 0.73)",
            self.script,
        )
        self.assertIn(
            "homeGoals === 0 && awayGoals === 0 ? 1.17 : exactScoreDivisor(matrix)",
            self.script,
        )
        self.assertIn(
            'code === "none" ? specialMarketOdds(scoreMatrix(match.xg.home, match.xg.away)[0][0], app.pricingMode)',
            self.script,
        )

    def test_client_generated_special_market_odds_use_the_server_price_ladder(self):
        helper = self.script.split("function quoteDecimalOdds", 1)[1].split(
            "function refreshSelectionUI", 1,
        )[0]

        self.assertIn(
            "const increment = clipped > 10 ? 1 : clipped > 5 ? 0.5 : 0.01;",
            helper,
        )
        self.assertNotIn("Math.min(1000", helper)
        self.assertIn(
            'return quoteDecimalOdds(pricingMode === "fair" ? rawFair : rawFair / divisor);',
            helper,
        )
        self.assertNotIn("/ 0.05", helper)

    def test_game_version_switch_does_not_open_startup_overlay(self):
        switcher = self.script.split("function renderGameVersionSwitcher(", 1)[1].split(
            "function maybeShowManagerPrompt()", 1,
        )[0]
        self.assertNotIn("beginStartupLoading(", switcher)
        self.assertIn("cancelStartupLoading();", switcher)
        self.assertIn("正在切换到 ${item.textContent}", switcher)

    def test_all_native_selects_are_upgraded_to_refresh_resilient_menus(self):
        self.assertIn("function customSelectsWithin(root = document)", self.script)
        self.assertIn('root?.querySelectorAll?.("select")', self.script)
        self.assertIn("function initializeCustomSelects()", self.script)
        initializer = self.script.split("function initializeCustomSelects()", 1)[1].split(
            'document.addEventListener("change"', 1,
        )[0]
        self.assertIn("syncCustomSelects();", initializer)
        self.assertIn("customSelectState.openKey = customSelectKey(select);", self.script)
        self.assertIn("const select = activeCustomSelect();", self.script)
        self.assertIn("if (root === document || selects.has(select)) renderCustomSelectMenu(select);", self.script)
        self.assertIn('menu.setAttribute("popover", "manual");', self.script)
        self.assertIn('menu.showPopover()', self.script)
        self.assertIn('menu.hidePopover()', self.script)
        self.assertIn('const host = select.closest("dialog[open]") || document.body;', self.script)
        self.assertIn("function customSelectMenuSignature(select)", self.script)
        self.assertIn("signature !== customSelectState.menuSignature", self.script)
        self.assertIn("!customSelectState.menuInteracting", self.script)
        self.assertIn("const placeBelow = below >= desiredHeight", self.script)
        self.assertIn("const availableHeight = Math.max(60", self.script)
        self.assertNotIn("below >= Math.min(menuHeight, 180)", self.script)
        self.assertIn("initializeCustomSelects(); bindEvents();", self.script)

        styles = (ROOT / "web" / "app.css").read_text(encoding="utf-8")
        self.assertIn(".fmodd-select-trigger {", styles)
        self.assertIn(".fmodd-select-menu { position:fixed;", styles)
        self.assertIn("inset:auto;", styles)
        self.assertIn("margin:0;", styles)
        self.assertIn(".fmodd-select-option.selected {", styles)
        self.assertIn("font-size:13px", styles)

    def test_font_select_options_preview_their_own_whitelisted_font(self):
        self.assertIn("function customSelectFontPreviewFamily(select, option)", self.script)
        self.assertIn('["chinese-font", "english-font"].includes(select.id)', self.script)
        self.assertIn("FONT_OPTIONS[option.value]?.source", self.script)
        self.assertIn('trigger.classList.toggle("font-preview-trigger"', self.script)

        styles = (ROOT / "web" / "app.css").read_text(encoding="utf-8")
        self.assertIn(
            ".fmodd-select-option.font-preview-option>span { font-family:var(--font-preview-family),sans-serif; }",
            styles,
        )
        self.assertIn(
            ".fmodd-select-trigger.font-preview-trigger .fmodd-select-value { font-family:var(--font-preview-family),sans-serif; }",
            styles,
        )

    def test_each_interface_language_has_a_scoped_font_list_and_default(self):
        for locale in ("en-GB", "zh-CN", "zh-TW", "ko-KR", "de-DE", "es-ES", "fr-FR", "ru-RU", "ja-JP", "pt-BR", "pt-PT"):
            self.assertIn(f'"{locale}": {{selectId:', self.script)
        self.assertIn('"en-GB": {selectId:"english-font", default:"segoe-ui"', self.script)
        self.assertIn('"zh-CN": {selectId:"chinese-font", default:"yahei-ui"', self.script)
        self.assertIn('"zh-TW": {selectId:"chinese-font", default:"yahei"', self.script)
        self.assertIn('"ko-KR": {selectId:"chinese-font", default:"malgun-gothic"', self.script)
        self.assertIn('"ja-JP": {selectId:"chinese-font", default:"yu-gothic-ui"', self.script)
        english_config = self.script.split('"en-GB": {selectId:', 1)[1].split("},", 1)[0]
        self.assertNotIn("yahei-ui", english_config)
        self.assertIn('"yahei-ui": "100%"', self.script)
        self.assertIn('"settings.font.family.yahei_ui"', self.script)
        self.assertIn('FONT_STORAGE_PREFIX = "fmodd-interface-font-"', self.script)

    def test_font_settings_can_restore_initial_families_without_weight_control(self):
        self.assertIn('id="reset-fonts"', self.html)
        self.assertIn('>恢复初始字体</button>', self.html)
        self.assertIn('resetButton.id = "reset-fonts";', self.script)
        handler = self.script.split('$("#reset-fonts").addEventListener("click"', 1)[1].split(
            '$("#font-size-slider").addEventListener', 1,
        )[0]
        self.assertIn("Object.keys(LOCALE_FONT_CONFIGS).forEach", handler)
        self.assertIn("Object.values(LEGACY_FONT_STORAGE_KEYS).forEach", handler)
        self.assertIn("app.interfaceFont = localeFontConfig(locale).defaultKey;", handler)
        self.assertIn("app.fontSizeStep = DEFAULT_FONT_SIZE_STEP;", handler)
        self.assertIn("localStorage.removeItem(FONT_SIZE_STORAGE_KEY);", handler)
        self.assertIn("localStorage.removeItem(FONT_SIZE_SCHEMA_KEY);", handler)
        self.assertIn("applyLocaleFontPreference(locale, app.interfaceFont);", handler)
        self.assertIn("applyFontSizePreference(app.fontSizeStep);", handler)
        self.assertIn('syncCustomSelects($(".font-settings"));', handler)
        self.assertNotIn("fontWeight", handler)

        styles = (ROOT / "web" / "app.css").read_text(encoding="utf-8")
        self.assertIn(".font-reset-button { min-height:38px;", styles)

    def test_unscaled_english_fonts_keep_native_weight_rendering(self):
        metric = self.script.split("function applyFontMetricScale", 1)[1].split(
            "function applyLocaleFontPreference", 1,
        )[0]
        self.assertIn('if (scale === "100%") {', metric)
        self.assertIn('root.style.setProperty("--fmodd-english-render-font", source);', metric)
        self.assertIn('englishFontMetricStyle.textContent = "";', metric)

    def test_default_font_size_does_not_add_data_specific_bump(self):
        self.assertNotIn("DATA_FONT_SIZE_BUMP_PX", self.script)
        self.assertIn("const size = Math.max(13, Math.round(Number(match[1]) * scale * 100) / 100);", self.script)

    def test_odds_controls_follow_font_weight_preference(self):
        styles = (ROOT / "web" / "app.css").read_text(encoding="utf-8")
        self.assertIn(".odds-button { min-height:36px;", styles)
        self.assertIn("font-size:13px; font-weight:700; }.odds-button:hover", styles)
        self.assertIn("font-size:13px; font-weight:700; text-align:center; }.market-choice", styles)
        self.assertIn("font-size:13px; font-weight:700; }.slider-add:hover", styles)
        self.assertNotIn(":root[data-font-weight] .odds-button", styles)

    def test_font_weight_setting_is_removed(self):
        self.assertNotIn('id="font-weight"', self.html)
        self.assertNotIn("文字粗细", self.script)
        self.assertNotIn("FONT_WEIGHT_STORAGE_KEY", self.script)
        self.assertNotIn("applyFontWeightPreference", self.script)

    def test_ui_locale_does_not_show_reload_note(self):
        control = self.script.split("function ensureUiLocaleControl", 1)[1].split(
            "function ensureFontControls", 1,
        )[0]
        self.assertIn('data-i18n="settings.language"', control)
        self.assertNotIn("切换后将重新载入界面，不会重启服务或修改游戏数据", control)
        self.assertNotIn("language_note", control)

    def test_default_font_uses_direct_system_font_stack(self):
        preferences = self.script.split("function applyLocaleFontPreference", 1)[1].split(
            "function storedFontSizeStep", 1,
        )[0]
        self.assertIn('root.style.setProperty("--fmodd-locale-font", FONT_OPTIONS[normalized].source);', preferences)
        self.assertIn("root.style.fontFamily = CJK_FONT_LOCALES.has(locale)", preferences)
        self.assertIn('"var(--fmodd-locale-font), var(--fmodd-english-render-font', preferences)

    def test_brand_mark_keeps_the_original_font_stack(self):
        styles = (ROOT / "web" / "app.css").read_text(encoding="utf-8")
        self.assertIn(
            '.brand { display:flex; align-items:center; gap:8px; font-family:"Microsoft YaHei UI","Segoe UI",sans-serif;',
            styles,
        )
        self.assertIn(
            'padding:0 2px; font-family:"Microsoft YaHei UI","Segoe UI",sans-serif; white-space:nowrap; }.sidebar-brand',
            styles,
        )

    def test_font_select_labels_avoid_duplicate_yahei_and_ui_suffixes(self):
        self.assertIn('label:"微软雅黑"', self.script)
        self.assertIn('label:"微软正黑体"', self.script)
        self.assertIn('label:"Segoe UI"', self.script)
        self.assertNotIn('"yahei-ui":"微软雅黑 UI", yahei:"微软雅黑"', self.script)
        self.assertNotIn('option value="yahei-ui">微软雅黑 UI</option>', self.html)
        self.assertNotIn('option value="yahei">微软雅黑</option>', self.html)
        self.assertIn('"settings.font.family.yahei_ui"', self.html)
        self.assertIn('"settings.font.family.jhenghei"', self.html)
        self.assertIn('"settings.font.family.segoe_ui"', self.html)

    def test_only_the_active_languages_font_control_is_visible(self):
        controls = self.script.split("function syncLocaleFontControls", 1)[1].split(
            "function ensureFontControls", 1,
        )[0]
        self.assertIn("label.hidden = !active;", controls)
        self.assertIn('const labelKey = isLatinControl ? "settings.font.latin_and_numbers" : "settings.font.language";', controls)
        self.assertIn("labelText.dataset.i18n = labelKey;", controls)
        self.assertIn("select.dataset.fontLocale = fontLocale;", controls)

    def test_custom_select_observer_ignores_its_own_trigger_text_updates(self):
        helper = self.script.split("function customSelectMutationRoots", 1)[1].split(
            "function initializeCustomSelects", 1,
        )[0]
        observer = self.script.split("function initializeCustomSelects", 1)[1].split(
            'document.addEventListener("change"', 1,
        )[0]

        self.assertIn('mutation.target.matches?.("select, option, optgroup")', helper)
        self.assertIn("mutation.addedNodes.forEach", helper)
        self.assertIn('node.querySelector?.("select")', helper)
        self.assertIn("const roots = customSelectMutationRoots(mutations);", observer)
        self.assertIn("queueCustomSelectSync(roots)", observer)
        observer_callback = observer.split("new MutationObserver", 1)[1]
        self.assertNotIn("syncCustomSelects();", observer_callback)

    def test_fixture_details_are_loaded_on_demand(self):
        self.assertIn('request("/api/match-markets"', self.script)
        self.assertIn("function matchMarketsLoaded(match)", self.script)
        self.assertIn("function loadMatchMarkets(match)", self.script)
        self.assertIn('match?.market_catalog_state === "full"', self.script)
        self.assertIn('match.market_catalog_state = "loading";', self.script)
        self.assertIn("if (app.stateLoadPromise) await app.stateLoadPromise;", self.script)
        self.assertIn("await loadState({fresh:true});", self.script)
        self.assertIn(
            "const currentMatch = bettingDataIndex().matchesByKey.get(key);",
            self.script,
        )
        self.assertIn("Object.assign(currentMatch, response.match || {});", self.script)
        self.assertNotIn("Object.assign(match, response.match || {});", self.script)
        self.assertIn('currentMatch.market_catalog_state = "core";', self.script)
        self.assertIn('if (!matchMarketsLoaded(match))', self.script)
        self.assertIn("loadMatchMarkets(match);", self.script)

    def test_integrity_notice_only_shows_explicit_penalty_waiver(self):
        block = self.script.split("function fmoddIntegrityAlert", 1)[1].split(
            "function fmoddRefereeDebtAlert", 1
        )[0]
        self.assertIn("const notice = item?.notice || item;", block)
        self.assertIn('localizedMailField(item || {}, "body")', block)
        self.assertIn('const waiverButton = $("#integrity-waive-fourth");', block)
        self.assertIn("const waiver = notice?.penalty_waiver;", block)
        self.assertIn("waiverButton.hidden = !waiver?.available && !waiver?.paid;", block)
        self.assertIn("body:JSON.stringify({mail_id:item?.id || notice?.id})", block)
        self.assertIn('/api/mail/item-integrity/waive', block)
        self.assertNotIn("fourth_waiver", block)
        self.assertNotIn('/api/integrity/waive-fourth', block)

        automatic = self.script.split("function maybeShowRefereeIntegrityNotice", 1)[1].split(
            "function managerOptionLabel", 1
        )[0]
        mail_click = self.script.split('$("#mail-content").addEventListener', 1)[1].split(
            '$("#settings-button")', 1
        )[0]
        self.assertIn("fmoddIntegrityAlert(mail);", automatic)
        self.assertIn("fmoddIntegrityAlert(mail);", mail_click)

        mail_params = self.script.split("function mailTemplateParameters", 1)[1].split(
            "function localizedMailField", 1
        )[0]
        self.assertIn("item?.amount || item?.annual_value || semanticParams.amount || 0", mail_params)
        self.assertIn("item?.player_name || semanticParams.names ||", mail_params)

    def test_update_reward_mail_requires_an_explicit_claim(self):
        alert = self.script.split(
            "function fmoddVersionUpdateRewardAlert", 1
        )[1].split("function fmoddRefereeDebtAlert", 1)[0]
        mail = self.script.split("function renderMail()", 1)[1].split(
            "async function loadMailPage", 1
        )[0]

        self.assertIn('/api/mail/version-update-reward/claim', alert)
        self.assertIn('claimButton.textContent = mail.claimed ? "已领取" : "领取 50M";', alert)
        self.assertIn('const reward = item.type === "version_update_reward";', mail)
        self.assertIn('item.claimed ? "已领取" : "待领取 · 50M"', mail)

    def test_purchase_confirmation_hides_stale_integrity_waiver_action(self):
        purchase_confirm = self.script.split(
            "function fmoddPurchaseConfirm", 1
        )[1].split("function fmoddAlert", 1)[0]

        self.assertIn('const waiverButton = $("#integrity-waive-fourth");', purchase_confirm)
        self.assertIn("waiverButton.hidden = true;", purchase_confirm)

    def test_unavailable_transfer_budget_shows_reason_and_refresh_action(self):
        bank = self.script.split("function renderBank()", 1)[1].split(
            "function openMoneyDialog", 1,
        )[0]
        self.assertIn('transferBudget.error || "当前没有可用的俱乐部转会预算"', bank)
        self.assertIn('transferBudget.available ? formatFullMoney(transferBudget.amount) : "暂不可用"', bank)
        self.assertIn('transferBudget.available && !transferBudget.read_only', bank)
        self.assertIn('transferBudget.status ||', bank)
        self.assertIn(": refreshClubButton;", bank)


if __name__ == "__main__":
    unittest.main()
