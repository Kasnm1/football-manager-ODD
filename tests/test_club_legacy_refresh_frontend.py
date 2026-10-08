from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = (Path(__file__).resolve().parents[1] / "src")


class ClubLegacyRefreshFrontendTests(unittest.TestCase):
    def test_scoped_revision_loading_and_failure_recovery(self):
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node is required for frontend behavior checks")
        code = r"""
const assert = require('node:assert/strict');
const source = require('node:fs').readFileSync(process.argv[1], 'utf8');
const helpers = source.slice(source.indexOf('function applyClubLegacyStatus('), source.indexOf('async function refreshHallOfFame('));
let app = {
  state:{data_scope_id:'a'}, page:'club', clubLegacyStatus:null,
  clubLegacyConsumedRevision:-1, clubLegacyRequestKey:0, clubLegacyRevision:0,
  clubLegacyPool:{}, clubLegacyScope:'a', clubLegacyLoading:false,
};
let requests = [], toasts = [], renders = 0;
let request = async path => {requests.push(path); return path.endsWith('=0') ? {clubs:[{id:10}]} : {club:{id:10}, players:[{id:42}]};};
const loadTransferHistory = async () => null;
const output = () => ({}), toast = text => toasts.push(text), renderHallOfFame = () => {renders++;};
const flush = async () => {for(let i=0;i<12;i++) await Promise.resolve();};
eval(helpers);
(async () => {
  applyClubLegacyStatus({data_scope_id:'a',revision:1,refreshing:false});
  assert.equal(requests.length,0,'hidden page must not fetch archives');
  applyClubLegacyStatus({data_scope_id:'b',revision:9,refreshing:true});
  assert.equal(app.clubLegacyStatus.revision,1,'foreign scope rejected');
  app.page='hall-of-fame';
  applyClubLegacyStatus({data_scope_id:'a',revision:1,refreshing:true});
  assert.equal(requests.length,0,'busy archive must not trigger GETs');
  applyClubLegacyStatus({data_scope_id:'a',revision:2,refreshing:false});
  await flush();
  assert.equal(requests.length,2);
  assert.equal(app.clubLegacyConsumedRevision,2);
  assert.equal(app.clubLegacyPool[10].players[0].id,42);
  applyClubLegacyStatus({data_scope_id:'a',revision:2,refreshing:false});
  await flush();
  assert.equal(requests.length,2,'unchanged completion is consumed once');

  let release;
  request = async path => {requests.push(path); return new Promise(resolve => {release=resolve;});};
  const oldRead = loadClubLegacyIndex({force:true});
  applyClubLegacyStatus({data_scope_id:'a',revision:3,refreshing:false});
  release({clubs:[{id:99}]});
  await oldRead;
  assert.equal(app.clubLegacyConsumedRevision,2,'obsolete response cannot consume revision');
  assert.equal(app.clubLegacyPool[99],undefined);
  assert.equal(app.clubLegacyLoading,false);

  request = async path => {if(path.endsWith('=0')) return {clubs:[{id:10}]}; throw new Error('detail failed');};
  await loadClubLegacyIndex({force:true});
  assert.equal(app.clubLegacyConsumedRevision,2,'partial failure remains retryable');
  assert.equal(app.clubLegacyPool[10].players[0].id,42,'failure preserves old usable data');
  assert.equal(toasts.length,1);
  request = async path => path.endsWith('=0') ? {clubs:[{id:10}]} : {club:{id:10},players:[{id:43}]};
  await loadClubLegacyIndex({force:true});
  assert.equal(app.clubLegacyConsumedRevision,3);
  assert.equal(app.clubLegacyPool[10].players[0].id,43);

  request = async path => new Promise(resolve => {release=resolve;});
  const foreignRead = loadClubLegacyIndex({force:true});
  app.state.data_scope_id='b';
  release({clubs:[{id:99}]});
  await foreignRead;
  assert.equal(app.clubLegacyScope,'a','foreign response never publishes');
  assert.equal(app.clubLegacyPool[99],undefined);
  assert.equal(app.clubLegacyLoading,false);
  assert.ok(renders>0);
  console.log('6 frontend behavior scenarios passed');
})().catch(error => {console.error(error); process.exitCode=1;});
"""
        result = subprocess.run(
            [node, "-e", code, str(ROOT / "web" / "app.js")],
            capture_output=True, text=True, encoding="utf-8", timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_independent_status_is_wired_without_polling_stale_status(self):
        source = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn("if (!status.status_snapshot_delayed) applyClubLegacyStatus(status.club_legacy_status);", source)
        self.assertIn("applyClubLegacyStatus(nextState.club_legacy_status);", source)
        reset = source.split("function resetAccountScopedWorldState() {", 1)[1].split("async function loadStateOnce", 1)[0]
        self.assertIn("app.clubLegacyRequestKey += 1", reset)
        self.assertIn("app.clubLegacyPool = {}", reset)
        render = source.split("function renderHallOfFame() {", 1)[1].split("function bindClubLegacyControls", 1)[0]
        self.assertIn("app.clubLegacyStatus?.refreshing", render)
        self.assertIn("app.clubLegacyStatus?.error", render)


if __name__ == "__main__":
    unittest.main()
