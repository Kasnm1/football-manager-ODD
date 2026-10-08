from pathlib import Path


ROOT = (Path(__file__).resolve().parents[1] / "src")


def test_asian_handicap_history_does_not_duplicate_zero_line() -> None:
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    history = script.split("function renderHistory()", 1)[1].split(
        "function renderIntegrityNoticeDetails", 1
    )[0]
    assert "const formattedLine = formatAsianLine(line);" in history
    assert "if (current.endsWith(` ${formattedLine}`)) return current;" in history
    assert "`${current || (code === \"home\" ? leg.home : leg.away)} ${formattedLine}`" in history
    assert "/[+-]\\d+(?:\\.\\d+)?$/" not in history


def test_opening_history_always_leaves_profit_analysis() -> None:
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    open_handler = script.split(
        '$("#history-button").addEventListener("click", () => {', 1
    )[1].split("});", 1)[0]
    assert 'app.historyMode = "history";' in open_handler
    assert open_handler.index('app.historyMode = "history";') < open_handler.index(
        "renderHistory();"
    )


def test_parlay_list_underlines_correct_selections() -> None:
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    styles = (ROOT / "web" / "app.css").read_text(encoding="utf-8")
    history_list = script.split("const historyList = (records, settled)", 1)[1].split(
        "if (analysisMode)", 1
    )[0]

    assert 'const selectionClass = ["parlay", "system_parlay"].includes(record.type) ? legOutcomeClass(result) : "";' in history_list
    assert 'class="history-list-leg-selection ${selectionClass}"' in history_list
    assert ".history-list-leg-selection.leg-profit" in styles
    assert "text-decoration:underline" in styles


def test_list_money_columns_use_two_decimal_compact_format() -> None:
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    history_list = script.split("const historyList = (records, settled)", 1)[1].split(
        "if (analysisMode)", 1
    )[0]

    assert "const listMoney = (value) => formatCompactMoney(value, 2);" in history_list
    assert '<td class="history-list-money">${listMoney(stake)}</td>' in history_list
    assert "refunded ? listMoney(Number(record.payout || 0))" in history_list
    assert "`-${listMoney(Math.abs(netResult))}` : listMoney(netResult)" in history_list
