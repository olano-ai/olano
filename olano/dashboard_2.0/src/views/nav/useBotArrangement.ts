// The stateful half of botArrangement.ts: one React state per sidebar, written
// through to localStorage on every change. Anything unreadable (storage off,
// a corrupt value, a value from a future version) reads as empty rather than
// throwing — a sidebar that cannot remember its pins is still a sidebar.

import { useCallback, useState } from "react";
import { EMPTY_ARRANGEMENT, parseArrangement, type Arrangement } from "./botArrangement";

// One key for the whole arrangement. Deliberately not the simple rail's
// `lc.simple.*` keys — see botArrangement.ts.
export const ARRANGEMENT_KEY = "lc.nav.botArrangement";

export function readArrangement(): Arrangement {
  try {
    const raw = localStorage.getItem(ARRANGEMENT_KEY);
    return raw ? parseArrangement(JSON.parse(raw)) : EMPTY_ARRANGEMENT;
  } catch {
    return EMPTY_ARRANGEMENT;
  }
}

export function writeArrangement(a: Arrangement): void {
  try {
    localStorage.setItem(ARRANGEMENT_KEY, JSON.stringify(a));
  } catch {
    // storage disabled / quota — keep working in-memory only.
  }
}

export type Updater = (fn: (prev: Arrangement) => Arrangement) => void;

export function useBotArrangement(): [Arrangement, Updater] {
  const [arrangement, setArrangement] = useState<Arrangement>(readArrangement);
  const update = useCallback<Updater>((fn) => {
    setArrangement((prev) => {
      const next = fn(prev);
      if (next !== prev) writeArrangement(next);
      return next;
    });
  }, []);
  return [arrangement, update];
}
