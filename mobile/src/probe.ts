/**
 * Asking an address whether it is an Olano deployment before the app commits
 * to it.
 *
 * `GET /api/status` is the runtime's public readiness probe: it needs no
 * login, the runtime answers it with `Access-Control-Allow-Origin: *`, and its
 * body has a shape no ordinary website shares (`status: "running"`, an
 * `agents` list and a `capabilities` object; see
 * olano/runtime/routes/status.py). That is what tells "this is Olano" apart
 * from "something answered".
 *
 * Pure apart from `fetch`, which is injectable, so `node --test` covers it.
 */

export const PROBE_TIMEOUT_MS = 8000;

export type ProbeFailure =
  /** The phone has no network connection. */
  | "offline"
  /** Nothing answered: a typo, a deployment that does not exist, or one that is switched off. */
  | "not_found"
  /** Something answered, but it is not an Olano runtime. */
  | "not_olano"
  /** The deployment's front door answered, but the runtime behind it did not (502/503/504). */
  | "unavailable"
  /** No answer within PROBE_TIMEOUT_MS. */
  | "timeout";

export type ProbeResult =
  | { ok: true; origin: string }
  | { ok: false; reason: ProbeFailure; status?: number };

export interface ProbeOptions {
  fetchImpl?: typeof fetch;
  timeoutMs?: number;
  /** Reads `navigator.onLine` by default; injectable for tests. */
  isOnline?: () => boolean;
}

/** True when a parsed `/api/status` body can only have come from an Olano runtime. */
export function isOlanoStatus(body: unknown): boolean {
  if (typeof body !== "object" || body === null || Array.isArray(body)) return false;
  const record = body as Record<string, unknown>;
  return (
    record.status === "running" &&
    Array.isArray(record.agents) &&
    typeof record.capabilities === "object" &&
    record.capabilities !== null &&
    !Array.isArray(record.capabilities)
  );
}

export async function probeDeployment(origin: string, options: ProbeOptions = {}): Promise<ProbeResult> {
  const fetchImpl = options.fetchImpl ?? fetch;
  const timeoutMs = options.timeoutMs ?? PROBE_TIMEOUT_MS;
  const isOnline = options.isOnline ?? (() => (typeof navigator === "undefined" ? true : navigator.onLine));
  if (!isOnline()) return { ok: false, reason: "offline" };

  const controller = new AbortController();
  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);

  try {
    let response: Response;
    try {
      response = await fetchImpl(`${origin}/api/status`, {
        method: "GET",
        cache: "no-store",
        credentials: "omit",
        redirect: "follow",
        headers: { Accept: "application/json" },
        signal: controller.signal,
      });
    } catch {
      // fetch rejects with an opaque TypeError for DNS failures, refused
      // connections, TLS errors and CORS refusals alike, so this cannot be
      // more specific than "nothing usable answered".
      if (timedOut) return { ok: false, reason: "timeout" };
      return { ok: false, reason: isOnline() ? "not_found" : "offline" };
    }

    if ([502, 503, 504].includes(response.status)) {
      return { ok: false, reason: "unavailable", status: response.status };
    }
    if (!response.ok) {
      return { ok: false, reason: "not_olano", status: response.status };
    }

    let body: unknown;
    try {
      body = await response.json();
    } catch {
      if (timedOut) return { ok: false, reason: "timeout" };
      return { ok: false, reason: "not_olano", status: response.status };
    }
    if (!isOlanoStatus(body)) {
      return { ok: false, reason: "not_olano", status: response.status };
    }
    return { ok: true, origin: finalOrigin(origin, response.url) };
  } finally {
    clearTimeout(timer);
  }
}

/**
 * The origin that actually answered. A deployment reached through a redirect
 * (`example.com` to `www.example.com`) must be stored under the address that
 * served it: the native web view treats any other host as an external link.
 */
function finalOrigin(requested: string, responseUrl: string): string {
  if (!responseUrl) return requested;
  try {
    const url = new URL(responseUrl);
    if (url.pathname !== "/api/status") return requested;
    if (url.protocol !== "https:" && url.protocol !== "http:") return requested;
    // Never let a redirect downgrade https to http.
    if (requested.startsWith("https:") && url.protocol !== "https:") return requested;
    return url.origin;
  } catch {
    return requested;
  }
}

/** Plain-language explanation of a probe failure for the connect screen. */
export function describeFailure(host: string, result: { reason: ProbeFailure; status?: number }): string {
  switch (result.reason) {
    case "offline":
      return "Your phone is offline. Connect to the internet and try again.";
    case "not_found":
      return `Couldn't reach ${host}. Check the address for typos. If it is right, the deployment may be stopped.`;
    case "not_olano":
      return `${host} answered, but it is not an Olano deployment. Use the address you open your Olano dashboard with.`;
    case "unavailable":
      return `${host} is not responding right now (error ${result.status ?? "unknown"}). It may be starting up or updating. Try again in a minute.`;
    case "timeout":
      return `${host} took too long to answer. Check your connection and try again.`;
  }
}
