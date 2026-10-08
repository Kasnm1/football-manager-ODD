from pathlib import Path
import re
import shutil
import subprocess

import pytest


ROOT = (Path(__file__).resolve().parents[1] / "src")


def test_bet_submission_has_timeout_busy_state_and_retry_identity() -> None:
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    assert "betSubmissionBusy: false" in script
    assert "app.betSubmissionBusy || !app.state?.betting_ready" in script
    assert 'error.code = "REQUEST_TIMEOUT"' in script
    assert "timeoutMs:15000" in script
    assert "payload.client_submission_id = app.betSubmissionRetry.id" in script
    assert 'if (error.code !== "REQUEST_TIMEOUT") app.betSubmissionRetry = null' in script
    assert "const acceptedIds = new Set" in script
    assert "没有重复扣款" in script


def test_maximum_stake_accounts_for_pending_single_bet_profit() -> None:
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    assert "function pendingSingleProfitByFixture()" in script
    assert 'record.status !== "pending" || record.demo_preview' in script
    assert "pendingProfit.get(selectionMatchKey(group[0])) || 0" in script
    assert "function floorStakeForProfit(" in script


def test_maximum_stake_large_limit_finishes_and_stays_below_profit_limit() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to execute the frontend stake calculation")
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    match = re.search(
        r"function floorStakeForProfit\(.*?\n}\n",
        script,
        flags=re.DOTALL,
    )
    assert match is not None
    probe = f"""
{match.group(0)}
const limit = 100_000_000_000_000;
const multiplier = 0.3;
const stake = floorStakeForProfit(limit, multiplier);
if (!Number.isFinite(stake) || stake <= 0 || stake * multiplier > limit + 1e-9) {{
  process.exit(1);
}}
"""
    subprocess.run(
        [node, "-e", probe],
        check=True,
        capture_output=True,
        text=True,
        timeout=2,
    )
