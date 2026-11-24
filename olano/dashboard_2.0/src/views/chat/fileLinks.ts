// Workspace files named in an agent's reply → links into the Files tab.
//
// The backend resolves every workspace file a reply (or a tool result) names
// into a MediaRef; `mentions` carries the exact tokens it matched in the text
// (relative, absolute or backticked). This module turns those refs into an
// index the Markdown renderer consults so that
//   - `reports/x.png`, `/abs/ws/reports/x.png` or a bare `x.png` in the prose
//     become links to the Files tab (`files_url`),
//   - `![alt](reports/x.png)` renders the file itself (`url`, the bytes),
//   - `[label](reports/x.png)` links it.
// Pure (no DOM, no React) so fileLinks.test.ts runs it under node:test.

import type { MediaRef } from "../../core/types";
// Explicit extension: node:test resolves the path as written (tsc's
// allowImportingTsExtensions and Vite both accept it). It is THE escaper, not
// a mirror — a token must be escaped exactly as Markdown escapes its prose or
// it never matches.
import { escapeHtml } from "../../ui/highlight.ts";

export interface FileLinkTarget {
  /** Files-tab deep link (`/files?path=…&agent=…`) — where a click navigates. */
  href: string;
  /** The bytes (`/api/files-raw/…`) — what an inline image/audio/video loads. */
  raw: string;
  kind: MediaRef["kind"];
  name: string;
}

interface ScanEntry {
  /** The token HTML-escaped the way Markdown escapes prose. */
  text: string;
  target: FileLinkTarget;
}

export interface FileLinkIndex {
  /** Raw token → target. Insertion order is longest token first. */
  targets: Map<string, FileLinkTarget>;
  /** The same tokens, escaped, longest first — what linkifyFileMentions scans with. */
  scan: ScanEntry[];
}

function isRootRelative(u: unknown): u is string {
  return typeof u === "string" && u.startsWith("/") && !u.startsWith("//");
}

// A mention token as the backend saw it, minus the backticks a code-span
// mention wears: the prose pass runs before the code-span pass, so the bare
// path is what it meets inside the backticks (and the code styling survives).
function normalizeToken(raw: unknown): string {
  if (typeof raw !== "string") return "";
  let t = raw.trim();
  if (t.length > 2 && t.startsWith("`") && t.endsWith("`")) t = t.slice(1, -1).trim();
  return t;
}

/**
 * Index every token that names one of `refs` — each `mention`, the
 * workspace-relative `path` and the bare `name` — longest first. A ref
 * without a `files_url` has nowhere to link and is skipped. A token two
 * different files claim (a bare name shared by two directories) is dropped:
 * no link beats a link to the wrong file.
 */
export function buildFileIndex(refs?: MediaRef[]): FileLinkIndex {
  const claims = new Map<string, FileLinkTarget | null>();
  for (const ref of refs ?? []) {
    if (!ref || !isRootRelative(ref.files_url) || !isRootRelative(ref.url)) continue;
    const target: FileLinkTarget = { href: ref.files_url, raw: ref.url, kind: ref.kind, name: ref.name };
    for (const candidate of [...(ref.mentions ?? []), ref.path, ref.name]) {
      const token = normalizeToken(candidate);
      if (!token) continue;
      const prev = claims.get(token);
      if (prev === undefined) claims.set(token, target);
      else if (prev !== null && prev.href !== target.href) claims.set(token, null);
    }
  }
  const entries = [...claims].filter((e): e is [string, FileLinkTarget] => e[1] !== null);
  entries.sort((a, b) => b[0].length - a[0].length);
  const scan = entries.map(([token, target]) => ({ text: escapeHtml(token), target }));
  scan.sort((a, b) => b.text.length - a.text.length);
  return { targets: new Map(entries), scan };
}

// Memoized per `media` array identity: the store hands out a NEW array on
// every merge, so a message's index is built once and every render of its
// (memoized) Markdown blocks sees the same object until the media changes.
const INDEX_CACHE = new WeakMap<MediaRef[], FileLinkIndex>();

/** The index for a message's media, or undefined when there is nothing to link. */
export function fileIndexFor(media: MediaRef[] | undefined): FileLinkIndex | undefined {
  if (!media?.length) return undefined;
  let idx = INDEX_CACHE.get(media);
  if (!idx) {
    idx = buildFileIndex(media);
    INDEX_CACHE.set(media, idx);
  }
  return idx.scan.length ? idx : undefined;
}

function attrValue(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/"/g, "&quot;");
}

/**
 * The anchor for a file target. `data-internal` marks it for the renderer's
 * delegated click handler (client-side navigation, no `target`); `labelHtml`
 * must already be escaped/rendered.
 */
export function fileAnchorHtml(target: FileLinkTarget, labelHtml: string): string {
  return `<a class="md-file" data-internal="1" href="${attrValue(target.href)}">${labelHtml}</a>`;
}

// Characters that continue a path or a word on either side of a match: a
// token found next to one of these is part of something longer (`x.png`
// inside `other/x.png`, `reports/x.png` inside `/ws/reports/x.png`).
const PATHISH = /[A-Za-z0-9_~/\\-]/;
const WORDISH = /[A-Za-z0-9_-]/;

// A `.` is a boundary breaker only when it glues two words (`foo.reports/x`,
// `x.png.bak`); the full stop after "see reports/x.png." is prose.
function boundaryOk(s: string, start: number, len: number): boolean {
  const end = start + len;
  const before = start > 0 ? s[start - 1] : "";
  const after = end < s.length ? s[end] : "";
  if (before) {
    if (PATHISH.test(before)) return false;
    if (before === "." && start > 1 && WORDISH.test(s[start - 2])) return false;
  }
  if (after) {
    if (PATHISH.test(after)) return false;
    if (after === "." && end + 1 < s.length && WORDISH.test(s[end + 1])) return false;
  }
  return true;
}

function matchAt(run: string, i: number, scan: ScanEntry[]): ScanEntry | null {
  for (const e of scan) {
    if (run.startsWith(e.text, i) && boundaryOk(run, i, e.text.length)) return e;
  }
  return null;
}

// One stash-free run of text. A character-by-character walk (not one global
// regex) so a token that fails its boundary check does not swallow the
// characters a longer token starting one position later needs.
function linkifyRun(run: string, scan: ScanEntry[], keep: (html: string) => string): string {
  const live = scan.filter((e) => run.includes(e.text));
  if (!live.length) return run;
  let out = "";
  let from = 0;
  let i = 0;
  while (i < run.length) {
    const hit = matchAt(run, i, live);
    if (hit) {
      out += run.slice(from, i) + keep(fileAnchorHtml(hit.target, hit.text));
      i += hit.text.length;
      from = i;
    } else {
      i++;
    }
  }
  return from === 0 ? run : out + run.slice(from);
}

/**
 * Replace every exact token occurrence in ALREADY HTML-ESCAPED prose with a
 * Files-tab anchor, via `keep` (the renderer's stash). Never touches the
 * inside of a `\x00n\x00` stash token — the text is split on them first, so
 * an image, a link or a code span built by an earlier pass is opaque here.
 * Longest tokens win at any position; a token inside a longer path or word
 * is left alone.
 */
export function linkifyFileMentions(
  escapedText: string,
  index: FileLinkIndex | undefined,
  keep: (html: string) => string,
): string {
  if (!index || index.scan.length === 0 || !escapedText) return escapedText;
  const parts = escapedText.split(/(\x00\d+\x00)/);
  for (let p = 0; p < parts.length; p += 2) parts[p] = linkifyRun(parts[p], index.scan, keep);
  return parts.join("");
}

function unescapeHtml(s: string): string {
  return s.includes("&") ? s.replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&") : s;
}

// The spellings a markdown URL may use for an indexed token: as written, with
// the renderer's escaping undone, percent-decoded (`my%20file.png`), and each
// of those without a leading `./`.
function candidates(url: string): string[] {
  const out: string[] = [];
  const add = (u: string) => {
    if (u && !out.includes(u)) out.push(u);
  };
  for (const base of [url, unescapeHtml(url)]) {
    add(base);
    try {
      add(decodeURIComponent(base));
    } catch {
      // a stray `%` — not an encoded path
    }
  }
  for (const u of [...out]) if (u.startsWith("./")) add(u.slice(2));
  return out;
}

/** The target an inline `[label](url)` / `![alt](url)` token names, if indexed. */
export function resolveFileLink(url: string, index: FileLinkIndex | undefined): FileLinkTarget | null {
  if (!index || index.targets.size === 0 || !url) return null;
  for (const c of candidates(url)) {
    const t = index.targets.get(c);
    if (t) return t;
  }
  return null;
}

/** For `![alt](token)`: the files-raw URL to load when `token` names an indexed file. */
export function resolveMarkdownImage(url: string, index: FileLinkIndex | undefined): string | null {
  return resolveFileLink(url, index)?.raw ?? null;
}
