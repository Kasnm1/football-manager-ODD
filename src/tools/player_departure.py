"""Account-scoped simulated offers; no FM addresses or native writes."""
from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta
import math
import random
import uuid
from typing import Any, Callable

from tools.account_store import load_document, update_document
from tools.domain_errors import ConflictError, ValidationError

DOCUMENT = 'player_departures'
OFFER_DAYS = 7
OFFER_GENERATION_VERSION = 3
MAX_AMOUNT = 2_000_000_000
# A listing price is only an ODD valuation input, never an FM market-value claim.
ALLOW_LISTING_SAMPLES = True
VALUATION_PEER_LIMIT = 128


def fail(key: str) -> None:
    raise ValidationError(key, code=key.replace('.', '_'), message_key=key)


def number(value: Any) -> int | None:
    try:
        if value is None or isinstance(value, bool) or not math.isfinite(float(value)):
            return None
        return int(value)
    except (ValueError, TypeError, OverflowError):
        return None


def reputation(player: dict) -> int:
    for key in ('world_reputation', 'international_reputation', 'current_reputation'):
        value = number(player.get(key))
        if value is not None and 0 <= value <= 10000:
            return value
    fail('departure.error.player')


def valid_value(player: dict, *, listing: bool = False) -> int | None:
    native = number(player.get('market_value'))
    if native is not None and 0 < native <= MAX_AMOUNT:
        return native
    value = number(player.get('asking_price')) if listing else None
    if value is not None and 0 < value <= MAX_AMOUNT and value not in (300_000_000, 0xFFFFFFFF):
        return value
    return None


def _bounded(player: dict, key: str, lower: int, upper: int) -> int | None:
    value = number(player.get(key))
    return value if value is not None and lower <= value <= upper else None


def _primary_positions(player: dict) -> set[str]:
    ratings = player.get('position_ratings') or {}
    if isinstance(ratings, dict) and ratings:
        valid = {key: value for key, raw in ratings.items()
                 if (value := number(raw)) is not None and 1 < value <= 20}
        if valid:
            highest = max(valid.values())
            return {key for key, value in valid.items() if value == highest}
    positions = player.get('primary_positions') or []
    return set(positions) if isinstance(positions, (list, tuple, set)) else set()


def _position_distance(player: dict, peer: dict) -> float | None:
    own, other = _primary_positions(player), _primary_positions(peer)
    if not own or not other:
        return .5 if own or other else 0
    if ('GK' in own) != ('GK' in other):
        return None  # Keep goalkeepers separate even in the sparse-pool fallback.
    if own & other:
        return 0
    groups = ({'SW', 'DL', 'DC', 'DR', 'WBL', 'WBR'},
              {'DM', 'MC', 'ML', 'MR'}, {'AML', 'AMC', 'AMR', 'ST'})
    return .5 if any(own & group and other & group for group in groups) else 1.5


def valuation_distance(player: dict, peer: dict) -> float | None:
    """Rank comparable save players; these scales never multiply their prices."""
    ca, peer_ca = _bounded(player, 'ca', 1, 200), _bounded(peer, 'ca', 1, 200)
    if ca is None or peer_ca is None:
        return None
    try:
        distance = abs(ca - peer_ca) / 15 + abs(reputation(player) - reputation(peer)) / 1000
    except ValidationError:
        return None
    for key, lower, upper, scale in (('age', 14, 60, 2), ('pa', 1, 200, 20)):
        own, other = _bounded(player, key, lower, upper), _bounded(peer, key, lower, upper)
        if own is not None and other is not None:
            distance += abs(own - other) / scale
        elif own is not None or other is not None:
            distance += .5
    position = _position_distance(player, peer)
    return distance + position if position is not None else None


def estimate_value(player: dict, peers: list[dict], *, listing: bool = False) -> dict:
    native = valid_value(player)
    if native:
        return {'amount': native, 'source': 'market_value', 'sample_count': 0}
    ca = number(player.get('ca'))
    reputation(player)
    if ca is None or not 1 <= ca <= 200:
        fail('departure.error.player')
    samples = []
    seen = set()
    for peer in peers:
        uid = number(peer.get('id'))
        value = valid_value(peer, listing=listing)
        peer_ca = number(peer.get('ca'))
        if not uid or uid == number(player.get('id')) or uid in seen or not value or peer_ca is None or not 1 <= peer_ca <= 200:
            continue
        distance = valuation_distance(player, peer)
        if distance is None:
            continue
        seen.add(uid)
        samples.append((distance, value, 1 / (1 + distance) ** 2, peer))
    samples.sort(key=lambda row: row[0])
    # Widen sample similarity only when fewer than five comparable prices exist.
    # Every fallback still uses current-save prices, with no fixed price model.
    age = _bounded(player, 'age', 14, 60)
    selected = samples
    for window, maximum in ((2, 2), (4, 4), (6, 6)):
        close = [row for row in samples if row[0] <= maximum
                 and (age is None or (other := _bounded(row[3], 'age', 14, 60)) is None
                      or abs(age - other) <= window)
                 and (window == 6 or (_position_distance(player, row[3]) or 0) <= .5)]
        if len(close) >= 5:
            selected = close
            break
    selected = selected[:21]
    if not selected:
        fail('departure.error.valuation')
    selected.sort(key=lambda row: row[1])
    midpoint = sum(row[2] for row in selected) / 2
    accumulated = 0.0
    amount = selected[-1][1]
    for _, value, weight, _ in selected:
        accumulated += weight
        if accumulated >= midpoint:
            amount = value
            break
    return {'amount': amount, 'source': 'odd_estimate', 'sample_count': len(selected)}




def valid_buyer_club(club: dict) -> bool:
    """Accept only current men's first-team club rows as simulated buyers."""
    if not isinstance(club, dict) or club.get('team_type') != 'club' or club.get('squad_type_code') not in (None, 0):
        return False
    text = ' '.join(str(club.get(key) or '') for key in ('name', 'competition', 'competition_name')).casefold()
    text += ' ' + ' '.join(str(value or '') for value in (club.get('competitions') or []))
    return not any(marker in text for marker in ('all-star', 'all star', 'allstar', '全明星', 'women', '女子', '女足', '女队', '女隊'))


def generate_offers(player: dict, clubs: list[dict], excluded: set[int], valuation: dict,
                    *, rng: Any = None) -> list[dict]:
    rng = rng or random.SystemRandom()
    rep = reputation(player)
    age = number(player.get('age'))
    # Keep low-tier buyers as a believable fallback, but prevent the large
    # unclassified native directory from overwhelming nearby-reputation clubs.
    reputation_floor = max(1, rep - 2000)
    pool, high_pool = {}, {}
    older = age is None or age > 30
    allow_high = not older or rng.random() < .02

    def competition_label(club: dict) -> str:
        values = [club.get('competition_name'), club.get('competition')] + list(club.get('competitions') or [])
        return next((str(value).strip() for value in values if str(value or '').strip() not in {'', '未分类', '未知', '未知赛事'}), '')

    def known_competition(club: dict) -> bool:
        return bool(competition_label(club))

    def trim(candidates: dict[int, tuple[dict, str, float]]) -> dict[int, tuple[dict, str, float]]:
        # A bounded nearest-neighbour set keeps thousands of distant amateur
        # rows from dominating the weighted draw.
        ordered = sorted(
            candidates.items(),
            key=lambda item: (
                0 if item[1][0].get('competition_id') and item[1][0].get('competition_id') == player.get('competition_id') else 1,
                abs(number(item[1][0].get('reputation')) - rep),
                -number(item[1][0].get('reputation') or 0),
                str(item[1][0].get('name') or '').casefold(),
            ),
        )
        return dict(ordered[:64])

    valid = {}
    for club in clubs:
        uid = number(club.get('id'))
        club_rep = number(club.get('reputation'))
        if not uid or uid in excluded or uid in valid or not club.get('name'):
            continue
        if not valid_buyer_club(club) or club_rep is None or not 1 <= club_rep <= 10000:
            continue
        valid[uid] = club
    candidates = {uid: club for uid, club in valid.items()
                  if number(club.get('reputation')) >= reputation_floor}
    if not candidates and valid:
        # Tiny directories still provide a real buyer: expand only to the
        # closest available reputation, rather than failing or inventing one.
        nearest = min(abs(number(club.get('reputation')) - rep) for club in valid.values())
        candidates = {uid: club for uid, club in valid.items()
                      if abs(number(club.get('reputation')) - rep) == nearest}
    recognized = {uid: club for uid, club in candidates.items() if known_competition(club)}
    candidates = recognized or candidates
    for uid, club in candidates.items():
        difference = number(club.get('reputation')) - rep
        tier = 'high' if difference > 1200 else 'low' if difference < -1200 else 'similar'
        weight = 1 / (1 + abs(difference) / 1800) ** 2
        if club.get('competition_id') == player.get('competition_id') and club.get('competition_id'):
            weight *= 1.35
        target = high_pool if tier == 'high' else pool
        target[uid] = (club, tier, weight * .02 if tier == 'high' and older else weight)
    pool = trim(pool)
    if allow_high:
        pool.update(trim(high_pool))
    elif not pool and high_pool:
        uid = min(high_pool, key=lambda key: int(high_pool[key][0].get('reputation') or 0))
        pool[uid] = high_pool[uid]
    if not pool:
        fail('departure.error.buyers')
    count = min(len(pool), rng.randint(3, 5))
    result = []
    for _ in range(count):
        uid = rng.choices(list(pool), weights=[row[2] for row in pool.values()], k=1)[0]
        club, tier, _weight = pool.pop(uid)
        maximum = {'low': 1.08, 'similar': 1.15, 'high': 1.25}[tier]
        minimum = 1.05 if tier == 'high' else 1.0
        amount = min(MAX_AMOUNT, math.ceil(int(valuation['amount']) * rng.uniform(minimum, maximum)))
        result.append({'id': uuid.uuid4().hex, 'team_id': uid, 'team_name': str(club['name']),
                       'competition_name': competition_label(club),
                       'amount': amount, 'tier': tier, 'status': 'open'})
    return result


def current_round(scope: str, player_id: int) -> dict | None:
    return load_document(DOCUMENT, {'rounds': {}}, scope).get('rounds', {}).get(str(player_id))


def check_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        fail('departure.error.date')


def start_round(scope: str, player: dict, source_id: int, game_date: str,
                build: Callable[[], tuple[dict, list[dict]]], *, excluded_buyers: set[int] | None = None) -> dict:
    today = check_date(game_date)
    uid = str(int(player['id']))
    def mutate(doc):
        rounds = doc.setdefault('rounds', {})
        old = rounds.get(uid)
        if old and old.get('status') == 'open' and (
                old.get('generation_version') != OFFER_GENERATION_VERSION
                or any(int(offer.get('team_id') or 0) in (excluded_buyers or set())
                       for offer in old.get('offers') or [])):
            # Refresh only on explicit search when ownership/employment changes.
            old = None
            rounds.pop(uid, None)
        if old:
            if old.get('status') == 'executing':
                fail('departure.error.uncertain')
            if today < check_date(old['created_date']):
                fail('departure.error.date')
            if today < check_date(old['expires_date']):
                if old['source_team_id'] != source_id:
                    fail('departure.error.changed')
                return deepcopy(old)
        valuation, offers = build()
        row = {'id': uuid.uuid4().hex, 'player_id': int(uid), 'player_name': str(player.get('name') or uid),
               'source_team_id': source_id, 'generation_version': OFFER_GENERATION_VERSION,
               'created_date': today.isoformat(),
               'expires_date': (today + timedelta(days=OFFER_DAYS)).isoformat(),
               'valuation': valuation, 'offers': offers, 'status': 'open'}
        rounds[uid] = row
        # Retain one round per person. Terminal old rounds can be pruned safely.
        if len(rounds) > 2048:
            for key, previous in sorted(rounds.items(), key=lambda item: item[1]['created_date']):
                if len(rounds) <= 2048:
                    break
                if previous.get('status') != 'executing' and check_date(previous['expires_date']) < today:
                    rounds.pop(key)
        return deepcopy(row)
    return update_document(DOCUMENT, {'rounds': {}}, mutate, scope)


def select_offer(scope: str, player_id: int, round_id: str, offer_id: str,
                 game_date: str, action: str) -> dict:
    today = check_date(game_date)
    def mutate(doc):
        row = doc.setdefault('rounds', {}).get(str(player_id))
        if not row or row['id'] != round_id:
            fail('departure.error.changed')
        if action == 'accept' and row.get('status') == 'sold' and row.get('accepted_offer_id') == offer_id:
            return deepcopy(row)
        if row.get('status') == 'executing':
            fail('departure.error.uncertain')
        if not check_date(row['created_date']) <= today < check_date(row['expires_date']):
            fail('departure.error.expired')
        offer = next((item for item in row['offers'] if item['id'] == offer_id), None)
        if row.get('status') != 'open' or not offer or offer['status'] != 'open':
            fail('departure.error.changed')
        if action == 'reject':
            offer['status'] = 'rejected'
        elif action == 'accept':
            row['status'] = 'executing'
            row['accepted_offer_id'] = offer_id
            row['execution_date'] = today.isoformat()
        else:
            fail('departure.error.changed')
        return deepcopy(row)
    return update_document(DOCUMENT, {'rounds': {}}, mutate, scope)


def complete_round(scope: str, player_id: int, round_id: str, receipt: dict) -> dict:
    def mutate(doc):
        row = doc['rounds'][str(player_id)]
        if row['id'] != round_id or row['status'] != 'executing':
            fail('departure.error.changed')
        row['status'] = 'sold'
        row['receipt'] = {key: receipt[key] for key in ('player_id', 'source_team_id', 'target_team_id', 'transfer_fee')}
        # Preserve the required finance receipt and compatibility with legacy sales.
        credit_key = 'seller_transfer_budget_after' if 'seller_transfer_budget_after' in receipt else 'seller_balance_after'
        row['receipt'][credit_key] = receipt[credit_key]
        for offer in row['offers']:
            offer['status'] = 'accepted' if offer['id'] == row['accepted_offer_id'] else 'closed'
        return deepcopy(row)
    return update_document(DOCUMENT, {'rounds': {}}, mutate, scope)


def restore_round(scope: str, player_id: int, round_id: str) -> None:
    def mutate(doc):
        row = doc['rounds'][str(player_id)]
        if row['id'] == round_id and row['status'] == 'executing':
            row['status'] = 'open'
            row.pop('accepted_offer_id', None)
    update_document(DOCUMENT, {'rounds': {}}, mutate, scope)
