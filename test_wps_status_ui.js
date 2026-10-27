// Execute the actual inline status/SSE handlers with a minimal DOM, no browser account.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('static/app_new.html', 'utf8');
for (const match of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)) new vm.Script(match[1]);
const start = html.indexOf('let wpsStatusRequest=');
const end = html.indexOf('async function bootstrap()', start);
assert(start > 0 && end > start);
const handle = html.split('\n').find(line => line.startsWith('function handleEvent(e)'));
const elements = new Map();
const classes = () => ({ toggle() {} });
const element = id => {
  if (!elements.has(id)) elements.set(id, { classList: classes(), setAttribute() {} });
  return elements.get(id);
};
let fetchHandler, fetchCount = 0;
const sandbox = {
  state: { wps: { files: ['keep-file'], default_file: { file_id: 'keep-default' } }, lastEventId: 0 },
  $: element, location: { href: '' }, AbortController, setTimeout, clearTimeout,
  Date, Math, Number,
  fetch(...args) { fetchCount++; return fetchHandler(...args); },
};
vm.createContext(sandbox);
vm.runInContext(html.slice(start, end) + '\n' + handle, sandbox);
const apply = data => sandbox.applyWpsStatus(data);
const badge = () => element('#wpsBadge');
const okResponse = wps => ({ ok: true, status: 200, async json() { return { ok: true, wps }; } });

(async () => {
  apply({ connected: true, status: 'connected', username: 'Tester' });
  assert.equal(badge().className, 'pill wps ok');
  sandbox.handleEvent({ type: 'wps_status', status: 'reauth_required', connected: false,
                        message: '需要重新授权', turn_id: 'turn-test', event_id: 1 });
  assert.equal(badge().className, 'pill wps bad');
  assert(badge().textContent.includes('需重新授权'));
  apply({ status: 'temporary_error', connected: false, message: '网络暂时异常' });
  assert.equal(badge().className, 'pill wps warn');
  assert(!badge().textContent.includes('需重新授权'));
  assert.equal(sandbox.state.wps.files[0], 'keep-file');
  assert.equal(sandbox.state.wps.default_file.file_id, 'keep-default');
  apply({ connected: true, status: 'connected', checked_at: 200 });
  sandbox.handleEvent({ type: 'wps_status', status: 'reauth_required', connected: false, checked_at: 100 });
  assert.equal(badge().className, 'pill wps ok'); // Old replayed SSE cannot override fresh bootstrap.

  // A slow, older green response cannot overwrite a newer red runtime event.
  let resolve;
  fetchHandler = () => new Promise(done => { resolve = done; });
  const pending = sandbox.refreshWpsStatus();
  const alsoPending = sandbox.refreshWpsStatus();
  assert.equal(fetchCount, 1);
  apply({ connected: false, status: 'reauth_required' });
  resolve(okResponse({ connected: true, status: 'connected' }));
  await Promise.all([pending, alsoPending]);
  assert.equal(badge().className, 'pill wps bad');

  fetchHandler = async () => okResponse({ connected: true, status: 'connected' });
  await sandbox.refreshWpsStatus();
  assert.equal(badge().className, 'pill wps ok');
  fetchHandler = async () => { throw new Error('network unavailable'); };
  await sandbox.refreshWpsStatus();
  assert.equal(badge().className, 'pill wps warn');
  assert(badge().title.includes('不代表授权已失效'));
  fetchHandler = async () => ({ ok: true, status: 200, async json() { return { ok: false }; } });
  await sandbox.refreshWpsStatus();
  assert.equal(badge().className, 'pill wps warn');
  assert.equal(sandbox.location.href, ''); // No unexpected OAuth popup/navigation.
  console.log('WPS status UI: syntax, live events, polling, race protection and recovery passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
