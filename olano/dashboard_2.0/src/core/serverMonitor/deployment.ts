// Where this dashboard is running: a box the human can reach a shell on, or a
// managed cloud instance they only ever touch through the browser.
//
// The connectivity overlay is the one surface that has to know this WHILE THE
// RUNTIME IS UNREACHABLE — which is exactly when it can't ask the runtime. So
// the answer comes from two offline-safe sources:
//
//   1. the hostname (`<slug>.olano.run` is always a managed instance), and
//   2. a value cached from the last successful /api/status probe, for a managed
//      instance served from a custom domain.
//
// Getting this wrong is not cosmetic: the old overlay told every cloud customer
// to "start it from a terminal: olano server start" on a box they have no
// shell on, next to a restart button that cannot work there (see canRestart in
// ServerMonitorProvider).

export type DeploymentKind = "managed" | "local" | "unknown";

// Deployment zones, newest first: olano.run is where managed instances live
// now; .olano.ai is the legacy zone boxes served from before the domain
// migration (and keep answering on during its grace period).
const MANAGED_SUFFIXES = [".olano.run", ".olano.ai"];

const KEY = "lc.deploymentKind";

// Hardcoded on purpose, same as views/onboarding/cloudDeployment: no build-time
// env channel reaches this bundle, and LIONCLAW_CLOUD_API_URL is the *API* base
// (api.olano.ai), so it is the wrong source for a console URL.
const CONSOLE_BASE = "https://cloud.olano.ai";

/**
 * The slug half of `<slug>.olano.run` (or the legacy `<slug>.olano.ai`), or
 * null for anything else — sslip.io dev hosts, bare IPs, custom domains.
 * Exactly one label: `a.b.olano.run` is not a deployment subdomain.
 */
export function slugFromHost(host: string): string | null {
  for (const suffix of MANAGED_SUFFIXES) {
    if (!host.endsWith(suffix)) continue;
    const label = host.slice(0, -suffix.length);
    return label && !label.includes(".") ? label : null;
  }
  return null;
}

function readCached(): DeploymentKind {
  try {
    const raw = localStorage.getItem(KEY);
    return raw === "managed" || raw === "local" ? raw : "unknown";
  } catch {
    return "unknown";
  }
}

/**
 * Record what the runtime just told us about itself, so the overlay still knows
 * which advice is valid after the runtime stops answering.
 *
 * `deployment_enforced` is the public half of the managed posture (it rides in
 * /api/status capabilities and is true exactly when an installation manifest
 * put the runtime under control-plane enforcement), which makes it the right
 * signal for "this box is somebody else's to operate".
 */
export function rememberDeploymentKind(enforced: boolean | undefined): void {
  if (enforced === undefined) return;
  try {
    localStorage.setItem(KEY, enforced ? "managed" : "local");
  } catch {
    // storage disabled / quota — fall back to the hostname check below.
  }
}

/**
 * Whether this instance is operated by the Olano cloud (no shell for the human,
 * Docker restarts it on its own) or self-hosted (their machine, their terminal).
 *
 * Hostname first: it needs no prior successful request, so it is right from the
 * very first paint — including a first load that never reaches a healthy
 * runtime. "unknown" only survives for a self-hosted-looking host that has not
 * completed a single probe yet; callers treat it as "don't promise either way".
 */
export function deploymentKind(host = window.location.hostname): DeploymentKind {
  if (slugFromHost(host)) return "managed";
  return readCached();
}

/**
 * This instance's card in the cloud console, for a managed deployment whose
 * runtime is unreachable — the one place the owner can actually act on the box.
 *
 * Built from the hostname alone (no API call: the runtime is down when this is
 * needed). A managed instance served from a custom domain has no slug to point
 * at, so it gets the deployment list instead.
 */
export function consoleLink(host = window.location.hostname): string {
  const slug = slugFromHost(host);
  return slug ? `${CONSOLE_BASE}/app/deployments/${slug}` : `${CONSOLE_BASE}/app/deployments`;
}

/**
 * The deployment's Security tab in the console: where the temporary sign-in
 * password of a just-deployed instance stays copyable until the owner replaces
 * it. Keyed on the deployment id the runtime reports (GET
 * /api/auth/sign-in-hint), never the hostname, so it also resolves for an
 * instance reached over a custom domain, sslip.io or a bare IP.
 */
export function consoleSecurityLink(deploymentId: string): string {
  return `${CONSOLE_BASE}/app/deployments/${encodeURIComponent(deploymentId)}?tab=security`;
}
