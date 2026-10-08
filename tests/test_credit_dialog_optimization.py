from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "web" / "app.js").read_text(encoding="utf-8")


def test_credit_terms_are_short_circuited_by_a_complete_render_signature():
    render = SCRIPT.split("function renderCreditTerms()", 1)[1].split(
        "function playerFractionalIntimacy", 1,
    )[0]

    assert "creditTermsRenderSignature" in SCRIPT
    assert "if (app.creditTermsRenderSignature === signature) return;" in render
    assert "credit.theoretical_multiplier" in render
    assert "credit.credit_multiplier" in render
    assert "selectedManagerName()" in render
    assert "moneyRenderSignature()" in render


def test_credit_terms_publish_a_live_status_without_changing_the_borrow_request():
    render = SCRIPT.split("function renderCreditTerms()", 1)[1].split(
        "function playerFractionalIntimacy", 1,
    )[0]
    submit = SCRIPT.split('$("#credit-form").addEventListener("submit"', 1)[1].split(
        '$("#history-button")', 1,
    )[0]

    assert 'setAttribute("role", "status")' in render
    assert 'setAttribute("aria-live", "polite")' in render
    assert 'request("/api/bank/borrow"' in submit
    assert 'body:JSON.stringify({amount})' in submit

