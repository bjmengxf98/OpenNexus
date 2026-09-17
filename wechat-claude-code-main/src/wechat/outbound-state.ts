import { randomUUID } from 'node:crypto';
import { join } from 'node:path';

import { DATA_DIR } from '../constants.js';
import { loadJson, saveJson } from '../store.js';

const DEFAULT_PENDING_TTL_MS = 30 * 60 * 1000;
const DEFAULT_MAX_PENDING = 50;

interface ContextEntry {
  userId: string;
  token: string;
  updatedAt: number;
}

export interface PendingOutboundMessage {
  id: string;
  toUserId: string;
  text: string;
  createdAt: number;
}

interface OutboundStateFile {
  contexts: ContextEntry[];
  pending: PendingOutboundMessage[];
}

interface OutboundStateOptions {
  rootDir?: string;
  now?: () => number;
  pendingTtlMs?: number;
  maxPending?: number;
}

function validateAccountId(accountId: string): void {
  if (!/^[a-zA-Z0-9_.@=-]+$/.test(accountId)) {
    throw new Error(`Invalid accountId: "${accountId}"`);
  }
}

function normalizeState(value: OutboundStateFile | null): OutboundStateFile {
  const contexts = Array.isArray(value?.contexts)
    ? value.contexts.filter((item) => (
      typeof item?.userId === 'string'
      && typeof item?.token === 'string'
      && item.token.length > 0
      && Number.isFinite(item?.updatedAt)
    ))
    : [];
  const pending = Array.isArray(value?.pending)
    ? value.pending.filter((item) => (
      typeof item?.id === 'string'
      && typeof item?.toUserId === 'string'
      && typeof item?.text === 'string'
      && item.text.length > 0
      && Number.isFinite(item?.createdAt)
    ))
    : [];
  return { contexts, pending };
}

/** Persist per-contact conversation capability and short-lived interactive sends. */
export function createOutboundStateStore(
  accountId: string,
  options: OutboundStateOptions = {},
) {
  validateAccountId(accountId);
  const now = options.now ?? (() => Date.now());
  const pendingTtlMs = options.pendingTtlMs ?? DEFAULT_PENDING_TTL_MS;
  const maxPending = options.maxPending ?? DEFAULT_MAX_PENDING;
  const rootDir = options.rootDir ?? join(DATA_DIR, 'outbound-state');
  const filePath = join(rootDir, `${accountId}.json`);
  let state = normalizeState(loadJson<OutboundStateFile | null>(filePath, null));

  function prunePending(): boolean {
    const cutoff = now() - pendingTtlMs;
    const before = state.pending.length;
    state.pending = state.pending.filter((item) => item.createdAt >= cutoff);
    return state.pending.length !== before;
  }

  function persist(): void {
    saveJson(filePath, state);
  }

  function getContext(userId: string): ContextEntry | null {
    const found = state.contexts.find((item) => item.userId === userId);
    return found ? { ...found } : null;
  }

  function rememberContext(userId: string, token: string): void {
    if (!userId || !token) return;
    state.contexts = state.contexts.filter((item) => item.userId !== userId);
    state.contexts.push({ userId, token, updatedAt: now() });
    persist();
  }

  function clearContext(userId: string): void {
    const before = state.contexts.length;
    state.contexts = state.contexts.filter((item) => item.userId !== userId);
    if (state.contexts.length !== before) persist();
  }

  function enqueue(toUserId: string, text: string): PendingOutboundMessage {
    const changed = prunePending();
    const existing = state.pending.find(
      (item) => item.toUserId === toUserId && item.text === text,
    );
    if (existing) {
      if (changed) persist();
      return { ...existing };
    }
    const item: PendingOutboundMessage = {
      id: randomUUID(),
      toUserId,
      text,
      createdAt: now(),
    };
    state.pending.push(item);
    if (state.pending.length > maxPending) {
      state.pending = state.pending.slice(-maxPending);
    }
    persist();
    return { ...item };
  }

  function listPending(toUserId?: string): PendingOutboundMessage[] {
    if (prunePending()) persist();
    return state.pending
      .filter((item) => !toUserId || item.toUserId === toUserId)
      .map((item) => ({ ...item }));
  }

  function removePending(id: string): void {
    const before = state.pending.length;
    state.pending = state.pending.filter((item) => item.id !== id);
    if (state.pending.length !== before) persist();
  }

  return {
    getContext,
    rememberContext,
    clearContext,
    enqueue,
    listPending,
    removePending,
  };
}
