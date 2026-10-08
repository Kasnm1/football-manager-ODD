"""Activity 2F player-sale orchestration over existing ownership/native APIs."""
from __future__ import annotations

from copy import deepcopy
from datetime import date
from heapq import nsmallest
from tools import player_departure as offers
from tools.club_economy import assert_activity_centre_unlocked
from tools.club_reader import (
    borrow_game_reader, invalidate_club_profile_cache,
    read_world_player_profile, _world_player_table_rows,
)
from tools.database_index import database_index_for_reader
from tools.preview_cup_odds import read_game_clock_from_reader
from tools.player_aliases import apply_player_aliases
from tools.world_clubs import (
    acquired_clubs_for_game_date, load_acquired_clubs,
    read_native_world_club_detail, read_native_world_club_player_membership,
)
from tools.player_movement import move_owned_club_player, player_movement_capabilities


class PlayerDepartureApplication:
    def __init__(self, state, *, runtime_enabled: bool = True):
        self.state = state
        self.runtime_enabled = runtime_enabled

    @staticmethod
    def _competition_maps(output):
        """Build current team/competition labels from the live competition projection."""
        by_team, by_id = {}, {}
        for competition in output.get('competition_formats') or []:
            name = str(competition.get('competition_name') or competition.get('name') or '').strip()
            comp_id = int(competition.get('competition_id') or competition.get('id') or 0)
            if not name or name in {'未分类', '未知赛事'}:
                continue
            if comp_id > 0:
                by_id[comp_id] = name
            for stage in competition.get('stages') or []:
                for team in stage.get('teams') or []:
                    team_id = int(team.get('id') or team.get('team_id') or 0)
                    if team_id > 0:
                        by_team[team_id] = name
        return by_team, by_id

    @classmethod
    def _label_directory(cls, directory, output):
        by_team, by_id = cls._competition_maps(output)
        result = dict(directory or {})
        clubs = []
        for source in result.get('clubs') or []:
            club = dict(source)
            label = str(club.get('competition_name') or club.get('competition') or '').strip()
            if label in {'', '未分类', '未知赛事'}:
                label = by_team.get(int(club.get('id') or 0)) or by_id.get(int(club.get('competition_id') or 0)) or ''
                if label:
                    club['competition'] = label
                    club['competition_name'] = label
            clubs.append(club)
        result['clubs'] = clubs
        return result

    @staticmethod
    def _excluded_buyer_ids(output, owned):
        # Portfolio and managed-team projections both identify native Team UIDs.
        # Club object UIDs are a separate namespace and must not be mixed in.
        managed = output.get('managed_teams') or ([output['managed_team']] if output.get('managed_team') else [])
        return {int(row.get('id') or 0) for row in owned if int(row.get('id') or 0) > 0} | {
            int(row.get('id') or 0) for row in managed
            if str(row.get('team_type') or 'club') == 'club' and int(row.get('id') or 0) > 0
        }

    @classmethod
    def _repair_round_labels(cls, rounds, directory, output, owned=()):
        by_team, by_id = cls._competition_maps(output)
        by_directory = {int(row.get('id') or 0): row for row in directory.get('clubs') or []}
        excluded = cls._excluded_buyer_ids(output, owned)
        for round_row in (rounds or {}).values():
            if not isinstance(round_row, dict):
                continue
            for offer in round_row.get('offers') or []:
                if int(offer.get('team_id') or 0) in excluded:
                    offer['_invalid_buyer'] = True
                    continue
                club = by_directory.get(int(offer.get('team_id') or 0))
                if club is None:
                    # Old rounds may point to rows that the current scan now
                    # rejects (for example an all-star team). Do not keep the
                    # stale card visible when a live directory is available.
                    if directory.get('clubs'):
                        offer['_invalid_buyer'] = True
                    continue
                if not offers.valid_buyer_club(club):
                    offer['_invalid_buyer'] = True
                    continue
                if str(offer.get('competition_name') or '').strip() not in {'', '未分类', '未知赛事'}:
                    continue
                label = (str(club.get('competition_name') or club.get('competition') or '').strip()
                         or by_team.get(int(offer.get('team_id') or 0))
                         or by_id.get(int(offer.get('competition_id') or 0)))
                if label and label not in {'未分类', '未知赛事'}:
                    offer['competition_name'] = label
        for round_row in (rounds or {}).values():
            if isinstance(round_row, dict):
                round_row['offers'] = [offer for offer in round_row.get('offers') or [] if not offer.pop('_invalid_buyer', False)]
        return rounds

    def _current_directory(self, scope, output):
        native_loader = getattr(self.state, '_world_club_native_cache', None)
        directory_loader = getattr(self.state, '_world_club_directory', None)
        native = native_loader(str(output.get('save_instance_id') or scope)) if callable(native_loader) else None
        raw = directory_loader(output, str(output.get('save_instance_id') or scope), native) if callable(directory_loader) else {'clubs': []}
        return self._label_directory(raw, output)

    def context(self):
        state = self.state
        with state.lock:
            scope = state._bind_current_save()
            output = deepcopy(state.output)
            assert_activity_centre_unlocked('talk_room')
            with borrow_game_reader() as reader:
                output['game_date'] = str(read_game_clock_from_reader(reader)['date'])
            owned = [row for row in acquired_clubs_for_game_date(
                load_acquired_clubs(scope).get('clubs', []), str(output.get('game_date') or '')
            ) if row.get('ownership_active') is not False]
        return scope, output, owned

    # Match background readers: acquire memory before account/state.
    # Waiting for memory while holding state blocks progress publication.
    def players(self, team_id: int = 0):
        with self.state._timed_user_memory_operation('player_departure_roster'), self.state._account_operation(None):
            scope, output, owned = self.context()
            clubs = [{'id': int(row['id']), 'name': str(row.get('name') or row['id'])} for row in owned]
            if not clubs:
                return {'data_scope_id': scope, 'clubs': [], 'players': [], 'rounds': {}, 'team_id': 0}
            team_id = int(team_id or clubs[0]['id'])
            if team_id not in {row['id'] for row in clubs}:
                offers.fail('departure.error.changed')
            _, _, club = self.state._owned_world_club_target(team_id)
            detail = read_native_world_club_detail(club, include_related_squads=True)
            apply_player_aliases(detail, scope)
            rows = []
            seen = set()
            for player in detail.get('players') or []:
                uid = int(player.get('id') or 0)
                if not uid or uid in seen:
                    continue
                seen.add(uid)
                row = {key: deepcopy(player.get(key)) for key in (
                    'id', 'name', 'age', 'ca', 'pa', 'world_reputation', 'international_reputation',
                    'positions', 'primary_positions', 'position_ratings', 'market_value',
                    'nationality', 'fitness', 'morale', 'sharpness', 'squad_team_id', 'squad_label')}
                row['source_team_id'] = team_id
                row['available'] = not bool(player.get('loan') or (player.get('transfer') or {}).get('future_transfer'))
                rows.append(row)
            self._preview_valuations(rows, str(output['game_date']))
            document = offers.load_document(offers.DOCUMENT, {'rounds': {}}, scope)
            directory = self._current_directory(scope, output)
            self._repair_round_labels(document.get('rounds') or {}, directory, output, owned)
            with borrow_game_reader() as reader:
                capability = player_movement_capabilities(reader.layout)['transfer']
                budget_supported = getattr(reader.layout, 'finance_remaining_transfer_budget_offset', None) is not None
            visible_rounds = {}
            for row in rows:
                current = deepcopy(document.get('rounds', {}).get(str(row['id'])))
                if current and current.get('status') == 'open' and current.get('generation_version') != offers.OFFER_GENERATION_VERSION:
                    # Mark legacy quotes closed in the response only. The next
                    # explicit search creates the corrected round; opening the
                    # room still never generates prices.
                    current['status'] = 'closed'
                    current['legacy_generation'] = True
                visible_rounds[str(row['id'])] = current
                if current and not current.get('legacy_generation') and str(output['game_date']) < str(current.get('expires_date') or ''):
                    row['valuation'] = deepcopy(current.get('valuation') or row.get('valuation'))
            return {'data_scope_id': scope, 'game_date': str(output.get('game_date') or ''),
                    'clubs': clubs, 'team_id': team_id, 'players': rows,
                    'rounds': visible_rounds,
                    'transfer_enabled': bool(self.runtime_enabled and capability.get('enabled') and budget_supported)}

    @staticmethod
    def _dated_peer(source, today):
        row = dict(source)
        try:
            birth = date.fromisoformat(str(row.get('date_of_birth')))
            row['age'] = today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))
        except (TypeError, ValueError):
            pass
        return row

    def _preview_valuations(self, players, game_date):
        """Preview save-index prices once per roster; never create or persist offers."""
        pending = []
        for player in players:
            if offers.valid_value(player):
                player['valuation'] = offers.estimate_value(player, [])
            else:
                pending.append(player)
        if not pending:
            return
        with borrow_game_reader() as reader:
            index = database_index_for_reader(reader)
            if index is None:
                return
            sources = index.all_player_rows()
        today = date.fromisoformat(game_date)
        known = {int(row['id']): row for row in players}
        by_ca, pool = {}, []
        for source in sources:
            if not offers.valid_value(source, listing=offers.ALLOW_LISTING_SAMPLES):
                continue
            row = self._dated_peer(source, today)
            row.update(known.get(int(row.get('id') or 0), {}))
            ca = offers.number(row.get('ca'))
            if ca is None or not 1 <= ca <= 200:
                continue
            try:
                row['world_reputation'] = offers.reputation(row)
            except offers.ValidationError:
                continue
            pool.append(row)
            by_ca.setdefault(ca, []).append(row)
        for player in pending:
            try:
                ca = offers.number(player.get('ca')) or 0
                rep = offers.reputation(player)
                peers = [peer for value in range(max(1, ca-15), min(200, ca+15)+1)
                         for peer in by_ca.get(value, []) if abs(peer['world_reputation']-rep) <= 1500]
                peers = peers if len(peers) >= 5 else pool
                age = offers.number(player.get('age'))
                close = []
                # Test the estimator's strict age band first, before scoring an
                # entire broad pool for every squad member on page entry.
                for peer in peers:
                    other_age = offers.number(peer.get('age'))
                    if age is not None and other_age is not None and abs(age-other_age) > 2:
                        continue
                    distance = offers.valuation_distance(player, peer)
                    if distance is not None and distance <= 2:
                        close.append((distance, peer))
                if len(close) >= 5:
                    peers = [row for _, row in nsmallest(offers.VALUATION_PEER_LIMIT, close, key=lambda item:item[0])]
                else:
                    peers = nsmallest(offers.VALUATION_PEER_LIMIT, peers,
                                     key=lambda row: float('inf') if (distance := offers.valuation_distance(player, row)) is None else distance)
                player['valuation'] = offers.estimate_value(player, peers, listing=offers.ALLOW_LISTING_SAMPLES)
            except offers.ValidationError:
                player['valuation'] = None

    def _market(self, scope, output, owned, player):
        state = self.state
        native = state._world_club_native_cache(str(output.get('save_instance_id') or scope))
        directory = self._label_directory(state._world_club_directory(output, str(output.get('save_instance_id') or scope), native), output)
        peers = []
        if not offers.valid_value(player):
            with borrow_game_reader() as reader:
                index = database_index_for_reader(reader)
                if index is None:
                    offers.fail('departure.error.valuation')
                candidates = [row for row in index.all_player_rows()
                              if int(row.get('id') or 0) != int(player['id'])
                              and 1 <= (offers.number(row.get('ca')) or 0) <= 200]
                priced = [row for row in candidates if offers.valid_value(row, listing=offers.ALLOW_LISTING_SAMPLES)]
                candidates = priced or candidates
                today = date.fromisoformat(str(output['game_date']))
                ranked = []
                for row in candidates:
                    row = self._dated_peer(row, today)
                    distance = offers.valuation_distance(player, row)
                    if distance is not None:
                        ranked.append((distance, row))
                peers = [row for _, row in sorted(ranked, key=lambda item: item[0])[:offers.VALUATION_PEER_LIMIT]]
                # Current bounded peer samples, after normal Player/Person validation.
                peers = _world_player_table_rows(reader, peers)
                rep_offset = reader.layout.player_world_reputation_offset
                for peer in peers:
                    address = int(str(peer.get('address') or '0'), 0)
                    peer['world_reputation'] = reader.u16(address + rep_offset) if address and rep_offset is not None else None
        valuation = offers.estimate_value(player, peers, listing=offers.ALLOW_LISTING_SAMPLES)
        generated = offers.generate_offers(player, directory.get('clubs') or [],
                                           self._excluded_buyer_ids(output, owned), valuation)
        return valuation, generated

    def search(self, payload):
        source_id = int(payload.get('team_id') or 0)
        uid = int(payload.get('player_id') or 0)
        with self.state._timed_user_memory_operation('player_departure_search'), self.state._account_operation(None):
            scope, output, owned = self.context()
            if str(payload.get('data_scope_id') or '') != scope:
                offers.fail('departure.error.changed')
            _, _, club = self.state._owned_world_club_target(source_id)
            member = read_native_world_club_player_membership(club, uid)
            if not member:
                offers.fail('departure.error.changed')
            player = read_world_player_profile(uid)
            if player.get('loan') or (player.get('transfer') or {}).get('future_transfer'):
                offers.fail('departure.error.ineligible')
            wrapper = {'players': [player]}
            apply_player_aliases(wrapper, scope)
            player = wrapper['players'][0]
            player['competition_id'] = club.get('competition_id')
            row = offers.start_round(scope, player, source_id, str(output.get('game_date') or ''),
                                     lambda: self._market(scope, output, owned, player),
                                     excluded_buyers=self._excluded_buyer_ids(output, owned))
            return {'data_scope_id': scope, 'round': self._response_round(scope, output, uid, row, owned)}

    def _response_round(self, scope, output, player_id, row, owned=()):
        result = deepcopy(row) if row else None
        if not result:
            return result
        directory = self._current_directory(scope, output)
        wrapper = {str(player_id): result}
        self._repair_round_labels(wrapper, directory, output, owned)
        return wrapper[str(player_id)]

    def action(self, payload, action):
        uid = int(payload.get('player_id') or 0)
        round_id = str(payload.get('round_id') or '')
        offer_id = str(payload.get('offer_id') or '')
        state = self.state
        with state._timed_user_memory_operation('player_departure_' + action), state._account_operation(None):
            scope, output, owned = self.context()
            if str(payload.get('data_scope_id') or '') != scope:
                offers.fail('departure.error.changed')
            today = str(output.get('game_date') or '')
            old = offers.current_round(scope, uid)
            if not old or old['id'] != round_id:
                offers.fail('departure.error.changed')
            if old.get('status') == 'sold' and action == 'accept' and old.get('accepted_offer_id') == offer_id:
                return {'data_scope_id': scope, 'round': self._response_round(scope, output, uid, old, owned), 'idempotent': True}
            source_id = int(old['source_team_id'])
            if source_id not in {int(row['id']) for row in owned}:
                offers.fail('departure.error.changed')
            if action == 'reject':
                row = offers.select_offer(scope, uid, round_id, offer_id, today, action)
                return {'data_scope_id': scope, 'round': self._response_round(scope, output, uid, row, owned)}
            if action != 'accept':
                offers.fail('departure.error.changed')
            _, _, source = state._owned_world_club_target(source_id)
            member = read_native_world_club_player_membership(source, uid)
            if not member:
                offers.fail('departure.error.changed')
            offer = next((row for row in old['offers'] if row['id'] == offer_id), None)
            if not offer or (old.get('status') == 'open' and old.get('generation_version') != offers.OFFER_GENERATION_VERSION) or int(offer['team_id']) in self._excluded_buyer_ids(output, owned):
                offers.fail('departure.error.changed')
            directory = self._current_directory(scope, output)
            target = next((club for club in directory.get('clubs') or [] if int(club.get('id') or 0) == int(offer['team_id'])), None)
            if directory.get('clubs') and not offers.valid_buyer_club(target or {}):
                offers.fail('departure.error.changed')
            # Re-resolve the target by UID in the native transaction, never a persisted address.
            with borrow_game_reader() as reader:
                capability = player_movement_capabilities(reader.layout)['transfer']
                if (not self.runtime_enabled or not capability.get('enabled')
                        or getattr(reader.layout, 'finance_remaining_transfer_budget_offset', None) is None):
                    offers.fail('departure.error.capability')
            row = offers.select_offer(scope, uid, round_id, offer_id, today, 'accept')
            if row.get('status') == 'sold':
                return {'data_scope_id': scope, 'round': self._response_round(scope, output, uid, row, owned), 'idempotent': True}
            def commit(receipt):
                offers.complete_round(scope, uid, round_id, receipt)
            try:
                result = move_owned_club_player(
                    source_team_id=source_id, source_team_address=source.get('address'),
                    source_squad_team_id=int(member.get('squad_team_id') or 0),
                    source_squad_team_address=member.get('squad_team_address'),
                    target_team_id=int(offer['team_id']), target_team_address=0,
                    player_id=uid, mode='transfer', transfer_fee=int(offer['amount']),
                    commit_transfer=commit,
                )
            except Exception as error:
                if getattr(error, 'rollback_incomplete', False):
                    offers.fail('departure.error.uncertain')
                try:
                    offers.restore_round(scope, uid, round_id)
                except Exception:
                    offers.fail('departure.error.uncertain')
                raise offers.ConflictError('departure.error.sale', code='player_departure_sale_failed',
                                           phase='native_transfer', retryable=False,
                                           message_key='departure.error.sale') from error
            invalidate_club_profile_cache(team_id=source_id)
            invalidate_club_profile_cache(team_id=int(offer['team_id']))
            with state.lock:
                state.data_version += 1
            return {'data_scope_id': scope, 'round': self._response_round(scope, output, uid, offers.current_round(scope, uid), owned),
                    'result': {'verified': bool(result.get('verified')), 'transfer_fee': int(offer['amount'])}}
