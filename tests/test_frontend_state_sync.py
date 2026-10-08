"""Execute the shared loader and its account-switch caller with deferred I/O."""
from pathlib import Path
import subprocess


def test_account_switch_awaits_fresh_state_after_an_older_inflight_read():
    root = (Path(__file__).resolve().parents[1] / "src")
    script = r'''
const fs = require("fs");
const vm = require("vm");
const assert = require("assert/strict");
const source = fs.readFileSync(process.argv[1] + "/web/app.js", "utf8");
function between(start, end) {
  const offset = source.indexOf(start);
  assert.ok(offset >= 0);
  const finish = source.indexOf(end, offset + start.length);
  assert.ok(finish > offset);
  return source.slice(offset, finish);
}
const loader = between("async function loadState({fresh = false} = {})", "function freeServicesEnabled");
const binding = between('$("#apply-saved-account").addEventListener', '$("#merge-same-manager-accounts")');
const deferred = [];
const toasts = [];
const app = {state:{data_scope_id:"account-a"}, selections:[{id:1}], stateReloadRequested:false};
let click;
const button = {disabled:false, addEventListener:(_name, handler) => {click = handler;}};
const context = {
  app,
  $: (selector) => selector === "#saved-account-select" ? {value:"account-b"} : button,
  request: async (path, options) => {
    assert.equal(path, "/api/save-account/select");
    assert.equal(JSON.parse(options.body).scope_id, "account-b");
    return {ok:true};
  },
  loadStateOnce: () => new Promise(resolve => deferred.push(state => {
    app.state = state;
    resolve(true);
  })),
  setStateSyncFeedback: () => {},
  uiText: key => key,
  toast: message => toasts.push({message, scope:app.state.data_scope_id}),
};
vm.createContext(context);
vm.runInContext(loader + binding, context);
(async () => {
  const poll = context.loadState();
  const coalescedPoll = context.loadState();
  assert.equal(deferred.length, 1, "ordinary polling stays coalesced");
  const selection = click();
  await new Promise(setImmediate);
  assert.equal(button.disabled, true);
  deferred[0]({data_scope_id:"account-a"});
  await new Promise(setImmediate);
  assert.equal(deferred.length, 2, "selection must queue a post-mutation read");
  assert.equal(button.disabled, true, "selection is still awaiting fresh state");
  assert.equal(toasts.length, 0, "do not announce completion from stale state");
  deferred[1]({data_scope_id:"account-b"});
  await Promise.all([poll, coalescedPoll, selection]);
  assert.equal(app.state.data_scope_id, "account-b");
  assert.equal(button.disabled, false);
  assert.equal(app.selections.length, 0);
  assert.equal(app.stateLoadPromise, null);
  assert.equal(toasts.length, 1);
  assert.equal(toasts[0].scope, "account-b");
  assert.equal(toasts[0].message, "settings.account.switched");
})().catch(error => {console.error(error); process.exitCode = 1;});
'''
    result = subprocess.run(
        ["node", "-e", script, str(root)],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
