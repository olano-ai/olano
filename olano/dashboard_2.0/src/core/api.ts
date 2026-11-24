// Typed fetch wrapper. Always sends the session cookie (credentials: include)
// and surfaces the backend's {detail} as a structured ApiError so callers can
// branch on auth states (setup_required / auth_required / invalid_credentials).

export class ApiError extends Error {
  status: number;
  detail: string;
  // Machine-readable error code from structured backend details (deployment
  // guard / actionable errors send {code, message, remediation}); "" when the
  // backend sent a plain-string detail.
  code: string;
  constructor(status: number, detail: string, code = "") {
    super(detail);
    this.status = status;
    this.detail = detail;
    this.code = code;
  }
}

function safeJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

// FastAPI's {detail} is not always a string: the deployment guard raises
// {code, message, remediation, ...} dicts and request-validation errors are
// arrays of {loc, msg, type}. String() on either renders "[object Object]" —
// which is exactly what users saw in error toasts — so flatten every shape
// into readable text and keep plain strings byte-identical (callers branch on
// e.detail === "setup_required" etc.).
function detailToText(detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const msgs = detail
      .map((item) => {
        if (typeof item === "string") return item;
        if (item && typeof item === "object") {
          const { msg, loc } = item as { msg?: unknown; loc?: unknown };
          if (typeof msg === "string") {
            const where = Array.isArray(loc) ? loc.filter((l) => l !== "body").join(".") : "";
            return where ? `${where}: ${msg}` : msg;
          }
        }
        return "";
      })
      .filter(Boolean);
    if (msgs.length) return msgs.join("; ");
  } else if (detail && typeof detail === "object") {
    const { message, remediation } = detail as { message?: unknown; remediation?: unknown };
    if (typeof message === "string" && message) {
      return typeof remediation === "string" && remediation
        ? `${message} ${remediation}`
        : message;
    }
  }
  try {
    return JSON.stringify(detail);
  } catch {
    return String(detail);
  }
}

export interface RequestOptions {
  /** Abort the underlying fetch — a collapsed tree node, an unmounted view.
   *  The promise then rejects with the DOMException `AbortError` fetch raises. */
  signal?: AbortSignal;
}

async function request<T>(
  method: string,
  path: string,
  body?: unknown,
  opts?: RequestOptions,
): Promise<T> {
  // FormData goes out as-is: setting Content-Type by hand would omit the
  // multipart boundary the browser generates, and JSON.stringify would flatten
  // the file to "{}".
  const isForm = typeof FormData !== "undefined" && body instanceof FormData;
  const res = await fetch(path, {
    method,
    credentials: "include",
    headers: body !== undefined && !isForm ? { "Content-Type": "application/json" } : undefined,
    body: body === undefined ? undefined : isForm ? (body as FormData) : JSON.stringify(body),
    signal: opts?.signal,
  });
  const text = await res.text();
  if (!res.ok) throw apiErrorFrom(res.status, res.statusText, text);
  return (text ? safeJson(text) : null) as T;
}

/** The ApiError for a failed response, from its status and body text. Shared
 *  by `request` and the XHR-based file upload (`views/files/upload.ts`), which
 *  needs upload progress that fetch cannot report. */
export function apiErrorFrom(status: number, statusText: string, text: string): ApiError {
  const data = text ? safeJson(text) : null;
  let detail = statusText;
  let code = "";
  if (data && typeof data === "object" && "detail" in data) {
    const raw = (data as { detail: unknown }).detail;
    detail = detailToText(raw) || statusText;
    if (raw && typeof raw === "object" && !Array.isArray(raw)) {
      const c = (raw as { code?: unknown }).code;
      if (typeof c === "string") code = c;
    }
  } else if (data && typeof data === "object" && "error" in data) {
    // The sealed volume's ceremony API (olano-sealer serve, reached at
    // /api/seal/* through Caddy) is not FastAPI: it answers
    // {error: <code>, message: <text>}. Keep its code where callers look
    // for one and its message where they read one.
    const { error, message } = data as { error?: unknown; message?: unknown };
    if (typeof error === "string" && error) code = error;
    if (typeof message === "string" && message) detail = message;
    else if (code) detail = code;
  }
  return new ApiError(status, detail, code);
}

// Short-TTL read cache with in-flight dedup for hot, widely-refetched GETs
// (today: the agent roster — a dozen views fetch /api/agents on mount, so a
// tab switch was a fresh round-trip each time). Not applied to api.get at
// large: only call sites that opt in via cachedGet can serve slightly-stale
// data. Mutating flows must call invalidate() (prefix match) so their next
// read is fresh.
type CacheEntry = { at: number; data: unknown };
const readCache = new Map<string, CacheEntry>();
const inflight = new Map<string, Promise<unknown>>();
// Bumped by invalidate(): a response from a request issued BEFORE the bump
// must not be written into the cache (it's a pre-mutation snapshot), and an
// invalidate-then-cachedGet must not dedupe onto such a request either — both
// would re-serve stale data for a full TTL right after an explicit refresh.
let cacheGen = 0;

function cachedGet<T>(path: string, ttlMs = 5000): Promise<T> {
  const hit = readCache.get(path);
  if (hit && Date.now() - hit.at < ttlMs) return Promise.resolve(hit.data as T);
  if (hit) readCache.delete(path); // expired — don't let dead entries pile up
  const pending = inflight.get(path);
  if (pending) return pending as Promise<T>;
  const gen = cacheGen;
  const p = request<T>("GET", path)
    .then((data) => {
      if (gen === cacheGen) readCache.set(path, { at: Date.now(), data });
      return data;
    })
    .finally(() => {
      if (inflight.get(path) === p) inflight.delete(path);
    });
  inflight.set(path, p);
  return p;
}

function invalidate(prefix: string): void {
  cacheGen++;
  for (const k of [...readCache.keys()]) if (k.startsWith(prefix)) readCache.delete(k);
  // Detach matching in-flight requests so the next cachedGet issues a fresh
  // one instead of joining a pre-mutation fetch (its result also won't be
  // cached, thanks to the generation check above).
  for (const k of [...inflight.keys()]) if (k.startsWith(prefix)) inflight.delete(k);
}

export const api = {
  get: <T>(path: string, opts?: RequestOptions) => request<T>("GET", path, undefined, opts),
  cachedGet,
  invalidate,
  post: <T>(path: string, body?: unknown) => request<T>("POST", path, body),
  // Multipart upload (voice recordings, files) with the same ApiError contract
  // as every other call, so callers can branch on `detail` codes as usual.
  postForm: <T>(path: string, form: FormData) => request<T>("POST", path, form),
  put: <T>(path: string, body?: unknown) => request<T>("PUT", path, body),
  patch: <T>(path: string, body?: unknown) => request<T>("PATCH", path, body),
  del: <T>(path: string) => request<T>("DELETE", path),
};
