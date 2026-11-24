import { useEffect, useState } from "react";
import { api } from "../../core/api";
import { slugFromHost } from "../../core/serverMonitor/deployment";
import type { DeploymentInfo } from "../admin/DeploymentSection";

export { slugFromHost };

// Where this instance lives in the olano cloud console.
//
// On a managed deployment the onboarding wizard asks for a current login
// password and a current vault passphrase that the owner never chose — the
// control plane generated both at provision time, mailed them once, and keeps
// them copyable on the deployment card. Linking straight there beats asking
// people to go hunting.

// Hardcoded on purpose. No build-time env channel reaches this bundle (no
// import.meta usage, no /api/config, no config blob in index.html), and
// LIONCLAW_CLOUD_API_URL is the *API* base — core/mcp_registry.py defaults it
// to api.olano.ai — so it is the wrong source for a console URL.
const CONSOLE_BASE = "https://cloud.olano.ai";
// `slugFromHost` (and the deployment-zone suffixes behind it) lives in
// core/serverMonitor/deployment: the connectivity overlay needs the same
// managed-vs-self-hosted read while the runtime is unreachable, and one copy
// beats two that can drift apart on the next zone migration. Re-exported above
// so this module stays the onboarding-side entry point.

/**
 * Deep link to this instance's card in the console, or null when the box is
 * not a managed cloud deployment.
 *
 * `mode` is the gate: an unmanaged/self-hosted install resolves to
 * posture "open" with a null mode, while managed-cloud sets mode "cloud" in
 * both the healthy and the quarantined path (deployment/resolver.py) — a
 * quarantined instance still wants the link.
 *
 * The slug makes a readable URL; deployment_id is the fallback that always
 * works, for a managed box reached over sslip.io or an IP.
 */
export function consoleUrl(info: DeploymentInfo, host: string): string | null {
  if (info.mode !== "cloud") return null;
  const ref = slugFromHost(host) ?? info.deployment_id;
  return ref ? `${CONSOLE_BASE}/app/deployments/${ref}` : null;
}

// Module-level memo rather than api.cachedGet: that cache has a 5s TTL, and the
// wizard shows one step at a time, so the password and vault panels would each
// pay a round-trip. A deployment's identity never changes under a running
// process, so hold it for good.
let cache: Promise<DeploymentInfo | null> | null = null;

function fetchDeployment(): Promise<DeploymentInfo | null> {
  if (!cache) {
    // Admin-only surface (_ADMIN_ONLY_PREFIXES), which the wizard already is.
    // A 403 or a dead network must leave the panels exactly as they were, so
    // failure is just "no link".
    const pending: Promise<DeploymentInfo | null> = api
      .get<DeploymentInfo>("/api/system/deployment")
      .catch(() => {
        // A failed fetch is never cached as data (the rule core/catalogResource
        // .ts exists to enforce). A first-boot runtime still coming up behind
        // the dashboard would otherwise pin every later step on "self-hosted,
        // no entitlements" for the rest of the session; dropping the memo lets
        // the next step that asks retry.
        if (cache === pending) cache = null;
        return null;
      });
    cache = pending;
  }
  return cache;
}

/**
 * This box's deployment record, tri-state: `undefined` while the lookup is in
 * flight, `null` for a self-hosted install (or a failed / forbidden lookup),
 * the record itself on a managed box.
 *
 * The loading state is load-bearing. A step that renders differently per
 * entitlement has to hold its skeleton while this is `undefined`, rather than
 * paint the self-hosted branch and swap it out a moment later.
 */
export function useDeployment(): DeploymentInfo | null | undefined {
  const [info, setInfo] = useState<DeploymentInfo | null | undefined>(undefined);
  useEffect(() => {
    let on = true;
    fetchDeployment().then((res) => {
      if (on) setInfo(res);
    });
    return () => {
      on = false;
    };
  }, []);
  return info;
}

/**
 * The console link for this instance — null while loading, for self-hosted
 * installs, and whenever the lookup fails. Callers render the link only when
 * it is non-null, so it simply appears a round-trip after mount.
 */
export function useCloudDeploymentUrl(): string | null {
  const info = useDeployment();
  return info ? consoleUrl(info, window.location.hostname) : null;
}

/**
 * Managed-cloud box: entitlements come from a signed plan, and the Olano
 * gateway can bill the included services to the deployment's credits. An
 * unmanaged install resolves to posture "open" with a null mode — it has no
 * gateway to bill through, so there BYOK is the only route to a model.
 */
export function isManagedDeployment(info: DeploymentInfo | null | undefined): boolean {
  return info?.mode === "cloud";
}

/**
 * One deployment feature flag, carrying the signed-settings open-map default:
 * a key the control plane never sent means *enabled* (deployment/schema.py,
 * `DeploymentSettings.feature_enabled`). Reading a missing key as "withheld"
 * would make every newly-introduced flag look like a downgrade to a box whose
 * artifact predates it.
 */
export function deploymentFeatureOn(
  info: DeploymentInfo | null | undefined,
  name: string,
): boolean {
  return info?.features?.[name] !== false;
}

// What a plan's Olano Credits actually buy, in the order the onboarding
// checklist reads them. Each entry names the signed per-service entitlement
// (deployment/schema.py KNOWN_FEATURES) so a plan that withholds one service
// simply drops that row — no surface may promise something the gateway would
// then refuse.
const OLANO_SERVICES: Array<{ id: string; flag: string }> = [
  { id: "intelligence", flag: "olano_models" },
  { id: "image", flag: "olano_image" },
  { id: "vision", flag: "olano_vision" },
  { id: "search", flag: "olano_web_search" },
  { id: "stt", flag: "olano_stt" },
  { id: "tts", flag: "olano_tts" },
];

/**
 * The managed services this deployment's plan covers on credits.
 *
 * Empty for a self-hosted box (no gateway to bill through) and for a
 * quarantined managed box, whose recovery posture switches every managed
 * service off — callers treat an empty list as "say nothing about credits"
 * rather than rendering an empty promise.
 */
export function includedOlanoServices(info: DeploymentInfo | null | undefined): string[] {
  if (!isManagedDeployment(info)) return [];
  return OLANO_SERVICES.filter((s) => deploymentFeatureOn(info, s.flag)).map((s) => s.id);
}

/**
 * What a pasted Olano API key buys a self-hosted box: every managed service,
 * since an account wallet has no plan withholding any of them. A fixed list on
 * purpose, not feature-derived: an open box reads every feature as on, which
 * would also be "every service" but for the wrong reason, and it must never be
 * promised until a key is actually connected (the caller checks that).
 */
export const SELF_HOSTED_OLANO_SERVICES: readonly string[] = OLANO_SERVICES.map((s) => s.id);
