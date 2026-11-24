// Ranked matching for the command palette.
//
// The palette used to do `label.toLowerCase().includes(q)` over ~24 nav rows,
// which needs no ranking because everything that matches fits on screen. A
// settings index is a few hundred rows where "model" hits a dozen fields, so
// the order is the whole product: the section you named must beat a field whose
// help text happens to mention it.
//
// Deliberately dependency-free — the scoring rules below are ~30 lines and a
// fuzzy library would bring its own ranking we would then have to fight.

/** What the scorer reads. Callers resolve i18n keys before calling. */
export interface Searchable {
  label: string;
  /** "Config › Speech" — the trail shown under the label. */
  breadcrumb?: string;
  /** The field's help text. Searched, at low weight. */
  hint?: string;
  /** Untranslated synonyms ("llm", "voice", "stt"). */
  keywords?: string[];
}

/** [start, end) offsets into `label`, for bolding the matched span. */
export type Range = [number, number];

export interface Scored<T> {
  item: T;
  score: number;
  ranges: Range[];
}

// Weights. Gaps are wide on purpose: a label hit must always outrank a hint hit
// no matter how many tokens the hint matches.
const W_LABEL_EXACT = 100;
const W_LABEL_PREFIX = 80;
const W_LABEL_WORD = 60;
const W_LABEL_SUB = 45;
const W_KEYWORD = 35;
const W_BREADCRUMB = 30;
const W_HINT = 15;

/**
 * Lowercase and strip diacritics, so "Configuración" matches "configuracion".
 * Both the query and the haystack go through this.
 */
export function normalize(s: string): string {
  return s
    .normalize("NFD")
    .replace(/\p{Diacritic}/gu, "")
    .toLowerCase();
}

/** Does `hay` contain `needle` at a word boundary (not just anywhere)? */
function wordBoundaryAt(hay: string, needle: string): number {
  let from = 0;
  for (;;) {
    const at = hay.indexOf(needle, from);
    if (at < 0) return -1;
    if (at === 0 || /[\s\-_/.·›(]/.test(hay[at - 1])) return at;
    from = at + 1;
  }
}

/** Best score one query token can earn against one item, plus where it hit. */
function scoreToken(item: Searchable, token: string): { score: number; range: Range | null } {
  const label = normalize(item.label);

  if (label === token) return { score: W_LABEL_EXACT, range: [0, item.label.length] };
  if (label.startsWith(token)) return { score: W_LABEL_PREFIX, range: [0, token.length] };

  const wordAt = wordBoundaryAt(label, token);
  if (wordAt >= 0) return { score: W_LABEL_WORD, range: [wordAt, wordAt + token.length] };

  const subAt = label.indexOf(token);
  if (subAt >= 0) return { score: W_LABEL_SUB, range: [subAt, subAt + token.length] };

  if (item.keywords?.some((k) => normalize(k).includes(token))) {
    return { score: W_KEYWORD, range: null };
  }
  if (item.breadcrumb && normalize(item.breadcrumb).includes(token)) {
    return { score: W_BREADCRUMB, range: null };
  }
  if (item.hint && normalize(item.hint).includes(token)) {
    return { score: W_HINT, range: null };
  }
  return { score: 0, range: null };
}

/**
 * Score every item against the query and return the matches, best first.
 *
 * Tokens are ANDed: "telegram allowed" keeps only items matching both, which is
 * what makes a two-word query narrow instead of widen.
 */
export function search<T extends Searchable>(items: T[], query: string): Scored<T>[] {
  const tokens = normalize(query).split(/\s+/).filter(Boolean);
  if (tokens.length === 0) return [];

  const out: Scored<T>[] = [];
  for (const item of items) {
    let total = 0;
    const ranges: Range[] = [];
    let matchedAll = true;
    for (const token of tokens) {
      const { score, range } = scoreToken(item, token);
      if (score === 0) {
        matchedAll = false;
        break;
      }
      total += score;
      if (range) ranges.push(range);
    }
    if (matchedAll) out.push({ item, score: total, ranges: mergeRanges(ranges) });
  }
  // Stable: Array.prototype.sort is stable in every engine we target, so equal
  // scores keep the catalog's own order rather than shuffling between renders.
  out.sort((a, b) => b.score - a.score);
  return out;
}

/** Collapse overlapping highlight ranges so the renderer can walk them in order. */
function mergeRanges(ranges: Range[]): Range[] {
  if (ranges.length < 2) return ranges;
  const sorted = [...ranges].sort((a, b) => a[0] - b[0]);
  const merged: Range[] = [sorted[0]];
  for (const [start, end] of sorted.slice(1)) {
    const last = merged[merged.length - 1];
    if (start <= last[1]) last[1] = Math.max(last[1], end);
    else merged.push([start, end]);
  }
  return merged;
}

/** Split `text` into alternating plain/highlighted chunks for rendering. */
export function highlightParts(text: string, ranges: Range[]): { text: string; hit: boolean }[] {
  if (ranges.length === 0) return [{ text, hit: false }];
  const parts: { text: string; hit: boolean }[] = [];
  let at = 0;
  for (const [start, end] of ranges) {
    if (start > at) parts.push({ text: text.slice(at, start), hit: false });
    parts.push({ text: text.slice(start, end), hit: true });
    at = end;
  }
  if (at < text.length) parts.push({ text: text.slice(at), hit: false });
  return parts;
}
