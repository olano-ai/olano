// Byte sizes as people read them (history size, a binary file's size).
export function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

type T = (key: string, vars?: Record<string, string>) => string;

// "3h ago" for a timestamp; the absolute time goes in a title attribute.
export function relTime(t: T, iso: string): string {
  const ms = Date.now() - Date.parse(iso);
  if (!Number.isFinite(ms) || ms < 60_000) return t("time.justNow");
  const mins = Math.floor(ms / 60_000);
  if (mins < 60) return t("time.mins", { n: String(mins) });
  const hrs = Math.floor(mins / 60);
  if (hrs < 48) return t("time.hrs", { n: String(hrs) });
  return t("time.days", { n: String(Math.floor(hrs / 24)) });
}

// "1m 05s" since a timestamp, for an operation still running.
export function elapsed(iso: string, now: number = Date.now()): string {
  const secs = Math.max(0, Math.floor((now - Date.parse(iso)) / 1000));
  if (!Number.isFinite(secs)) return "";
  const m = Math.floor(secs / 60);
  const s = secs % 60;
  return m ? `${m}m ${String(s).padStart(2, "0")}s` : `${s}s`;
}
