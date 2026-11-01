// Exercise the real page handlers with deferred history/SSE responses, no live app.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('static/app_new.html', 'utf8');
for (const match of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)) new vm.Script(match[1]);
const declaration = html.split('\n').find(line => line.startsWith('const state='));
const start = html.includes('function viewingTurn()') ? html.indexOf('function viewingTurn()') : html.indexOf('async function selectConversation(');
const source = html.slice(start, html.indexOf('function showUploadNotice(', start));
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; };
const response = data => ({ ok: true, status: 200, async json() { return data; } });

function setup() {
  const elements = new Map(), messages = [], notices = [], statuses = [], calls = [];
  function element(id) {
    if (!elements.has(id)) {
      const classes = new Set();
      const e = { textContent: '', value: '', disabled: false, children: [], focus() {}, setAttribute() {},
        classList: { toggle(n, on) { on ? classes.add(n) : classes.delete(n); }, contains(n) { return classes.has(n); } } };
      Object.defineProperty(e, 'innerHTML', { get() { return e._html || ''; }, set(v) { e._html = v; if (id === '#chatInner') messages.length = 0; } });
      elements.set(id, e);
    }
    return elements.get(id);
  }
  let handler = async () => response({ ok: true, messages: [] });
  const sandbox = {
    $: element, chat: element('#chatInner'), input: element('#messageInput'), sendBtn: element('#sendBtn'),
    location: {}, innerWidth: 700, localStorage: { getItem() { return null; } },
    TextDecoder, AbortController, setTimeout, clearTimeout,
    esc: String, roles: {}, toolLabel: String, formatTokens: String,
    renderConversations() {}, closeMobileSidebar() {}, resizeInput() {}, renderChips() {},
    scrollBottom() {}, welcome() { messages.length = 0; },
    addMessage(role, content) { messages.push({ role, content }); },
    setStatus(text, kind) { statuses.push({ text, kind }); }, clearStatus() {},
    toast(text) { notices.push(text); },
    fetch(url, options) { calls.push({ url, options }); return handler(url, options); },
  };
  vm.createContext(sandbox); vm.runInContext(declaration + '\n' + source, sandbox);
  const state = vm.runInContext('state', sandbox);
  Object.assign(state, { currentId: 1, turnConversationId: 1, activeTurnId: 'turn-a', sending: true,
    turnGeneration: 1, conversations: [{ id: 1, title: 'A' }, { id: 2, title: 'B' }] });
  return { s: sandbox, state, messages, notices, statuses, calls, element, fetchWith(f) { handler = f; } };
}

(async () => {
  {
    const t = setup();
    t.fetchWith(async url => response({ ok: true, messages: [{ role: 'assistant', content: url.includes('/2/') ? 'history B' : 'saved A' }] }));
    await t.s.selectConversation(2);
    assert.equal(t.state.currentId, 2); assert.equal(t.state.activeTurnId, 'turn-a'); assert(t.state.sending);
    assert.equal(t.messages[0].content, 'history B');
    assert.equal(t.element('#turnNotice').classList.contains('hidden'), false);
    assert(t.s.sendBtn.disabled); assert.equal(t.s.sendBtn.classList.contains('stop'), false);
    const before = t.statuses.length;
    t.s.handleEvent({ type: 'tool', turn_id: 'turn-a', event_id: 1, name: 'list_records' });
    assert.equal(t.statuses.length, before);
    await t.s.selectConversation(1);
    assert(t.statuses.at(-1).text.includes('list_records'));
    assert(t.s.sendBtn.classList.contains('stop'));
    await t.s.selectConversation(2);
    t.s.handleEvent({ type: 'done', turn_id: 'turn-a', event_id: 2, conversation_id: 1, reply: 'result A' });
    assert.equal(t.state.currentId, 2); assert.equal(t.messages[0].content, 'history B');
    assert.equal(t.messages.length, 1); assert.equal(t.state.sending, false);
    assert(t.notices.some(x => x.includes('完成')));
    await t.s.selectConversation(1); assert.equal(t.messages[0].content, 'saved A');
    assert(!t.calls.some(x => /cancel|\/chat$/.test(x.url)));
  }
  {
    const t = setup(), a = deferred(), b = deferred();
    t.fetchWith(url => url.includes('/1/') ? a.promise : b.promise);
    const first = t.s.selectConversation(1), second = t.s.selectConversation(2);
    b.resolve(response({ ok: true, messages: [{ role: 'assistant', content: 'B newest' }] })); await second;
    a.resolve(response({ ok: true, messages: [{ role: 'assistant', content: 'A stale' }] })); await first;
    assert.equal(t.state.currentId, 2); assert.equal(t.messages[0].content, 'B newest');
  }
  {
    const t = setup(), old = deferred(); let reads = 0;
    t.fetchWith(() => ++reads === 1 ? old.promise : Promise.resolve(response({ ok: true, messages: [{ role: 'assistant', content: 'saved result A' }] })));
    const loading = t.s.selectConversation(1);
    t.s.handleEvent({ type: 'done', turn_id: 'turn-a', event_id: 3, conversation_id: 1, reply: 'result A' });
    old.resolve(response({ ok: true, messages: [] })); await loading;
    assert.equal(reads, 2); assert.equal(t.messages.length, 1); assert.equal(t.messages[0].content, 'saved result A');
    t.s.handleEvent({ type: 'done', turn_id: 'turn-a', event_id: 3, conversation_id: 1, reply: 'duplicate' });
    assert.equal(t.messages.length, 1); assert.equal(t.state.activeTurnId, '');
  }
  {
    const t = setup(), post = deferred();
    Object.assign(t.state, { sending: false, currentId: 0, activeTurnId: '', turnConversationId: 0 });
    t.s.input.value = 'new task';
    t.fetchWith(url => url === '/api/app-new/chat' ? post.promise : Promise.resolve(response({ ok: true, messages: [{ role: 'assistant', content: 'history B' }] })));
    const sending = t.s.send(); await t.s.selectConversation(2);
    const bytes = new TextEncoder().encode('data: '+JSON.stringify({ type: 'accepted', turn_id: 'new-turn', event_id: 10, conversation_id: 3 })+'\n\ndata: '+JSON.stringify({ type: 'done', turn_id: 'new-turn', event_id: 11, conversation_id: 3, reply: 'new result' })+'\n\n');
    let sent = false;
    post.resolve({ ok: true, status: 200, headers: { get() { return 'new-turn'; } }, body: { getReader() { return { async read() { if (sent) return { done: true }; sent = true; return { value: bytes, done: false }; } }; } } });
    await sending;
    assert.equal(t.state.currentId, 2); assert.equal(t.state.turnConversationId, 3);
    assert.equal(t.messages.length, 1); assert.equal(t.messages[0].content, 'history B');
    assert.equal(JSON.parse(t.calls[0].options.body).conversation_id, 0);
  }
  {
    const t = setup(); t.state.currentId = 2;
    t.fetchWith(async () => response({ ok: true, turn: { id: 'turn-a', conversation_id: 1, status: 'completed' } }));
    await t.s.resumeTurn('turn-a');
    assert.equal(t.state.currentId, 2); assert.equal(t.state.activeTurnId, '');
    assert.equal(t.calls.length, 1); // No navigation or history request for A.
  }
  {
    const t = setup(); t.state.currentId = 2;
    t.fetchWith(async () => { throw new Error('offline'); });
    await t.s.resumeTurn('turn-a'); assert.equal(t.state.activeTurnId, 'turn-a');
    assert.equal(t.statuses.length, 0); assert(t.state.sending);
    t.fetchWith(async () => response({ ok: true })); await t.s.cancelActiveTurn();
    assert(t.calls.at(-1).url.endsWith('/turn-a/cancel')); assert.equal(t.state.currentId, 2);
  }
  {
    const t = setup(), pending = deferred(); t.fetchWith(() => pending.promise);
    const resuming = t.s.resumeTurn('turn-a');
    t.state.turnGeneration = 2; t.state.activeTurnId = 'turn-b'; t.state.turnConversationId = 2;
    pending.resolve(response({ ok: true, turn: { conversation_id: 1, status: 'completed' } })); await resuming;
    t.s.handleEvent({ type: 'done', turn_id: 'turn-a', event_id: 99, conversation_id: 1, reply: 'stale' }, 1);
    assert.equal(t.state.activeTurnId, 'turn-b'); assert.equal(t.state.turnConversationId, 2); assert(t.state.sending);
  }
  {
    const t = setup();
    t.fetchWith(async url => {
      if (url.endsWith('/bootstrap')) return response({ ok: true, user: { display_name: 'test', role: 'staff' },
        wps: { connected: true, files: [] }, conversations: [{ id: 1 }, { id: 2 }], current_conversation_id: 2,
        active_turn: { id: 'turn-a', conversation_id: 1 } });
      if (url.endsWith('/2/messages')) return response({ ok: true, messages: [{ role: 'assistant', content: 'B after reload' }] });
      return response({ ok: true, turn: { id: 'turn-a', conversation_id: 1, status: 'completed' } });
    });
    await t.s.bootstrap();
    assert.equal(t.state.currentId, 2); assert.equal(t.messages[0].content, 'B after reload');
    assert.equal(t.state.activeTurnId, ''); assert.equal(t.state.sending, false);
    assert(!t.calls.some(x => x.url.endsWith('/1/messages')));
  }
  console.log('Conversation browsing: 8 asynchronous navigation, completion, recovery and event isolation cases passed');
})().catch(e => { console.error(e); process.exitCode = 1; });
