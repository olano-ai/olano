import assert from "node:assert/strict";
import { test } from "node:test";

import { AVATAR_PREFIX, clearAvatars, MAX_AVATARS, readAvatar, stamped, writeAvatar, type AvatarStore } from "./avatarCache.ts";

function memoryStore(): AvatarStore & { items: Map<string, string> } {
  const items = new Map<string, string>();
  return {
    items,
    getItem: (k) => items.get(k) ?? null,
    setItem: (k, v) => void items.set(k, v),
    removeItem: (k) => void items.delete(k),
    key: (i) => [...items.keys()][i] ?? null,
    get length() {
      return items.size;
    },
  };
}

const PNG = "data:image/png;base64,iVBORw0KGgo=";

test("only a stamped address is kept, and only under its own deployment", () => {
  const store = memoryStore();
  writeAvatar(store, "https://a", "/api/agents/x/avatar?v=abc", PNG);
  writeAvatar(store, "https://a", "/api/agents/y/avatar", PNG);
  assert.equal(stamped("/api/agents/x/avatar?v=abc"), true);
  assert.equal(stamped("/api/agents/y/avatar"), false);
  assert.equal(readAvatar(store, "https://a", "/api/agents/x/avatar?v=abc"), PNG);
  assert.equal(readAvatar(store, "https://a", "/api/agents/y/avatar"), null);
  assert.equal(readAvatar(store, "https://b", "/api/agents/x/avatar?v=abc"), null);
  assert.equal(readAvatar(store, "", "/api/agents/x/avatar?v=abc"), null);
});

test("the oldest avatars go when the set is full", () => {
  const store = memoryStore();
  for (let i = 0; i < MAX_AVATARS + 3; i += 1) writeAvatar(store, "o", `/api/agents/a${i}/avatar?v=${i}`, PNG);
  assert.equal(readAvatar(store, "o", "/api/agents/a0/avatar?v=0"), null);
  assert.equal(readAvatar(store, "o", "/api/agents/a2/avatar?v=2"), null);
  assert.equal(readAvatar(store, "o", "/api/agents/a3/avatar?v=3"), PNG);
  assert.equal([...store.items.keys()].filter((k) => k.startsWith(AVATAR_PREFIX) && !k.endsWith("index")).length, MAX_AVATARS);
});

test("a broken store costs the copy, never an exception", () => {
  const store = memoryStore();
  store.items.set(`${AVATAR_PREFIX}index`, "{nope");
  assert.doesNotThrow(() => writeAvatar(store, "o", "/api/agents/a/avatar?v=1", PNG));
  assert.equal(readAvatar(store, "o", "/api/agents/a/avatar?v=1"), PNG);
  const full: AvatarStore = {
    ...store,
    setItem: () => {
      throw new Error("QuotaExceededError");
    },
  };
  assert.doesNotThrow(() => writeAvatar(full, "o", "/api/agents/b/avatar?v=1", PNG));
});

test("signing out drops every kept avatar and nothing else", () => {
  const store = memoryStore();
  writeAvatar(store, "o", "/api/agents/a/avatar?v=1", PNG);
  store.setItem("olano.theme", "dark");
  clearAvatars(store);
  assert.deepEqual([...store.items.keys()], ["olano.theme"]);
});
