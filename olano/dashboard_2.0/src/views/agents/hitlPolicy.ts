// HITL approval-policy loader + helpers. Fetches the tool→category map from the
// backend (the single source of truth in `olano.security.hitl_policy`) so the
// dashboard can compute a tool's *effective* approval state without duplicating
// the policy. The per-agent profile, toggles, and per-tool overrides are layered
// on top here, mirroring `resolve_approval_set()` on the server.

import { useEffect, useState } from "react";
import { api } from "../../core/api";

export interface HitlPolicy {
  categories: Record<string, string[]>; // category -> tool names
  profiles: Record<string, string[]>; // profile -> categories
  write_toggle_category: string;
  shell_toggle_category: string;
}

const EMPTY: HitlPolicy = {
  categories: {},
  profiles: {},
  write_toggle_category: "write_external",
  shell_toggle_category: "shell",
};

let cached: HitlPolicy | null = null;
let inflight: Promise<HitlPolicy> | null = null;

async function fetchPolicy(): Promise<HitlPolicy> {
  if (cached) return cached;
  if (inflight) return inflight;
  inflight = (async () => {
    const p = await api.get<HitlPolicy>("/api/hitl/policy").catch(() => EMPTY);
    cached = { ...EMPTY, ...p };
    return cached;
  })();
  try {
    return await inflight;
  } finally {
    inflight = null;
  }
}

export function useHitlPolicy(): HitlPolicy {
  const [p, setP] = useState<HitlPolicy>(cached ?? EMPTY);
  useEffect(() => {
    let alive = true;
    void fetchPolicy().then((x) => {
      if (alive) setP(x);
    });
    return () => {
      alive = false;
    };
  }, []);
  return p;
}

export type ToolApproval = "default" | "always" | "never";

interface SecurityLike {
  profile?: string;
  hitl_enabled?: boolean;
  hitl_write_tools?: boolean;
  hitl_shell?: boolean;
  tools?: Record<string, { approval?: ToolApproval }>;
}

export function toolCategory(policy: HitlPolicy, name: string): string | null {
  for (const [cat, tools] of Object.entries(policy.categories)) {
    if (tools.includes(name)) return cat;
  }
  return null;
}

// Categories enforced by the profile + the two explicit toggles.
function enforcedCategories(policy: HitlPolicy, sec: SecurityLike): Set<string> {
  const profile = sec.profile || "developer";
  const cats = new Set<string>(policy.profiles[profile] || policy.profiles["developer"] || []);
  if (sec.hitl_write_tools) cats.add(policy.write_toggle_category);
  if (sec.hitl_shell) cats.add(policy.shell_toggle_category);
  return cats;
}

export interface EffectiveApproval {
  required: boolean;
  source: "override" | "profile" | "default";
}

// Resolve whether a tool requires approval, mirroring resolve_approval_set().
export function effectiveApproval(
  policy: HitlPolicy,
  sec: SecurityLike,
  name: string,
): EffectiveApproval {
  if (sec.hitl_enabled === false) return { required: false, source: "default" };
  const override = sec.tools?.[name]?.approval;
  if (override === "always") return { required: true, source: "override" };
  if (override === "never") return { required: false, source: "override" };
  const cat = toolCategory(policy, name);
  if (cat && enforcedCategories(policy, sec).has(cat)) {
    return { required: true, source: "profile" };
  }
  return { required: false, source: "default" };
}
