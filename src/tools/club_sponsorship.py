"""ODD-side sponsorship search, offers and contracts for acquired clubs.

This module intentionally does not touch Football Manager's native sponsor
vector.  It owns the small, account-scoped workflow around delayed mail,
three candidate offers and one accepted annual contract.  The application
service is responsible for the optional native club-balance credit.
"""

from __future__ import annotations

import hashlib
import uuid
from copy import deepcopy
from datetime import date, timedelta
from typing import Any

from tools.account_store import load_document, update_document
from tools.i18n_runtime import mail_template_fields


SCHEMA_VERSION = 1
MAX_OFFERS = 3
SEARCH_COOLDOWN_DAYS = 30
OFFER_EXPIRY_DAYS = 30

SPONSOR_PROFILES: tuple[dict[str, Any], ...] = (
    {
        "industry": "运动饮料企业",
        "requirements": (
            "指定一名一线队球员担任品牌形象大使；"
            "每赛季参加 6 次商业活动。"
        ),
        "requirement_kind": "player_ambassador",
        "activity_count": 6,
        "performance_bonus": "联赛排名进入前四，额外奖励年度赞助费的 10%。",
    },
    {
        "industry": "通讯服务企业",
        "requirements": "每赛季参加 4 次社区活动，并允许官方社交媒体合作宣传。",
        "requirement_kind": "community_events",
        "activity_count": 4,
        "performance_bonus": "赢得国内杯赛，额外奖励年度赞助费的 8%。",
    },
    {
        "industry": "物流服务企业",
        "requirements": "保持地区合作独家，并每赛季参加 3 次商业接待活动。",
        "requirement_kind": "commercial_events",
        "activity_count": 3,
        "performance_bonus": "完成赛季目标，额外奖励年度赞助费的 5%。",
    },
    {
        "industry": "金融服务机构",
        "requirements": "每赛季参加 3 次商业活动，并在俱乐部官方渠道保留合作伙伴介绍。",
        "requirement_kind": "commercial_events",
        "activity_count": 3,
        "performance_bonus": "俱乐部当季实现利润，额外奖励年度赞助费的 5%。",
    },
    {
        "industry": "体育装备企业",
        "requirements": "每赛季参加 5 次青训或社区活动，并保持合作期间的行业独家。",
        "requirement_kind": "community_events",
        "activity_count": 5,
        "performance_bonus": "青训评级提升，额外奖励年度赞助费的 6%。",
    },
    {
        "industry": "旅游与航空企业",
        "requirements": "每赛季参加 4 次海外或地区推广活动。",
        "requirement_kind": "commercial_events",
        "activity_count": 4,
        "performance_bonus": "参加洲际赛事，额外奖励年度赞助费的 12%。",
    },
)


def _empty_state() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "campaigns": {},
        "contracts": {},
        "payments": {},
    }


def _normalize_state(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        payload = _empty_state()
    payload.setdefault("schema_version", SCHEMA_VERSION)
    for key in ("campaigns", "contracts", "payments"):
        if not isinstance(payload.get(key), dict):
            payload[key] = {}
    return payload


def load_sponsorships(scope_id: str = "") -> dict[str, Any]:
    return _normalize_state(load_document("club_sponsorships", _empty_state(), scope_id or None))


def _parse_game_date(value: Any) -> date:
    try:
        return date.fromisoformat(str(value or ""))
    except ValueError as error:
        raise ValueError("当前游戏日期无效，无法安排赞助招商") from error


def _date(value: date) -> str:
    return value.isoformat()


def league_quote_annual(balances: Any) -> int:
    """Return one quarter of the complete league's arithmetic mean."""
    values = [int(value) for value in balances]
    if not values:
        raise ValueError("当前联赛没有可用于报价的俱乐部结余")
    return int(sum(values) / (len(values) * 4))


def _add_years(value: date, years: int) -> date:
    """Add calendar years while keeping 29 February contracts valid."""
    target_year = value.year + int(years)
    try:
        return value.replace(year=target_year)
    except ValueError:
        # A one-year contract signed on leap day ends on the last day of
        # February in the following non-leap year.
        return value.replace(year=target_year, month=2, day=28)


def _stable_delay(scope_id: str, team_id: int, attempt: int, game_date: str) -> int:
    digest = hashlib.sha256(
        f"{scope_id}:{team_id}:{attempt}:{game_date}".encode("utf-8")
    ).digest()
    return (3 if attempt == 1 else 2) + digest[0] % (6 if attempt == 1 else 7)


def _profile(scope_id: str, team_id: int, attempt: int, game_date: str) -> dict[str, Any]:
    digest = hashlib.sha256(
        f"profile:{scope_id}:{team_id}:{attempt}:{game_date}".encode("utf-8")
    ).digest()
    return deepcopy(SPONSOR_PROFILES[digest[0] % len(SPONSOR_PROFILES)])


def _candidate_player(candidates: Any, digest_seed: str) -> dict[str, Any] | None:
    rows = [
        row for row in candidates or []
        if isinstance(row, dict) and int(row.get("id") or 0) > 0
    ]
    if not rows:
        return None
    digest = hashlib.sha256(digest_seed.encode("utf-8")).digest()
    row = rows[digest[0] % len(rows)]
    return {
        "id": int(row.get("id") or 0),
        "name": str(row.get("name") or row.get("id") or "队内球员"),
    }


def _public_offer(offer: dict[str, Any]) -> dict[str, Any]:
    return {
        key: deepcopy(offer.get(key))
        for key in (
            "offer_id", "team_id", "team_name", "competition_id",
            "competition_name", "attempt", "industry", "annual_value",
            "contract_years", "requirements", "requirement_kind",
            "ambassador_player", "activity_count", "performance_bonus",
            "sent_game_date", "available_after_game_date", "expires_game_date",
            "status", "action_state",
        )
        if key in offer
    }


def _public_campaign(campaign: dict[str, Any], game_date: str) -> dict[str, Any]:
    current = _parse_game_date(game_date)
    offers = []
    for offer in campaign.get("offers") or []:
        if not isinstance(offer, dict):
            continue
        available = str(offer.get("available_after_game_date") or "")
        try:
            if available and current < date.fromisoformat(available):
                continue
        except ValueError:
            continue
        offers.append(_public_offer(offer))
    result = {
        key: deepcopy(campaign.get(key))
        for key in (
            "campaign_id", "team_id", "team_name", "competition_id",
            "competition_name", "started_game_date", "quote_base_annual",
            "attempts_used", "status", "next_available_game_date",
        )
        if key in campaign
    }
    result["offers"] = offers
    result["offers_total"] = len(campaign.get("offers") or [])
    result["offers_visible"] = len(offers)
    result["max_offers"] = MAX_OFFERS
    return result


def public_sponsorship_state(scope_id: str, team_id: int, game_date: str) -> dict[str, Any]:
    state = load_sponsorships(scope_id)
    key = str(int(team_id))
    campaign = state["campaigns"].get(key)
    contract = state["contracts"].get(key)
    result: dict[str, Any] = {
        "team_id": int(team_id),
        "campaign": _public_campaign(campaign, game_date) if isinstance(campaign, dict) else None,
        "contract": deepcopy(contract) if isinstance(contract, dict) else None,
    }
    return result


def _build_offer(
    *, scope_id: str, team_id: int, team_name: str, competition_id: int,
    competition_name: str, attempt: int, annual_value: int, game_date: str,
    candidates: Any,
) -> dict[str, Any]:
    profile = _profile(scope_id, team_id, attempt, game_date)
    sent = _parse_game_date(game_date)
    available = sent + timedelta(days=_stable_delay(scope_id, team_id, attempt, game_date))
    expires = available + timedelta(days=OFFER_EXPIRY_DAYS)
    offer_id = str(uuid.uuid4())
    ambassador = None
    if profile["requirement_kind"] == "player_ambassador":
        ambassador = _candidate_player(
            candidates, f"ambassador:{scope_id}:{team_id}:{attempt}:{game_date}",
        )
        if ambassador:
            profile["requirements"] = (
                f"指定 {ambassador['name']} 担任品牌形象大使；"
                f"每赛季参加 {profile['activity_count']} 次商业活动。"
            )
        else:
            profile["requirements"] = (
                f"指定一名一线队球员担任品牌形象大使；"
                f"每赛季参加 {profile['activity_count']} 次商业活动。"
            )
    return {
        "offer_id": offer_id,
        "team_id": int(team_id),
        "team_name": str(team_name),
        "competition_id": int(competition_id),
        "competition_name": str(competition_name),
        "attempt": int(attempt),
        "industry": str(profile["industry"]),
        "annual_value": int(annual_value),
        "contract_years": 1,
        "requirements": str(profile["requirements"]),
        "requirement_kind": str(profile["requirement_kind"]),
        "ambassador_player": ambassador,
        "activity_count": int(profile["activity_count"]),
        "performance_bonus": str(profile["performance_bonus"]),
        "sent_game_date": game_date,
        "available_after_game_date": _date(available),
        "expires_game_date": _date(expires),
        "status": "pending",
        "action_state": "pending",
    }


def _offer_mail(offer: dict[str, Any]) -> dict[str, Any]:
    ambassador = offer.get("ambassador_player") or {}
    requirements = str(offer.get("requirements") or "")
    message = (
        f"一家{offer.get('industry') or '企业'}希望与{offer.get('team_name') or '本俱乐部'}合作。"
        f"固定赞助费 {int(offer.get('annual_value') or 0):,} 英镑/年，合同期限 {offer.get('contract_years') or 1} 年。"
        f"要求：{requirements}"
    )
    if ambassador:
        message += f" 代言球员：{ambassador.get('name') or ambassador.get('id')}。"
    return {
        "id": str(uuid.uuid4()),
        "source_id": f"sponsorship_offer:{offer['offer_id']}",
        "type": "sponsorship_offer",
        **mail_template_fields("sponsorship_offer", {
            "team": str(offer.get("team_name") or ""),
            "industry": str(offer.get("industry") or ""),
            "amount": int(offer.get("annual_value") or 0),
            "years": int(offer.get("contract_years") or 1),
        }),
        "title": f"{offer.get('industry') or '潜在合作伙伴'}赞助意向",
        "message": message,
        "created_at": f"{offer.get('sent_game_date')}T09:00:00",
        "game_date": offer.get("sent_game_date"),
        "available_after_game_date": offer.get("available_after_game_date"),
        "team_id": offer.get("team_id"),
        "team_name": offer.get("team_name"),
        "offer_id": offer.get("offer_id"),
        "attempt": offer.get("attempt"),
        "annual_value": offer.get("annual_value"),
        "contract_years": offer.get("contract_years"),
        "industry": offer.get("industry"),
        "action_state": "pending",
    }


def _update_state(scope_id: str, mutator: Any) -> Any:
    return update_document("club_sponsorships", _empty_state(), mutator, scope_id or None)


def start_search(
    scope_id: str, *, team_id: int, team_name: str, competition_id: int,
    competition_name: str, annual_value: int, game_date: str, candidates: Any = None,
) -> dict[str, Any]:
    if int(team_id) <= 0:
        raise ValueError("俱乐部 ID 无效")
    annual_value = int(annual_value)
    if annual_value <= 0:
        raise ValueError("当前联赛平均结余不足，暂时没有可用赞助报价")
    game = _parse_game_date(game_date)
    campaign_key = str(int(team_id))
    campaign_id = str(uuid.uuid4())
    offer = _build_offer(
        scope_id=scope_id, team_id=team_id, team_name=team_name,
        competition_id=competition_id, competition_name=competition_name,
        attempt=1, annual_value=annual_value, game_date=game_date,
        candidates=candidates,
    )
    mail = _offer_mail(offer)

    def mutate(state: dict[str, Any]) -> dict[str, Any]:
        state = _normalize_state(state)
        existing = state["campaigns"].get(campaign_key)
        if isinstance(existing, dict) and str(existing.get("status") or "") in {
            "searching", "choosing", "accepted",
        }:
            raise ValueError("该俱乐部已有进行中的赞助招商或合同")
        if isinstance(existing, dict):
            try:
                last = _parse_game_date(existing.get("last_closed_game_date"))
                if game < last + timedelta(days=SEARCH_COOLDOWN_DAYS):
                    raise ValueError("该俱乐部仍在招商冷却期，请稍后再试")
            except ValueError as error:
                if "冷却期" in str(error):
                    raise
        state["campaigns"][campaign_key] = {
            "campaign_id": campaign_id,
            "team_id": int(team_id),
            "team_name": str(team_name),
            "competition_id": int(competition_id),
            "competition_name": str(competition_name),
            "started_game_date": game_date,
            "quote_base_annual": annual_value,
            "attempts_used": 1,
            "status": "searching",
            "next_available_game_date": offer["available_after_game_date"],
            "offers": [offer],
        }
        state.setdefault("campaigns", {})[campaign_key]["mail_ids"] = [mail["id"]]
        return {
            "campaign": _public_campaign(state["campaigns"][campaign_key], game_date),
            "offer": _public_offer(offer), "mail": mail,
        }

    return _update_state(scope_id, mutate)


def continue_search(
    scope_id: str, *, team_id: int, game_date: str, candidates: Any = None,
) -> dict[str, Any]:
    game = _parse_game_date(game_date)
    key = str(int(team_id))
    result: dict[str, Any] = {}

    def mutate(state: dict[str, Any]) -> dict[str, Any]:
        nonlocal result
        state = _normalize_state(state)
        campaign = state["campaigns"].get(key)
        if not isinstance(campaign, dict):
            raise ValueError("该俱乐部当前没有进行中的招商")
        attempts = int(campaign.get("attempts_used") or 0)
        if attempts >= MAX_OFFERS:
            campaign["status"] = "choosing"
            raise ValueError("本轮已经找到三份报价，请从已有意向中选择")
        next_date = str(campaign.get("next_available_game_date") or "")
        if next_date:
            try:
                if game < date.fromisoformat(next_date):
                    raise ValueError(f"下一份意向将在 {next_date} 后送达")
            except ValueError as error:
                if "下一份意向" in str(error):
                    raise
        attempt = attempts + 1
        offer = _build_offer(
            scope_id=scope_id, team_id=team_id,
            team_name=str(campaign.get("team_name") or team_id),
            competition_id=int(campaign.get("competition_id") or 0),
            competition_name=str(campaign.get("competition_name") or ""),
            attempt=attempt, annual_value=int(campaign.get("quote_base_annual") or 0),
            game_date=game_date, candidates=candidates,
        )
        mail = _offer_mail(offer)
        campaign.setdefault("offers", []).append(offer)
        campaign["attempts_used"] = attempt
        campaign["status"] = "choosing" if attempt >= MAX_OFFERS else "searching"
        campaign["next_available_game_date"] = offer["available_after_game_date"]
        campaign.setdefault("mail_ids", []).append(mail["id"])
        result = {
            "campaign": _public_campaign(campaign, game_date),
            "offer": _public_offer(offer), "mail": mail,
        }
        return result

    return _update_state(scope_id, mutate)


def accept_offer(scope_id: str, *, team_id: int, offer_id: str, game_date: str) -> dict[str, Any]:
    _parse_game_date(game_date)
    key = str(int(team_id))
    offer_id = str(offer_id or "").strip()
    if not offer_id:
        raise ValueError("赞助报价标识缺失")

    def mutate(state: dict[str, Any]) -> dict[str, Any]:
        state = _normalize_state(state)
        campaign = state["campaigns"].get(key)
        if not isinstance(campaign, dict):
            raise ValueError("该俱乐部没有可接受的赞助报价")
        existing = state["contracts"].get(key)
        if isinstance(existing, dict) and str(existing.get("status") or "") == "active":
            if str(existing.get("accepted_offer_id") or "") != offer_id:
                raise ValueError("该俱乐部已经有生效中的 ODD 赞助合同")
            payment = next(
                (
                    row for row in state.get("payments", {}).values()
                    if isinstance(row, dict)
                    and str(row.get("contract_id") or "") == str(existing.get("contract_id") or "")
                ),
                None,
            )
            return {
                "contract": deepcopy(existing),
                "payment": deepcopy(payment) if payment else None,
                "campaign": _public_campaign(campaign, game_date),
                "idempotent": True,
            }
        offer = next(
            (row for row in campaign.get("offers") or []
             if isinstance(row, dict) and str(row.get("offer_id") or "") == offer_id),
            None,
        )
        if not offer:
            raise ValueError("赞助报价不存在或已失效")
        available = str(offer.get("available_after_game_date") or "")
        if available and _parse_game_date(game_date) < _parse_game_date(available):
            raise ValueError("该赞助意向尚未送达")
        expires = str(offer.get("expires_game_date") or "")
        if expires and _parse_game_date(game_date) > _parse_game_date(expires):
            raise ValueError("该赞助报价已经过期")
        offer["status"] = "accepted"
        offer["action_state"] = "accepted"
        for row in campaign.get("offers") or []:
            if isinstance(row, dict) and row is not offer:
                row["status"] = "not_selected"
                row["action_state"] = "closed"
        campaign["status"] = "accepted"
        contract_id = str(uuid.uuid4())
        contract = {
            "contract_id": contract_id,
            "team_id": int(team_id),
            "team_name": str(campaign.get("team_name") or team_id),
            "competition_id": int(campaign.get("competition_id") or 0),
            "competition_name": str(campaign.get("competition_name") or ""),
            "sponsor_industry": str(offer.get("industry") or "企业"),
            "annual_value": int(offer.get("annual_value") or 0),
            "contract_years": int(offer.get("contract_years") or 1),
            "start_game_date": str(game_date),
            "end_game_date": _date(_add_years(
                _parse_game_date(game_date), int(offer.get("contract_years") or 1),
            )),
            "requirements": str(offer.get("requirements") or ""),
            "requirement_kind": str(offer.get("requirement_kind") or ""),
            "ambassador_player": deepcopy(offer.get("ambassador_player")),
            "performance_bonus": str(offer.get("performance_bonus") or ""),
            "status": "active",
            "source": "odd_simulated",
            "accepted_offer_id": offer_id,
            "accepted_game_date": str(game_date),
        }
        state["contracts"][key] = contract
        payment_id = f"sponsorship_payment:{contract_id}:signing"
        payment = {
            "payment_id": payment_id,
            "contract_id": contract_id,
            "team_id": int(team_id),
            "team_name": contract["team_name"],
            "amount": int(contract["annual_value"]),
            "game_date": str(game_date),
            "status": "pending_native_credit",
        }
        state["payments"][payment_id] = payment
        return {
            "contract": deepcopy(contract),
            "payment": deepcopy(payment),
            "campaign": _public_campaign(campaign, game_date),
        }

    return _update_state(scope_id, mutate)


def rollback_acceptance(
    scope_id: str, *, team_id: int, offer_id: str,
) -> dict[str, Any]:
    """Undo a just-created contract when the native balance credit failed."""
    key = str(int(team_id))
    offer_id = str(offer_id or "").strip()

    def mutate(state: dict[str, Any]) -> dict[str, Any]:
        state = _normalize_state(state)
        contract = state["contracts"].get(key)
        if not isinstance(contract, dict):
            return {"rolled_back": True, "already_absent": True}
        if str(contract.get("accepted_offer_id") or "") != offer_id:
            raise ValueError("赞助合同回滚标识不匹配")
        contract_id = str(contract.get("contract_id") or "")
        state["contracts"].pop(key, None)
        for payment_id, payment in list(state.get("payments", {}).items()):
            if isinstance(payment, dict) and str(payment.get("contract_id") or "") == contract_id:
                state["payments"].pop(payment_id, None)
        campaign = state["campaigns"].get(key)
        if isinstance(campaign, dict):
            for offer in campaign.get("offers") or []:
                if isinstance(offer, dict) and str(offer.get("offer_id") or "") == offer_id:
                    offer["status"] = "pending"
                    offer["action_state"] = "pending"
                elif isinstance(offer, dict) and offer.get("status") == "not_selected":
                    offer["status"] = "pending"
                    offer["action_state"] = "pending"
            campaign["status"] = "choosing" if int(campaign.get("attempts_used") or 0) >= MAX_OFFERS else "searching"
        return {"rolled_back": True, "contract_id": contract_id}

    return _update_state(scope_id, mutate)


def mark_payment(scope_id: str, payment_id: str, *, status: str, native: dict[str, Any] | None = None) -> dict[str, Any]:
    payment_id = str(payment_id or "").strip()
    if status not in {"pending_native_credit", "paid", "failed"}:
        raise ValueError("赞助付款状态无效")

    def mutate(state: dict[str, Any]) -> dict[str, Any]:
        state = _normalize_state(state)
        payment = state["payments"].get(payment_id)
        if not isinstance(payment, dict):
            raise ValueError("赞助付款记录不存在")
        if payment.get("status") == "paid":
            return deepcopy(payment)
        payment["status"] = status
        if native is not None:
            payment["native"] = deepcopy(native)
        return deepcopy(payment)

    return _update_state(scope_id, mutate)


__all__ = [
    "MAX_OFFERS", "SPONSOR_PROFILES", "accept_offer", "continue_search",
    "league_quote_annual", "load_sponsorships", "mark_payment", "public_sponsorship_state", "rollback_acceptance",
    "start_search",
]
