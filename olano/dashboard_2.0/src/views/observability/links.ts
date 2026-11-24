// "Open in Langfuse" links, built from GET /api/observability/langfuse/links.
//
// Admin-only on both sides: the route sits under the admin-only
// /api/observability prefix, and the hook asks only for an admin, so a member
// never fires a request that would 403. And only for an admin whose account
// has Langfuse links turned on (Admin -> Users; off by default, always on for
// a single-user install), so nobody else sees them or pays for the request. One request per agent per page load
// (module cache); a thread deleted or a key rotated later only changes where
// a link points, never whether the page works. The URL builders are pure and
// live in ./linkUrls.ts for node's --test.

import { useEffect, useState } from "react";

import { api } from "../../core/api";
import { useAuth } from "../../core/auth";
import type { LangfuseLinks } from "./linkUrls";

export { sessionUrl, traceUrl, type LangfuseLinks } from "./linkUrls";

const cache = new Map<string, Promise<LangfuseLinks>>();

function load(agent: string): Promise<LangfuseLinks> {
  let hit = cache.get(agent);
  if (!hit) {
    hit = api
      .get<LangfuseLinks>(`/api/observability/langfuse/links?agent=${encodeURIComponent(agent)}`)
      .catch(() => ({ available: false }));
    cache.set(agent, hit);
  }
  return hit;
}

/** The agent's link targets for an admin with links on, else `null` (and no request). */
export function useLangfuseLinks(agent: string | undefined | null): LangfuseLinks | null {
  const auth = useAuth();
  const shown = !!auth.user?.is_admin && auth.langfuseLinks;
  const [links, setLinks] = useState<LangfuseLinks | null>(null);
  useEffect(() => {
    if (!shown || !agent) {
      setLinks(null);
      return;
    }
    let live = true;
    void load(agent).then((v) => {
      if (live) setLinks(v);
    });
    return () => {
      live = false;
    };
  }, [agent, shown]);
  return links;
}
