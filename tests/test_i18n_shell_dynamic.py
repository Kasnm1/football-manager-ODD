from __future__ import annotations

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def run_catalog_probe() -> dict[str, object]:
    probe = r'''
const fs = require("fs");
const vm = require("vm");
const root = process.argv[1];
const context = {
  window: {FMODDI18nModules: []},
  document: {documentElement: {dataset: {}}, querySelector: () => null, dispatchEvent: () => {}},
  NodeFilter: {SHOW_TEXT: 4},
  CustomEvent: class CustomEvent {},
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(root + "/web/i18n.shell.js", "utf8"), context);
const module = context.window.FMODDI18nModules[0];
vm.runInContext(fs.readFileSync(root + "/web/i18n.js", "utf8"), context);
const i18n = context.window.FMODDI18n;
const keys = Object.fromEntries(Object.entries(module.messages).map(([locale, catalog]) => [locale, Object.keys(catalog).sort()]));
i18n.setLocale("en-GB", {persist:false, root:null});
const english = i18n.t("manager.club_change.message", {manager:"Alex", previous:"Old Town", next:"New Town"});
i18n.setLocale("ko-KR", {persist:false, root:null});
const korean = i18n.t("manager.club_change.message", {manager:"Alex", previous:"Old Town", next:"New Town"});
console.log(JSON.stringify({keys, english, korean}));
'''
    result = subprocess.run(
        ["node", "-e", probe, str(ROOT)],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return json.loads(result.stdout)


def run_refresh_progress_probe() -> dict[str, str]:
    probe = r'''
const fs = require("fs"), vm = require("vm"), root = process.argv[1];
const html = fs.readFileSync(root + "/web/index.html", "utf8");
const sources = [...html.matchAll(/<script\s+src="([^"]+)"/g)].map((match) => match[1]);
const context = {
  window:{FMODDI18nModules:[],FMODDLocalePacks:{}},
  document:{documentElement:{dataset:{}},querySelector:()=>null,dispatchEvent:()=>{}},
  NodeFilter:{SHOW_TEXT:4}, CustomEvent:class CustomEvent {},
};
vm.createContext(context);
for (const source of sources) {
  if (!source.startsWith("/i18n") || source === "/i18n.js") continue;
  vm.runInContext(fs.readFileSync(root + "/web" + source, "utf8"), context, {filename:source});
}
vm.runInContext(fs.readFileSync(root + "/web/i18n.js", "utf8"), context);
const appSource = fs.readFileSync(root + "/web/app.js", "utf8");
const helper = appSource.slice(
  appSource.indexOf("function liveRefreshProgressText("),
  appSource.indexOf("function updateHomeConnectionProgress"),
);
vm.runInContext('const uiText = (key, parameters = {}) => window.FMODDI18n.t(key, parameters);', context);
vm.runInContext(helper, context);
context.window.FMODDI18n.setLocale("ko-KR", {persist:false, root:null});
context.app = {state:{}};
context.sourceState = {
  refreshing:true,
  refresh_stage:"championship_markets",
  refresh_progress:{text:"正在生成并结算冠军盘",completed:3,total:10},
};
context.unknownState = {
  refreshing:true,
  refresh_stage:"future_unknown_stage",
  refresh_progress:{text:"后端中文不得显示"},
};
console.log(JSON.stringify({
  known:vm.runInContext("liveRefreshProgressText(sourceState)", context),
  unknown:vm.runInContext("liveRefreshProgressText(unknownState)", context),
}));
'''
    result = subprocess.run(
        ["node", "-e", probe, str(ROOT)], check=True, capture_output=True,
        text=True, encoding="utf-8",
    )
    return json.loads(result.stdout)


def test_shell_catalog_has_complete_locale_parity_and_parameterised_output() -> None:
    output = run_catalog_probe()
    keys = output["keys"]
    assert keys["en-GB"] == keys["zh-CN"] == keys["ko-KR"]
    assert output["english"] == (
        "Alex’s managed club changed from “Old Town” to “New Town”. "
        "Switch to the new club? FMODD will read its data and keep the current manager account."
    )
    assert output["korean"] == (
        "Alex의 담당 클럽 변경: “Old Town” → “New Town”. "
        "새 클럽으로 전환할까요? FMODD가 데이터를 읽고 현재 감독 계정을 유지합니다."
    )


def test_shell_source_uses_semantic_messages_and_localized_request_header() -> None:
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    assert 'new Headers(fetchOptions.headers || {})' in script
    assert 'requestHeaders.set("X-FMODD-Locale", activeUiLocale())' in script
    assert 'const useSpecificLegacyDetail = Boolean(payload.error_detail_safe && payload.error);' in script
    assert 'localizeServerError(payload.error)' in script
    assert 'error.payload = payload;' in script
    assert 'const recoveryMessage = error instanceof Error' in script
    assert 'refreshRequiredMessage(recoveryMessage)' in script
    for key in (
        'uiText("funding.available")',
        'uiText("request.in_progress")',
        'uiText("manager.club_change.message"',
        'uiPlural("topbar.up_to_date_settled"',
        'uiText("page.requires_save")',
    ):
        assert key in script

    home_view_model = script.split("function homeViewModel()", 1)[1].split(
        "function homeCardStateSignature", 1,
    )[0]
    assert "connection.message_code" in home_view_model
    assert "connection.message" not in home_view_model.replace(
        "connection.message_code", "",
    )
    assert "home.connection.${String(connection.message_code" in home_view_model


def test_scoped_shell_fallbacks_use_semantic_messages() -> None:
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    first_segment = script.split("function resetAccountScopedWorldState", 1)[0]
    for key in (
        "shell.select.empty",
        "shell.select.aria",
        "shell.select.placeholder",
        "funding.daily_spend",
        "operations.unconfirmed_detail",
        "operations.background_not_started",
        "operations.connection_success",
        "operations.connection_incomplete",
        "operations.refresh_completed",
        "shell.not_connected",
        "refresh.timeout",
        "shop.product_fallback",
        "inventory.operation_done",
        "inventory.operation_busy",
    ):
        assert f'uiText("{key}"' in first_segment

    for literal in (
        "没有可选项",
        "选择选项",
        "请选择",
        "每日支出",
        "未连接存档",
        "后台任务未启动，请查看当前状态后重试",
        "已连接当前存档",
        "连接存档未完成",
        "刷新完成",
        "盘口刷新超时，请确认游戏仍在运行",
        "处理完成",
        "已有道具正在处理中，请稍候",
    ):
        assert literal not in first_segment


def test_runtime_refresh_copy_never_displays_backend_source_language_text() -> None:
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    progress = script.split("function liveRefreshProgressText(", 1)[1].split(
        "function updateHomeConnectionProgress", 1,
    )[0]
    startup = script.split("function updateStartupLoading()", 1)[1].split(
        "function clubStatusSnapshot", 1,
    )[0]
    world_clubs = script.split("function renderWorldClubs()", 1)[1].split(
        "async function sellOwnedClub", 1,
    )[0]
    world_nations = script.split("function renderWorldNations()", 1)[1].split(
        "function worldNationRankingMetric", 1,
    )[0]
    club_status = script.split("function syncClubPageStatus", 1)[1].split(
        "function renderClub()", 1,
    )[0]

    assert "progress.text" not in progress
    assert "state.status" not in progress
    assert "clubStatus.progress_text" not in startup
    assert "scan.progress_text ||" not in world_clubs
    assert "scan.progress_text ||" not in world_nations
    assert "status.progress_text ||" not in club_status
    assert 'uiText("refresh.stage.championship_markets")' in progress


def test_korean_refresh_status_ignores_backend_chinese_progress_text() -> None:
    output = run_refresh_progress_probe()
    assert output["known"] == "우승팀 베팅을 생성하고 정산하는 중 3/10"
    assert output["unknown"] == "배당률을 새로 고치는 중"
