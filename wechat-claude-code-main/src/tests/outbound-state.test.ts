import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

import { createOutboundStateStore } from '../wechat/outbound-state.js';

function withTempStore(run: (rootDir: string) => void): void {
  const rootDir = mkdtempSync(join(tmpdir(), 'opennexus-wechat-state-'));
  try {
    run(rootDir);
  } finally {
    rmSync(rootDir, { recursive: true, force: true });
  }
}

test('persists context tokens per account and contact', () => {
  withTempStore((rootDir) => {
    let currentTime = 1000;
    const first = createOutboundStateStore('bot@example', {
      rootDir,
      now: () => currentTime,
    });
    first.rememberContext('user-a', 'token-a');
    currentTime = 365 * 24 * 60 * 60 * 1000;
    const restored = createOutboundStateStore('bot@example', {
      rootDir,
      now: () => currentTime,
    });
    assert.deepEqual(restored.getContext('user-a'), {
      userId: 'user-a', token: 'token-a', updatedAt: 1000,
    });
    assert.equal(restored.getContext('user-b'), null);
  });
});

test('deduplicates queued interactive messages and removes delivered items', () => {
  withTempStore((rootDir) => {
    const store = createOutboundStateStore('bot@example', { rootDir, now: () => 1000 });
    const first = store.enqueue('user-a', 'hello');
    const duplicate = store.enqueue('user-a', 'hello');
    assert.equal(duplicate.id, first.id);
    assert.equal(store.listPending('user-a').length, 1);
    store.removePending(first.id);
    assert.deepEqual(store.listPending('user-a'), []);
  });
});

test('expires queued messages instead of replaying stale notifications', () => {
  withTempStore((rootDir) => {
    let currentTime = 1000;
    const store = createOutboundStateStore('bot@example', {
      rootDir,
      now: () => currentTime,
      pendingTtlMs: 500,
    });
    store.enqueue('user-a', 'hello');
    currentTime = 1501;
    assert.deepEqual(store.listPending('user-a'), []);
  });
});
