"""Isolated roster and account-record regressions; no live FM or account IO."""
from contextlib import nullcontext
from copy import deepcopy
import json
import struct
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from fm_odds_web import LocalOddsState
from tools import club_reader, club_economy, money, account_store


class RosterReader:
    def __init__(self):
        self.module_base = 0x100000
        self.layout = SimpleNamespace(
            team_vtable_rva=0x100, national_team_vtable_rva=0x200,
            actual_player_vtable_rvas={0x300}, player_and_non_player_vtable_rvas={0x400},
            player_person_offset=0x10, player_and_non_player_person_offset=0x20,
        )
        self.teams = {9: (0x9000, 0x8000), 10: (0xA000, 0xB000)}
        self.vectors = {0x8000: [0x2000 + i * 0x100 for i in range(20)], 0xB000: [0x5000]}
        self.identifiers = {address: 42 + i for i, address in enumerate(self.vectors[0x8000])}
        self.identifiers[0x5000] = 99
        self.ptr_array = MagicMock(side_effect=lambda address, count: self.vectors[address][:count])
        self.player_reads = []

    def bytes(self, address, size):
        raw = bytearray(size)
        team = next(((uid, begin) for uid, (target, begin) in self.teams.items() if target == address), None)
        if team:
            uid, begin = team
            struct.pack_into('<Q', raw, 0, self.module_base + self.layout.team_vtable_rva)
            struct.pack_into('<I', raw, club_reader.ENTITY_UID, uid)
            struct.pack_into('<Q', raw, club_reader.TEAM_ROSTER_BEGIN, begin)
            struct.pack_into('<Q', raw, club_reader.TEAM_ROSTER_END, begin + len(self.vectors[begin]) * 8)
        elif address in self.identifiers:
            self.player_reads.append(address)
            struct.pack_into('<Q', raw, 0, self.module_base + 0x300)
            struct.pack_into('<I', raw, 0x10 + club_reader.ENTITY_UID, self.identifiers[address])
        return bytes(raw)

    def ptr(self, address):
        for begin, pointers in self.vectors.items():
            if begin <= address < begin + len(pointers) * 8:
                return pointers[(address - begin) // 8]
        return 0


@pytest.mark.parametrize('session_type', [club_reader.PlayerActivityMemorySession, club_reader.CanteenSeaCucumberMemorySession])
def test_twenty_players_share_one_roster_read_but_keep_each_uid_check(session_type):
    reader = RosterReader()
    session = session_type(reader, None)
    for i in range(20):
        address = reader.vectors[0x8000][i]
        assert session.resolve_roster_player(9, '0x9000', 42 + i, hex(address)) == hex(address)
    assert reader.ptr_array.call_count == 1
    assert len(reader.player_reads) == 20
    reader.identifiers[0x2000] = 777
    assert session.resolve_roster_player(9, '0x9000', 42, '0x2000') is None
    assert reader.ptr_array.call_count == 1
    # A second request must obtain its own roster snapshot.
    fresh = session_type(reader, None)
    assert fresh.resolve_roster_player(9, '0x9000', 43, '0x2100') == '0x2100'
    assert reader.ptr_array.call_count == 2


def test_roster_pointer_replacement_without_resize_does_not_accept_departed_player():
    reader = RosterReader()
    session = club_reader.PlayerActivityMemorySession(reader, None)
    assert session.resolve_roster_player(9, '0x9000', 42, '0x2000') == '0x2000'
    reader.vectors[0x8000][0] = 0x5000
    assert session.resolve_roster_player(9, '0x9000', 42, '0x2000') is None
    assert reader.ptr_array.call_count == 2
    assert session.resolve_roster_player(9, '0x9000', 99, '0x5000') == '0x5000'


def test_roster_relocation_and_different_squad_have_independent_snapshots():
    reader = RosterReader()
    session = club_reader.PlayerActivityMemorySession(reader, None)
    assert session.resolve_roster_player(9, '0x9000', 42, '0x2000') == '0x2000'
    reader.teams[9] = (0x9000, 0xC000)
    reader.vectors[0xC000] = [0x2100]
    assert session.resolve_roster_player(9, '0x9000', 42, '0x2000') is None
    assert session.resolve_roster_player(10, '0xA000', 99, '0x5000') == '0x5000'
    assert reader.ptr_array.call_count == 3


def test_roster_null_slots_keep_original_pointer_positions():
    reader = RosterReader()
    reader.vectors[0x8000].insert(0, 0)
    session = club_reader.PlayerActivityMemorySession(reader, None)
    assert session.resolve_roster_player(9, '0x9000', 42, '0x2000') == '0x2000'
    assert session.resolve_roster_player(9, '0x9000', 43, '0x2100') == '0x2100'
    assert reader.ptr_array.call_count == 1


def test_activity_target_resolution_uses_batch_reader_and_preserves_single_path():
    state = LocalOddsState.__new__(LocalOddsState)
    state.lock = threading.RLock()
    players = [{'id': 42 + i, 'address': hex(0x2000 + i * 0x100)} for i in range(2)]
    profile = {'team': {'id': 9, 'address': '0x9000', 'team_type': 'club'}, 'players': players}
    state.club_profiles = {9: profile}
    state.club_profile = None
    state.output = {'managed_teams': [profile['team']]}
    state._reload_player_profile = MagicMock(side_effect=AssertionError('must not reload'))
    reader = RosterReader()
    session = club_reader.PlayerActivityMemorySession(reader, None)
    with patch('fm_odds_web.resolve_team_player_address', side_effect=AssertionError('separate reader')):
        for player in players:
            target = state._live_player_effect_target(player['id'], 9, memory_session=session)
            assert target.address == player['address']
    assert reader.ptr_array.call_count == 1
    with patch('fm_odds_web.resolve_team_player_address', return_value='0x2000') as single:
        assert state._live_player_profile(42, 9)[1]['id'] == 42
    single.assert_called_once()


@pytest.mark.parametrize('canteen', [False, True])
def test_real_batch_entrypoints_reuse_rosters_without_live_native_writes(canteen):
    state = LocalOddsState.__new__(LocalOddsState)
    state.lock = threading.RLock()
    state.memory_lock = threading.RLock()
    players = [{'id': 42 + i, 'name': str(i), 'address': hex(0x2000 + i * 0x100)} for i in range(2)]
    profile = {'team': {'id': 9, 'address': '0x9000', 'team_type': 'club'}, 'players': players}
    state.club_profiles = {9: profile}
    state.club_profile = None
    state.output = {'managed_teams': [profile['team']]}
    state._reload_player_profile = MagicMock(side_effect=AssertionError('unexpected reload'))
    state._canteen_sea_cucumber_context = MagicMock(return_value=({'id': 7}, 7, '2026-07-28'))
    state._archive_activity_relationship = MagicMock(return_value={})
    reader = RosterReader()
    session_class = club_reader.CanteenSeaCucumberMemorySession if canteen else club_reader.PlayerActivityMemorySession
    session = session_class(reader, None)
    session.apply_effect = MagicMock(return_value={'field': 'fitness', 'after': 100, 'fitness': 100, 'sharpness': 100})
    session.apply_intimacy = MagicMock(return_value={'before': 0, 'after': 1, 'applied': 1})

    def activity(_manager, _player, _name, _activity, _date, _intimacy, apply):
        result = apply(1)
        return {'integer_applied': 1, 'intimacy': 1, 'effect': result['effect']}

    opener = 'canteen_sea_cucumber_memory_session' if canteen else 'player_activity_memory_session'
    with (
        patch(f'fm_odds_web.{opener}', return_value=nullcontext(session)),
        patch('fm_odds_web.resolve_team_player_address', side_effect=AssertionError('separate roster reader')),
        patch('fm_odds_web.perform_player_activity', side_effect=activity),
        patch('fm_odds_web.charge_combined_funds', return_value={'total': 1}),
        patch('fm_odds_web.public_economy', return_value={}),
        patch('fm_odds_web.welfare_status', return_value={}),
        patch('fm_odds_web.local_purchase_price', return_value=1),
    ):
        if canteen:
            response = state.canteen_sea_cucumbers({'team_id': 9, 'player_ids': [42, 43]})
        else:
            response = state.player_activity_batch({'items': [
                {'player_id': uid, 'team_id': 9, 'activity': 'hot_spring'} for uid in [42, 43]
            ]})
    assert response['completed'] == 2, response
    assert reader.ptr_array.call_count == 1
    assert session.apply_effect.call_count == 2
    assert session.apply_intimacy.call_count == 2


def test_normalized_history_survives_document_copies_and_only_processes_new_or_edited_rows():
    payload = {'transactions': [{'amount': i + 0.105} for i in range(1000)]}
    assert club_economy._normalize_economy_money(payload)
    copied = deepcopy(payload)
    copied['transactions'].append({'amount': '0.105'})
    copied['transactions'][0].update(amount_minor=222)
    copied['transactions'][1]['display_type'] = 'display only'
    with patch.object(money, 'migrate_money_fields', wraps=money.migrate_money_fields) as migrate:
        assert club_economy._normalize_economy_money(copied)
    assert migrate.call_count == 2
    assert copied['transactions'][0]['amount'] == 2.22
    assert copied['transactions'][-1]['amount_minor'] == 11
    assert payload['transactions'][0]['amount_minor'] == 11
    with patch.object(money, 'migrate_money_fields', wraps=money.migrate_money_fields) as migrate:
        assert not club_economy._normalize_economy_money(deepcopy(copied))
    assert migrate.call_count == 0


@pytest.mark.parametrize('mutation', [
    lambda row: row.__setitem__('amount_minor', True),
    lambda row: row.update(amount_minor=True),
    lambda row: row.__ior__({'amount_minor': True}),
])
def test_changed_money_still_rejects_invalid_minor_units(mutation):
    rows = [{'amount': 1.0}]
    money.migrate_money_records(rows, ('amount',))
    mutation(rows[0])
    with pytest.raises(money.InvalidMoney):
        money.migrate_money_records(deepcopy(rows), ('amount',))


def test_deleting_or_replacing_history_records_revalidates_legacy_money():
    rows = [{'amount': 1.0}]
    money.migrate_money_records(rows, ('amount',))
    rows[0].pop('amount_minor')
    rows[0]['amount'] = '0.105'
    assert money.migrate_money_records(rows, ('amount',))
    assert rows[0]['amount_minor'] == 11
    rows[0] = {'amount': '1e100'}
    assert money.migrate_money_records(rows, ('amount',))
    assert rows[0]['amount_minor'] == 10 ** 102


def test_json_and_external_reload_keep_plain_schema_and_force_validation():
    rows = [{'amount': 1.23}]
    money.migrate_money_records(rows, ('amount',))
    encoded = json.dumps(rows)
    assert json.loads(encoded) == [{'amount': 1.23, 'amount_minor': 123}]
    other_account = json.loads(encoded)
    other_account[0]['amount_minor'] = True
    with pytest.raises(money.InvalidMoney):
        money.migrate_money_records(other_account, ('amount',))


@pytest.mark.parametrize('key,fields', [
    ('salary_payments', ('gross_weekly', 'net_amount')),
    ('club_dividend_payments', ('amount',)),
    ('club_dividend_forecasts', ('estimated_amount',)),
    ('medical_treatments', ('daily_cost', 'charged_today')),
    ('match_intelligence_price_locks', ('combined_funds',)),
    ('transfer_budget_withdrawals', ('amount', 'bank_deducted', 'wallet_deducted')),
    ('transfer_budget_rollbacks', ('amount', 'bank_deducted', 'wallet_deducted', 'unrecovered')),
    ('inventory', ('price',)),
])
def test_all_economy_history_collections_keep_new_and_changed_values_valid(key, fields):
    row = {field: '0.105' for field in fields}
    mapping = key in {'club_dividend_forecasts', 'medical_treatments', 'match_intelligence_price_locks'}
    payload = {key: {'row': row} if mapping else [row]}
    assert club_economy._normalize_economy_money(payload)
    normalized = payload[key]['row'] if mapping else payload[key][0]
    assert all(normalized[f'{field}_minor'] == 11 for field in fields)
    normalized[f'{fields[0]}_minor'] = 456
    assert club_economy._normalize_economy_money(payload)
    assert normalized[fields[0]] == 4.56


def test_real_account_store_copy_and_reload_contract_in_temporary_directory(tmp_path):
    # Only this temporary file is used; neither account scope nor app root is consulted.
    path = tmp_path / 'scope-a.fmodd'
    payload = account_store._empty('scope-a')
    economy = {'transactions': [{'amount': 1.23}]}
    club_economy._normalize_economy_money(economy)
    payload['documents']['economy'] = economy
    try:
        account_store._write_container(path, payload)
        returned = account_store._read_container(path)['documents']['economy']
        with patch.object(money, 'migrate_money_fields', wraps=money.migrate_money_fields) as migrate:
            assert not club_economy._normalize_economy_money(returned)
        assert migrate.call_count == 0
        returned['transactions'][0]['amount_minor'] = 456
        assert account_store._read_container(path)['documents']['economy']['transactions'][0]['amount_minor'] == 123
        account_store._CONTAINER_CACHE.pop(path, None)
        reloaded = account_store._read_container(path)['documents']['economy']
        with patch.object(money, 'migrate_money_fields', wraps=money.migrate_money_fields) as migrate:
            assert not club_economy._normalize_economy_money(reloaded)
        assert migrate.call_count == 1
    finally:
        account_store._CONTAINER_CACHE.pop(path, None)
