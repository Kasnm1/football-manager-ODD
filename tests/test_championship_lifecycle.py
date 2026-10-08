from __future__ import annotations

import copy
import json
import shutil
import subprocess
import threading
from contextlib import ExitStack
import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_championship_odds import _cup_output, _output, _standings
from tools import championship_odds as championship
from tools.betting_account import has_pending_championship_bets, pending_snapshot_paths, settle_championship_bets
from tools.preview_cup_odds import native_terminal_final_fixture
from tools.storage_management import clear_cache_files
from fm_odds_web import LocalOddsState, publish_championship_markets, championship_market_dependency_signature, championship_refund_settlement_dates


class ChampionshipLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        for name, path in [('cup_state_path', self.root / 'cups.json'), ('league_state_path', self.root / 'leagues.json')]:
            patcher = patch(f'tools.championship_odds.{name}', return_value=path)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch('tools.preview_cup_odds.read_result_history', return_value=[])
        patcher.start()
        self.addCleanup(patcher.stop)

    def league_output(self, game_date='2027-06-01', count=20):
        output = _output(count)
        output.update(game_date=game_date, game_time='12:00', matches=[], season_results=[])
        output['competition_formats'] = [{
            'competition_id': 11, 'competition_kind': 'league',
            'opening_stage_type': 'league', 'opening_slot_count': count,
            'opening_field_complete': True,
            'opening_teams': [{'id': i} for i in range(1, count + 1)],
            'first_fixture_date': '2026-08-08', 'last_fixture_date': '2027-05-30',
            'season_fixture_bounds_verified': True,
            'format_last_observed_at': game_date,
            'competition_season_address': '0x1000', 'actual_competition_address': '0x2000',
        }]
        return output

    def test_famous_league_settles_from_verified_terminal_table_without_result_rows(self):
        markets = championship.build_championship_markets(self.league_output(), _standings(20, 38))
        market = markets['competitions'][0]
        self.assertEqual((market['status'], market['winner_team_id'], market['settlement_date']), ('complete', 1, '2027-05-30'))

    def test_retained_old_native_season_is_settlement_only_after_rollover(self):
        markets = championship.build_championship_markets(self.league_output('2027-08-20'), _standings(20, 38))
        self.assertEqual(markets['competitions'], [])
        self.assertEqual(markets['settlement_competitions'][0]['season_key'], '11:2026/27')

    def test_current_native_18_team_field_overrides_famous_default(self):
        output = self.league_output('2026-09-01', 18)
        markets = championship.build_championship_markets(output, _standings(18))
        self.assertEqual(len(markets['competitions'][0]['teams']), 18)
        self.assertEqual(markets['competitions'][0]['expected_matches'], 34)

    def test_famous_hybrid_cannot_settle_from_regular_league_leader(self):
        output = self.league_output()
        output['competition_formats'][0]['has_terminal_cup_stage'] = True
        markets = championship.build_championship_markets(output, _standings(20, 38))
        self.assertEqual(markets['competitions'], [])
        self.assertFalse(markets['settlement_competitions'])

    def test_unidentified_completed_table_is_not_reset_or_relabelled(self):
        output = _output()
        output['game_date'] = '2027-07-01'
        standings = _standings(20, 38)
        original = copy.deepcopy(standings)
        markets = championship.build_championship_markets(output, standings)
        self.assertEqual(markets['competitions'], [])
        self.assertEqual(standings, original)

    def test_two_leg_final_requires_advancement_not_return_leg_score(self):
        result = {'date': '2027-05-30', 'home': {'id': 1}, 'away': {'id': 2}, 'home_goals': 2, 'away_goals': 0}
        final = {'leg_count': 2}
        self.assertIsNone(championship._final_result_winner_id(result, final))
        self.assertIsNone(championship._final_result_winner_id({**result, 'winner_side':'home'}, final))
        self.assertEqual(championship._final_result_winner_id({**result, 'advanced_team_id': 2}, final), 2)
        self.assertIsNone(championship._final_result_winner_id({**result, 'advanced_team_id': 3}, final))

    def test_native_terminal_hint_recovers_final_outside_match_window(self):
        output = _cup_output('2027-05-31')
        final = {'date': '2027-05-30', 'home_id': 1, 'away_id': 2, 'leg_count': 1}
        output['competition_formats'][0]['terminal_final_fixture'] = final
        output['matches'] = []
        output['settlement_results'] = [{'date': '2027-05-30', 'competition_id': 1301394, 'home': {'id': 1}, 'away': {'id': 2}, 'home_goals': 2, 'away_goals': 1}]
        markets = championship.build_championship_markets(output, {'competitions': []})
        cup = next(m for m in markets['competitions'] if m['competition_id'] == 1301394)
        self.assertEqual((cup['status'], cup['winner_team_id']), ('complete', 1))

    def test_round_metadata_identifies_final_but_not_semifinal_or_third_place(self):
        output = {'matches': [{'competition_id': 123, 'fixture_date': '2027-05-30', 'home': {'id': 1}, 'away': {'id': 2}, 'competition_round': {'stage_index': 2, 'stage_count': 3, 'round_index': 4, 'round_count': 5, 'round_tie_count': 1, 'match_role': 'final', 'tie_address': 100}}]}
        self.assertEqual(championship._final_fixture_from_rounds(output, 123)['home_id'], 1)
        for role, index in [('third_place', 3), ('knockout', 3)]:
            output['matches'][0]['competition_round'].update(match_role=role, round_index=index)
            self.assertIsNone(championship._final_fixture_from_rounds(output, 123))

    def test_all_scanned_final_legs_choose_return_outside_odds_window(self):
        first = SimpleNamespace(address=10, stage_index=2, group_index=4, home_team=1, away_team=2, match_date=date(2027,5,20), kickoff_minutes=900)
        second = SimpleNamespace(address=20, stage_index=2, group_index=4, home_team=2, away_team=1, match_date=date(2027,5,27), kickoff_minutes=900)
        semifinal = SimpleNamespace(address=30, stage_index=2, group_index=3, home_team=3, away_team=4, match_date=date(2027,5,10), kickoff_minutes=900)
        reader = SimpleNamespace(team=lambda pointer: {'id': pointer})
        def context(_reader, fixture, **kwargs):
            return {'is_competition_final': True, 'tie_address': 100, 'stage_index':2, 'round_index':4, 'orientation':'first' if fixture.address == 10 else 'second'}
        with patch('tools.preview_cup_odds.fixture_knockout_context', side_effect=context) as lookup:
            final = native_terminal_final_fixture(reader, [first, semifinal, second], [{'stage_index':2, 'stage_type':'cup','round_tie_counts':[8,4,2,1,1]}])
        self.assertEqual((final['date'], final['home_id'], final['away_id'], final['leg_count']), ('2027-05-27',2,1,2))
        self.assertEqual(lookup.call_count, 2)

    def test_preseason_legacy_key_migrates_once_and_settlement_is_idempotent(self):
        records=[{'bet_id':'preseason','type':'championship','status':'pending','competition_id':11,'season_key':'11:wrong','placed_at':'2026-08-01','team_id':1,'stake':100,'odds':4.0}]
        wallet={'balance':0.0,'transactions':[]}
        markets={'competitions':[{'status':'complete','competition_id':11,'season_key':'11:2026/27','season_label':'2026/27','season_start':'2026-08-08','season_end':'2027-06-30','settlement_date':'2027-05-30','winner_team_id':1,'winner_team_name':'Team 1'}]}
        with patch('tools.betting_account.load_bets',return_value=records),patch('tools.betting_account.load_wallet',return_value=wallet),patch('tools.betting_account._commit_account') as commit:
            self.assertEqual(settle_championship_bets(markets)['settled'],1)
            self.assertEqual(settle_championship_bets(markets)['settled'],0)
        self.assertEqual(wallet['balance'],400.0)
        commit.assert_called_once()

    def test_pending_cup_final_evidence_survives_cache_clear(self):
        root=self.root/'storage'
        cup=root/'cache'/'account-test'/'competitions'/'cups.json'
        league=cup.with_name('leagues.json')
        cup.parent.mkdir(parents=True)
        cup.write_text('{}');league.write_text('{}')
        with patch('tools.betting_account.load_bets',return_value=[{'type':'championship','status':'pending'}]),patch('tools.championship_odds.cup_state_path',return_value=cup),patch('tools.championship_odds.league_state_path',return_value=league):
            protected=pending_snapshot_paths()
            self.assertTrue(has_pending_championship_bets())
        with patch('tools.storage_management._validated_data_root',return_value=root),patch('tools.storage_management.configured_data_root',return_value=root),patch('tools.storage_management.ensure_data_directories'):
            clear_cache_files(protected_paths=protected)
        self.assertTrue(cup.exists() and league.exists())

    def test_intraday_final_clock_changes_market_dependency(self):
        before={'game_date':'2027-05-30','game_time':'14:00'}
        after={**before,'game_time':'17:00'}
        self.assertNotEqual(championship_market_dependency_signature(before,{}), championship_market_dependency_signature(after,{}))

    def test_history_refund_date_uses_historical_settlement_market(self):
        markets={'competitions':[],'settlement_competitions':[{'season_key':'cup:2026/27','status':'awaiting_result','settlement_date':'2027-05-30'}]}
        self.assertEqual(championship_refund_settlement_dates(markets),{'cup:2026/27':'2027-05-30'})

    def test_frontend_current_team_lookup_survives_legacy_historical_row(self):
        node=shutil.which('node')
        if not node:
            self.skipTest('Node unavailable')
        source=(Path(__file__).resolve().parents[1]/'web'/'app.js').read_text(encoding='utf8')
        start=source.index('function championshipDataIndex() {')
        end=source.index('\nfunction championshipSettlementDate(',start)
        script='''const data={competitions:[{competition_id:1,season_key:'1:2027/28',status:'open',teams:[{team_id:7}]},{competition_id:1,season_key:'1:2026/27',status:'complete',teams:[]}]}; const app={}; function championshipData(){return data;} function output(){return {competitions:[]};} function activeUiLocale(){return 'en-GB';} '''+source[start:end]+'''\nconst index=championshipDataIndex(); if(!index.teamsByCompetitionId.get(1).has(7))throw new Error('Current selection lost'); console.log(index.competitions.length);'''
        result=subprocess.run([node,'-e',script],capture_output=True,text=True,check=True)
        self.assertEqual(result.stdout.strip(),'1')

    def test_same_year_new_tournament_preserves_pending_old_final(self):
        old = _cup_output('2027-05-02')
        old['competition_formats'][0].update(competition_season_address='0x1000', terminal_final_fixture={'date':'2027-05-01','home_id':1,'away_id':2})
        championship.build_championship_markets(old, {'competitions':[]})
        new = _cup_output('2027-05-11')
        new['competition_formats'][0].update(competition_season_address='0x2000',first_fixture_date='2027-05-10')
        for match in new['matches']:
            match['fixture_date']='2027-05-10'
        markets = championship.build_championship_markets(new, {'competitions':[]})
        self.assertEqual([m['season_key'] for m in markets['competitions']], ['1301394:2026/27@2027-05-10'])
        previous=markets['settlement_competitions'][0]
        self.assertEqual((previous['season_key'],previous['settlement_date']), ('1301394:2026/27','2027-05-01'))
        stored=json.loads((self.root/'cups.json').read_text())
        self.assertEqual(len(stored['seasons']),2)

    def test_partial_league_window_cannot_become_terminal_by_elapsed_date(self):
        output=self.league_output('2027-01-01')
        output['competition_formats'][0].update(season_fixture_bounds_verified=False,last_fixture_date='2026-12-30',format_last_observed_at='2027-01-01')
        market=championship.build_championship_markets(output,_standings(20,20))['competitions'][0]
        self.assertNotEqual(market['status'],'complete')

    def test_historical_awaiting_result_rebuilds_cached_publication(self):
        output={'save_instance_id':'save-1','game_date':'2027-07-01','performance':{}}
        awaiting={'model_version':championship.CHAMPIONSHIP_MODEL_VERSION,'competitions':[],'settlement_competitions':[{'status':'awaiting_result'}]}
        complete={**awaiting,'settlement_competitions':[{'status':'complete','winner_team_id':1}]}
        with patch('fm_odds_web.public_league_standings',return_value={'competitions':[]}),patch('fm_odds_web.build_championship_markets',side_effect=[awaiting,complete]) as build:
            publish_championship_markets(output)
            refreshed={k:v for k,v in output.items() if k!='championship_markets'}
            markets,hit=publish_championship_markets(refreshed,output)
        self.assertFalse(hit)
        self.assertEqual(build.call_count,2)
        self.assertEqual(markets['settlement_competitions'][0]['winner_team_id'],1)

    def test_light_worker_settles_pending_championship_when_display_disabled(self):
        event=threading.Event()
        output={'save_instance_id':'test-only','game_date':'2027-05-30','game_time':'14:00','matches':[]}
        state=SimpleNamespace(memory_lock=threading.RLock(),lock=threading.RLock(),output=output,output_path=None,club_contexts=[],data_version=1,
            refresh_cancel_event=event,_data_scope_id=Mock(return_value='isolated'),_due_existing_result_keys=Mock(return_value=set()),
            _finalize_settlement=Mock(),_merge_light_results=Mock(return_value=(dict(output),0)),_total_weekly_salary=Mock(return_value=0))
        replacements={
            'load_settings':{'championship_enabled':False},'read_game_clock':{'date':'2027-05-30','minutes':1020,'time':'17:00'},
            'has_pending_championship_bets':True,'set_active_save_id':None,'pending_due_result_keys':set(),
            'settled_missing_half_score_keys':set(),'pending_cup_final_result_keys':set(),'pending_result_fixture_hints':{},
            'probe_live_completed_results':{'results':[]},'sleep':None,'settle_pending_bets':{'settled':0,'returned':0.0},
            'publish_championship_markets':({'competitions':[{'status':'complete','winner_team_id':1}]},False),
            'settle_championship_bets':{'settled':1,'returned':400.0},'archive_salary_payment_mail':None,
            'cache_data_root':self.root,'save_odds':None,
        }
        with ExitStack() as stack:
            mocks={name:stack.enter_context(patch('fm_odds_web.'+name,return_value=value)) for name,value in replacements.items()}
            def publish(updated,previous):
                generated=replacements['publish_championship_markets']
                updated['championship_markets']=generated[0]
                return generated
            mocks['publish_championship_markets'].side_effect=publish
            LocalOddsState._result_refresh_worker(state,event)
        self.assertIsNone(state.error)
        mocks['settle_championship_bets'].assert_called_once()
        self.assertEqual(state.last_background_settled,1)
        self.assertEqual(state.data_version,2)
        mocks['save_odds'].assert_called_once()
        self.assertEqual(mocks['publish_championship_markets'].call_args.args[0]['game_time'],'17:00')

    def test_legacy_preseason_migration_does_not_bind_to_later_same_year_tournament(self):
        records=[{'bet_id':'old','type':'championship','status':'pending','competition_id':1,'season_key':'1:legacy','placed_at':'2027-03-01','team_id':1,'stake':100,'odds':4.0}]
        market={'competition_id':1,'season_key':'1:2027@2027-05-10','season_label':'2027','season_start':'2027-05-10','season_window_start':'2027-01-01','season_end':'2027-12-31','settlement_date':'2027-06-01','status':'complete','winner_team_id':1}
        with patch('tools.betting_account.load_bets',return_value=records),patch('tools.betting_account.load_wallet',return_value={'balance':0,'transactions':[]}),patch('tools.betting_account._commit_account') as commit:
            result=settle_championship_bets({'competitions':[market]})
        self.assertEqual(result['settled'],0)
        commit.assert_not_called()

    def test_reallocated_cup_pointer_with_recovered_earlier_fixtures_keeps_season_key(self):
        old=_cup_output('2026-10-02')
        old['competition_formats'][0].update(competition_season_address='0x1000',first_fixture_date='2026-10-01')
        championship.build_championship_markets(old,{'competitions':[]})
        current=_cup_output('2026-10-03')
        current['competition_formats'][0]['competition_season_address']='0x2000'
        markets=championship.build_championship_markets(current,{'competitions':[]})
        self.assertEqual(markets['competitions'][0]['season_key'],'1301394:2026/27')
        self.assertEqual(len(json.loads((self.root/'cups.json').read_text())['seasons']),1)
