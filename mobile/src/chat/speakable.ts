/**
 * A reply as words to read aloud: what a person would say if they read the
 * message to you. Markdown marks go, a link is read as its text, an address
 * as the site it points to, a table row as its cells, and a code block (which
 * nobody wants read out symbol by symbol) as nothing at all.
 *
 * Pure: no DOM, no network, so `node --test` covers it.
 */

/** Longest text read in one go: a text-to-speech service's own limit is 4096 characters. */
export const SPEAK_MAX_CHARS = 4000;

const FENCE = /^\s*(```|~~~)/;
const TABLE_RULE = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;

function siteName(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return "a link";
  }
}

function inline(line: string): string {
  return (
    line
      // Images say nothing; links say their text.
      .replace(/!\[[^\]]*\]\([^)]*\)/g, "")
      .replace(/\[([^\]]+)\]\((?:[^()]|\([^)]*\))*\)/g, "$1")
      .replace(/<(https?:\/\/[^>\s]+)>/g, (_, url: string) => siteName(url))
      .replace(/\bhttps?:\/\/[^\s)<>]+/g, (url) => siteName(url))
      .replace(/<br\s*\/?>/gi, " ")
      .replace(/<\/?[a-z][^>]*>/gi, "")
      .replace(/`([^`]+)`/g, "$1")
      .replace(/(\*\*|__)(?=\S)(.+?)(?<=\S)\1/g, "$2")
      .replace(/(^|[^\w*])\*(?=\S)(.+?)(?<=\S)\*(?!\w)/g, "$1$2")
      .replace(/(^|\W)_(?=\S)(.+?)(?<=\S)_(?!\w)/g, "$1$2")
      .replace(/~~(.+?)~~/g, "$1")
  );
}

/** The words of a markdown reply, ready for a voice. "" when there is nothing to say. */
export function speakableText(markdown: string): string {
  const out: string[] = [];
  let inFence = false;
  for (const raw of (markdown || "").replace(/\r\n?/g, "\n").split("\n")) {
    if (FENCE.test(raw)) {
      inFence = !inFence;
      continue;
    }
    if (inFence) continue;
    let line = raw.trim();
    if (!line) {
      out.push("");
      continue;
    }
    if (TABLE_RULE.test(line) && line.includes("-")) continue;
    if (/^(-{3,}|\*{3,}|_{3,})$/.test(line)) continue;
    if (line.startsWith("|")) {
      line = line
        .replace(/^\||\|$/g, "")
        .split("|")
        .map((cell) => cell.trim())
        .filter(Boolean)
        .join(", ");
    }
    line = line
      .replace(/^#{1,6}\s+/, "")
      .replace(/^(>\s*)+/, "")
      .replace(/^[-*+]\s+\[[ xX]\]\s+/, "")
      .replace(/^[-*+•]\s+/, "")
      .replace(/^(\d+)[.)]\s+/, "$1. ");
    line = inline(line).replace(/\s{2,}/g, " ").trim();
    if (line) out.push(line);
  }
  const text = out
    .join("\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
  return clip(text);
}

/** Cuts a long text at the last sentence that fits. */
function clip(text: string, max = SPEAK_MAX_CHARS): string {
  if (text.length <= max) return text;
  const head = text.slice(0, max);
  const end = Math.max(head.lastIndexOf(". "), head.lastIndexOf("! "), head.lastIndexOf("? "), head.lastIndexOf("\n"));
  return (end > max / 2 ? head.slice(0, end + 1) : head).trim();
}
