from contextlib import contextmanager, nullcontext
from copy import deepcopy
from pathlib import Path
import json
import random
import re
import struct
import subprocess
from types import SimpleNamespace
import threading

import pytest
from tools import player_departure as offers
from tools import player_departure_application as app
from tools import player_movement as movement
from tools.domain_errors import ValidationError
from tools.player_departure_messages import MESSAGES

ROOT = Path(__file__).resolve().parents[1]
PLAYER = {'id': 10, 'name': 'Player', 'age': 25, 'ca': 130, 'world_reputation': 6000, 'market_value': 1000000}
CLUBS = [{'id': uid, 'name': f'Club {uid}', 'team_type':'club', 'reputation': rep, 'competition_name': 'League'}
         for uid, rep in [(1, 6000), (2, 6500), (3, 6200), (4, 3000), (5, 8500), (6, 4500)]]

@pytest.fixture
def documents(monkeypatch):
    store = {}
    def load(key, default, scope):
        return deepcopy(store.get((scope, key), default))
    def update(key, default, mutate, scope):
        value = load(key, default, scope)
        result = mutate(value)
        store[(scope, key)] = deepcopy(value)
        return deepcopy(result)
    monkeypatch.setattr(offers, 'load_document', load)
    monkeypatch.setattr(offers, 'update_document', update)
    return store


def start(scope='save-A', today='2026-01-01', player=None):
    return offers.start_round(scope, player or PLAYER, 99, today, lambda: (
        offers.estimate_value(PLAYER, []), offers.generate_offers(PLAYER, CLUBS, {99}, {'amount': 1000000}, rng=random.Random(7))))


def test_estimate_uses_valid_near_peers_and_keeps_native_value():
    assert offers.estimate_value(PLAYER, [])['source'] == 'market_value'
    player = dict(PLAYER, market_value=None)
    peers = [dict(player, id=20+i, market_value=value) for i, value in enumerate([900000, 1000000, 1100000, 1200000, 1300000])]
    peers.extend([dict(player, id=50, asking_price=300000000), dict(player, id=51, ca=10, world_reputation=500, market_value=1000)])
    result = offers.estimate_value(player, peers)
    assert result == {'amount': 1100000, 'source': 'odd_estimate', 'sample_count': 5}
    with pytest.raises(ValidationError):
        offers.estimate_value(player, [dict(player, id=55, asking_price=500000)])
    assert offers.estimate_value(player, [dict(player, id=55, asking_price=500000)], listing=True)['amount'] == 500000


@pytest.mark.parametrize('age,pa,price,other_age,other_pa,other_price', [
    (25, 173, 60_000_000, 35, 150, 2_000_000),
    (34, 150, 8_000_000, 23, 180, 100_000_000),
])
def test_valuation_uses_comparable_save_prices_for_young_and_old_players(age, pa, price, other_age, other_pa, other_price):
    player = dict(PLAYER, market_value=None, age=age, pa=pa, primary_positions=['ST'])
    comparable = [dict(player, id=20+i, asking_price=price+i*100_000) for i in range(5)]
    others = [dict(player, id=100+i, age=other_age, pa=other_pa, asking_price=other_price) for i in range(30)]
    assert offers.estimate_value(player, others+comparable, listing=True) == {
        'amount':price+200_000, 'source':'odd_estimate', 'sample_count':5}


def test_valuation_separates_keeper_and_position_even_when_ca_rep_are_identical():
    player = dict(PLAYER, market_value=None, pa=160, primary_positions=['ST'])
    peers = [dict(player, id=20+i, asking_price=10_000_000+i*100_000) for i in range(5)]
    peers += [dict(player, id=100+i, primary_positions=['DC'], asking_price=90_000_000) for i in range(30)]
    keepers = [dict(player, id=200+i, primary_positions=['GK'], asking_price=1_000_000) for i in range(30)]
    assert offers.estimate_value(player, peers+keepers, listing=True)['amount'] == 10_200_000
    # Positions with low familiarity must not classify an outfield player as GK.
    one = dict(player, id=999, asking_price=12_000_000, primary_positions=[], position_ratings={'ST':20,'GK':2})
    assert offers.estimate_value(player, keepers+[one], listing=True)['amount'] == 12_000_000


def test_valuation_potential_filters_samples_and_tracks_save_price_inflation():
    player = dict(PLAYER, market_value=None, pa=190, primary_positions=['ST'])
    peers = [dict(player, id=20+i, pa=185, asking_price=20_000_000+i*1_000_000) for i in range(5)]
    peers += [dict(player, id=100+i, pa=130, asking_price=1_000_000) for i in range(30)]
    result = offers.estimate_value(player, peers, listing=True)
    assert result['amount'] == 22_000_000 and result['sample_count'] == 5
    inflated = [dict(peer, asking_price=peer['asking_price']*3) for peer in peers]
    assert offers.estimate_value(player, inflated, listing=True)['amount'] == result['amount']*3


def test_valuation_sparse_pool_widens_without_inventing_a_price():
    player = dict(PLAYER, market_value=None, pa=170, primary_positions=['ST'])
    peer = dict(player, id=20, age=34, pa=140, primary_positions=['DC'], asking_price=3_200_000)
    result = offers.estimate_value(player, [peer], listing=True)
    assert result == {'amount':3_200_000, 'source':'odd_estimate', 'sample_count':1}
    peer.pop('age'); peer.pop('pa'); peer.pop('primary_positions')
    assert offers.estimate_value(player, [peer], listing=True)['amount'] == 3_200_000


@pytest.mark.parametrize('seed', range(20))
def test_offers_are_real_distinct_external_clubs_with_price_floor(seed):
    result = offers.generate_offers(PLAYER, CLUBS + [CLUBS[1]], {1, 99}, {'amount': 1000000}, rng=random.Random(seed))
    assert 1 <= len(result) <= 5
    assert len({row['team_id'] for row in result}) == len(result)
    assert all(row['team_id'] in {2,3,4,5,6} and row['amount'] >= 1000000 for row in result)
    assert len([row for row in result if row['tier'] == 'similar']) >= 1


def test_small_pool_and_old_player_have_nonempty_backstop():
    player = dict(PLAYER, age=35)
    result = offers.generate_offers(player, CLUBS[:1], set(), {'amount': 1000000}, rng=random.Random(1))
    assert len(result) == 1
    for seed in range(30):
        result = offers.generate_offers(player, CLUBS, set(), {'amount': 1000000}, rng=random.Random(seed))
        assert result and all(row['tier'] != 'high' for row in result)
    assert offers.generate_offers(player, CLUBS[4:5], set(), {'amount': 1000000}, rng=random.Random(1))


def test_round_reuses_quotes_rejection_does_not_replenish_and_expires(documents):
    row = start()
    repeat = offers.start_round('save-A', PLAYER, 99, '2026-01-02', lambda: pytest.fail('must not reroll'))
    assert row == repeat
    rejected = offers.select_offer('save-A', 10, row['id'], row['offers'][0]['id'], '2026-01-02', 'reject')
    assert len(rejected['offers']) == len(row['offers'])
    assert rejected['offers'][0]['status'] == 'rejected'
    assert start(today='2026-01-08')['id'] != row['id']
    assert offers.current_round('save-B', 10) is None
    with pytest.raises(ValidationError):
        offers.select_offer('save-A', 10, row['id'], row['offers'][1]['id'], '2026-01-09', 'accept')


def test_execution_is_reserved_and_completion_idempotent(documents):
    row = start()
    chosen = row['offers'][0]['id']
    offers.select_offer('save-A', 10, row['id'], chosen, '2026-01-02', 'accept')
    with pytest.raises(ValidationError):
        start(today='2026-01-02')
    receipt = {'player_id': 10, 'source_team_id':99, 'target_team_id':row['offers'][0]['team_id'], 'transfer_fee':1000000, 'seller_balance_after':2000000}
    sold = offers.complete_round('save-A', 10, row['id'], receipt)
    assert sum(item['status'] == 'accepted' for item in sold['offers']) == 1
    assert all(item['status'] in ('accepted','closed') for item in sold['offers'])
    assert offers.select_offer('save-A', 10, row['id'], chosen, '2026-01-02', 'accept') == sold


@pytest.fixture
def application(monkeypatch, documents):
    state = SimpleNamespace(lock=threading.RLock(), data_version=0)
    state._account_operation = lambda _scope: nullcontext()
    state._timed_user_memory_operation = lambda _label: nullcontext()
    state._owned_world_club_target = lambda uid: ('save-A', {}, {'id':uid, 'address':'0x1000'})
    service = app.PlayerDepartureApplication(state)
    service.context = lambda: ('save-A', {'game_date':'2026-01-02'}, [{'id':99}])
    monkeypatch.setattr(app, 'borrow_game_reader', lambda: nullcontext(SimpleNamespace(layout=SimpleNamespace(finance_remaining_transfer_budget_offset=32))))
    monkeypatch.setattr(app, 'player_movement_capabilities', lambda _layout: {'transfer':{'enabled':True}})
    monkeypatch.setattr(app, 'read_native_world_club_player_membership', lambda *_: {'squad_team_id':199, 'squad_team_address':'0x2000'})
    monkeypatch.setattr(app, 'invalidate_club_profile_cache', lambda **_: None)
    return service


def test_sale_commits_once_for_actual_youth_squad(application, monkeypatch):
    row = start()
    payload = {'data_scope_id':'save-A', 'player_id':10, 'round_id':row['id'], 'offer_id':row['offers'][0]['id']}
    calls = []
    def move(**kwargs):
        calls.append(kwargs)
        kwargs['commit_transfer']({'player_id':10, 'source_team_id':99, 'target_team_id':kwargs['target_team_id'], 'transfer_fee':kwargs['transfer_fee'], 'seller_transfer_budget_after':2000000})
        return {'verified':True}
    monkeypatch.setattr(app, 'move_owned_club_player', move)
    assert application.action(payload, 'accept')['round']['status'] == 'sold'
    assert application.action(payload, 'accept')['idempotent']
    assert len(calls) == 1
    assert calls[0]['source_squad_team_id'] == 199
    assert calls[0]['transfer_fee'] == row['offers'][0]['amount']
    receipt = offers.current_round('save-A', 10)['receipt']
    assert receipt['seller_transfer_budget_after'] == 2000000
    assert 'seller_balance_after' not in receipt
    assert application.state.data_version == 1


def test_failed_sale_restores_offer_but_incomplete_rollback_stays_reserved(application, monkeypatch):
    row = start()
    payload = {'data_scope_id':'save-A', 'player_id':10, 'round_id':row['id'], 'offer_id':row['offers'][0]['id']}
    error = RuntimeError('synthetic write failure')
    def move(**kwargs): raise error
    monkeypatch.setattr(app, 'move_owned_club_player', move)
    with pytest.raises(offers.ConflictError): application.action(payload, 'accept')
    assert offers.current_round('save-A', 10)['status'] == 'open'
    error.rollback_incomplete = True
    with pytest.raises(ValidationError): application.action(payload, 'accept')
    assert offers.current_round('save-A', 10)['status'] == 'executing'


def test_sale_without_budget_layout_is_rejected_before_reserving_or_writing(application, monkeypatch):
    row = start()
    reader = SimpleNamespace(layout=SimpleNamespace(finance_remaining_transfer_budget_offset=None))
    monkeypatch.setattr(app,'borrow_game_reader',lambda:nullcontext(reader))
    monkeypatch.setattr(app,'move_owned_club_player',lambda **_:pytest.fail('must not transfer without a verified budget field'))
    with pytest.raises(ValidationError):
        application.action({'data_scope_id':'save-A','player_id':10,'round_id':row['id'],'offer_id':row['offers'][0]['id']},'accept')
    assert offers.current_round('save-A',10)['status'] == 'open'


def test_stale_scope_is_rejected_before_membership_or_write(application, monkeypatch):
    monkeypatch.setattr(app, 'read_native_world_club_player_membership', lambda *_: pytest.fail('must not read game'))
    with pytest.raises(ValidationError): application.search({'data_scope_id':'save-B', 'team_id':99, 'player_id':10})
    with pytest.raises(ValidationError): application.action({'data_scope_id':'save-B', 'player_id':10}, 'accept')


@pytest.mark.parametrize('balance,amount,expected', [(100,1000,1100), (-100,1000,900), (2147483600,1000,None)])
def test_seller_credit_checks_pointer_and_int32_overflow(balance, amount, expected):
    fields = {224:struct.pack('<i',9_000_000),232:struct.pack('<i',balance)}
    reader = SimpleNamespace(layout=SimpleNamespace(club_finance_offset=16, finance_balance_offset=24,finance_remaining_transfer_budget_offset=32),
                             ptr=lambda address: {116:200,208:100}.get(address,0),
                             bytes=lambda address,size: fields.get(address))
    if expected is None:
        with pytest.raises(ValueError): movement._sale_credit(reader,100,amount)
    else:
        address, before, after, total = movement._sale_credit(reader,100,amount)
        assert (address,struct.unpack('<i',after)[0],total) == (232,expected,expected)
        assert struct.unpack('<i',before)[0] == balance
        fields[address] = after
        assert struct.unpack('<i',fields[224])[0] == 9_000_000
    reader.ptr = lambda _address: 0
    with pytest.raises(RuntimeError): movement._sale_credit(reader,100,amount)


def test_sale_requires_verified_transfer_budget_field_without_balance_fallback():
    reader = SimpleNamespace(layout=SimpleNamespace(club_finance_offset=16,finance_balance_offset=24,finance_remaining_transfer_budget_offset=None),
                             ptr=lambda _:pytest.fail('must not use another finance field'))
    with pytest.raises(RuntimeError):
        movement._sale_credit(reader,100,1_000_000)


def test_localizations_have_complete_keys_and_placeholders():
    expected = set(MESSAGES['en-GB'])
    for locale, messages in MESSAGES.items():
        assert set(messages) == expected, locale
        for key, value in messages.items():
            assert sorted(re.findall(r'\{(\w+)\}', value)) == sorted(re.findall(r'\{(\w+)\}', MESSAGES['en-GB'][key])), (locale,key)
    assert MESSAGES['zh-CN']['departure.estimate'] == 'ODD估值'
    script = "const fs=require('fs'),vm=require('vm'),ctx={window:{}};vm.createContext(ctx);vm.runInContext(fs.readFileSync(process.argv[1]+'/web/i18n.departure.js','utf8'),ctx);process.stdout.write(JSON.stringify(Object.fromEntries(Object.entries(ctx.window.FMODDI18nModules[0].messages).map(([locale,items])=>[locale,Object.fromEntries(['departure.search','departure.done','departure.confirm'].map(key=>[key,items[key]]))]))));"
    labels = json.loads(subprocess.check_output(['node','-e',script,str(ROOT)], encoding='utf-8'))
    assert labels == {locale:{key:messages[key] for key in ('departure.search','departure.done','departure.confirm')} for locale,messages in MESSAGES.items()}


def test_card_render_escapes_names_and_preserves_sold_result():
    script = r'''
const fs=require('fs'),vm=require('vm'),ctx={window:{}};vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[1]+'/web/player_departure_page.js','utf8'),ctx);
const escape=s=>String(s).replace(/[<>]/g,c=>({'<':'&lt;','>':'&gt;'}[c]));
const data={clubs:[{id:99,name:'Seller'}],team_id:99,players:[{id:10,name:'<player>',available:false}],rounds:{10:{status:'sold',expires_date:'2026-01-08',valuation:{amount:1000,source:'odd_estimate'},offers:[{id:'one',team_name:'<Club>',competition_name:'League',amount:1000,status:'accepted'}]}}};
const html=ctx.window.FMODDDepartureView.render(data,{t:k=>k,escape,money:x=>'GBP'+x,portrait:()=>'',selectedId:10});
const offers=ctx.window.FMODDDepartureView.render(data,{t:k=>k,escape,money:x=>'GBP'+x,portrait:()=>'',selectedId:10,offersOpen:true});
process.stdout.write(JSON.stringify({escaped:offers.includes('&lt;Club&gt;'),sold:html.includes('departure.done'),accept:html.includes('data-departure-accept'),estimate:offers.includes('departure.estimate'),sidebar:html.includes('departure-player-sidebar'),offerSheet:offers.includes('departure-offer-sheet'),close:offers.includes('data-departure-close-offers'),initialNoOffers:!html.includes('departure-offer-sheet'),modal:offers.includes('<dialog')&&offers.includes('aria-modal="true"'),rosterKept:offers.includes('departure-roster-shell'),rowPrice:html.includes('departure-row-price')&&html.includes('GBP1000')}));
'''
    result = json.loads(subprocess.check_output(['node','-e',script,str(ROOT)], text=True))
    assert result == {'escaped':True,'sold':True,'accept':False,'estimate':True,'sidebar':True,'offerSheet':True,'close':True,'initialNoOffers':True,'modal':True,'rosterKept':True,'rowPrice':True}


def test_departure_roster_has_preview_price_and_single_action_without_portraits():
    script = r'''
const fs=require('fs'),vm=require('vm'),ctx={window:{}};vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[1]+'/web/player_departure_page.js','utf8'),ctx);
const data={clubs:[{id:99,name:'Seller'}],team_id:99,players:[{id:10,name:'Player',age:25,ca:150,pa:175,positions:['ST'],valuation:{amount:2000000,source:'odd_estimate'}}],rounds:{}};
const html=ctx.window.FMODDDepartureView.render(data,{t:k=>k,escape:String,money:x=>'GBP'+x,portrait:()=>{throw Error('portrait must not render')},selectedId:10});
process.stdout.write(JSON.stringify({price:html.includes('GBP2000000'),facts:html.includes('CA 150 / PA 175'),checkbox:html.includes('activity-player-check'),hero:html.includes('departure-player-identity'),actions:(html.match(/data-departure-search/g)||[]).length,view:html.includes('data-departure-open-offers'),modal:html.includes('<dialog'),portrait:html.includes('<img')||html.includes('legacy-hof-portrait')}));
'''
    result = json.loads(subprocess.check_output(['node','-e',script,str(ROOT)], text=True))
    assert result == {'price':True,'facts':True,'checkbox':True,'hero':False,'actions':1,'view':False,'modal':False,'portrait':False}


def test_departure_dialog_dismissal_and_focus_return():
    script = r'''
const fs=require('fs'),vm=require('vm');
const source=fs.readFileSync(process.argv[1]+'/web/app.js','utf8');
const handlers={},dialog={open:false,addEventListener:(key,fn)=>handlers[key]=fn,showModal(){this.open=true},getBoundingClientRect:()=>({left:40,right:400,top:40,bottom:400})};
let refreshed=0,focused=0,prevented=0;
const buttons={'[data-departure-search]':{addEventListener(){},focus(){focused++}}};
const area={querySelector:key=>key==='[data-departure-dialog]'?dialog:buttons[key],querySelectorAll:()=>[]};
const app={departureOffersOpen:true,departureBusy:false};
const ctx={app,$:()=>area,refreshDepartureView:()=>refreshed++,loadPlayerDepartures(){},actPlayerDeparture(){}};vm.createContext(ctx);
vm.runInContext(source.slice(source.indexOf('function bindPlayerDepartureControls()'),source.indexOf('function renderActivity()')),ctx);
ctx.bindPlayerDepartureControls();
if(!dialog.open) throw Error('native modal must open after explicit offer action');
app.departureBusy=true;handlers.cancel({preventDefault(){prevented++}});
if(!app.departureOffersOpen||refreshed) throw Error('pending action must remain protected');
app.departureBusy=false;handlers.cancel({preventDefault(){prevented++}});
if(app.departureOffersOpen||refreshed!==1||focused!==1) throw Error('Escape must close and return focus');
app.departureOffersOpen=true;
handlers.click({target:dialog,clientX:60,clientY:60});
if(!app.departureOffersOpen) throw Error('inside click must not dismiss');
handlers.click({target:dialog,clientX:0,clientY:0});
process.stdout.write(JSON.stringify({closed:!app.departureOffersOpen,refreshed,focused,prevented}));
'''
    result = json.loads(subprocess.check_output(['node','-e',script,str(ROOT)], text=True))
    assert result == {'closed':True,'refreshed':2,'focused':2,'prevented':2}


def test_departure_roster_loading_does_not_open_offers():
    script = r'''
const fs=require('fs'),vm=require('vm');
const source=fs.readFileSync(process.argv[1]+'/web/app.js','utf8');
const app={departureData:null,departureBusy:false,state:{data_scope_id:'save-A'},departureGeneration:0,departureSelectedId:10,departureOffersOpen:false};
const ctx={app,refreshDepartureView(){},request:async()=>({team_id:99,players:[{id:10}],rounds:{10:{status:'open',offers:[{id:'one'}]}}})};vm.createContext(ctx);
vm.runInContext(source.slice(source.indexOf('async function loadPlayerDepartures('),source.indexOf('async function actPlayerDeparture(')),ctx);
(async()=>{await ctx.loadPlayerDepartures();process.stdout.write(JSON.stringify({loaded:Boolean(app.departureData),modalOpen:app.departureOffersOpen,busy:app.departureBusy}));})();
'''
    result = json.loads(subprocess.check_output(['node','-e',script,str(ROOT)], text=True))
    assert result == {'loaded':True,'modalOpen':False,'busy':False}


def test_departure_search_opens_choices_only_after_quotes_arrive():
    script = r'''
const fs=require('fs'),vm=require('vm');
const source=fs.readFileSync(process.argv[1]+'/web/app.js','utf8');
const app={departureData:{data_scope_id:'save-A',team_id:99,players:[{id:10}],rounds:{}},state:{data_scope_id:'save-A'},departureSelectedId:10,departureGeneration:0,departureOffersOpen:false,departureBusy:false};
let finish,requested;
const ctx={app,refreshDepartureView(){},request:(url,options)=>{requested={url,body:JSON.parse(options.body)};return new Promise(resolve=>finish=resolve)}};vm.createContext(ctx);
vm.runInContext(source.slice(source.indexOf('async function actPlayerDeparture('),source.indexOf('function bindPlayerDepartureControls()')),ctx);
(async()=>{
 const pending=ctx.actPlayerDeparture('search');
 if(app.departureOffersOpen||!app.departureBusy) throw Error('do not open empty choices while waiting');
 finish({round:{status:'open',offers:[{id:'one',team_id:100}]}});await pending;
 process.stdout.write(JSON.stringify({opened:app.departureOffersOpen,busy:app.departureBusy,url:requested.url,player:requested.body.player_id,offers:app.departureData.rounds['10'].offers.length}));
})();
'''
    result = json.loads(subprocess.check_output(['node','-e',script,str(ROOT)], text=True))
    assert result == {'opened':True,'busy':False,'url':'/api/activity/player-departure/search','player':10,'offers':1}


def test_departure_accept_uses_single_confirmation_and_closes_offer_sheet_after_sale():
    source = (ROOT / 'web' / 'app.js').read_text(encoding='utf-8')
    messages = (ROOT / 'web' / 'i18n.departure.js').read_text(encoding='utf-8')
    close_before_confirm = source.index('app.departureOffersOpen = false;\n    refreshDepartureView();\n    if (!await fmoddConfirm')
    restore_on_cancel = source.index('app.departureOffersOpen = true;\n      refreshDepartureView();\n      return;', close_before_confirm)
    close_after_sale = source.index('if (response.round?.status === "sold") {', restore_on_cancel)
    close_after_sale = source.index('app.departureOffersOpen = false;', close_after_sale)
    assert close_before_confirm < restore_on_cancel < close_after_sale
    assert '"departure.confirm": "将{player}以{amount}转会到{club}？"' in messages
    assert '请先备份存档' not in messages


def test_card_render_handles_loading_without_departure_data():
    script = r'''
const fs=require('fs'),vm=require('vm'),ctx={window:{}};vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[1]+'/web/player_departure_page.js','utf8'),ctx);
const html=ctx.window.FMODDDepartureView.render(null,{t:k=>k,escape:String,money:x=>String(x),portrait:()=>'',selectedId:0});
process.stdout.write(JSON.stringify({rendered:html.includes('departure-panel'),hasError:html.includes('transfer_enabled')}));
'''
    result = json.loads(subprocess.check_output(['node','-e',script,str(ROOT)], text=True))
    assert result == {'rendered':True,'hasError':False}


def test_roster_covers_all_owned_clubs_and_related_squads_without_quotes(application, monkeypatch):
    application.context = lambda: ('save-A', {'game_date':'2026-01-02'}, [{'id':99,'name':'Seller'},{'id':199,'name':'Second group'}])
    calls = []
    def detail(club, **kwargs):
        calls.append((club['id'], kwargs))
        return {'players':[dict(PLAYER,squad_team_id=99),dict(PLAYER,id=11,squad_team_id=199),dict(PLAYER,id=12,squad_team_id=299,loan={'active':True})]}
    monkeypatch.setattr(app,'read_native_world_club_detail', detail)
    monkeypatch.setattr(app,'apply_player_aliases',lambda *_: None)
    monkeypatch.setattr(application,'_market',lambda *_: pytest.fail('opening room must not generate prices'))
    result = application.players(199)
    assert result['clubs'] == [{'id':99,'name':'Seller'},{'id':199,'name':'Second group'}]
    assert {row['squad_team_id'] for row in result['players']} == {99,199,299}
    assert [row['available'] for row in result['players']] == [True,True,False]
    assert calls == [(199,{'include_related_squads':True})]
    assert all(value is None for value in result['rounds'].values())


def test_roster_preview_uses_save_index_once_without_generating_or_persisting_quotes(application, monkeypatch):
    targets = [dict(PLAYER,market_value=None,pa=170,positions=['ST']),
               dict(PLAYER,id=11,market_value=None,pa=170,positions=['ST'])]
    peers = [dict(PLAYER,id=100+i,market_value=None,pa=170,date_of_birth='2001-01-01',asking_price=2_000_000+i*100_000) for i in range(5)]
    reads = []
    index = SimpleNamespace(all_player_rows=lambda:reads.append('index') or peers)
    monkeypatch.setattr(app,'database_index_for_reader',lambda _:index)
    monkeypatch.setattr(app,'_world_player_table_rows',lambda *_:pytest.fail('preview must not expand every player live'))
    monkeypatch.setattr(offers,'generate_offers',lambda *_args,**_kwargs:pytest.fail('preview must not generate quotes'))
    monkeypatch.setattr(offers,'update_document',lambda *_args,**_kwargs:pytest.fail('preview must not persist a round'))
    application._preview_valuations(targets,'2026-01-02')
    assert reads == ['index']
    assert all(row['valuation'] == {'amount':2_200_000,'source':'odd_estimate','sample_count':5} for row in targets)
    assert application.state.data_version == 0


def test_game_clock_used_for_ownership_timeline(application, monkeypatch):
    # Account output can precede the actual native date between full refreshes.
    del application.context
    state = application.state
    state._bind_current_save = lambda: 'save-A'
    state.output = {'game_date':'2026-01-01'}
    monkeypatch.setattr(app,'assert_activity_centre_unlocked',lambda *_:None)
    monkeypatch.setattr(app,'read_game_clock_from_reader',lambda *_:{'date':'2026-01-05'})
    monkeypatch.setattr(app,'load_acquired_clubs',lambda *_:{'clubs':[{'id':99,'acquired_game_date':'2026-01-03'},{'id':199,'acquired_game_date':'2026-01-07'}]})
    scope,output,owned = application.context()
    assert scope == 'save-A' and output['game_date'] == '2026-01-05'
    assert [row['id'] for row in owned] == [99]
    assert state.output['game_date'] == '2026-01-01'


def test_search_reads_valid_listing_peers_only_on_request(application, monkeypatch):
    state = application.state
    state._world_club_native_cache = lambda *_: {}
    state._world_club_directory = lambda *_: {'clubs':CLUBS + [{'id':99,'name':'Owned','reputation':6000}]}
    reader = SimpleNamespace(layout=SimpleNamespace(player_world_reputation_offset=16), u16=lambda _:6000)
    monkeypatch.setattr(app,'borrow_game_reader',lambda: nullcontext(reader))
    monkeypatch.setattr(app,'read_world_player_profile',lambda _:dict(PLAYER,market_value=None))
    monkeypatch.setattr(app,'apply_player_aliases',lambda *_:None)
    peers=[dict(PLAYER,id=30,market_value=None,asking_price=2000000,address='0x100'),dict(PLAYER,id=31,market_value=None,asking_price=300000000,address='0x200')]
    monkeypatch.setattr(app,'database_index_for_reader',lambda _:SimpleNamespace(all_player_rows=lambda:peers))
    reads=[]
    monkeypatch.setattr(app,'_world_player_table_rows',lambda _,rows: reads.extend(rows) or rows)
    result=application.search({'data_scope_id':'save-A','team_id':99,'player_id':10})
    assert len(reads)==1 and reads[0]['id']==30
    assert result['round']['valuation']['source']=='odd_estimate'
    assert result['round']['valuation']['amount']==2000000
    assert all(row['amount']>=2000000 and row['team_id']!=99 for row in result['round']['offers'])


def test_market_preselection_uses_birth_date_and_potential_before_bounded_reads(application, monkeypatch):
    state = application.state
    state._world_club_native_cache = lambda *_: {}
    state._world_club_directory = lambda *_: {'clubs':CLUBS}
    reader = SimpleNamespace(layout=SimpleNamespace(player_world_reputation_offset=16), u16=lambda _:6000)
    monkeypatch.setattr(app, 'borrow_game_reader', lambda: nullcontext(reader))
    player = dict(PLAYER, market_value=None, pa=175, primary_positions=['ST'])
    rows = [dict(player, id=100+i, age=None, date_of_birth='1988-01-01', pa=130,
                 asking_price=2_000_000, address=hex(1000+i)) for i in range(140)]
    rows += [dict(player, id=300+i, age=None, date_of_birth='2001-01-01', ca=132,
                  asking_price=60_000_000+i*100_000, address=hex(2000+i)) for i in range(5)]
    monkeypatch.setattr(app, 'database_index_for_reader', lambda _:SimpleNamespace(all_player_rows=lambda:rows))
    reads = []
    monkeypatch.setattr(app, '_world_player_table_rows', lambda _, rows: reads.extend(rows) or rows)
    valuation, bids = application._market('save-A', {'game_date':'2026-01-02'}, [{'id':99}], player)
    assert len(reads) <= offers.VALUATION_PEER_LIMIT
    assert {row['id'] for row in reads[:5]} == set(range(300,305))
    assert valuation['amount'] == 60_200_000 and valuation['sample_count'] == 5
    assert bids and all(bid['amount'] >= valuation['amount'] for bid in bids)


@pytest.mark.parametrize('operation', ['players', 'search', 'accept', 'reject'])
def test_departure_waits_for_memory_without_holding_account_lock(operation):
    """A background reader must still publish state while departure waits for it."""
    state = SimpleNamespace(lock=threading.RLock())
    memory_lock = threading.RLock()
    waiting = threading.Event()
    errors = []

    @contextmanager
    def account_operation(_scope):
        with state.lock:
            yield

    @contextmanager
    def memory_operation(_label):
        waiting.set()
        acquired = memory_lock.acquire(timeout=2)
        if not acquired:
            raise TimeoutError('departure remained blocked on the background reader')
        try:
            yield
        finally:
            memory_lock.release()

    state._account_operation = account_operation
    state._timed_user_memory_operation = memory_operation
    service = app.PlayerDepartureApplication(state)
    # No native reader or real account storage enters this concurrency check.
    service.context = lambda: ('current-scope', {}, [])

    def foreground():
        try:
            if operation == 'players':
                service.players()
            elif operation == 'search':
                service.search({'data_scope_id':'stale-scope'})
            else:
                service.action({'data_scope_id':'stale-scope'}, operation)
        except ValidationError:
            # A stale account request is rejected after acquiring both guards.
            if operation == 'players':
                errors.append('unexpected roster validation failure')
        except Exception as error:
            errors.append(error)

    # This thread models _club_refresh_worker, which already owns the memory
    # lock when it needs state.lock to publish progress. Events fix the ordering.
    memory_lock.acquire()
    worker = threading.Thread(target=foreground, daemon=True)
    worker.start()
    account_available = False
    try:
        assert waiting.wait(timeout=2), 'foreground did not start its memory wait'
        account_available = state.lock.acquire(blocking=False)
        if account_available:
            state.lock.release()
    finally:
        # Release even on a failing baseline so no test thread stays deadlocked.
        memory_lock.release()
        worker.join(timeout=2)
    assert not worker.is_alive(), 'departure did not finish after the background reader'
    assert not errors
    assert account_available, 'departure held account state while waiting for background memory'



def test_non_club_and_all_star_buyers_are_excluded():
    clubs = [
        {'id': 1, 'name':'中超联赛全明星队', 'team_type':'club', 'reputation':6000, 'competition':'中超联赛'},
        {'id': 2, 'name':'Real Club', 'team_type':'club', 'reputation':6000, 'competition':'西甲'},
        {'id': 3, 'name':'National Team', 'team_type':'national', 'reputation':6000, 'competition':'国际赛'},
    ]
    result = offers.generate_offers(PLAYER, clubs, set(), {'amount':1000000}, rng=random.Random(3))
    assert result and {row['team_id'] for row in result} == {2}
    assert result[0]['competition_name'] == '西甲'
    assert all('全明星' not in row['team_name'] for row in result)


def test_current_competition_projection_repairs_old_uncategorized_cards(application):
    output = {'competition_formats':[{'competition_id':88,'competition_name':'中超联赛','competition_kind':'league','stages':[{'teams':[{'id':2}]}]}]}
    directory = {'clubs':[{'id':2,'name':'Club 2','team_type':'club','competition':'未分类','competition_id':88}]}
    labeled = application._label_directory(directory, output)
    rounds = {'10':{'offers':[{'team_id':2,'competition_name':'未分类'}]}}
    application._repair_round_labels(rounds, labeled, output)
    assert labeled['clubs'][0]['competition'] == '中超联赛'
    assert rounds['10']['offers'][0]['competition_name'] == '中超联赛'


def test_known_competitions_and_reputation_floor_beat_unclassified_tail():
    clubs = [
        {'id': 1, 'name': 'Recognized 1', 'team_type': 'club', 'reputation': 6100, 'competition': '英超联赛'},
        {'id': 2, 'name': 'Recognized 2', 'team_type': 'club', 'reputation': 6800, 'competition': '英超联赛'},
        {'id': 3, 'name': 'Distant amateur', 'team_type': 'club', 'reputation': 1200, 'competition': '未分类'},
        {'id': 4, 'name': 'Distant semi-pro', 'team_type': 'club', 'reputation': 3500, 'competition': '未分类'},
    ]
    result = offers.generate_offers(dict(PLAYER, world_reputation=6500), clubs, set(), {'amount': 1000000}, rng=random.Random(3))
    assert result
    assert {row['team_id'] for row in result} <= {1, 2}
    assert all(row['competition_name'] == '英超联赛' for row in result)


def test_old_round_cards_missing_from_live_directory_are_removed(application):
    directory = {'clubs': [{'id': 2, 'name': 'Live Club', 'team_type': 'club', 'reputation': 6000, 'competition': '英超联赛'}]}
    rounds = {'10': {'offers': [
        {'team_id': 999, 'team_name': '中超联赛全明星队', 'competition_name': '未分类'},
        {'team_id': 2, 'team_name': 'Live Club', 'competition_name': '未分类'},
    ]}}
    application._repair_round_labels(rounds, directory, {})
    assert [row['team_id'] for row in rounds['10']['offers']] == [2]
    assert rounds['10']['offers'][0]['competition_name'] == '英超联赛'


def test_large_low_reputation_directory_does_not_swamp_elite_player():
    player = dict(PLAYER, age=31, world_reputation=7077)
    nearby = [dict(CLUBS[0], id=i, reputation=6500 + i * 10, competition='League') for i in range(1, 31)]
    tail = [dict(CLUBS[0], id=1000+i, reputation=1200, competition_name='未分类') for i in range(10000)]
    for seed in range(20):
        result = offers.generate_offers(player, nearby + tail, set(), {'amount':1000000}, rng=random.Random(seed))
        assert result and all(row['team_id'] < 1000 for row in result)


def test_tiny_directory_expands_to_nearest_real_buyer():
    result = offers.generate_offers(dict(PLAYER, world_reputation=9000), CLUBS[3:4], set(), {'amount':1000000}, rng=random.Random(1))
    assert len(result) == 1 and result[0]['team_id'] == 4


def test_generation_migration_replaces_only_legacy_open_round(documents):
    row = start()
    stored = documents[('save-A', offers.DOCUMENT)]['rounds']['10']
    stored['generation_version'] = 2
    replaced = start(today='2026-01-02')
    assert replaced['id'] != row['id']
    assert replaced['generation_version'] == offers.OFFER_GENERATION_VERSION
    stored = documents[('save-A', offers.DOCUMENT)]['rounds']['10']
    stored.pop('generation_version')
    stored['status'] = 'executing'
    with pytest.raises(ValidationError):
        start(today='2026-01-03')
    stored['status'] = 'sold'
    result = offers.start_round('save-A', PLAYER, 99, '2026-01-03', lambda: pytest.fail('must retain completed sale'))
    assert result['id'] == replaced['id'] and result['status'] == 'sold'


@pytest.mark.parametrize('output,expected', [
    ({'managed_team':{'id':1,'club_id':5001}}, {1,99}),
    ({'managed_teams':[{'id':1,'team_type':'club'},{'id':2,'team_type':'club'},
                       {'id':3,'team_type':'national'}]}, {1,2,99}),
    ({'managed_teams':[],'managed_team':{'id':2}}, {2,99}),
])
def test_buyer_exclusions_use_owned_and_managed_team_uids(output, expected):
    assert app.PlayerDepartureApplication._excluded_buyer_ids(output, [{'id':99}]) == expected


def test_market_never_quotes_owned_or_any_managed_club(application):
    application.state._world_club_native_cache = lambda *_: {}
    application.state._world_club_directory = lambda *_: {'clubs':CLUBS}
    output = {'managed_teams':[{'id':1,'team_type':'club'},{'id':2,'team_type':'club'}]}
    _, result = application._market('save-A', output, [{'id':3},{'id':99}], PLAYER)
    assert result and {row['team_id'] for row in result} <= {4,5,6}


def test_roster_hides_cached_owned_and_managed_quotes_without_reroll(application, monkeypatch):
    row = start()
    buyer_ids = [offer['team_id'] for offer in row['offers']]
    owned = [{'id':99}, {'id':buyer_ids[0]}]
    output = {'game_date':'2026-01-02','managed_team':{'id':buyer_ids[1],'team_type':'club'}}
    application.context = lambda: ('save-A',output,owned)
    monkeypatch.setattr(app,'read_native_world_club_detail',lambda *_,**__: {'players':[dict(PLAYER)]})
    monkeypatch.setattr(app,'apply_player_aliases',lambda *_:None)
    monkeypatch.setattr(application,'_market',lambda *_:pytest.fail('opening roster must not reroll'))
    shown = application.players(99)['rounds']['10']['offers']
    assert {offer['team_id'] for offer in shown} == set(buyer_ids[2:])
    # Filtering the response does not destroy the persisted round or generate quotes.
    assert offers.current_round('save-A',10) == row


@pytest.mark.parametrize('reason', ['owned','managed'])
def test_accept_rejects_newly_owned_or_managed_buyer_before_native_write(application, monkeypatch, reason):
    row = start()
    offer = row['offers'][0]
    owned = [{'id':99}]+([{'id':offer['team_id']}] if reason == 'owned' else [])
    output = {'game_date':'2026-01-02'}
    if reason == 'managed':
        output['managed_team'] = {'id':offer['team_id'],'team_type':'club'}
    application.context = lambda: ('save-A',output,owned)
    monkeypatch.setattr(app,'move_owned_club_player',lambda **_:pytest.fail('forbidden buyer must never transfer'))
    with pytest.raises(ValidationError):
        application.action({'data_scope_id':'save-A','player_id':10,'round_id':row['id'],
                            'offer_id':offer['id']}, 'accept')
    assert offers.current_round('save-A',10) == row


def test_explicit_search_refreshes_forbidden_cached_buyers_then_reuses_clean_round(application, monkeypatch):
    old = start()
    owned_id, managed_id = [offer['team_id'] for offer in old['offers'][:2]]
    owned = [{'id':99},{'id':owned_id}]
    output = {'game_date':'2026-01-02','managed_teams':[{'id':managed_id,'team_type':'club'}]}
    application.context = lambda: ('save-A',output,owned)
    application.state._world_club_native_cache = lambda *_: {}
    application.state._world_club_directory = lambda *_: {'clubs':CLUBS}
    monkeypatch.setattr(app,'read_world_player_profile',lambda _:dict(PLAYER))
    monkeypatch.setattr(app,'apply_player_aliases',lambda *_:None)
    payload = {'data_scope_id':'save-A','player_id':10,'team_id':99}
    result = application.search(payload)['round']
    assert result['id'] != old['id']
    assert result['offers'] and not ({owned_id,managed_id,99} & {offer['team_id'] for offer in result['offers']})
    monkeypatch.setattr(application,'_market',lambda *_:pytest.fail('valid quotes must be reused'))
    assert application.search(payload)['round'] == result


@pytest.mark.parametrize('status',['executing','sold'])
def test_buyer_exclusion_does_not_rebuild_reserved_or_completed_rounds(documents,status):
    row = start()
    chosen = row['offers'][0]
    offers.select_offer('save-A',10,row['id'],chosen['id'],'2026-01-02','accept')
    if status == 'sold':
        offers.complete_round('save-A',10,row['id'], {'player_id':10,'source_team_id':99,
            'target_team_id':chosen['team_id'],'transfer_fee':chosen['amount'], 'seller_transfer_budget_after':2000000})
    def search():
        return offers.start_round('save-A',PLAYER,99,'2026-01-02',lambda:pytest.fail('must not rebuild'),
                                  excluded_buyers={chosen['team_id']})
    if status == 'executing':
        with pytest.raises(ValidationError): search()
    else:
        assert search()['status'] == 'sold'
