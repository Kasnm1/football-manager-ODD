from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_coaching_management_exposes_persistent_transfer_history_tab() -> None:
    index = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    backend = (ROOT / "tools" / "transfer_history.py").read_text(encoding="utf-8")

    assert 'data-club-tab="transfers"' in index
    assert 'data-i18n="nav.transfers"' in index
    assert 'request("/api/club/transfer-history")' in script
    assert "function renderTransferHistory()" in script
    for key in (
        "world.transfer.loading",
        "world.transfer.all_clubs",
        "world.transfer.direction_in",
        "world.transfer.direction_out",
        "world.transfer.all_years",
        "world.transfer.club_option",
    ):
        assert f'uiText("{key}"' in script
    transfer_renderer = script.split("function renderTransferHistory()", 1)[1].split("function bindTransferHistoryControls", 1)[0]
    assert "全部执教俱乐部" not in transfer_renderer
    assert "换队后永久保留" not in transfer_renderer
    assert 'data-transfer-history-direction="in"' in script
    assert 'data-transfer-history-direction="out"' in script
    assert 'data-transfer-history-year' in script
    assert 'uiText("world.transfer.all_years")' in script
    assert "FMODD 持续追踪" not in script
    assert "永久保留人物库追踪记录" not in script
    assert "FMODD 持续追踪" not in backend
    assert "永久保留人物库追踪记录" not in backend


def test_transfer_history_hides_the_shared_save_metadata_subtitle() -> None:
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    index = (ROOT / "web" / "index.html").read_text(encoding="utf-8")

    assert 'id="club-subtitle"' not in index
    assert '$("#club-subtitle")' not in script


def test_transfer_player_opens_hall_of_fame_profile_and_can_return() -> None:
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    assert "async function openTransferHistoryPlayer(playerId, teamId)" in script
    assert 'app.clubLegacyReturnPage = "club-transfers"' in script
    assert 'showPage("hall-of-fame")' in script
    assert 'app.clubLegacySelectedUid = Number(playerId)' in script
    assert '"legacy.profile.back_transfers"' in script
    assert 'app.clubTab = "transfers"' in script


def test_transfer_history_visible_text_respects_minimum_font_size() -> None:
    styles = (ROOT / "web" / "app.css").read_text(encoding="utf-8")
    dark_styles = (ROOT / "web" / "dark-theme.css").read_text(encoding="utf-8")

    block = styles.split(".transfer-history{", 1)[1]
    assert ".transfer-history-table th,.transfer-history-table td" in block
    assert "font-size:13px" in block
    assert ".transfer-history-directions" in styles
    assert ':root[data-theme="dark"] .transfer-history-table th' in dark_styles
