from pathlib import Path


ROOT = (Path(__file__).resolve().parents[1] / "src")


def test_random_match_intelligence_header_shows_score_window_hint():
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    facilities = (ROOT / "web" / "i18n.facilities.js").read_text(encoding="utf-8")

    assert 'activityHead("随机比赛情报", "开赛时间前1h有概率获取比分情报"' in script
    assert '"activity.intelligence_score_window_hint":["Score intelligence may become available within 1 hour before kick-off.", "开赛时间前1h有概率获取比分情报"' in facilities


def test_activity_centre_exposes_seventh_floor_intelligence_flow():
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    styles = (ROOT / "web" / "app.css").read_text(encoding="utf-8")
    economy = (ROOT / "tools" / "club_economy.py").read_text(encoding="utf-8")

    assert 'intelligence:{floor:"5F",name:"情报中心"' in script
    assert 'price:Number(centreState.intelligence?.price ?? 50000000)' in script
    assert 'kind:"match_intelligence"' in script
    assert 'request("/api/activity/intelligence")' in script
    assert 'request("/api/activity/intelligence/purchase"' in script
    assert 'name:"情报中心"' in script
    assert "收藏赛事或球队比赛的秘密情报，请在到达开赛时间时获取" not in script
    assert "天机阁" not in script
    assert "线人网络" not in script
    assert "当前报价 ${formatPounds(price)}。" in script
    assert '"activity.confirm_intelligence"' in script
    assert "申请密报" not in script
    assert "动态资金百分比报价" not in script
    assert "intelligence-pricing-note" not in script
    assert "data-intelligence-rate" not in script
    assert "data-intelligence-purchase" in script
    assert "data-intelligence-random" in script
    assert "tier.random_price" in script
    assert "tier.specific_price" in script
    assert "tier.random_description" not in script
    assert "tier.specific_description" not in script
    assert 'data-intelligence-mode="${mode}"' in script
    assert '"activity.intelligence_random_title"' in script
    assert '"activity.intelligence_specific_title"' in script
    assert '"activity.intelligence_obtained_title"' in script
    assert '"activity.intelligence_today"' in script
    assert '"activity.intelligence_previous"' in script
    assert 'data-intelligence-history-group="today"' in script
    assert 'data-intelligence-history-group="previous"' in script
    assert "旧存档中没有购买游戏日期的情报归入" not in script
    assert 'id="intelligence-specific-competition"' in script
    assert 'id="intelligence-specific-fixture"' in script
    assert 'id="intelligence-history-competition"' in script
    assert '<option value="all"' in script
    assert '<option value="${favoriteCompetitionKey}"' in script
    assert '<option value="${favoriteTeamKey}"' in script
    assert "收藏赛事" in script
    assert "收藏球队比赛" in script
    assert "app.favoriteCompetitionIds.has" in script
    assert "app.favoriteTeamIds.has" in script
    assert 'id="intelligence-search"' in script
    assert '"activity.intelligence_search_placeholder"' in script
    assert '"activity.intelligence_no_filtered"' in script
    assert 'app.intelligenceView = "obtained"' in script
    assert 'data-intelligence-toggle="${escapeHtml(fixtureId)}"' in script
    assert "可继续获取" not in script
    assert "当前没有未获取的赛事" not in script
    assert "到达关注比赛的开赛时间后，点击刷新预测。" not in script
    assert '"activity.intelligence_none_today"' in script
    assert '"activity.intelligence_none_previous"' in script
    assert "先选择情报类型，购买成功后揭晓随机比赛" not in script
    assert "付款后才会随机抽取一场" not in script
    assert "依次选择赛事和比赛，再选择情报类型" not in script
    assert "选择赛事和具体比赛后获取情报" not in script
    assert "开赛时获取收藏比赛情报" not in script
    assert "点击获取此项情报" not in script
    assert "今天购买的随机或指定比赛情报会显示在这里" not in script
    assert "旧记录和此前游戏日期购买的情报会显示在这里" not in script
    assert "正在读取当前开赛窗口" not in script
    assert 'summary ? `<p>${escapeHtml(summary)}</p>` : ""' in script
    obtained_tiers_start = script.index("const tiers = (candidate.tiers || []).map")
    purchase_tiers_start = script.index("const tierChoiceButtons", obtained_tiers_start)
    assert "tier.description" not in script[obtained_tiers_start:purchase_tiers_start]
    assert "tier.description" not in script[purchase_tiers_start:]
    assert "胜负情报" in economy
    assert '"总进球数情报"' in economy
    assert '"比分情报"' in economy
    assert "三个预测项目独立购买；今日价格固定，结果分别显示" not in script
    assert "intelligence-prediction-note" not in script
    assert "今日情报价格已锁定" not in script
    assert 'class="intelligence-price-lock' not in script
    obtained_start = script.index("const obtained = activityIndex.obtainedCandidates;")
    obtained_end = script.index('if (selectedActivity.kind === "club_media")', obtained_start)
    assert "priceLock" not in script[obtained_start:obtained_end]
    assert 'pricing.pricing_source === "single_betting_limit"' not in script
    assert '"单关投注上限" : "银行与钱包合计资金"' not in script
    assert "intelligencePricingDate !== gameDate" in script
    assert "reveal.goal_events" not in script
    assert "const eventLabel = (event)" not in script
    assert 'app.activityFloor === "intelligence"' in script
    assert "Number(candidate.kickoff_minutes) >= currentMinutes" not in script
    assert "const candidates = activityIndex.candidates;" in script
    assert 'selectedActivity.kind !== "match_intelligence"' in script
    assert '"activity.confirm_intelligence"' in script
    assert 'class="intelligence-refresh-button" data-intelligence-refresh' in script
    assert '"activity.refresh_intelligence"' in script
    assert '"activity.intelligence_random_title"' in script
    assert '"activity.intelligence_specific_title"' in script
    assert ".intelligence-refresh-button" in styles
    assert 'class="intelligence-history-tabs"' in script
    assert 'class="intelligence-history-toolbar"' in script
    assert 'class="intelligence-rail"' not in script
    assert 'class="intelligence-date-group"' in script
    assert ".intelligence-history-tabs" in styles
    assert ".intelligence-history-filters" in styles
    assert ".intelligence-rail" not in styles
    assert ".intelligence-competition-heading" not in styles
    assert "intelligence-favorite-filter" not in script
    assert "intelligence-favorite-filter" not in styles
    assert ".intelligence-search" in styles
    assert ".intelligence-price-lock" not in styles
    assert "button.intelligence-tier" in styles
    assert ".intelligence-date-group>header" in styles
    assert ".intelligence-list{display:block" in styles
    assert ".intelligence-card" in styles
    assert "grid-template-columns:repeat(3,minmax(0,1fr))" in styles
    assert ".intelligence-card.compact>.intelligence-card-body{display:none}" in styles
    assert ".intelligence-building .activity-scene{display:block;min-height:0;border-right:1px" in styles
    assert ".intelligence-building .activity-workspace{grid-template-columns:minmax(0,1fr) minmax(360px,410px)}" in styles
    assert ".intelligence-building .activity-floor-panel{width:100%;min-height:0;display:flex;flex-direction:column;overflow:hidden}" in styles
    assert ".intelligence-building .intelligence-card-toggle{grid-template-columns:minmax(0,1fr) 78px 18px}" in styles
    assert ".intelligence-building .activity-scene-node{flex-basis:175px;width:175px;grid-template-rows:1fr}" in styles
    assert "match-intelligence-7f-v1.png" in styles
    assert ".activity-scene.intelligence{background-color:#07110d" in styles
    assert "background-size:contain" in styles
    assert ".intelligence-purchase-card" in styles
    assert 'classList.toggle("has-intelligence-alert", view.intelligenceAvailable)' in script
    assert ".has-intelligence-alert::after" in styles
    assert "grid-template-columns:repeat(5,minmax(0,1fr))" in styles


def test_private_future_results_are_removed_before_snapshot_persistence():
    server = (ROOT / "fm_odds_web.py").read_text(encoding="utf-8")
    generator = (ROOT / "tools" / "preview_cup_odds.py").read_text(encoding="utf-8")

    assert '"_match_intelligence_results": match_intelligence_results' in generator
    pop_at = server.index('raw_output.pop("_match_intelligence_results"')
    save_at = server.index("save_odds(output, path)", pop_at)
    publish_at = server.index("self.output = output", save_at)
    assert pop_at < save_at < publish_at


def test_intelligence_notice_is_driven_by_background_probe_state():
    server = (ROOT / "fm_odds_web.py").read_text(encoding="utf-8")

    assert '"match_intelligence_notice": intelligence_notice' in server
    assert "purchasable_match_intelligence_candidates(" in server
    assert 'not bool(tier.get("purchased"))' in server
    assert 'purchase_mode="random" if random_purchase else "specific"' in server
    assert "watch_match_intelligence" in server
    assert "probe_match_intelligence_candidates" in server
    assert "and (kickoff is None or int(kickoff) < current_minutes)" in server
    public_start = server.index("def public_match_intelligence")
    public_end = server.index("def buy_match_intelligence", public_start)
    public_method = server[public_start:public_end]
    assert "_probe_match_intelligence_if_idle" not in public_method
    assert "with self.lock" not in public_method
    assert "set_active_save_id(scope_id)" in public_method
    assert "open_match_intelligence_status" in public_method
    assert "deepcopy(intelligence)" in public_method
