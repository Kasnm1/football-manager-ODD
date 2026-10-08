from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


ROOT = (Path(__file__).resolve().parents[1] / "src")


def commerce_probe() -> dict[str, object]:
    probe = r'''
const fs = require("fs");
const vm = require("vm");
const root = process.argv[1];
const context = {window:{FMODDI18nModules:[]}, document:{documentElement:{dataset:{}}, querySelector:()=>null, dispatchEvent:()=>{}}, NodeFilter:{SHOW_TEXT:4}, CustomEvent:class CustomEvent {}};
vm.createContext(context);
vm.runInContext(fs.readFileSync(root + "/web/i18n.commerce.js", "utf8"), context);
const module = context.window.FMODDI18nModules[0];
vm.runInContext(fs.readFileSync(root + "/web/i18n.js", "utf8"), context);
const i18n = context.window.FMODDI18n;
const keys = Object.fromEntries(Object.entries(module.messages).map(([locale, catalog]) => [locale, Object.keys(catalog).sort()]));
i18n.setLocale("en-GB", {persist:false, root:null});
const english = [i18n.t("bank.page", {page:2, total:7}), i18n.t("betting.upcoming", {days:3, count:8}), i18n.t("mail.page_summary", {page:2, pages:4, total:73}), i18n.t("analysis.record", {wins:8, losses:3, returns:1}), i18n.t("bank.overview"), i18n.t("bank.salary_schedule", {date:"Sunday"}), i18n.t("money.dialog.repay_source", {amount:"£2.00"}), i18n.t("bank.sugar_daddy.confirm_title")];
i18n.setLocale("ko-KR", {persist:false, root:null});
const korean = [i18n.t("bank.page", {page:2, total:7}), i18n.t("betting.upcoming", {days:3, count:8}), i18n.t("mail.page_summary", {page:2, pages:4, total:73}), i18n.t("analysis.record", {wins:8, losses:3, returns:1}), i18n.t("bank.overview"), i18n.t("bank.salary_schedule", {date:"Sunday"}), i18n.t("money.dialog.repay_source", {amount:"£2.00"}), i18n.t("bank.sugar_daddy.confirm_title")];
console.log(JSON.stringify({keys, messages:module.messages, english, korean}));
'''
    result = subprocess.run(
        ["node", "-e", probe, str(ROOT)], check=True, capture_output=True,
        text=True, encoding="utf-8",
    )
    return json.loads(result.stdout)


def placeholders(value: str) -> set[str]:
    import re

    return set(re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", value))


def test_commerce_catalog_locale_and_placeholder_parity() -> None:
    output = commerce_probe()
    keys = output["keys"]
    assert keys["en-GB"] == keys["zh-CN"] == keys["ko-KR"]
    for key in keys["en-GB"]:
        variants = [output["messages"][locale][key] for locale in ("en-GB", "zh-CN", "ko-KR")]
        assert placeholders(variants[0]) == placeholders(variants[1]) == placeholders(variants[2])
    assert output["english"] == [
        "Page 2 / 7", "Next 3 days · 8 matches",
        "Page 2 / 4 · 73 messages", "W 8 · L 3 · R 1",
        "Account overview", "Paid every Sunday · next Sunday",
        "Bank first, then wallet if needed; up to £2.00", "Confirm support type change",
    ]
    assert output["korean"] == [
        "2 / 7페이지", "향후 3일 · 8경기",
        "2 / 4페이지 · 총 73통", "승 8 · 패 3 · 반환 1",
        "계정 개요", "매주 일요일 지급 · 다음 Sunday",
        "은행에서 먼저 차감하고 부족하면 지갑에서 차감 · 최대 £2.00", "지원 유형 변경 확인",
    ]


def test_commerce_renderers_use_semantic_messages_not_source_chinese_copy() -> None:
    script = "\n".join(
        (ROOT / "web" / name).read_text(encoding="utf-8")
        for name in ("app.js", "bet_analysis_page.js")
    )
    for key in (
        'uiText("lottery.not_configured")',
        'uiText("league.slot_label", {slot:slot + 1})',
        'uiText("championship.team_stats"',
        'uiText("shop.confirm_purchase")',
        'uiText("bank.month_change")',
        'uiText("betting.outrights_note"',
        'uiText("results.subtitle"',
        'uiText("team_form.loading_with_standing"',
        'uiText("calendar.previous_month")',
        'uiText("mail.page_summary"',
        'uiText("sponsor.accept_warning")',
        'uiText("analysis.explanation"',
        'uiText("refund.order_summary"',
        'uiText("history.issue_missing"',
        'uiText("history.system_shape")',
        'uiText("history.table.bet_id")',
    ):
        assert key in script
    for literal in (
        '"待配置"', '"正在同步积分榜"', '"购物车是空的"',
        '"正在整理账单"', '"赛季冠军"', '"当前选择没有已完成赛果"',
        '"正在读取邮件"', '"正在整理收益数据"', '"退款处理中..."',
        '"球队近期战绩"', '"正在读取近期赛果"', '"当前读取范围内暂无最近赛果"',
        '"投注收益分析"', '"切换为卡片视图"', '"暂无未结算注单"',
    ):
        assert literal not in script


def test_bank_finance_renderers_use_semantic_messages_and_preserve_raw_values() -> None:
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    segment = script.split("function renderBankStatementDialog()", 1)[1].split("function openCreditDialog()", 1)[0]
    for key in (
        'uiText("bank.refresh_club")',
        'uiText("bank.dividend.schedule"',
        'uiText("bank.available_funds")',
        'uiText("bank.cashflow_income")',
        'uiText("bank.loan_summary"',
        'uiText("bank.credit_rules_dialog"',
        'uiText("money.dialog.recharge_title")',
        'uiText("money.dialog.transfer_budget_out_source"',
        'uiText("bank.sugar_daddy.confirm"',
        'uiText("bank.sugar_daddy.done"',
    ):
        assert key in segment
    assert 'transferBudget.error || uiText("bank.transfer_budget_unavailable")' in segment
    assert 'escapeHtml(transferBudgetStatus)' in segment
    assert 'error.message || uiText("bank.club_funds_read_failed")' in segment
    for literal in (
        '"账户总览"', '"可用资金总额"', '"现金流与收入"', '"钱包充值"',
        '"信用评级规则"', '"俱乐部资助类型无效"', '"正在修改"',
    ):
        assert literal not in segment


def test_betting_commerce_renderers_use_semantic_messages_for_assigned_flow() -> None:
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    segment = script.split("function renderCompetitions()", 1)[1].split("function marketLabel(", 1)[0]
    for key in (
        'uiText("betting.rail_competition")',
        'uiText("betting.group_favorites")',
        'uiText("betting.market_updated")',
        'uiText("market.match_result")',
        'uiText("betting.precision_score")',
        'uiText("betting.add")',
        'uiText("betting.not_ready")',
        'uiText("betting.payout_limit"',
        'uiText("betting.reuse_unavailable"',
    ):
        assert key in segment
    assert "uiLegacy(" not in segment
    assert re.search(r"[\u3400-\u9fff\uac00-\ud7a3]", segment) is None


def test_mail_dynamic_types_use_stable_localized_templates() -> None:
    output = commerce_probe()
    for key in (
        "mail.type.sponsor_payment.title",
        "mail.type.sponsor_payment.message",
        "mail.type.referee_integrity.title",
        "mail.type.referee_integrity.message",
        "mail.type.referee_integrity.body",
        "mail.type.doping_integrity.title",
        "mail.type.doping_integrity.message",
        "mail.type.doping_integrity.body",
        "mail.type.referee_apology.title",
        "mail.type.referee_apology.message",
        "mail.type.referee_apology.body",
        "mail.correspondent.manager_fallback",
        "mail.correspondent.referee_fallback",
    ):
        assert key in output["keys"]["en-GB"]
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    for mail_type in ("sponsorship_payment", "referee_integrity", "doping_integrity", "referee_apology"):
        assert f'{mail_type}:{{title:"mail.type.' in script
    assert '"mail.correspondent.manager_fallback"' in script
    assert '"mail.correspondent.referee_fallback"' in script


def test_locale_change_invalidates_dynamic_render_signatures_and_renders_open_dialogs() -> None:
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    listener = script.split('document.addEventListener("fmodd:localechange"', 1)[1]
    listener = listener.split("window.FMODDI18n?.applyShell()", 1)[0]
    assert 'key.endsWith("RenderSignature")' in listener
    assert "render();" in listener
    assert 'if ($("#history-dialog")?.open) renderHistory();' in listener
    assert 'if ($("#manual-refund-dialog")?.open) renderManualRefundDialog();' in listener
