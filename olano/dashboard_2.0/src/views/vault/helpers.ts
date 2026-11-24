import { ApiError } from "../../core/api";

// A translator function as returned by useT() — passed in so these non-component
// helpers can localise their output without calling hooks.
type Translate = (key: string, vars?: Record<string, string>) => string;

// Mirrors the original dashboard's fmtRel(): "just now" / "Nm ago" / "Nh ago" /
// "Nd ago" and falls back to a locale date beyond a week. `t` localises the
// relative-time phrases; number/date formatting logic is unchanged.
export function relativeTime(iso: string | null | undefined, t: Translate): string {
  if (!iso) return t("time.dash");
  const dt = new Date(iso);
  const time = dt.getTime();
  if (Number.isNaN(time)) return t("time.dash");
  const s = (Date.now() - time) / 1000;
  if (s < 0) {
    // Future timestamp (e.g. an expiry date): describe time remaining.
    const f = -s;
    if (f < 60) return t("time.inMoments");
    if (f < 3600) return t("time.inMinutes", { n: String(Math.floor(f / 60)) });
    if (f < 86400) return t("time.inHours", { n: String(Math.floor(f / 3600)) });
    if (f < 604800) return t("time.inDays", { n: String(Math.floor(f / 86400)) });
    return dt.toLocaleDateString();
  }
  if (s < 60) return t("time.justNow");
  if (s < 3600) return t("time.minutesAgo", { n: String(Math.floor(s / 60)) });
  if (s < 86400) return t("time.hoursAgo", { n: String(Math.floor(s / 3600)) });
  if (s < 604800) return t("time.daysAgo", { n: String(Math.floor(s / 86400)) });
  return dt.toLocaleDateString();
}

// True when an expiry timestamp is within 7 days (or already past) — drives the
// orange "expires soon" badge, matching the original's < 7 * 86400_000 check.
export function expiresSoon(iso: string | null): boolean {
  if (!iso) return false;
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return false;
  return t - Date.now() < 7 * 24 * 60 * 60 * 1000;
}

// True when an expiry timestamp has already passed — drives the red "expired"
// badge (a small refinement over the original, which only had "expires soon").
export function isExpired(iso: string | null): boolean {
  if (!iso) return false;
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return false;
  return t <= Date.now();
}

export function errMessage(err: unknown, fallback: string): string {
  return err instanceof ApiError ? err.detail : fallback;
}

export const ENC_LABEL = "AES-256-GCM, PBKDF2-SHA256 · ~/.olano/vault/vault.db";
