from __future__ import annotations

import json
import math
import re
import threading
import uuid
from datetime import date, datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import Any

from tools.account_store import (
    load_document, save_document, save_documents, update_documents,
)
from tools.app_paths import save_data_root
from tools.money import (
    from_minor, migrate_money_fields, multiply_minor, read_minor,
    round_money, to_minor, write_minor,
)
from tools.i18n_runtime import mail_template_fields
from tools.pass_methods import pass_leg_sizes, pass_subsets


INITIAL_BALANCE = 10000.0
BANKRUPTCY_RELIEF_AMOUNT = 5000.0
BANKRUPTCY_RELIEF_THRESHOLD = 1.0
VERSION_UPDATE_REWARD_AMOUNT = 50_000_000.0
LIVE_SETTLEMENT_MINIMUM_MATCH_MINUTES = 105
MANUAL_REFUND_WAIT_DAYS = 3
RESULT_SEARCH_WAIT_DAYS = 3
SETTLED_DETAIL_BACKFILL_DAYS = 3
SETTLED_BET_HISTORY_LIMIT = 500
HALF_TIME_MARKETS = {
    "HT_1X2", "HTFT", "HT_AH", "HT_OU", "HT_TEAM_GOALS", "HT_TOTAL_GOALS",
    "HT_SCORE", "HIGHEST_SCORING_HALF", "SH_1X2", "SH_AH", "SH_OU",
    "SH_TEAM_GOALS", "SH_TOTAL_GOALS", "SH_SCORE",
}
LIVE_DETAIL_MARKETS = HALF_TIME_MARKETS | {
    "FIRST_SCORE", "ADVANCE", "EXTRA_TIME", "PENALTIES", "ADVANCE_METHOD",
}
UNTRUSTED_SETTLEMENT_REASONS = {
    "fixture_cancelled",
    "half_time_result_timeout",
    "half_time_result_unavailable",
    "first_scorer_unavailable",
    "missing_result_details",
}
_ACCOUNT_LOCK = threading.RLock()
_SNAPSHOT_FIXTURE_HINT_CACHE: dict[
    Path, tuple[int, int, dict[tuple[str, int, int, int], int]]
] = {}


def _account_locked(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with _ACCOUNT_LOCK:
            return function(*args, **kwargs)
    return wrapped


def bets_path() -> Path:
    return save_data_root() / "bets" / "fm26_bets.jsonl"


def wallet_path() -> Path:
    return save_data_root() / "bets" / "fm26_wallet.json"


def transaction_journal_path() -> Path:
    return save_data_root() / "bets" / "fm26_account_transaction.json"


def bet_analytics_archive_path() -> Path:
    return save_data_root() / "bets" / "fm26_bet_analytics.json"


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _round_money(value: float) -> float:
    return round_money(value)


_WALLET_MONEY_FIELDS = ("initial_balance", "balance")
_TRANSACTION_MONEY_FIELDS = ("amount", "balance_after")
_BET_MONEY_FIELDS = (
    "stake", "unit_stake", "potential_return", "potential_profit", "payout",
    "balance_before_settlement", "balance_after_settlement",
    "manual_refund_fee",
)


def _normalize_wallet_money(wallet: dict[str, Any]) -> bool:
    changed = migrate_money_fields(wallet, _WALLET_MONEY_FIELDS)
    schema_version = max(2, int(wallet.get("schema_version") or 1))
    changed = changed or wallet.get("schema_version") != schema_version
    wallet["schema_version"] = schema_version
    for transaction in wallet.setdefault("transactions", []):
        if isinstance(transaction, dict):
            changed = migrate_money_fields(
                transaction, _TRANSACTION_MONEY_FIELDS,
            ) or changed
    return changed


def _normalize_bet_money(record: dict[str, Any]) -> bool:
    changed = migrate_money_fields(record, _BET_MONEY_FIELDS)
    for leg in record.get("legs") or []:
        if isinstance(leg, dict):
            changed = migrate_money_fields(leg, _BET_MONEY_FIELDS) or changed
    return changed


def _wallet_balance_minor(wallet: dict[str, Any]) -> int:
    return read_minor(wallet, "balance")


def _set_wallet_balance_minor(wallet: dict[str, Any], balance_minor: int) -> None:
    write_minor(wallet, "balance", balance_minor)


def _reverse_wallet_credit(
    wallet: dict[str, Any], requested_minor: int,
) -> tuple[int, int]:
    """Reverse an old credit without allowing the wallet to become negative."""
    requested_minor = int(requested_minor)
    if requested_minor < 0:
        raise ValueError("钱包回收金额不能小于 0")
    available_minor = max(0, _wallet_balance_minor(wallet))
    reversed_minor = min(available_minor, requested_minor)
    unrecovered_minor = requested_minor - reversed_minor
    _set_wallet_balance_minor(wallet, available_minor - reversed_minor)
    return reversed_minor, unrecovered_minor


def _set_record_money(record: dict[str, Any], field: str, value: Any) -> int:
    minor = to_minor(value)
    write_minor(record, field, minor)
    return minor


def _atomic_write_text(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(contents, encoding="utf-8")
    temporary.replace(path)


def _bets_text(records: list[dict[str, Any]]) -> str:
    return "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)


def _recover_account_transaction() -> None:
    path = transaction_journal_path()
    with _ACCOUNT_LOCK:
        if not path.exists():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            records = payload["bets"]
            wallet = payload["wallet"]
            if not isinstance(records, list) or not isinstance(wallet, dict):
                raise ValueError("投注账户事务日志无效")
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            raise RuntimeError(f"无法恢复投注账户事务：{error}") from error
        save_documents(
            {"bets": records, "wallet": wallet}, legacy_path=wallet_path(),
        )
        path.unlink(missing_ok=True)


def _commit_account(records: list[dict[str, Any]], wallet: dict[str, Any], reason: str) -> None:
    with _ACCOUNT_LOCK:
        _normalize_wallet_money(wallet)
        for record in records:
            if isinstance(record, dict):
                _normalize_bet_money(record)
        save_documents(
            {"bets": records, "wallet": wallet}, legacy_path=wallet_path(),
        )


def _new_wallet() -> dict[str, Any]:
    created_at = now()
    wallet = {
        "schema_version": 2,
        "initial_balance": INITIAL_BALANCE,
        "initial_balance_minor": to_minor(INITIAL_BALANCE),
        "balance": INITIAL_BALANCE,
        "balance_minor": to_minor(INITIAL_BALANCE),
        "credit_effective_bet_count": 0,
        "transactions": [{
            "id": str(uuid.uuid4()),
            "at": created_at,
            "type": "opening_balance",
            "amount": INITIAL_BALANCE,
            "amount_minor": to_minor(INITIAL_BALANCE),
            "balance_after": INITIAL_BALANCE,
            "balance_after_minor": to_minor(INITIAL_BALANCE),
        }],
    }
    return wallet


def load_wallet() -> dict[str, Any]:
    _recover_account_transaction()
    wallet = load_document("wallet", None, legacy_path=wallet_path())
    if wallet is None:
        wallet = _new_wallet()
        save_wallet(wallet)
        return wallet
    try:
        changed = _normalize_wallet_money(wallet)
        wallet.setdefault("transactions", [])
        if "credit_effective_bet_count" not in wallet:
            wallet["credit_effective_bet_count"] = sum(
                1 for record in load_bets()
                if float(record.get("stake") or 0) > 1000
            )
            changed = True
        if changed:
            save_wallet(wallet)
        return wallet
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise RuntimeError(f"无法读取投注钱包：{error}") from error


def save_wallet(wallet: dict[str, Any]) -> None:
    _normalize_wallet_money(wallet)
    save_document("wallet", wallet, legacy_path=wallet_path())


def _adjust_wallet(
    wallet: dict[str, Any], amount: float, transaction_type: str,
    **metadata: Any,
) -> dict[str, Any]:
    amount_minor = to_minor(amount)
    balance_minor = _wallet_balance_minor(wallet) + amount_minor
    if balance_minor < 0:
        raise ValueError("钱包余额不足")
    _set_wallet_balance_minor(wallet, balance_minor)
    transaction = {
        "id": str(uuid.uuid4()),
        "at": now(),
        "type": str(transaction_type),
        **metadata,
    }
    write_minor(transaction, "amount", amount_minor)
    write_minor(transaction, "balance_after", balance_minor)
    wallet.setdefault("transactions", []).append(transaction)
    return wallet


@_account_locked
def commit_documents_with_wallet_adjustments(
    documents: dict[str, Any],
    adjustments: list[dict[str, Any]],
    *, legacy_path: Path | None = None,
) -> dict[str, Any]:
    """Atomically commit account documents and their wallet transaction legs."""
    wallet = load_wallet()
    for adjustment in adjustments:
        if not isinstance(adjustment, dict):
            raise ValueError("钱包交易无效")
        metadata = dict(adjustment.get("metadata") or {})
        _adjust_wallet(
            wallet,
            adjustment.get("amount", 0),
            str(adjustment.get("type") or "account_adjustment"),
            **metadata,
        )
    save_documents(
        {**documents, "wallet": wallet},
        legacy_path=legacy_path or wallet_path(),
    )
    return wallet


def available_balance() -> float:
    return from_minor(_wallet_balance_minor(load_wallet()))


def credit_effective_bet_count() -> int:
    return max(0, int(load_wallet().get("credit_effective_bet_count") or 0))


@_account_locked
def adjust_balance(amount: float, transaction_type: str, **metadata: Any) -> dict[str, Any]:
    """Apply a validated casino-wallet adjustment used by the bank layer."""
    wallet = load_wallet()
    _adjust_wallet(wallet, amount, transaction_type, **metadata)
    save_wallet(wallet)
    return wallet


def version_update_reward_key(app_version: str) -> str:
    """Collapse letter-suffixed builds such as 2.2.0b/c/d into one reward version."""
    resolved = str(app_version or "").strip()
    match = re.fullmatch(
        r"[vV]?(\d+)\.(\d+)\.(\d+)(?:[A-Za-z]+)?",
        resolved,
    )
    if not match:
        return resolved.casefold()
    return ".".join(str(int(part)) for part in match.groups())


def _matches_version_update_reward(
    record: dict[str, Any], reward_version: str,
) -> bool:
    source = str(record.get("source_id") or record.get("id") or "")
    if source == f"version_update_reward:{reward_version}":
        return True
    stored_version = str(
        record.get("reward_version") or record.get("app_version") or ""
    ).strip()
    if not stored_version and source.startswith("version_update_reward:"):
        stored_version = source.split(":", 1)[1]
    return bool(
        stored_version
        and version_update_reward_key(stored_version) == reward_version
    )


@_account_locked
def queue_version_update_reward(
    scope_id: str, app_version: str,
    amount: float = VERSION_UPDATE_REWARD_AMOUNT,
) -> dict[str, Any]:
    """Queue one claimable mail for an existing account and reward version."""
    resolved_scope = str(scope_id or "").strip()
    resolved_version = str(app_version or "").strip()
    reward_minor = to_minor(amount)
    if not resolved_scope:
        raise ValueError("更新奖励缺少账户作用域")
    if not resolved_version:
        raise ValueError("更新奖励缺少应用版本")
    if reward_minor <= 0:
        raise ValueError("更新奖励金额必须大于 0")

    reward_version = version_update_reward_key(resolved_version)
    source_id = f"version_update_reward:{reward_version}"
    created_at = now()

    def mutate(documents: dict[str, Any]) -> dict[str, Any]:
        wallet = documents["wallet"]
        mail = documents["mail"]
        _normalize_wallet_money(wallet)
        transactions = wallet.setdefault("transactions", [])
        already_credited = any(
            isinstance(row, dict)
            and _matches_version_update_reward(row, reward_version)
            for row in transactions
        )
        reward_mail = next((
            row for row in mail
            if isinstance(row, dict)
            and _matches_version_update_reward(row, reward_version)
        ), None)
        mail_created = reward_mail is None
        if mail_created:
            reward_mail = {
                "id": source_id,
                "source_id": source_id,
                "type": "version_update_reward",
                **mail_template_fields("version_update_reward", {
                    "app_version": resolved_version,
                    "amount": from_minor(reward_minor),
                }),
                "title": "更新奖励",
                "message": (
                    f"FMODD {resolved_version} 更新奖励 50M "
                    f"{'已到账' if already_credited else '待领取'}。"
                ),
                "body": (
                    "50M 更新奖励已存入本存档的钱包。"
                    if already_credited else
                    f"检测到 FMODD 已更新至 {resolved_version}，"
                    "请点击领取，将 50M 更新奖励存入本存档的钱包。"
                ),
                "amount": from_minor(reward_minor),
                "amount_minor": reward_minor,
                "app_version": resolved_version,
                "reward_version": reward_version,
                "created_at": created_at,
                "read": False,
                "claimed": already_credited,
                "claimable": not already_credited,
            }
            if already_credited:
                reward_mail["claimed_at"] = created_at
            mail.append(reward_mail)
        elif already_credited and not reward_mail.get("claimed"):
            # Releases before claim support credited immediately. Mark their mail
            # complete so opening it cannot credit the same reward a second time.
            reward_mail["claimed"] = True
            reward_mail["claimable"] = False
            reward_mail.setdefault("claimed_at", created_at)
            reward_mail["message"] = (
                f"FMODD {resolved_version} 更新奖励 50M 已到账。"
            )
        return {
            "scope_id": resolved_scope,
            "app_version": resolved_version,
            "reward_version": reward_version,
            "amount": from_minor(reward_minor),
            "queued": mail_created and not already_credited,
            "already_credited": already_credited,
            "mail_created": mail_created,
            "balance": from_minor(_wallet_balance_minor(wallet)),
        }

    return update_documents(
        {"wallet": _new_wallet(), "mail": []}, mutate, resolved_scope,
    )


@_account_locked
def claim_version_update_reward(
    scope_id: str, mail_id: str,
    amount: float = VERSION_UPDATE_REWARD_AMOUNT,
) -> dict[str, Any]:
    """Atomically claim a queued update reward without allowing double credit."""
    resolved_scope = str(scope_id or "").strip()
    resolved_mail_id = str(mail_id or "").strip()
    reward_minor = to_minor(amount)
    if not resolved_scope:
        raise ValueError("更新奖励缺少账户作用域")
    if not resolved_mail_id:
        raise ValueError("请选择要领取的更新奖励邮件")
    if reward_minor <= 0:
        raise ValueError("更新奖励金额必须大于 0")

    claimed_at = now()

    def mutate(documents: dict[str, Any]) -> dict[str, Any]:
        wallet = documents["wallet"]
        mail = documents["mail"]
        _normalize_wallet_money(wallet)
        reward_mail = next((
            row for row in mail
            if isinstance(row, dict)
            and str(row.get("id") or "") == resolved_mail_id
            and row.get("type") == "version_update_reward"
        ), None)
        if reward_mail is None:
            raise ValueError("更新奖励邮件不存在")

        stored_version = str(
            reward_mail.get("reward_version")
            or reward_mail.get("app_version") or ""
        ).strip()
        reward_version = version_update_reward_key(stored_version)
        if not reward_version:
            raise ValueError("更新奖励邮件版本无效")
        source_id = f"version_update_reward:{reward_version}"
        transactions = wallet.setdefault("transactions", [])
        already_credited = any(
            isinstance(row, dict)
            and _matches_version_update_reward(row, reward_version)
            for row in transactions
        )
        if not already_credited:
            _adjust_wallet(
                wallet, from_minor(reward_minor), "version_update_reward",
                id=source_id, source_id=source_id,
                app_version=str(reward_mail.get("app_version") or stored_version),
                reward_version=reward_version,
            )

        reward_mail["claimed"] = True
        reward_mail["claimable"] = False
        reward_mail["read"] = True
        reward_mail.setdefault("claimed_at", claimed_at)
        reward_mail["message"] = (
            f"FMODD {reward_mail.get('app_version') or stored_version} "
            "更新奖励 50M 已领取。"
        )
        reward_mail["body"] = "50M 更新奖励已存入本存档的钱包。"
        return {
            "scope_id": resolved_scope,
            "mail": dict(reward_mail),
            "reward_version": reward_version,
            "amount": from_minor(reward_minor),
            "credited": not already_credited,
            "claimed": True,
            "balance": from_minor(_wallet_balance_minor(wallet)),
        }

    return update_documents(
        {"wallet": _new_wallet(), "mail": []}, mutate, resolved_scope,
    )


@_account_locked
def grant_bankruptcy_relief(bank_balance: float) -> dict[str, Any]:
    """Credit emergency funds when both balances are below the relief threshold."""
    wallet = load_wallet()
    balance = from_minor(_wallet_balance_minor(wallet))
    bank = _round_money(float(bank_balance))
    has_pending_bets = any(
        record.get("status") == "pending" and not record.get("demo_preview")
        for record in load_bets()
    )
    balances_eligible = all(
        math.isfinite(value) and value < BANKRUPTCY_RELIEF_THRESHOLD
        for value in (bank, balance)
    )
    if not balances_eligible or has_pending_bets:
        return {
            "granted": False,
            "amount": 0.0,
            "balance": balance,
            "bank_balance": bank,
            "has_pending_bets": has_pending_bets,
        }

    transaction_id = str(uuid.uuid4())
    _adjust_wallet(
        wallet, BANKRUPTCY_RELIEF_AMOUNT, "bankruptcy_relief",
        id=transaction_id,
    )
    relieved_balance = from_minor(_wallet_balance_minor(wallet))
    save_wallet(wallet)
    return {
        "granted": True,
        "transaction_id": transaction_id,
        "amount": BANKRUPTCY_RELIEF_AMOUNT,
        "balance": relieved_balance,
        "bank_balance": bank,
        "has_pending_bets": False,
    }


@_account_locked
def add_funds(amount: float) -> dict[str, Any]:
    if amount not in {500, 1000}:
        raise ValueError("充值金额必须为 500 或 1000")
    wallet = load_wallet()
    _adjust_wallet(wallet, amount, "funding")
    save_wallet(wallet)
    return wallet


@_account_locked
def cheat_balance(amount: float | None = None, clear: bool = False) -> dict[str, Any]:
    wallet = load_wallet()
    before_minor = _wallet_balance_minor(wallet)
    if clear:
        delta_minor = -before_minor
        transaction_type = "cheat_balance_clear"
    else:
        if amount not in {500, 5000}:
            raise ValueError("作弊资金金额必须为 500 或 5000")
        delta_minor = to_minor(amount)
        transaction_type = "cheat_funding"
    _adjust_wallet(wallet, from_minor(delta_minor), transaction_type)
    save_wallet(wallet)
    return wallet


def load_bets() -> list[dict[str, Any]]:
    _recover_account_transaction()
    records = load_document("bets", [], legacy_path=bets_path())
    valid = [record for record in records if isinstance(record, dict)]
    changed = len(valid) != len(records)
    for record in valid:
        changed = _normalize_bet_money(record) or changed
    if changed:
        save_document("bets", valid, legacy_path=bets_path())
    return valid


def public_bet_history(
    records: list[dict[str, Any]] | None = None,
    settled_limit: int = SETTLED_BET_HISTORY_LIMIT,
) -> list[dict[str, Any]]:
    """Return newest-first history with every pending bet and limited settlements."""
    visible: list[dict[str, Any]] = []
    settled_count = 0
    for record in reversed(load_bets() if records is None else records):
        if (record.get("status") or "pending") == "pending":
            visible.append(record)
            continue
        if settled_count < max(0, int(settled_limit)):
            visible.append(record)
            settled_count += 1
    return visible


@_account_locked
def integrity_bet_snapshot(bet_ids: set[str]) -> list[dict[str, Any]]:
    return [
        record for record in load_bets()
        if str(record.get("bet_id") or "") in bet_ids
    ]


@_account_locked
def legacy_integrity_bet_snapshot(audit_version: str) -> list[dict[str, Any]]:
    """Return settled pre-integrity bets plus an unfinished audit from this version."""
    rows = []
    for record in load_bets():
        if record.get("status") == "pending" or record.get("integrity_assessed"):
            continue
        if record.get("legacy_integrity_audit_version") == audit_version:
            rows.append(record)
            continue
        legs = record.get("legs") or [record]
        if legs and all("integrity" not in leg for leg in legs):
            rows.append(record)
    return rows


@_account_locked
def attach_legacy_integrity_evidence(
    audit_version: str,
    evidence_by_bet: dict[str, list[dict[str, Any] | None]],
    manager_name: str,
    weekly_salary: float,
) -> list[str]:
    """Persist legacy evidence before assessment so a restart cannot punish twice."""
    if not evidence_by_bet:
        return []
    records = load_bets()
    prepared: list[str] = []
    changed = False
    for record in records:
        bet_id = str(record.get("bet_id") or "")
        if bet_id not in evidence_by_bet or record.get("integrity_assessed"):
            continue
        if record.get("status") == "pending":
            continue
        legs = record.get("legs") or [record]
        evidence_rows = evidence_by_bet[bet_id]
        if len(legs) != len(evidence_rows):
            continue
        for leg, evidence in zip(legs, evidence_rows):
            leg["integrity"] = evidence
        record["legacy_integrity_audit_version"] = audit_version
        record["legacy_integrity_audit_at"] = now()
        record.setdefault("integrity_manager_name", manager_name or "经理")
        record.setdefault("integrity_weekly_salary", round(float(weekly_salary), 2))
        prepared.append(bet_id)
        changed = True
    if changed:
        save_bets(records)
    return prepared


@_account_locked
def mark_integrity_bets_assessed(assessments: dict[str, list[str]]) -> None:
    if not assessments:
        return
    records = load_bets()
    changed = False
    for record in records:
        bet_id = str(record.get("bet_id") or "")
        if bet_id not in assessments or record.get("status") == "pending":
            continue
        record["integrity_assessed"] = True
        record["integrity_event_keys"] = list(assessments[bet_id])
        changed = True
    if changed:
        save_bets(records)


@_account_locked
def migrate_settlement_balance_snapshots() -> int:
    """Backfill per-bet settlement balances from older aggregate wallet entries."""
    records = load_bets()
    missing = {
        str(record.get("bet_id")): record
        for record in records
        if record.get("status") != "pending"
        and record.get("bet_id")
        and ("balance_before_settlement" not in record or "balance_after_settlement" not in record)
    }
    if not missing:
        return 0

    changed = 0
    for transaction in load_wallet().get("transactions", []):
        if transaction.get("type") != "bet_settlement":
            continue
        bet_ids = [str(value) for value in transaction.get("bet_ids", [])]
        settlement_time = transaction.get("at")
        matched = [
            missing[bet_id]
            for bet_id in bet_ids
            if bet_id in missing and missing[bet_id].get("settled_at") == settlement_time
        ]
        if not matched:
            continue

        balance_minor = (
            read_minor(transaction, "balance_after")
            - read_minor(transaction, "amount")
        )
        for record in matched:
            write_minor(record, "balance_before_settlement", balance_minor)
            balance_minor += read_minor(record, "payout")
            write_minor(record, "balance_after_settlement", balance_minor)
            changed += 1

    if changed:
        save_bets(records)
    return changed


def pending_result_keys() -> set[tuple[str, int, int, int]]:
    keys: set[tuple[str, int, int, int]] = set()
    for record in load_bets():
        if record.get("status") != "pending" or record.get("demo_preview"):
            continue
        for leg in record.get("legs") or [record]:
            key = _leg_result_key(leg, record)
            if key:
                keys.add(key)
    return keys


def _leg_due_for_settlement(
    leg: dict[str, Any], record: dict[str, Any],
    game_date: str, game_minutes: int | None,
    minimum_match_minutes: int = LIVE_SETTLEMENT_MINIMUM_MATCH_MINUTES,
) -> bool:
    """Return whether the leg has reached its earliest scheduled finish."""
    key = _leg_result_key(leg, record)
    if key is None:
        return False
    try:
        fixture_day = date.fromisoformat(key[0])
        current_day = date.fromisoformat(str(game_date))
    except ValueError:
        return False
    if game_minutes is None:
        # Callers without a live clock may settle historical results only.
        return fixture_day < current_day
    try:
        current_minutes = int(game_minutes)
        minimum_minutes = max(0, int(minimum_match_minutes))
    except (TypeError, ValueError):
        return False
    if not 0 <= current_minutes < 24 * 60:
        return False
    kickoff = _leg_kickoff_minutes(leg, record)
    # Legacy records without a kickoff wait until even a 23:59 fixture could
    # have completed.  This is deliberately conservative rather than guessing.
    kickoff = 24 * 60 - 1 if kickoff is None else int(kickoff)
    current_total = current_day.toordinal() * 24 * 60 + current_minutes
    earliest_finish = (
        fixture_day.toordinal() * 24 * 60 + kickoff + minimum_minutes
    )
    return current_total >= earliest_finish


def _record_due_for_settlement(
    record: dict[str, Any], game_date: str, game_minutes: int | None,
) -> bool:
    legs = record.get("legs") or [record]
    return bool(legs) and all(
        _leg_due_for_settlement(leg, record, game_date, game_minutes)
        for leg in legs
    )


def pending_due_result_keys(
    game_date: str,
    game_minutes: int,
    minimum_match_minutes: int = LIVE_SETTLEMENT_MINIMUM_MATCH_MINUTES,
) -> set[tuple[str, int, int, int]]:
    """Return pending legs whose earliest scheduled finish has passed."""
    due: set[tuple[str, int, int, int]] = set()
    for record in load_bets():
        if record.get("status") != "pending" or record.get("demo_preview"):
            continue
        for leg in record.get("legs") or [record]:
            key = _leg_result_key(leg, record)
            if not key:
                continue
            if _leg_due_for_settlement(
                leg, record, game_date, game_minutes, minimum_match_minutes,
            ):
                due.add(key)
    return due


@_account_locked
def settled_missing_half_score_keys(
    game_date: str,
    max_age_days: int = SETTLED_DETAIL_BACKFILL_DAYS,
) -> set[tuple[str, int, int, int]]:
    """Return recent settled legs whose immutable history lacks half-time detail."""
    try:
        current_date = date.fromisoformat(str(game_date))
        maximum_age = max(0, int(max_age_days))
    except (TypeError, ValueError):
        return set()

    keys: set[tuple[str, int, int, int]] = set()
    for record in load_bets():
        if (
            record.get("status") in {
                "pending", "no_result", "manual_refund", "schedule_refund",
            }
            or record.get("demo_preview")
            or record.get("type") == "championship"
        ):
            continue
        legs = record.get("legs") or [record]
        evaluations = record.get("settlement") or []
        if len(legs) != len(evaluations):
            continue
        for leg, evaluation in zip(legs, evaluations):
            if (
                not isinstance(evaluation, dict)
                or evaluation.get("half_score") not in {None, ""}
            ):
                continue
            try:
                score_home, score_away = (
                    int(value)
                    for value in str(evaluation.get("score") or "").split("-", 1)
                )
            except (TypeError, ValueError):
                continue
            if score_home < 0 or score_away < 0:
                continue
            key = _leg_result_key(leg, record)
            if key is None:
                continue
            try:
                age_days = (current_date - date.fromisoformat(key[0])).days
            except ValueError:
                continue
            if 0 <= age_days <= maximum_age:
                keys.add(key)
    return keys


@_account_locked
def pending_detail_capture_keys(
    game_date: str, game_minutes: int,
    *, lead_minutes: int = 30, tail_minutes: int = 180,
) -> set[tuple[str, int, int, int]]:
    """Return detail-dependent pending fixtures inside their short capture window."""
    try:
        current_day = date.fromisoformat(str(game_date))
        current_total = current_day.toordinal() * 24 * 60 + int(game_minutes)
    except (TypeError, ValueError):
        return set()
    keys: set[tuple[str, int, int, int]] = set()
    for record in load_bets():
        if record.get("status") != "pending" or record.get("demo_preview"):
            continue
        for leg in record.get("legs") or [record]:
            if str(leg.get("market") or record.get("market") or "") not in LIVE_DETAIL_MARKETS:
                continue
            key = _leg_result_key(leg, record)
            kickoff = _leg_kickoff_minutes(leg, record)
            if key is None or kickoff is None:
                continue
            try:
                fixture_day = date.fromisoformat(key[0])
            except ValueError:
                continue
            kickoff_total = fixture_day.toordinal() * 24 * 60 + kickoff
            if kickoff_total - int(lead_minutes) <= current_total <= kickoff_total + int(tail_minutes):
                keys.add(key)
    return keys


@_account_locked
def pending_bet_result_search(
    bet_id: str, game_date: str,
    minimum_days: int = RESULT_SEARCH_WAIT_DAYS,
) -> dict[str, Any]:
    """Validate one overdue pending bet and return its exact result keys."""
    requested_id = str(bet_id or "").strip()
    record = next(
        (
            item for item in load_bets()
            if str(item.get("bet_id") or "") == requested_id
        ),
        None,
    )
    if not record or record.get("status") != "pending" or record.get("demo_preview"):
        raise ValueError("该注单不存在或已经结算")
    if record.get("type") == "championship":
        raise ValueError("冠军盘请使用结算赛果功能")
    keys = {
        key for leg in record.get("legs") or [record]
        if (key := _leg_result_key(leg, record)) is not None
    }
    if not keys:
        raise ValueError("该注单缺少可搜索的比赛标识")
    try:
        current_date = date.fromisoformat(str(game_date))
        latest_fixture_date = max(date.fromisoformat(key[0]) for key in keys)
    except ValueError as error:
        raise ValueError("无法确认注单比赛日期") from error
    overdue_days = (current_date - latest_fixture_date).days
    if overdue_days < int(minimum_days):
        remaining = max(1, int(minimum_days) - overdue_days)
        raise ValueError(f"比赛结束未满{minimum_days}天，请再等待{remaining}天")
    return {
        "bet_id": requested_id,
        "keys": keys,
        "legs": [
            {"key": key, "leg": dict(leg)}
            for leg in record.get("legs") or [record]
            if (key := _leg_result_key(leg, record)) is not None
        ],
        "latest_fixture_date": latest_fixture_date.isoformat(),
        "overdue_days": overdue_days,
    }


def _adjacent_fixture_keys(
    left: tuple[str, int, int, int] | None,
    right: tuple[str, int, int, int] | None,
) -> bool:
    """Identify FM duplicate fixture rows whose dates drift by one day."""
    if not left or not right or left[1:] != right[1:]:
        return False
    try:
        return abs((date.fromisoformat(left[0]) - date.fromisoformat(right[0])).days) <= 1
    except ValueError:
        return False


def _result_for_leg(
    key: tuple[str, int, int, int] | None,
    exact_results: dict[tuple[str, int, int, int], dict[str, Any]],
    matchup_results: dict[tuple[int, int, int], list[dict[str, Any]]],
    fixture_address: Any = None,
) -> dict[str, Any] | None:
    if not key:
        return None
    exact = exact_results.get(key)
    if exact is not None:
        return exact
    try:
        fixture_date = date.fromisoformat(key[0])
    except ValueError:
        return None
    candidates = []
    for item in matchup_results.get(key[1:], []):
        try:
            distance = abs((date.fromisoformat(str(item["date"])) - fixture_date).days)
        except (KeyError, TypeError, ValueError):
            continue
        if distance <= 1:
            candidates.append((distance, item))
    if not candidates:
        return None
    nearest_distance = min(distance for distance, _item in candidates)
    nearest = [item for distance, item in candidates if distance == nearest_distance]
    return nearest[0] if len(nearest) == 1 else None


def save_bets(records: list[dict[str, Any]]) -> None:
    for record in records:
        if isinstance(record, dict):
            _normalize_bet_money(record)
    save_document("bets", records, legacy_path=bets_path())


def _analytics_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "bet_id": str(record.get("bet_id") or ""),
        "date": str(record.get("game_date") or record.get("placed_at") or "")[:10],
        "settled_at": str(record.get("settled_at") or ""),
        "stake": _round_money(float(record.get("stake") or 0)),
        "payout": _round_money(float(record.get("payout") or 0)),
        "status": str(record.get("status") or "void"),
    }


def _load_analytics_archive() -> list[dict[str, Any]]:
    payload = load_document(
        "bet_analytics", {}, legacy_path=bet_analytics_archive_path(),
    )
    rows = payload.get("records", []) if isinstance(payload, dict) else []
    return [item for item in rows if isinstance(item, dict)]


def betting_profit_analysis() -> dict[str, Any]:
    archived = _load_analytics_archive()
    current = [
        _analytics_record(record) for record in load_bets()
        if record.get("status") != "pending" and not record.get("demo_preview")
    ]
    unique: dict[str, dict[str, Any]] = {}
    for index, record in enumerate([*archived, *current]):
        key = str(record.get("bet_id") or f"legacy-{index}")
        unique[key] = record
    records = list(unique.values())

    stake_minors = [to_minor(item.get("stake") or 0) for item in records]
    payout_minors = [to_minor(item.get("payout") or 0) for item in records]
    total_stake_minor = sum(stake_minors)
    total_payout_minor = sum(payout_minors)
    net_profit_minor = total_payout_minor - total_stake_minor
    total_stake = from_minor(total_stake_minor)
    total_payout = from_minor(total_payout_minor)
    net_profit = from_minor(net_profit_minor)
    result_minors = [
        payout_minor - stake_minor
        for payout_minor, stake_minor in zip(payout_minors, stake_minors)
    ]
    results = [from_minor(value) for value in result_minors]
    wins = sum(1 for value in results if value > 0)
    losses = sum(1 for value in results if value < 0)
    pushes = len(results) - wins - losses

    longest_win = longest_loss = current_streak = 0
    current_direction = 0
    for value in results:
        direction = 1 if value > 0 else -1 if value < 0 else 0
        if direction == 0:
            continue
        current_streak = current_streak + 1 if direction == current_direction else 1
        current_direction = direction
        if direction > 0:
            longest_win = max(longest_win, current_streak)
        else:
            longest_loss = max(longest_loss, current_streak)

    daily: dict[str, dict[str, Any]] = {}
    for record, stake_minor, payout_minor, profit_minor in zip(
        records, stake_minors, payout_minors, result_minors,
    ):
        day = str(record.get("date") or "")[:10]
        if not day:
            continue
        row = daily.setdefault(day, {
            "date": day, "stake_minor": 0, "payout_minor": 0,
            "profit_minor": 0, "bets": 0,
        })
        row["stake_minor"] += stake_minor
        row["payout_minor"] += payout_minor
        row["profit_minor"] += profit_minor
        row["bets"] += 1
    daily_rows = []
    cumulative_minor = 0
    for day in sorted(daily):
        row = daily[day]
        cumulative_minor += row["profit_minor"]
        daily_rows.append({
            "date": day,
            "stake": from_minor(row["stake_minor"]),
            "payout": from_minor(row["payout_minor"]),
            "profit": from_minor(row["profit_minor"]),
            "cumulative_profit": from_minor(cumulative_minor),
            "bets": int(row["bets"]),
        })

    pending_exposure = from_minor(sum(
        read_minor(record, "stake") for record in load_bets()
        if record.get("status") == "pending" and not record.get("demo_preview")
    ))
    decisive = wins + losses
    return {
        "settled_bets": len(records),
        "total_stake": total_stake,
        "total_payout": total_payout,
        "net_profit": net_profit,
        "roi": round(net_profit / total_stake * 100, 2) if total_stake else 0.0,
        "win_rate": round(wins / decisive * 100, 2) if decisive else 0.0,
        "wins": wins,
        "losses": losses,
        "pushes": pushes,
        "pending_exposure": pending_exposure,
        "maximum_profit": max([0.0] + results),
        "maximum_loss": min([0.0] + results),
        "current_streak": current_streak * current_direction,
        "longest_win_streak": longest_win,
        "longest_loss_streak": longest_loss,
        "daily": daily_rows,
        "tickets": [
            {
                "date": str(record.get("date") or "")[:10],
                "profit": profit,
            }
            for record, profit in zip(records, results)
        ],
    }


@_account_locked
def clear_settled_bet_history(game_date: str) -> dict[str, Any]:
    records = load_bets()
    settled = [
        record for record in records
        if record.get("status") != "pending" and not record.get("demo_preview")
    ]
    retained = [record for record in records if record.get("status") == "pending"]
    deleted = len(records) - len(retained)
    if deleted:
        archived = _load_analytics_archive()
        known = {str(item.get("bet_id") or "") for item in archived}
        archived.extend(
            _analytics_record(record) for record in settled
            if str(record.get("bet_id") or "") not in known
        )
        save_document(
            "bet_analytics", {"schema_version": 1, "records": archived},
            legacy_path=bet_analytics_archive_path(),
        )
        save_bets(retained)
    return {
        "deleted": deleted,
        "remaining": len(retained),
        "game_date": game_date,
    }


@_account_locked
def has_pending_championship_bets() -> bool:
    return any(
        record.get("type") == "championship"
        and record.get("status") == "pending"
        and not record.get("demo_preview")
        for record in load_bets()
    )


def pending_snapshot_paths() -> set[Path]:
    paths: set[Path] = set()
    has_pending_championship = False
    for record in load_bets():
        if record.get("status") != "pending" or record.get("demo_preview"):
            continue
        has_pending_championship = has_pending_championship or record.get("type") == "championship"
        value = str(record.get("source_snapshot") or "").strip()
        if value:
            paths.add(Path(value).expanduser())
    if has_pending_championship:
        try:
            from tools.championship_odds import cup_state_path, league_state_path

            paths.update((league_state_path(), cup_state_path()))
        except (OSError, RuntimeError, ValueError):
            pass
    return paths


@_account_locked
def pending_result_fixture_hints(
    required_keys: set[tuple[str, int, int, int]] | None = None,
) -> dict[tuple[str, int, int, int], int]:
    """Recover exact fixture addresses without scanning memory or snapshot trees.

    With no explicit key set this remains limited to pending bets. Callers that
    already selected exact result keys may also recover hints from settled bets;
    this supports later read-only backfill of missing result details.
    """
    required = set(required_keys or ())
    hints: dict[tuple[str, int, int, int], int] = {}
    ambiguous: set[tuple[str, int, int, int]] = set()
    snapshot_paths: set[Path] = set()
    for record in load_bets():
        if record.get("demo_preview"):
            continue
        if record.get("status") != "pending" and not required:
            continue
        record_relevant = not required
        for leg in record.get("legs") or [record]:
            key = _leg_result_key(leg, record)
            if not key or required and key not in required:
                continue
            record_relevant = True
            try:
                address = int(str(leg.get("fixture_address") or "0"), 0)
            except (TypeError, ValueError):
                address = 0
            if address and key not in ambiguous:
                previous = hints.get(key)
                if previous is not None and previous != address:
                    # The public identity is insufficient when two different
                    # native fixture objects claim it. Fail closed.
                    hints.pop(key, None)
                    ambiguous.add(key)
                else:
                    hints[key] = address
        source = str(record.get("source_snapshot") or "").strip()
        if source and record_relevant:
            snapshot_paths.add(Path(source).expanduser())

    for path in snapshot_paths:
        try:
            stat = path.stat()
            cached = _SNAPSHOT_FIXTURE_HINT_CACHE.get(path)
            if cached and cached[:2] == (stat.st_mtime_ns, stat.st_size):
                snapshot_hints = cached[2]
            else:
                payload = json.loads(path.read_text(encoding="utf-8"))
                snapshot_hints = {}
                snapshot_ambiguous = set()
                for match in payload.get("matches") or []:
                    try:
                        key = (
                            str(match["fixture_date"]), int(match["competition_id"]),
                            int(match["home"]["id"]), int(match["away"]["id"]),
                        )
                        address = int(
                            str(match.get("fixture_address") or "0"), 0,
                        )
                    except (KeyError, TypeError, ValueError):
                        continue
                    if address and key not in snapshot_ambiguous:
                        previous = snapshot_hints.get(key)
                        if previous is not None and previous != address:
                            snapshot_hints.pop(key, None)
                            snapshot_ambiguous.add(key)
                        else:
                            snapshot_hints[key] = address
                _SNAPSHOT_FIXTURE_HINT_CACHE[path] = (
                    stat.st_mtime_ns, stat.st_size, snapshot_hints,
                )
                while len(_SNAPSHOT_FIXTURE_HINT_CACHE) > 32:
                    _SNAPSHOT_FIXTURE_HINT_CACHE.pop(
                        next(iter(_SNAPSHOT_FIXTURE_HINT_CACHE)), None,
                    )
        except (OSError, json.JSONDecodeError, TypeError):
            continue
        for key, address in snapshot_hints.items():
            if not required or key in required:
                if key in ambiguous:
                    continue
                previous = hints.get(key)
                if previous is not None and previous != address:
                    hints.pop(key, None)
                    ambiguous.add(key)
                else:
                    hints[key] = address
    return hints


def _single_bet_fixture_key(record: dict[str, Any]) -> tuple[str, int, int, int] | None:
    if record.get("type") != "single":
        return None
    try:
        fixture_date = str(record.get("fixture_date") or "")
        competition_id = int(record.get("competition_id"))
        home_id = int(record.get("home_id"))
        away_id = int(record.get("away_id"))
    except (TypeError, ValueError):
        return None
    if not fixture_date or competition_id <= 0 or home_id <= 0 or away_id <= 0:
        return None
    return fixture_date, competition_id, home_id, away_id


def _single_bet_potential_profit_minor(record: dict[str, Any]) -> int:
    if "potential_profit" in record or "potential_profit_minor" in record:
        return max(0, read_minor(record, "potential_profit"))
    try:
        odds = float(record.get("odds") or 0)
    except (TypeError, ValueError):
        return 0
    if not math.isfinite(odds) or odds <= 1:
        return 0
    stake_minor = read_minor(record, "stake")
    return max(0, multiply_minor(stake_minor, odds) - stake_minor)


def _assert_pending_single_profit_limit(
    existing: list[dict[str, Any]], incoming: list[dict[str, Any]], maximum_profit: float,
) -> None:
    limit_minor = to_minor(maximum_profit)
    if limit_minor <= 0:
        raise ValueError("单场最大可赢额必须大于 0")

    totals: dict[tuple[str, int, int, int], int] = {}
    for record in existing:
        if record.get("status") != "pending" or record.get("demo_preview"):
            continue
        key = _single_bet_fixture_key(record)
        if key is not None:
            totals[key] = totals.get(key, 0) + _single_bet_potential_profit_minor(record)

    incoming_keys: set[tuple[str, int, int, int]] = set()
    for record in incoming:
        key = _single_bet_fixture_key(record)
        if key is None:
            continue
        incoming_keys.add(key)
        totals[key] = totals.get(key, 0) + _single_bet_potential_profit_minor(record)

    if any(totals[key] > limit_minor for key in incoming_keys):
        raise ValueError(
            "该场比赛未结算单场注单的累计可赢额不能超过 "
            f"{from_minor(limit_minor):,.2f}"
        )


@_account_locked
def reserve_bets(
    records: list[dict[str, Any]], total_stake: float, *,
    single_profit_limit: float | None = None,
) -> dict[str, Any]:
    total_stake_minor = to_minor(total_stake)
    total_stake = from_minor(total_stake_minor)
    wallet = load_wallet()
    existing_bets = load_bets()
    submission_ids = {
        str(record.get("client_submission_id") or "").strip()
        for record in records
        if str(record.get("client_submission_id") or "").strip()
    }
    if len(submission_ids) == 1:
        submission_id = next(iter(submission_ids))
        reserved = [
            record for record in existing_bets
            if str(record.get("client_submission_id") or "") == submission_id
        ]
        if reserved:
            return {
                **wallet,
                "reserved_bets": reserved,
                "already_reserved": True,
            }
    if total_stake_minor <= 0:
        raise ValueError("下注金额必须大于 0")
    if total_stake_minor > _wallet_balance_minor(wallet):
        raise ValueError("可用余额不足")

    if single_profit_limit is not None:
        _assert_pending_single_profit_limit(
            existing_bets, records, single_profit_limit,
        )

    _set_wallet_balance_minor(
        wallet, _wallet_balance_minor(wallet) - total_stake_minor,
    )
    wallet["credit_effective_bet_count"] = (
        max(0, int(wallet.get("credit_effective_bet_count") or 0))
        + sum(1 for record in records if float(record.get("stake") or 0) > 1000)
    )
    transaction = {
        "id": str(uuid.uuid4()),
        "at": now(),
        "type": "bet_placed",
        "bet_ids": [record["bet_id"] for record in records],
    }
    write_minor(transaction, "amount", -total_stake_minor)
    write_minor(transaction, "balance_after", _wallet_balance_minor(wallet))
    wallet["transactions"].append(transaction)
    _commit_account(existing_bets + records, wallet, "reserve_bets")
    return {
        **wallet,
        "reserved_bets": records,
        "already_reserved": False,
    }


def _record_has_started(
    record: dict[str, Any], game_date: str, game_minutes: int,
    settlement_fixture_kickoffs: dict[tuple[str, int, int, int], int] | None = None,
) -> bool:
    try:
        current_date = date.fromisoformat(str(game_date))
    except ValueError:
        raise ValueError("无法读取当前游戏日期")
    if record.get("type") == "championship":
        try:
            return current_date >= date.fromisoformat(str(record.get("season_start") or ""))
        except ValueError:
            return True
    for leg in record.get("legs") or [record]:
        fixture_value = (
            leg.get("settlement_fixture_date")
            or leg.get("fixture_date") or record.get("fixture_date")
        )
        try:
            fixture_date = date.fromisoformat(str(fixture_value))
        except ValueError:
            continue
        if fixture_date < current_date:
            return True
        if fixture_date > current_date:
            continue
        kickoff = _leg_kickoff_minutes(leg, record, settlement_fixture_kickoffs)
        if kickoff is not None and int(game_minutes) >= kickoff:
            return True
    return False


def _leg_kickoff_minutes(
    leg: dict[str, Any], record: dict[str, Any],
    settlement_fixture_kickoffs: dict[tuple[str, int, int, int], int] | None = None,
) -> int | None:
    kickoff = leg.get("settlement_kickoff_minutes")
    if kickoff is None and leg.get("settlement_fixture_date"):
        try:
            settlement_key = (
                str(leg["settlement_fixture_date"]),
                int(leg.get("competition_id") or record.get("competition_id")),
                int(leg.get("settlement_home_id", leg.get("home_id"))),
                int(leg.get("settlement_away_id", leg.get("away_id"))),
            )
        except (TypeError, ValueError):
            settlement_key = None
        if settlement_key is not None:
            kickoff = (settlement_fixture_kickoffs or {}).get(settlement_key)
    if kickoff is None:
        kickoff = leg.get("kickoff_minutes")
    if kickoff is None:
        kickoff = record.get("kickoff_minutes")
    if kickoff is None:
        kickoff_time = str(
            leg.get("settlement_kickoff_time")
            or leg.get("kickoff_time") or record.get("kickoff_time") or ""
        )
        try:
            hours, minutes = (int(value) for value in kickoff_time.split(":", 1))
            kickoff = hours * 60 + minutes
        except (TypeError, ValueError):
            return None
    try:
        value = int(kickoff)
    except (TypeError, ValueError):
        return None
    return value if 0 <= value < 24 * 60 else None


def _manual_refund_unlock_at(
    record: dict[str, Any],
    settlement_fixture_kickoffs: dict[tuple[str, int, int, int], int] | None = None,
) -> datetime | None:
    latest_kickoff: datetime | None = None
    for leg in record.get("legs") or [record]:
        fixture_value = (
            leg.get("settlement_fixture_date")
            or leg.get("fixture_date") or record.get("fixture_date")
        )
        try:
            fixture_date = date.fromisoformat(str(fixture_value))
        except ValueError:
            continue
        kickoff = _leg_kickoff_minutes(leg, record, settlement_fixture_kickoffs)
        # Missing kickoff data must not make a post-match refund open early.
        if kickoff is None:
            kickoff = 24 * 60 - 1
        candidate = datetime.combine(fixture_date, datetime.min.time()) + timedelta(
            minutes=kickoff,
        )
        if latest_kickoff is None or candidate > latest_kickoff:
            latest_kickoff = candidate
    if latest_kickoff is None:
        return None
    return latest_kickoff + timedelta(
        days=MANUAL_REFUND_WAIT_DAYS,
        minutes=LIVE_SETTLEMENT_MINIMUM_MATCH_MINUTES,
    )


def _championship_refund_unlock_at(
    record: dict[str, Any],
    championship_settlement_dates: dict[str, str] | None = None,
) -> datetime | None:
    season_key = str(record.get("season_key") or "")
    settlement_date = str(
        (championship_settlement_dates or {}).get(season_key)
        or record.get("settlement_date")
        or record.get("season_end")
        or ""
    )
    try:
        settled_day = date.fromisoformat(settlement_date)
    except ValueError:
        return None
    return datetime.combine(
        settled_day + timedelta(days=MANUAL_REFUND_WAIT_DAYS),
        datetime.min.time(),
    )


def _manual_refund_quote(
    record: dict[str, Any], game_date: str, game_minutes: int,
    championship_settlement_dates: dict[str, str] | None = None,
    settlement_fixture_kickoffs: dict[tuple[str, int, int, int], int] | None = None,
) -> dict[str, Any]:
    stake_minor = read_minor(record, "stake")
    stake = from_minor(stake_minor)
    started = _record_has_started(
        record, game_date, game_minutes, settlement_fixture_kickoffs,
    )
    try:
        current = datetime.combine(
            date.fromisoformat(str(game_date)), datetime.min.time(),
        ) + timedelta(minutes=int(game_minutes))
    except (TypeError, ValueError):
        raise ValueError("无法读取当前游戏日期") from None
    championship = record.get("type") == "championship"
    unlock_at = (
        _championship_refund_unlock_at(record, championship_settlement_dates)
        if started and championship
        else _manual_refund_unlock_at(record, settlement_fixture_kickoffs)
        if started else None
    )
    available = not started or bool(unlock_at and current >= unlock_at)
    fee_minor = 0 if started else multiply_minor(stake_minor, 0.01)
    refund_minor = stake_minor - fee_minor
    fee = from_minor(fee_minor)
    refund = from_minor(refund_minor)
    return {
        "bet_id": str(record.get("bet_id") or ""),
        "stake": stake,
        "started": started,
        "available": available,
        "available_at": unlock_at.isoformat(timespec="minutes") if unlock_at else None,
        "fee": fee,
        "refund": refund if available else 0.0,
        "refund_type": "post_match_three_day_full" if started else "early_cashout",
    }


def manual_refund_options(
    game_date: str, game_minutes: int,
    championship_settlement_dates: dict[str, str] | None = None,
    settlement_fixture_kickoffs: dict[tuple[str, int, int, int], int] | None = None,
) -> dict[str, Any]:
    quotes = [
        _manual_refund_quote(
            record, game_date, game_minutes, championship_settlement_dates,
            settlement_fixture_kickoffs,
        )
        for record in load_bets()
        if record.get("status") == "pending" and not record.get("demo_preview")
    ]
    return {
        "game_date": str(game_date),
        "game_minutes": int(game_minutes),
        "options": [quote for quote in quotes if quote["available"]],
        "all_options": quotes,
    }


@_account_locked
def manual_refund_pending_bets(
    bet_ids: set[str], game_date: str, game_minutes: int,
    championship_settlement_dates: dict[str, str] | None = None,
    settlement_fixture_kickoffs: dict[tuple[str, int, int, int], int] | None = None,
) -> dict[str, Any]:
    requested_ids = {str(value).strip() for value in bet_ids if str(value).strip()}
    if not requested_ids:
        raise ValueError("请至少选择一笔待退款订单")

    records = load_bets()
    selected = [
        record for record in records
        if str(record.get("bet_id") or "") in requested_ids
        and record.get("status") == "pending"
        and not record.get("demo_preview")
    ]
    selected_ids = {str(record.get("bet_id") or "") for record in selected}
    if selected_ids != requested_ids:
        raise ValueError("所选订单已结算或不存在，请刷新后重新选择")

    quotes = {
        str(record.get("bet_id") or ""): _manual_refund_quote(
            record, game_date, game_minutes, championship_settlement_dates,
            settlement_fixture_kickoffs,
        )
        for record in selected
    }
    if any(not quote["available"] for quote in quotes.values()):
        raise ValueError("比赛结束未满3天，暂不能手动退款")

    wallet = load_wallet()
    refund_time = now()
    total_refund_minor = 0
    total_fee_minor = 0
    refunded = []
    for record in selected:
        quote = quotes[str(record.get("bet_id") or "")]
        refund_minor = to_minor(quote["refund"])
        fee_minor = to_minor(quote["fee"])
        refund = from_minor(refund_minor)
        fee = from_minor(fee_minor)
        if record.get("type") == "championship":
            season_key = str(record.get("season_key") or "")
            effective_settlement_date = str(
                (championship_settlement_dates or {}).get(season_key)
                or record.get("settlement_date")
                or record.get("season_end")
                or ""
            )
            if effective_settlement_date:
                record["settlement_date"] = effective_settlement_date
                record["fixture_date"] = effective_settlement_date
        legs = record.get("legs") or [record]
        record["status"] = "manual_refund"
        record["settled_at"] = refund_time
        record["manual_refunded_at"] = refund_time
        record["manual_refund_type"] = quote["refund_type"]
        _set_record_money(record, "manual_refund_fee", fee)
        _set_record_money(record, "payout", refund)
        record.pop("partial_settlement", None)
        record["settlement"] = [
            {
                "outcome": "void",
                "score": "手动退款",
                "result_date": str(leg.get("fixture_date") or record.get("fixture_date") or ""),
                "reason": quote["refund_type"],
            }
            for leg in legs
        ]
        balance_before_minor = _wallet_balance_minor(wallet)
        write_minor(record, "balance_before_settlement", balance_before_minor)
        _set_wallet_balance_minor(wallet, balance_before_minor + refund_minor)
        write_minor(
            record, "balance_after_settlement", _wallet_balance_minor(wallet),
        )
        total_refund_minor += refund_minor
        total_fee_minor += fee_minor
        refunded.append(quote)

    total_refund = from_minor(total_refund_minor)
    total_fee = from_minor(total_fee_minor)
    transaction = {
        "id": str(uuid.uuid4()),
        "at": refund_time,
        "type": "manual_bet_refund",
        "fee": total_fee,
        "fee_minor": total_fee_minor,
        "bet_ids": sorted(selected_ids),
    }
    write_minor(transaction, "amount", total_refund_minor)
    write_minor(transaction, "balance_after", _wallet_balance_minor(wallet))
    wallet["transactions"].append(transaction)
    _commit_account(records, wallet, "manual_refund_pending_bets")
    return {
        "refunded": len(refunded),
        "returned": total_refund,
        "fee": total_fee,
        "balance": from_minor(_wallet_balance_minor(wallet)),
        "records": refunded,
    }


@_account_locked
def refund_pending_schedule_changes(
    changes: dict[tuple[str, int, int, int], dict[str, Any]],
) -> dict[str, Any]:
    """Fully refund verified postponements; disappeared legs use timed voids."""
    # A fixture that merely disappears may be an FM phantom fixture whose
    # memory slot was recycled. Do not refund the whole ticket immediately;
    # the normal settlement path voids only that leg after three game days.
    actionable_changes = {
        key: change for key, change in changes.items()
        if str(change.get("reason") or "") != "fixture_disappeared"
    }
    if not actionable_changes:
        return {"refunded": 0, "returned": 0.0, "records": []}

    records = load_bets()
    wallet = load_wallet()
    refund_time = now()
    refunded: list[dict[str, Any]] = []
    total_refund_minor = 0
    refunded_ids: list[str] = []
    for record in records:
        if (
            record.get("status") != "pending"
            or record.get("demo_preview")
            or record.get("type") == "championship"
        ):
            continue
        legs = record.get("legs") or [record]
        affected = [
            (index, actionable_changes[key])
            for index, leg in enumerate(legs)
            if (key := _leg_result_key(leg, record)) in actionable_changes
        ]
        if not affected:
            continue

        affected_by_index = dict(affected)
        primary_change = affected[0][1]
        stake_minor = read_minor(record, "stake")
        stake = from_minor(stake_minor)
        record["status"] = "schedule_refund"
        record["settled_at"] = refund_time
        record["schedule_refunded_at"] = refund_time
        record["schedule_refund_reason"] = str(primary_change.get("reason") or "fixture_disappeared")
        record["schedule_refund_changes"] = [dict(change) for _index, change in affected]
        write_minor(record, "payout", stake_minor)
        record["settled_odds"] = 1.0
        record.pop("partial_settlement", None)
        record["settlement"] = [
            {
                "outcome": "void",
                "score": (
                    "比赛延期，整单自动退款"
                    if affected_by_index.get(index, {}).get("reason") == "fixture_postponed"
                    else "比赛取消或消失，整单自动退款"
                    if index in affected_by_index
                    else "关联比赛异常，整单自动退款"
                ),
                "result_date": str(
                    leg.get("settlement_fixture_date")
                    or leg.get("fixture_date") or record.get("fixture_date") or ""
                ),
                "reason": str(
                    affected_by_index.get(index, {}).get("reason")
                    or "related_schedule_change"
                ),
                "return_multiplier": 1.0,
                "settled_odds": 1.0,
            }
            for index, leg in enumerate(legs)
        ]
        balance_before_minor = _wallet_balance_minor(wallet)
        write_minor(record, "balance_before_settlement", balance_before_minor)
        _set_wallet_balance_minor(wallet, balance_before_minor + stake_minor)
        write_minor(
            record, "balance_after_settlement", _wallet_balance_minor(wallet),
        )
        total_refund_minor += stake_minor
        bet_id = str(record.get("bet_id") or "")
        refunded_ids.append(bet_id)
        first_leg = legs[0] if legs else {}
        refunded.append({
            "bet_id": bet_id,
            "status": "schedule_refund",
            "type": record.get("type", "single"),
            "legs": len(legs),
            "home": first_leg.get("home"),
            "away": first_leg.get("away"),
            "game_date": str(primary_change.get("original_date") or ""),
            "stake": stake,
            "payout": stake,
            "profit": 0.0,
            "refund_reason": record["schedule_refund_reason"],
            "refund_changes": [dict(change) for _index, change in affected],
        })

    if not refunded:
        return {"refunded": 0, "returned": 0.0, "records": []}
    total_refund = from_minor(total_refund_minor)
    transaction = {
        "id": str(uuid.uuid4()),
        "at": refund_time,
        "type": "bet_schedule_refund",
        "bet_ids": refunded_ids,
    }
    write_minor(transaction, "amount", total_refund_minor)
    write_minor(transaction, "balance_after", _wallet_balance_minor(wallet))
    wallet["transactions"].append(transaction)
    _commit_account(records, wallet, "refund_pending_schedule_changes")
    return {
        "refunded": len(refunded),
        "returned": total_refund,
        "balance": from_minor(_wallet_balance_minor(wallet)),
        "records": refunded,
    }


@_account_locked
def reopen_bets(bet_ids: set[str], reason: str) -> dict[str, Any]:
    records = load_bets()
    wallet = load_wallet()
    reopened = []
    payout_reversal_minor = 0
    for record in records:
        if record.get("bet_id") not in bet_ids or record.get("status") == "pending":
            continue
        payout_reversal_minor += read_minor(record, "payout")
        record["status"] = "pending"
        record["reopened_at"] = now()
        record["reopen_reason"] = reason
        for field in (
            "settled_at", "payout", "settlement", "settled_odds",
            "settlement_summary", "balance_before_settlement",
            "balance_after_settlement", "integrity_assessed",
            "integrity_event_keys",
        ):
            record.pop(field, None)
            record.pop(f"{field}_minor", None)
        reopened.append(record["bet_id"])
    if reopened:
        reversed_minor, unrecovered_minor = _reverse_wallet_credit(
            wallet, payout_reversal_minor,
        )
        transaction = {
            "id": str(uuid.uuid4()),
            "at": now(),
            "type": "premature_settlement_reversal",
            "bet_ids": reopened,
            "reason": reason,
        }
        write_minor(transaction, "amount", -reversed_minor)
        write_minor(transaction, "requested_reversal", payout_reversal_minor)
        write_minor(transaction, "unrecovered_reversal", unrecovered_minor)
        write_minor(transaction, "balance_after", _wallet_balance_minor(wallet))
        wallet["transactions"].append(transaction)
        _commit_account(records, wallet, "reopen_bets")
    return {
        "reopened": len(reopened),
        "reversed": from_minor(reversed_minor if reopened else 0),
        "unrecovered": from_minor(unrecovered_minor if reopened else 0),
        "balance": from_minor(_wallet_balance_minor(wallet)),
    }


@_account_locked
def migrate_legacy_bets() -> int:
    records = load_bets()
    timestamps_changed = False
    for record in records:
        if not record.get("placed_at_game") and record.get("game_date"):
            record.setdefault("recorded_at", record.get("placed_at"))
            record["placed_at_game"] = record["game_date"]
            record["placed_at"] = record["game_date"]
            timestamps_changed = True
    legacy = [record for record in records if not record.get("status")]
    if not legacy:
        if timestamps_changed:
            save_bets(records)
        return 0
    wallet = load_wallet()
    if any(item.get("type") == "legacy_bet_migration" for item in wallet["transactions"]):
        return 0

    total_stake_minor = 0
    for record in legacy:
        original_time = record.get("placed_at")
        record["bet_id"] = str(uuid.uuid4())
        record["status"] = "pending"
        record["recorded_at"] = original_time
        record["placed_at_game"] = record.get("game_date")
        record["placed_at"] = record.get("game_date") or original_time
        record["fixture_date"] = record.get("fixture_date") or record.get("game_date")
        if record.get("type") == "parlay":
            for leg in record.get("legs", []):
                leg.setdefault("fixture_date", record.get("fixture_date"))
                leg.setdefault("competition_id", record.get("competition_id"))
                leg.setdefault("competition_name", record.get("competition_name"))
            stake_minor = read_minor(record, "stake")
            write_minor(
                record, "potential_return",
                multiply_minor(stake_minor, record.get("odds", 1)),
            )
        else:
            home = record.get("home", {})
            away = record.get("away", {})
            if isinstance(home, dict):
                record["home_id"] = home.get("id")
                record["home"] = home.get("name")
            if isinstance(away, dict):
                record["away_id"] = away.get("id")
                record["away"] = away.get("name")
            record["type"] = "single"
            stake_minor = read_minor(record, "stake")
            write_minor(
                record, "potential_return",
                multiply_minor(stake_minor, record.get("odds", 1)),
            )
        stake_minor = read_minor(record, "stake")
        potential_return_minor = read_minor(record, "potential_return")
        write_minor(
            record, "potential_profit", potential_return_minor - stake_minor,
        )
        total_stake_minor += stake_minor

    _set_wallet_balance_minor(
        wallet, _wallet_balance_minor(wallet) - total_stake_minor,
    )
    transaction = {
        "id": str(uuid.uuid4()),
        "at": now(),
        "type": "legacy_bet_migration",
        "bet_ids": [record["bet_id"] for record in legacy],
    }
    write_minor(transaction, "amount", -total_stake_minor)
    write_minor(transaction, "balance_after", _wallet_balance_minor(wallet))
    wallet["transactions"].append(transaction)
    _commit_account(records, wallet, "migrate_legacy_bets")
    return len(legacy)


def result_key(result: dict[str, Any]) -> tuple[str, int, int, int]:
    return (
        result["date"],
        int(result["competition"]["id"]),
        int(result["home_team"]["id"]),
        int(result["away_team"]["id"]),
    )


def _leg_result_key(leg: dict[str, Any], record: dict[str, Any]) -> tuple[str, int, int, int] | None:
    if record.get("type") == "championship":
        return None
    fixture_date = (
        leg.get("settlement_fixture_date")
        or leg.get("fixture_date") or record.get("fixture_date")
    )
    competition_id = leg.get("competition_id") or record.get("competition_id")
    if not fixture_date or competition_id is None:
        return None
    home_id = leg.get("settlement_home_id", leg["home_id"])
    away_id = leg.get("settlement_away_id", leg["away_id"])
    return (str(fixture_date), int(competition_id), int(home_id), int(away_id))


def _missing_result_void_evaluation(
    leg: dict[str, Any], record: dict[str, Any], game_date: str | None,
) -> dict[str, Any] | None:
    """Void one leg after three game days without any matching final result."""
    key = _leg_result_key(leg, record)
    if key is None or not game_date:
        return None
    try:
        elapsed_days = (
            date.fromisoformat(str(game_date)) - date.fromisoformat(key[0])
        ).days
    except ValueError:
        return None
    if elapsed_days < RESULT_SEARCH_WAIT_DAYS:
        return None
    return {
        "outcome": "void",
        "score": "赛果缺失，3天后走水",
        "result_date": key[0],
        "reason": "missing_result_after_three_days",
        "return_multiplier": 1.0,
        "settled_odds": 1.0,
    }


def _selection_code(leg: dict[str, Any]) -> str:
    explicit = leg.get("selection_code")
    if explicit:
        return str(explicit)
    selection = str(leg.get("selection", "")).lower()
    market = leg.get("market")
    if market == "1X2":
        if selection == str(leg.get("home", "")).lower():
            return "home"
        if selection == str(leg.get("away", "")).lower():
            return "away"
        return "draw"
    if market == "BTTS":
        return "yes" if selection in {"yes", "是"} else "no"
    if market == "AH":
        return "home" if selection == str(leg.get("home", "")).lower() else "away"
    if market == "OU":
        return "over" if selection == "over" or selection.startswith("大") else "under"
    return ""


def _knockout_decision_method(result: dict[str, Any]) -> str:
    method = str(result.get("decided_by") or "")
    return "regular" if method == "regular_time" else method


def _knockout_advanced_team_id(
    leg: dict[str, Any], result: dict[str, Any],
) -> int | None:
    """Return an advancement winner only when the knockout evidence proves it."""
    try:
        explicit = int(result.get("advanced_team_id") or 0)
    except (TypeError, ValueError):
        explicit = 0
    if explicit > 0:
        return explicit
    side = str(result.get("winner_side") or "")
    if side not in {"home", "away"}:
        return None
    method = _knockout_decision_method(result)
    try:
        single_leg = int(leg.get("settlement_leg_count") or 0) == 1
    except (TypeError, ValueError):
        single_leg = False
    # Extra-time and shootout winners necessarily decide the tie, including a
    # two-legged tie. A regular-time match winner decides advancement only
    # when the bet-time knockout context proves this was a single-leg tie.
    if method not in {"extra_time", "penalties"} and not single_leg:
        return None
    team = result.get(f"{side}_team") or {}
    try:
        team_id = int(team.get("id") or 0)
    except (TypeError, ValueError):
        team_id = 0
    if team_id <= 0:
        try:
            team_id = int(
                leg.get(f"settlement_{side}_id") or leg.get(f"{side}_id") or 0
            )
        except (TypeError, ValueError):
            team_id = 0
    return team_id or None


def missing_result_detail_fields(
    leg: dict[str, Any], result: dict[str, Any],
) -> tuple[str, ...]:
    """Describe settlement evidence absent from an otherwise completed result."""
    market = str(leg.get("market") or "")
    if market in HALF_TIME_MARKETS:
        return () if (
            result.get("half_home_goals") is not None
            and result.get("half_away_goals") is not None
        ) else ("半场比分",)
    if market == "FIRST_SCORE":
        total_goals = int(result.get("home_goals") or 0) + int(result.get("away_goals") or 0)
        return () if total_goals == 0 or result.get("first_scoring_team") in {"home", "away", "none"} else ("首个进球队伍",)
    if market == "ADVANCE":
        return () if _knockout_advanced_team_id(leg, result) is not None else ("晋级球队",)
    if market in {"EXTRA_TIME", "PENALTIES"}:
        return () if _knockout_decision_method(result) in {"regular", "extra_time", "penalties"} else ("决胜方式",)
    if market == "ADVANCE_METHOD":
        missing = []
        if _knockout_advanced_team_id(leg, result) is None:
            missing.append("晋级球队")
        if _knockout_decision_method(result) not in {"regular", "extra_time", "penalties"}:
            missing.append("决胜方式")
        return tuple(missing)
    return ()


def result_details_available(leg: dict[str, Any], result: dict[str, Any]) -> bool:
    """Return false while a completed result still lacks market-specific details."""
    return not missing_result_detail_fields(leg, result)


def _asian_handicap_outcome(
    home_goals: int, away_goals: int, home_line: float, side: str,
) -> str:
    team_difference = (
        home_goals - away_goals if side == "home" else away_goals - home_goals
    )
    team_line = home_line if side == "home" else -home_line
    lines = (
        [team_line - 0.25, team_line + 0.25]
        if abs(round(team_line * 4)) % 2 == 1 else [team_line]
    )
    parts = []
    for line in lines:
        value = team_difference + line
        parts.append("won" if value > 0 else "lost" if value < 0 else "void")
    outcomes = set(parts)
    if outcomes == {"won", "void"}:
        return "half_won"
    if outcomes == {"lost", "void"}:
        return "half_lost"
    return parts[0]


def _asian_total_outcome(goals: int, line: float, side: str) -> str:
    lines = (
        [line - 0.25, line + 0.25]
        if abs(round(line * 4)) % 2 == 1 else [line]
    )
    parts = []
    for actual_line in lines:
        value = goals - actual_line
        if side == "under":
            value = -value
        parts.append("won" if value > 0 else "lost" if value < 0 else "void")
    outcomes = set(parts)
    if outcomes == {"won", "void"}:
        return "half_won"
    if outcomes == {"lost", "void"}:
        return "half_lost"
    return parts[0]


def _return_multiplier(leg: dict[str, Any], evaluation: dict[str, Any]) -> float:
    stored = evaluation.get("return_multiplier")
    if stored is not None:
        return float(stored)
    odds = float(leg.get("odds") or 1.0)
    return {
        "won": odds,
        "half_won": (odds + 1.0) / 2.0,
        "void": 1.0,
        "half_lost": 0.5,
        "lost": 0.0,
    }.get(str(evaluation.get("outcome")), 0.0)


def evaluate_leg(leg: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    home_goals = int(result["home_goals"])
    away_goals = int(result["away_goals"])
    market = leg["market"]
    code = _selection_code(leg)

    half_result_available = (
        result.get("half_home_goals") is not None
        and result.get("half_away_goals") is not None
    )
    if market in HALF_TIME_MARKETS and not half_result_available:
        raise ValueError("半场赛果暂不可用")
    half_home_goals = int(result.get("half_home_goals") or 0)
    half_away_goals = int(result.get("half_away_goals") or 0)

    if market == "1X2":
        winner = "home" if home_goals > away_goals else "away" if away_goals > home_goals else "draw"
        outcome = "won" if code == winner else "lost"
    elif market == "DOUBLE_CHANCE":
        winner = "home" if home_goals > away_goals else "away" if away_goals > home_goals else "draw"
        outcome = "won" if winner in code.split("_") else "lost"
    elif market == "BTTS":
        both_scored = home_goals > 0 and away_goals > 0
        outcome = "won" if (code == "yes") == both_scored else "lost"
    elif market == "GOAL_PARITY":
        parity = "odd" if (home_goals + away_goals) % 2 else "even"
        outcome = "won" if code == parity else "lost"
    elif market == "FIRST_SCORE":
        first_side = result.get("first_scoring_team")
        if home_goals + away_goals == 0:
            first_side = "none"
        if first_side not in {"home", "away", "none"}:
            raise ValueError("首个进球队伍暂不可用")
        outcome = "won" if code == first_side else "lost"
    elif market == "AH":
        line = float(leg["line"])
        outcome = _asian_handicap_outcome(home_goals, away_goals, line, code)
    elif market == "OU":
        outcome = _asian_total_outcome(home_goals + away_goals, float(leg["line"]), code)
    elif market == "TEAM_OU":
        side, direction = code.split("_", 1)
        goals = home_goals if side == "home" else away_goals
        outcome = _asian_total_outcome(goals, float(leg["line"]), direction)
    elif market == "WINNING_MARGIN":
        difference = home_goals - away_goals
        actual = (
            "draw" if difference == 0
            else f"home_{difference}" if difference in {1, 2}
            else "home_3_plus" if difference >= 3
            else f"away_{-difference}" if difference in {-1, -2}
            else "away_3_plus"
        )
        outcome = "won" if code == actual else "lost"
    elif market == "CLEAN_SHEET":
        clean = away_goals == 0 if code.startswith("home") else home_goals == 0
        outcome = "won" if clean == code.endswith("yes") else "lost"
    elif market == "WIN_TO_NIL":
        won_to_nil = (
            home_goals > 0 and away_goals == 0
            if code == "home" else away_goals > 0 and home_goals == 0
        )
        outcome = "won" if won_to_nil else "lost"
    elif market == "ADVANCE":
        advanced_team_id = _knockout_advanced_team_id(leg, result)
        if advanced_team_id is None:
            raise ValueError("晋级球队暂不可用")
        outcome = "won" if int(code) == advanced_team_id else "lost"
    elif market == "EXTRA_TIME":
        played = _knockout_decision_method(result) in {"extra_time", "penalties"}
        outcome = "won" if (code == "yes") == played else "lost"
    elif market == "PENALTIES":
        played = _knockout_decision_method(result) == "penalties"
        outcome = "won" if (code == "yes") == played else "lost"
    elif market == "ADVANCE_METHOD":
        advanced_team_id = _knockout_advanced_team_id(leg, result)
        if advanced_team_id is None:
            raise ValueError("晋级球队暂不可用")
        actual = f"{advanced_team_id}_{_knockout_decision_method(result)}"
        outcome = "won" if code == actual else "lost"
    elif market == "HT_1X2":
        winner = (
            "home" if half_home_goals > half_away_goals
            else "away" if half_away_goals > half_home_goals else "draw"
        )
        outcome = "won" if code == winner else "lost"
    elif market == "HTFT":
        half_winner = (
            "home" if half_home_goals > half_away_goals
            else "away" if half_away_goals > half_home_goals else "draw"
        )
        full_winner = "home" if home_goals > away_goals else "away" if away_goals > home_goals else "draw"
        outcome = "won" if code == f"{half_winner}_{full_winner}" else "lost"
    elif market == "HT_AH":
        outcome = _asian_handicap_outcome(
            half_home_goals, half_away_goals, float(leg["line"]), code,
        )
    elif market == "HT_OU":
        outcome = _asian_total_outcome(
            half_home_goals + half_away_goals, float(leg["line"]), code,
        )
    elif market == "SH_1X2":
        second_home_goals = home_goals - half_home_goals
        second_away_goals = away_goals - half_away_goals
        winner = (
            "home" if second_home_goals > second_away_goals
            else "away" if second_away_goals > second_home_goals else "draw"
        )
        outcome = "won" if code == winner else "lost"
    elif market == "SH_AH":
        outcome = _asian_handicap_outcome(
            home_goals - half_home_goals,
            away_goals - half_away_goals,
            float(leg["line"]), code,
        )
    elif market == "SH_OU":
        second_goals = (
            home_goals + away_goals - half_home_goals - half_away_goals
        )
        outcome = _asian_total_outcome(second_goals, float(leg["line"]), code)
    elif market == "SH_TEAM_GOALS":
        target = int(float(leg["line"]))
        goals = (
            home_goals - half_home_goals
            if code.startswith("home") else away_goals - half_away_goals
        )
        outcome = "won" if (goals >= target if code.endswith("plus") else goals == target) else "lost"
    elif market == "SH_TOTAL_GOALS":
        target = int(float(leg["line"]))
        goals = home_goals + away_goals - half_home_goals - half_away_goals
        outcome = "won" if (goals >= target if code in {"two_plus", "three_plus"} else goals == target) else "lost"
    elif market == "SH_SCORE":
        try:
            target_home, target_away = (int(value) for value in str(leg["line"]).split("-", 1))
        except (TypeError, ValueError):
            raise ValueError("下半场精确比分选项无效")
        second_score = (home_goals - half_home_goals, away_goals - half_away_goals)
        outcome = "won" if second_score == (target_home, target_away) else "lost"
    elif market == "HIGHEST_SCORING_HALF":
        first_goals = half_home_goals + half_away_goals
        second_goals = home_goals + away_goals - first_goals
        actual = "first" if first_goals > second_goals else "second" if second_goals > first_goals else "equal"
        outcome = "won" if code == actual else "lost"
    elif market == "HT_TOTAL_GOALS":
        target = int(float(leg["line"]))
        goals = half_home_goals + half_away_goals
        outcome = "won" if (goals >= target if code in {"two_plus", "three_plus"} else goals == target) else "lost"
    elif market == "HT_TEAM_GOALS":
        target = int(float(leg["line"]))
        goals = half_home_goals if code.startswith("home") else half_away_goals
        outcome = "won" if (goals >= target if code.endswith("plus") else goals == target) else "lost"
    elif market == "HT_SCORE":
        try:
            target_home, target_away = (int(value) for value in str(leg["line"]).split("-", 1))
        except (TypeError, ValueError):
            raise ValueError("半场精确比分选项无效")
        outcome = "won" if (half_home_goals, half_away_goals) == (target_home, target_away) else "lost"
    elif market == "TEAM_GOALS":
        target = int(float(leg["line"]))
        goals = home_goals if code.startswith("home") else away_goals
        outcome = "won" if (goals >= target if code.endswith("plus") else goals == target) else "lost"
    elif market == "TOTAL_GOALS":
        target = int(float(leg["line"]))
        goals = home_goals + away_goals
        outcome = "won" if (goals >= target if code in {"six_plus", "seven_plus"} else goals == target) else "lost"
    elif market == "SCORE":
        try:
            target_home, target_away = (int(value) for value in str(leg["line"]).split("-", 1))
        except (TypeError, ValueError):
            raise ValueError("精确比分选项无效")
        outcome = "won" if (home_goals, away_goals) == (target_home, target_away) else "lost"
    else:
        raise ValueError(f"不支持的投注市场：{market}")
    evaluation = {
        "outcome": outcome,
        "score": f"{home_goals}-{away_goals}",
        "half_score": f"{half_home_goals}-{half_away_goals}" if half_result_available else None,
        "result_date": result["date"],
    }
    evaluation["return_multiplier"] = _return_multiplier(leg, evaluation)
    return evaluation


def _system_group_indexes(record: dict[str, Any], leg_count: int) -> list[int]:
    indexes: list[int] = []
    if record.get("type") == "system_parlay" and record.get("groups"):
        for group_index, group in enumerate(record["groups"]):
            indexes.extend([group_index] * len(group.get("selections") or []))
    return indexes if len(indexes) == leg_count else list(range(leg_count))


def _has_legacy_same_group_duplicate(
    record: dict[str, Any], evaluations: list[dict[str, Any]],
) -> bool:
    """Detect the old bug that voided option 2+ inside one system group."""
    legs = record.get("legs") or [record]
    if record.get("type") != "system_parlay" or len(evaluations) != len(legs):
        return False
    group_indexes = _system_group_indexes(record, len(legs))
    first_evaluation: dict[int, dict[str, Any]] = {}
    seen_keys: dict[int, list[tuple[str, int, int, int]]] = {}
    for index, (leg, evaluation) in enumerate(zip(legs, evaluations)):
        group_index = group_indexes[index]
        first_evaluation.setdefault(group_index, evaluation)
        key = _leg_result_key(leg, record)
        if (
            evaluation.get("reason") == "duplicate_fixture_leg"
            and first_evaluation[group_index].get("reason") != "duplicate_fixture_leg"
            and any(
                _adjacent_fixture_keys(key, previous)
                for previous in seen_keys.get(group_index, [])
            )
        ):
            return True
        if key:
            seen_keys.setdefault(group_index, []).append(key)
    return False


def _reopen_untrusted_settlements(
    records: list[dict[str, Any]], wallet: dict[str, Any], settlement_time: str,
    results: dict[tuple[str, int, int, int], dict[str, Any]],
    results_by_matchup: dict[tuple[int, int, int], list[dict[str, Any]]],
) -> tuple[int, int, int]:
    reopened = 0
    conflicts = 0
    conflicts_changed = 0
    for record in records:
        evaluations = record.get("settlement") or []
        legs = record.get("legs") or [record]
        was_missing_result_refund = record.get("status") == "no_result"
        has_untrusted_evaluation = any(
            item.get("reason") in UNTRUSTED_SETTLEMENT_REASONS
            or item.get("outcome") == "no_result"
            for item in evaluations
        )
        missing_detail_legs = [
            (leg, evaluation)
            for leg, evaluation in zip(record.get("legs") or [record], evaluations)
            if evaluation.get("reason") == "missing_result_details"
        ]
        recovered_missing_details = bool(missing_detail_legs) and all(
            (
                result := _result_for_leg(
                    _leg_result_key(leg, record), results, results_by_matchup,
                    leg.get("fixture_address"),
                )
            ) is not None
            and result_details_available(leg, result)
            for leg, _evaluation in missing_detail_legs
        )
        has_legacy_same_group_duplicate = _has_legacy_same_group_duplicate(
            record, evaluations,
        )
        has_corrected_result_score = bool(
            record.get("status") in {"won", "lost", "void"}
            and len(legs) == len(evaluations)
            and not has_legacy_same_group_duplicate
            and any(
                result is not None
                and evaluation.get("reason") != "duplicate_fixture_leg"
                and re.fullmatch(
                    r"\d+-\d+", str(evaluation.get("score") or ""),
                ) is not None
                and str(evaluation.get("score")) != (
                    f"{int(result['home_goals'])}-{int(result['away_goals'])}"
                )
                for leg, evaluation in zip(legs, evaluations)
                for result in [
                    _result_for_leg(
                        _leg_result_key(leg, record), results, results_by_matchup,
                        leg.get("fixture_address"),
                    )
                ]
            )
        )
        if has_corrected_result_score:
            observed = []
            for leg, evaluation in zip(legs, evaluations):
                result = _result_for_leg(
                    _leg_result_key(leg, record), results, results_by_matchup,
                    leg.get("fixture_address"),
                )
                if result is None:
                    continue
                observed.append({
                    "stored_score": str(evaluation.get("score") or ""),
                    "observed_score": (
                        f"{int(result['home_goals'])}-{int(result['away_goals'])}"
                    ),
                    "result_source": result.get("result_source") or result.get("source"),
                    "result_address": result.get("result_address"),
                })
            conflict = {
                "reason": "conflicting_final_score",
                "detected_at": settlement_time,
                "observations": observed,
            }
            previous_conflict = record.get("settlement_conflict")
            if isinstance(previous_conflict, dict):
                conflict["detected_at"] = previous_conflict.get(
                    "detected_at", settlement_time,
                )
            if previous_conflict != conflict:
                record["settlement_conflict"] = conflict
                conflicts_changed += 1
            conflicts += 1
            continue
        if (
            record.get("status") == "pending"
            or record.get("demo_preview")
            or not (
                was_missing_result_refund
                or has_untrusted_evaluation
                or recovered_missing_details
                or has_legacy_same_group_duplicate
            )
        ):
            continue
        reversal_reason = (
            "legacy_same_group_duplicate_reversal"
            if has_legacy_same_group_duplicate else "untrusted_settlement_reversal"
        )
        if recovered_missing_details:
            reversal_reason = "recovered_result_details_reversal"
        previous_payout_minor = read_minor(record, "payout")
        reversed_minor, unrecovered_minor = _reverse_wallet_credit(
            wallet, previous_payout_minor,
        )
        transaction = {
            "id": str(uuid.uuid4()),
            "at": settlement_time,
            "type": "premature_cancellation_reversal",
            "bet_ids": [record["bet_id"]],
            "reason": reversal_reason,
        }
        write_minor(transaction, "amount", -reversed_minor)
        write_minor(transaction, "requested_reversal", previous_payout_minor)
        write_minor(transaction, "unrecovered_reversal", unrecovered_minor)
        write_minor(transaction, "balance_after", _wallet_balance_minor(wallet))
        wallet["transactions"].append(transaction)
        record["status"] = "pending"
        record["reopened_at"] = settlement_time
        record["reopen_reason"] = reversal_reason
        for field in (
            "settled_at", "payout", "settlement", "settled_odds",
            "settlement_summary", "balance_before_settlement",
            "balance_after_settlement", "no_result_reason",
            "no_result_check_count",
        ):
            record.pop(field, None)
            record.pop(f"{field}_minor", None)
        reopened += 1
    return reopened, conflicts, conflicts_changed


def _completed_result_conflicts(
    completed_results: list[dict[str, Any]],
) -> dict[tuple[str, int, int, int], list[dict[str, Any]]]:
    grouped: dict[
        tuple[str, int, int, int], dict[tuple[int, int], list[dict[str, Any]]]
    ] = {}
    for item in completed_results:
        try:
            key = result_key(item)
            score = (int(item["home_goals"]), int(item["away_goals"]))
        except (KeyError, TypeError, ValueError):
            continue
        grouped.setdefault(key, {}).setdefault(score, []).append(item)
    conflicts = {}
    for key, scores in grouped.items():
        rows = [item for items in scores.values() for item in items]
        if len(scores) > 1 or any(item.get("result_conflict") for item in rows):
            candidates = [{
                "home_goals": score[0],
                "away_goals": score[1],
                "sources": sorted({
                    str(item.get("result_source") or item.get("source") or "unknown")
                    for item in items
                }),
                "addresses": sorted({
                    str(item.get("result_address") or "") for item in items
                    if item.get("result_address")
                }),
            } for score, items in sorted(scores.items())]
            for item in rows:
                for candidate in item.get("result_conflict_candidates") or []:
                    if not isinstance(candidate, dict):
                        continue
                    try:
                        candidates.append({
                            "home_goals": int(candidate["home_goals"]),
                            "away_goals": int(candidate["away_goals"]),
                            "sources": [str(candidate.get("result_source") or "unknown")],
                            "addresses": [str(candidate["result_address"])]
                            if candidate.get("result_address") else [],
                        })
                    except (KeyError, TypeError, ValueError):
                        continue
            conflicts[key] = list({
                (
                    candidate["home_goals"], candidate["away_goals"],
                    tuple(candidate["sources"]), tuple(candidate["addresses"]),
                ): candidate
                for candidate in candidates
            }.values())
    return conflicts


def _evaluate_record_legs(
    record: dict[str, Any],
    results: dict[tuple[str, int, int, int], dict[str, Any]],
    results_by_matchup: dict[tuple[int, int, int], list[dict[str, Any]]],
    game_date: str | None = None,
    *,
    void_missing_after_timeout: bool = False,
) -> list[dict[str, Any]] | None:
    """Return complete evaluations, or None while any required result is missing."""
    legs = record.get("legs") or [record]
    evaluations = []
    group_indexes = _system_group_indexes(record, len(legs))

    evaluated_group_keys: dict[int, tuple[str, int, int, int]] = {}
    for index, leg in enumerate(legs):
        group_index = group_indexes[index]
        key = _leg_result_key(leg, record)
        if any(
            previous_group != group_index
            and _adjacent_fixture_keys(key, previous_key)
            for previous_group, previous_key in evaluated_group_keys.items()
        ):
            evaluations.append({
                "outcome": "void",
                "score": "重复赛程",
                "result_date": key[0] if key else "",
                "reason": "duplicate_fixture_leg",
                "settled_odds": 1.0,
            })
            continue
        if key:
            evaluated_group_keys.setdefault(group_index, key)
        result = _result_for_leg(
            key, results, results_by_matchup, leg.get("fixture_address"),
        )
        if result is None:
            missing_evaluation = (
                _missing_result_void_evaluation(leg, record, game_date)
                if void_missing_after_timeout else None
            )
            if missing_evaluation is None:
                return None
            evaluations.append(missing_evaluation)
            continue
        if not result_details_available(leg, result):
            # A completed score without the market-specific evidence is not a
            # push. Keep the bet pending until the detail is recovered or the
            # user explicitly chooses the existing manual-refund path.
            return None
        try:
            evaluations.append(evaluate_leg(leg, result))
        except (KeyError, TypeError, ValueError):
            return None
    return evaluations or None


def _evaluate_available_record_legs(
    record: dict[str, Any],
    results: dict[tuple[str, int, int, int], dict[str, Any]],
    results_by_matchup: dict[tuple[int, int, int], list[dict[str, Any]]],
    game_date: str | None,
    game_minutes: int | None,
    *,
    void_missing_after_timeout: bool = False,
) -> list[dict[str, Any] | None]:
    """Evaluate trustworthy due legs without settling the enclosing ticket."""
    legs = record.get("legs") or [record]
    evaluations: list[dict[str, Any] | None] = []
    group_indexes = _system_group_indexes(record, len(legs))
    evaluated_group_keys: dict[int, tuple[str, int, int, int]] = {}

    for index, leg in enumerate(legs):
        if game_date and not _leg_due_for_settlement(
            leg, record, game_date, game_minutes,
        ):
            evaluations.append(None)
            continue
        group_index = group_indexes[index]
        key = _leg_result_key(leg, record)
        if any(
            previous_group != group_index
            and _adjacent_fixture_keys(key, previous_key)
            for previous_group, previous_key in evaluated_group_keys.items()
        ):
            evaluations.append({
                "outcome": "void",
                "score": "重复赛程",
                "result_date": key[0] if key else "",
                "reason": "duplicate_fixture_leg",
                "settled_odds": 1.0,
            })
            continue
        if key:
            evaluated_group_keys.setdefault(group_index, key)
        result = _result_for_leg(
            key, results, results_by_matchup, leg.get("fixture_address"),
        )
        if result is None:
            evaluation = (
                _missing_result_void_evaluation(leg, record, game_date)
                if void_missing_after_timeout else None
            )
            evaluations.append(evaluation)
            continue
        if not result_details_available(leg, result):
            evaluations.append(None)
            continue
        try:
            evaluations.append(evaluate_leg(leg, result))
        except (KeyError, TypeError, ValueError):
            evaluations.append(None)
    return evaluations


def _backfill_settled_half_scores(
    records: list[dict[str, Any]],
    results: dict[tuple[str, int, int, int], dict[str, Any]],
    results_by_matchup: dict[tuple[int, int, int], list[dict[str, Any]]],
    backfilled_at: str,
) -> int:
    """Enrich immutable settlement history without recalculating any payout."""
    if not any(
        item.get("half_home_goals") is not None
        and item.get("half_away_goals") is not None
        for item in results.values()
    ):
        return 0
    updated = 0
    for record in records:
        if record.get("status") in {"pending", "no_result"} or record.get("demo_preview"):
            continue
        legs = record.get("legs") or [record]
        evaluations = record.get("settlement") or []
        if len(legs) != len(evaluations):
            continue
        record_updated = False
        for leg, evaluation in zip(legs, evaluations):
            if evaluation.get("half_score") not in {None, ""}:
                continue
            result = _result_for_leg(
                _leg_result_key(leg, record), results, results_by_matchup,
                leg.get("fixture_address"),
            )
            if result is None:
                continue
            try:
                home_goals = int(result["home_goals"])
                away_goals = int(result["away_goals"])
                half_home = int(result["half_home_goals"])
                half_away = int(result["half_away_goals"])
            except (KeyError, TypeError, ValueError):
                continue
            if (
                evaluation.get("score") != f"{home_goals}-{away_goals}"
                or not 0 <= half_home <= home_goals
                or not 0 <= half_away <= away_goals
            ):
                continue
            evaluation["half_score"] = f"{half_home}-{half_away}"
            record_updated = True
            updated += 1
        if record_updated:
            record["result_details_backfilled_at"] = backfilled_at
    return updated


def _record_settlement_amount(
    record: dict[str, Any], evaluations: list[dict[str, Any]],
) -> tuple[str, float, float, dict[str, Any] | None]:
    legs = record.get("legs") or [record]
    stake_minor = read_minor(record, "stake")
    stake = from_minor(stake_minor)
    if record.get("type") != "system_parlay" or not record.get("groups"):
        multiplier = math.prod(
            _return_multiplier(leg, evaluation)
            for leg, evaluation in zip(legs, evaluations)
        )
        payout_minor = multiply_minor(stake_minor, multiplier)
        payout = from_minor(payout_minor)
        status = (
            "void" if all(item["outcome"] == "void" for item in evaluations)
            else "won" if payout > stake else "lost" if payout < stake else "void"
        )
        return status, payout, multiplier, None

    unit_stake_minor = read_minor(record, "unit_stake")
    unit_stake = from_minor(unit_stake_minor)
    offset = 0
    group_returns = []
    group_hits = []
    all_void = True
    for group in record["groups"]:
        selections = group.get("selections") or []
        group_evaluations = evaluations[offset:offset + len(selections)]
        offset += len(selections)
        group_returns.append(sum(
            _return_multiplier(leg, evaluation)
            for leg, evaluation in zip(selections, group_evaluations)
        ))
        group_hits.append(sum(
            1 for leg, evaluation in zip(selections, group_evaluations)
            if _return_multiplier(leg, evaluation) > 0
        ))
        all_void = all_void and all(
            evaluation["outcome"] == "void" for evaluation in group_evaluations
        )
    try:
        _, pass_sizes = pass_leg_sizes(len(group_returns), record.get("pass_code"))
    except ValueError:
        pass_sizes = (int(record.get("pass_size") or len(group_returns)),)
    subsets = pass_subsets(len(group_returns), pass_sizes)
    payout_minor = multiply_minor(
        unit_stake_minor,
        sum(math.prod(group_returns[index] for index in subset) for subset in subsets),
    )
    payout = from_minor(payout_minor)
    multiplier = payout / unit_stake if unit_stake > 0 else 0.0
    status = "void" if all_void or payout == stake else "won" if payout > stake else "lost"
    summary = {
        "winning_combinations": sum(
            math.prod(group_hits[index] for index in subset) for subset in subsets
        ),
        "combination_count": int(record.get("combination_count") or 0),
    }
    return status, payout, multiplier, summary


def _apply_record_settlement(
    record: dict[str, Any], evaluations: list[dict[str, Any]],
    wallet: dict[str, Any], settlement_time: str,
) -> float:
    status, payout, multiplier, summary = _record_settlement_amount(record, evaluations)
    record["status"] = status
    record["settled_at"] = settlement_time
    payout_minor = _set_record_money(record, "payout", payout)
    record["settled_odds"] = round(multiplier, 6)
    record["settlement"] = evaluations
    record.pop("partial_settlement", None)
    if summary is not None:
        record["settlement_summary"] = summary
    else:
        record.pop("settlement_summary", None)
    balance_before_minor = _wallet_balance_minor(wallet)
    write_minor(record, "balance_before_settlement", balance_before_minor)
    balance_after_minor = balance_before_minor + payout_minor
    _set_wallet_balance_minor(wallet, balance_after_minor)
    write_minor(record, "balance_after_settlement", balance_after_minor)
    transaction = {
        "id": str(uuid.uuid4()),
        "at": settlement_time,
        "type": "bet_settlement",
        "bet_ids": [record["bet_id"]],
    }
    write_minor(transaction, "amount", payout_minor)
    write_minor(transaction, "balance_after", balance_after_minor)
    wallet["transactions"].append(transaction)
    return payout


@_account_locked
def settle_pending_bets(
    completed_results: list[dict[str, Any]],
    game_date: str | None = None,
    *,
    game_minutes: int | None = None,
    trusted_same_day_keys: set[tuple[str, int, int, int]] | None = None,
    void_missing_after_timeout: bool = False,
) -> dict[str, Any]:
    migrated = migrate_legacy_bets()
    # FM can materialize a result object before a same-day fixture is publicly completed.
    # Waiting for the game date to advance prevents premature settlement.
    if game_date:
        trusted = set(trusted_same_day_keys or ())
        completed_results = [
            item for item in completed_results
            if str(item.get("date", "")) < game_date or result_key(item) in trusted
        ]
    source_conflicts = _completed_result_conflicts(completed_results)
    completed_results = [
        item for item in completed_results
        if result_key(item) not in source_conflicts
    ]
    results = {result_key(item): item for item in completed_results}
    results_by_matchup: dict[tuple[int, int, int], list[dict[str, Any]]] = {}
    for item in completed_results:
        item_key = result_key(item)
        results_by_matchup.setdefault(item_key[1:], []).append(item)
    records = load_bets()
    wallet = load_wallet()
    settlement_time = now()
    source_conflicts_changed = 0
    for record in records:
        if record.get("status") != "pending" or record.get("demo_preview"):
            continue
        record_keys = {
            key for leg in record.get("legs") or [record]
            if (key := _leg_result_key(leg, record)) is not None
        }
        matching = {
            key: source_conflicts[key] for key in record_keys & set(source_conflicts)
        }
        if not matching:
            continue
        conflict = {
            "reason": "conflicting_result_sources",
            "detected_at": settlement_time,
            "results": [{
                "date": key[0], "competition_id": key[1],
                "home_id": key[2], "away_id": key[3],
                "candidates": candidates,
            } for key, candidates in sorted(matching.items())],
        }
        previous_conflict = record.get("settlement_conflict")
        if isinstance(previous_conflict, dict):
            conflict["detected_at"] = previous_conflict.get(
                "detected_at", settlement_time,
            )
        if previous_conflict != conflict:
            record["settlement_conflict"] = conflict
            source_conflicts_changed += 1
    reopened, settled_conflicts, settled_conflicts_changed = _reopen_untrusted_settlements(
        records, wallet, settlement_time, results, results_by_matchup,
    )
    settled: list[dict[str, Any]] = []
    total_return_minor = 0
    partial_settlements_changed = 0
    for record in records:
        if record.get("status") != "pending" or record.get("demo_preview"):
            continue
        if record.get("type") == "championship":
            continue
        if record.get("type") in {"parlay", "system_parlay"}:
            partial_evaluations = _evaluate_available_record_legs(
                record, results, results_by_matchup, game_date, game_minutes,
                void_missing_after_timeout=void_missing_after_timeout,
            )
            previous_partial = record.get("partial_settlement")
            if not isinstance(previous_partial, list):
                previous_partial = []
            partial_evaluations = [
                None
                if _leg_result_key(leg, record) in source_conflicts
                else evaluation
                if evaluation is not None
                else previous_partial[index]
                if index < len(previous_partial)
                and isinstance(previous_partial[index], dict)
                else None
                for index, (leg, evaluation) in enumerate(zip(
                    record.get("legs") or [record], partial_evaluations,
                ))
            ]
            next_partial = (
                partial_evaluations
                if any(item is not None for item in partial_evaluations)
                else None
            )
            if next_partial is None:
                if "partial_settlement" in record:
                    record.pop("partial_settlement", None)
                    partial_settlements_changed += 1
            elif record.get("partial_settlement") != next_partial:
                record["partial_settlement"] = next_partial
                partial_settlements_changed += 1
        if game_date and not _record_due_for_settlement(
            record, game_date, game_minutes,
        ):
            continue
        evaluations = _evaluate_record_legs(
            record, results, results_by_matchup, game_date,
            void_missing_after_timeout=void_missing_after_timeout,
        )
        if evaluations is None:
            continue
        _apply_record_settlement(record, evaluations, wallet, settlement_time)
        total_return_minor += read_minor(record, "payout")
        settled.append(record)

    details_updated = _backfill_settled_half_scores(
        records, results, results_by_matchup, settlement_time,
    )
    if (
        settled or reopened or details_updated
        or partial_settlements_changed
        or source_conflicts_changed or settled_conflicts_changed
    ):
        _commit_account(records, wallet, "settle_pending_bets")
    settled_records = []
    for record in settled:
        legs = record.get("legs") or [record]
        first_leg = legs[0] if legs else {}
        evaluations = record.get("settlement") or []
        settled_records.append({
            "bet_id": record.get("bet_id"),
            "status": record.get("status"),
            "type": record.get("type", "single"),
            "legs": len(legs),
            "leg_records": [dict(leg) for leg in legs],
            "settlement": [dict(item) for item in evaluations if isinstance(item, dict)],
            "home": first_leg.get("home"),
            "away": first_leg.get("away"),
            "game_date": evaluations[0].get("result_date") if evaluations else game_date,
            "stake": from_minor(read_minor(record, "stake")),
            "payout": from_minor(read_minor(record, "payout")),
            "profit": from_minor(max(
                read_minor(record, "payout") - read_minor(record, "stake"), 0,
            )),
        })
    return {
        "migrated": migrated,
        "reopened": reopened,
        "conflicts": len(source_conflicts) + settled_conflicts,
        "result_conflicts": [{
            "date": key[0], "competition_id": key[1],
            "home_id": key[2], "away_id": key[3],
            "candidates": candidates,
        } for key, candidates in sorted(source_conflicts.items())],
        "details_updated": details_updated,
        "settled": len(settled),
        "settled_records": settled_records,
        "returned": from_minor(total_return_minor),
        "balance": from_minor(_wallet_balance_minor(wallet)),
    }


@_account_locked
def settle_championship_bets(markets: dict[str, Any]) -> dict[str, Any]:
    current_rows = list(markets.get("competitions") or [])
    current_keys = {
        str(item.get("season_key") or "") for item in current_rows
    }
    market_rows = [
        *current_rows,
        *[
            item for item in markets.get("settlement_competitions") or []
            if str(item.get("season_key") or "") not in current_keys
        ],
    ]
    completed_markets = [
        item for item in market_rows
        if item.get("status") == "complete" and item.get("winner_team_id")
    ]
    completed = {
        str(item.get("season_key") or ""): item
        for item in completed_markets
    }
    if not completed:
        return {
            "settled": 0, "bet_ids": [], "settled_records": [],
            "returned": 0.0,
        }
    records = load_bets()
    wallet = load_wallet()
    settlement_time = now()
    settled = []
    total_return_minor = 0
    for record in records:
        if record.get("status") != "pending" or record.get("type") != "championship":
            continue
        competition = completed.get(str(record.get("season_key") or ""))
        if not competition:
            try:
                competition_id = int(record.get("competition_id") or 0)
                placed_at = date.fromisoformat(str(record.get("placed_at") or "")[:10])
            except (TypeError, ValueError):
                competition_id = 0
                placed_at = None
            candidates = []
            if competition_id and placed_at:
                for item in completed_markets:
                    if int(item.get("competition_id") or 0) != competition_id:
                        continue
                    try:
                        date_qualified = "@" in str(item.get("season_key") or "")
                        season_start = date.fromisoformat(str(
                            item.get("season_start") if date_qualified else (
                                item.get("season_window_start") or item.get("season_start") or ""
                            )
                        )[:10])
                        label = str(item.get("season_label") or str(item.get("season_key") or "").split(":", 1)[-1])
                        cross_year = re.fullmatch(r"(\d{4})/(\d{2})", label)
                        calendar_year = re.fullmatch(r"\d{4}", label)
                        if cross_year and int(cross_year[2]) == (int(cross_year[1]) + 1) % 100:
                            canonical_start = date(int(cross_year[1]), 7, 1)
                            canonical_end = date(int(cross_year[1]) + 1, 6, 30)
                        elif calendar_year:
                            canonical_start = date(int(label), 1, 1)
                            canonical_end = date(int(label), 12, 31)
                        else:
                            canonical_start = canonical_end = None
                        if not date_qualified and canonical_start and canonical_end.isoformat() == str(item.get("season_end") or "") and canonical_start <= season_start <= canonical_end:
                            season_start = canonical_start
                        season_end = date.fromisoformat(str(
                            item.get("settlement_date")
                            or item.get("season_end") or ""
                        )[:10])
                    except ValueError:
                        continue
                    if season_start <= placed_at <= season_end:
                        candidates.append(item)
            if len(candidates) == 1:
                competition = candidates[0]
                record["settlement_season_key"] = str(
                    competition.get("season_key") or ""
                )
                record["settlement_season_key_source"] = (
                    "competition_id+placed_at_window"
                )
        if not competition:
            continue
        winner_id = int(competition["winner_team_id"])
        selected_id = int(record.get("team_id") or 0)
        outcome = "won" if selected_id == winner_id else "lost"
        evaluation = {
            "outcome": outcome,
            "score": f"冠军：{competition.get('winner_team_name') or winner_id}",
            "result_date": str(
                competition.get("settlement_date")
                or record.get("settlement_date")
                or competition.get("season_end")
                or ""
            ),
            "settled_odds": float(record.get("odds") or 1.0),
        }
        _apply_record_settlement(record, [evaluation], wallet, settlement_time)
        total_return_minor += read_minor(record, "payout")
        settled.append(record)
    if settled:
        _commit_account(records, wallet, "settle_championship_bets")
    settled_records = [{
        "bet_id": record.get("bet_id"),
        "status": record.get("status"),
        "type": "championship",
        "legs": 1,
        "home": record.get("team_name") or record.get("home"),
        "away": record.get("away") or "赛事冠军",
        "competition_name": record.get("competition_name"),
        "game_date": (
            (record.get("settlement") or [{}])[0].get("result_date")
            or record.get("settlement_date")
        ),
        "stake": from_minor(read_minor(record, "stake")),
        "payout": from_minor(read_minor(record, "payout")),
        "profit": from_minor(max(
            read_minor(record, "payout") - read_minor(record, "stake"), 0,
        )),
    } for record in settled]
    return {
        "settled": len(settled),
        "bet_ids": [str(record.get("bet_id") or "") for record in settled],
        "settled_records": settled_records,
        "returned": from_minor(total_return_minor),
        "balance": from_minor(_wallet_balance_minor(wallet)),
    }
