import { ApiError } from "../../core/api";
import type { ApprovePulseResponse, CognitionOverrides } from "./types";

// ── per-agent overrides ──────────────────────────────────────────────────────

/** Every scalar field that can carry a per-agent cognition override.
 *
 * One list, read by three things that must never disagree about what an
 * override *is*: the badge on the settings header, the count beside the tier
 * table, and the Reset button that clears them. Adding a knob and forgetting
 * one of the others is how the budget fields would have ended up counted but
 * unresettable — or worse, resettable but invisible. */
export const OVERRIDE_FIELDS = [
  "enabled",
  "autonomous",
  "auto_approve",
  "intensity",
  "worker_max_iterations",
  "worker_timeout_seconds",
] as const;

/** How many settings this agent has an opinion about (0 ⇒ inherits everything). */
export function countOverrides(ov: CognitionOverrides | undefined | null): number {
  if (!ov) return 0;
  const scalars = OVERRIDE_FIELDS.map((k) => ov[k]);
  const feats = Object.values(ov.features || {});
  return [...scalars, ...feats].filter((v) => v !== null && v !== undefined).length;
}

// ── error / time formatting ──────────────────────────────────────────────────

export function errMessage(e: unknown, fallback: string): string {
  if (e instanceof ApiError) return e.detail || fallback;
  if (e instanceof Error) return e.message || fallback;
  return fallback;
}

// "just now" / "5m ago" / "3h ago" / "2d ago" / a date — mirrors formatTs().
export function formatTs(iso: string | null | undefined): string {
  if (!iso) return "";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "";
  const diff = Date.now() - t;
  if (diff < 60_000) return "just now";
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}m ago`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)}h ago`;
  if (diff < 604_800_000) return `${Math.floor(diff / 86_400_000)}d ago`;
  return new Date(t).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function relFuture(iso: string | null | undefined): string {
  if (!iso) return "—";
  const diff = Date.parse(iso) - Date.now();
  if (Number.isNaN(diff)) return "—";
  if (diff <= 0) return "soon";
  const mins = Math.round(diff / 60_000);
  if (mins < 60) return `in ${mins}m`;
  const hrs = Math.round(mins / 60);
  if (hrs < 48) return `in ${hrs}h`;
  return `in ${Math.round(hrs / 24)}d`;
}

export function relPast(iso: string | null | undefined): string {
  if (!iso) return "never";
  const diff = Date.now() - Date.parse(iso);
  if (Number.isNaN(diff)) return "never";
  const mins = Math.round(diff / 60_000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 48) return `${hrs}h ago`;
  return `${Math.round(hrs / 24)}d ago`;
}

// Compact cron → human cadence, matching the original _cronHuman().
export function cronHuman(cron: string | null | undefined): string {
  if (!cron) return "—";
  const p = cron.trim().split(/\s+/);
  if (p.length !== 5) return cron;
  const [min, hr, dom, , dow] = p;
  const dayNames: Record<string, string> = {
    "0": "Sun",
    "1": "Mon",
    "2": "Tue",
    "3": "Wed",
    "4": "Thu",
    "5": "Fri",
    "6": "Sat",
  };
  if (dow !== "*") {
    const days = dow
      .split(",")
      .map((d) => dayNames[d] || d)
      .join(", ");
    return `weekly · ${days}`;
  }
  if (hr.startsWith("*/")) return `every ${hr.slice(2)}h`;
  if (hr.includes(",")) return `${hr.split(",").length}×/day`;
  if (dom.startsWith("*/")) return `every ${dom.slice(2)}d`;
  const h = String(hr).padStart(2, "0");
  const m = String(min).padStart(2, "0");
  return `daily · ${h}:${m}`;
}

// ── colours ──────────────────────────────────────────────────────────────────

export function priorityColor(p: string): string {
  if (p === "high") return "var(--danger)";
  if (p === "low") return "var(--text-3)";
  return "var(--text-2)";
}

export function statusColor(s: string): string {
  switch (s) {
    case "approved":
    case "promoted":
    case "completed":
    case "active":
    case "empty":
      return "var(--success)";
    case "rejected":
    case "discarded":
    case "error":
      return "var(--danger)";
    case "exploring":
      return "var(--accent)";
    case "skipped":
      return "var(--text-3)";
    default:
      return "var(--text-2)";
  }
}

// A workspace-relative file reference (linkable to the Files view). URLs also
// contain "/" but are not workspace files — they must stay plain text.
export function isFileRef(r: string): boolean {
  if (/^[a-z][a-z0-9+.-]*:\/\//i.test(r)) return false;
  return /\.(md|txt|ya?ml|json|py|js|ts)$/i.test(r) || r.includes("/");
}

// ── outcome card (after approving a pulse item) ──────────────────────────────

export interface Outcome {
  tone: "success" | "pending";
  heading: string;
  detail: string;
  fileRef: string;
}

// Translator function shape (matches what useT(STR) returns). Passed in so this
// non-component helper can localise its user-facing outcome strings.
type Translate = (key: string, vars?: Record<string, string>) => string;

export function describeOutcome(
  result: Partial<ApprovePulseResponse> & { job_id?: string },
  t: Translate,
): Outcome {
  const kind = result.kind || "";
  const action = result.action_type || "noop";
  const refs = result.references || [];
  const fileRef = refs.find((r) => isFileRef(r)) || "";
  const msg = result.message || "";

  if (action === "run_worker" || result.job_id) {
    return {
      tone: "pending",
      heading: t("outcome.working.heading"),
      detail:
        t("outcome.working.detail") +
        (result.job_id ? t("outcome.working.job", { job: result.job_id }) : ""),
      fileRef: "",
    };
  }
  if (kind === "learned_skill") {
    const name = fileRef
      ? fileRef.split("/").pop()!.replace(/\.md$/, "")
      : result.title || t("outcome.skill.name");
    return {
      tone: "success",
      heading: t("outcome.skill.heading", { name }),
      detail: t("outcome.skill.detail"),
      fileRef,
    };
  }
  if (action === "append_lines" || action === "append_text" || action === "composite") {
    return {
      tone: "success",
      heading: msg || t("outcome.changes"),
      detail: fileRef ? t("outcome.changes.detail") : "",
      fileRef,
    };
  }
  if (action === "restore_backup") {
    return { tone: "success", heading: msg || t("outcome.restored"), detail: "", fileRef };
  }
  const noAction = t("outcome.noAction");
  return {
    tone: "success",
    heading:
      msg && msg !== noAction
        ? msg
        : result.title
          ? t("outcome.approvedTitle", { title: result.title })
          : t("outcome.approved"),
    detail: msg === noAction || !msg ? t("outcome.noChanges") : "",
    fileRef,
  };
}

// ── tiny markdown → HTML (inline-safe subset) ────────────────────────────────
// The platform's markdown is light: bold, italics, inline code, headings,
// fenced code, links, bullet lists. We escape first, then apply markers, so the
// output is XSS-safe for use with dangerouslySetInnerHTML.

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

export function renderMarkdownHtml(src: string): string {
  if (!src) return "";
  // Pull fenced code blocks out first so their contents aren't transformed.
  const blocks: string[] = [];
  let text = src.replace(/```([\s\S]*?)```/g, (_m, code: string) => {
    const i = blocks.length;
    blocks.push(
      `<pre style="background:var(--code-bg);border:1px solid var(--code-border);border-radius:var(--r-sm);padding:10px 12px;overflow:auto;font-family:var(--font-mono);font-size:12px;white-space:pre-wrap;word-break:break-word">${escapeHtml(
        code.replace(/^\n/, "").replace(/\n$/, ""),
      )}</pre>`,
    );
    return ` BLOCK${i} `;
  });

  text = escapeHtml(text);

  const lines = text.split("\n");
  const out: string[] = [];
  let inList = false;
  const flush = () => {
    if (inList) {
      out.push("</ul>");
      inList = false;
    }
  };
  for (const raw of lines) {
    const line = raw;
    const block = line.match(/^ BLOCK(\d+) $/);
    if (block) {
      flush();
      out.push(blocks[Number(block[1])]);
      continue;
    }
    const h = line.match(/^(#{1,4})\s+(.*)$/);
    if (h) {
      flush();
      const lvl = Math.min(6, h[1].length + 2);
      out.push(`<h${lvl} style="margin:10px 0 4px;font-size:${16 - h[1].length}px">${inline(h[2])}</h${lvl}>`);
      continue;
    }
    const li = line.match(/^\s*[-*]\s+(.*)$/);
    if (li) {
      if (!inList) {
        out.push('<ul style="margin:4px 0;padding-left:20px">');
        inList = true;
      }
      out.push(`<li>${inline(li[1])}</li>`);
      continue;
    }
    flush();
    if (line.trim() === "") out.push("<br/>");
    else out.push(`<div>${inline(line)}</div>`);
  }
  flush();
  return out.join("");
}

function inline(s: string): string {
  return s
    .replace(/`([^`]+)`/g, '<code style="font-family:var(--font-mono);font-size:0.92em;background:var(--code-bg);padding:1px 4px;border-radius:3px">$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\*([^*]+)\*/g, "<em>$1</em>")
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>');
}

export function hasMarkdown(s: string): boolean {
  return /\*\*|`|#{1,3} |\n\n/.test(s);
}
