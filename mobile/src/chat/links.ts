/**
 * Links in a conversation, so every address a person would expect to tap is
 * a link: a full URL, a `www.` address, a URL an agent wrapped in code
 * formatting, and the ones in the person's own messages.
 *
 * The shared renderer (renderProse) links full URLs and `[label](url)` in a
 * reply; `linkifyReply` adds the rest before it runs, as plain Markdown links.
 * `splitLinks` cuts a plain-text message into text and links for React to
 * draw, with no HTML involved.
 *
 * Pure, so `node --test` covers it.
 */

import { escapeHtml, renderProse } from "../shared/dashboard.ts";

/** Punctuation a sentence leaves on a URL's tail ("see www.x.dev." / "(www.x.dev)"). */
const TRAILING = /[.,:;!?'"»”)\]}*_~]+$/;

/** Separates a URL from the punctuation the sentence hung on it. */
export function trimUrl(raw: string): { url: string; rest: string } {
  let url = raw;
  let rest = "";
  for (;;) {
    const m = TRAILING.exec(url);
    if (!m) break;
    // A closing paren the URL opened belongs to it (Wikipedia links).
    if (m[0].startsWith(")") && (url.match(/\(/g)?.length ?? 0) > (url.slice(0, -m[0].length).match(/\)/g)?.length ?? 0)) {
      const keep = url.length - m[0].length + 1;
      rest = url.slice(keep) + rest;
      url = url.slice(0, keep);
      break;
    }
    url = url.slice(0, -m[0].length);
    rest = m[0] + rest;
  }
  return { url, rest };
}

/** The address a link opens: `www.x.dev` gets https://. Null for anything that is not a web address. */
export function hrefFor(url: string): string | null {
  const href = /^www\./i.test(url) ? `https://${url}` : url;
  if (!/^https?:\/\/[^/\s.]+\.[^/\s]+/i.test(href) && !/^https?:\/\/(localhost|\d{1,3}(\.\d{1,3}){3})(:\d+)?/i.test(href)) return null;
  // A quote would end an HTML attribute: never part of an address we hand on.
  return href.replace(/"/g, "%22");
}

const URLISH = /(https?:\/\/[^\s<>`]+|\bwww\.[a-z0-9-]+(?:\.[a-z0-9-]+)+[^\s<>`]*)/gi;

export interface TextPart {
  text: string;
  /** Set when this part is a link: the address it opens. */
  href?: string;
}

/** A plain-text message cut into text and links. */
export function splitLinks(text: string): TextPart[] {
  const out: TextPart[] = [];
  let last = 0;
  for (const match of text.matchAll(URLISH)) {
    const start = match.index ?? 0;
    const { url } = trimUrl(match[0]);
    const href = hrefFor(url);
    if (!href) continue;
    if (start > last) out.push({ text: text.slice(last, start) });
    out.push({ text: url, href });
    last = start + url.length;
  }
  if (last < text.length) out.push({ text: text.slice(last) });
  return out.length ? out : [{ text }];
}

/**
 * A reply's Markdown with the addresses the shared renderer would leave as
 * text turned into Markdown links: a URL alone in code formatting
 * (`` `https://x.dev/a` ``) and a bare `www.` address. Everything else is left
 * exactly as it was, including URLs inside links, images and longer code,
 * except that a `"` inside a URL is percent-encoded.
 */
export function linkifyReply(markdown: string): string {
  // A quote never belongs in an address, and the renderer would put it in an
  // attribute as it is: percent-encode it first.
  let out = markdown.replace(/https?:\/\/[^\s]*"[^\s]*/gi, (url) => url.replace(/"/g, "%22"));
  // A code span holding nothing but an address.
  out = out.replace(/`((?:https?:\/\/|www\.)[^\s`]+)`/gi, (whole, inner: string) => {
    const href = hrefFor(inner);
    return href && !/[[\]()]/.test(inner) ? `[${inner}](${href})` : whole;
  });
  // A bare www. address at the start of a line, after a space or an opening paren.
  out = out.replace(/(^|[\s(])(www\.[a-z0-9-]+(?:\.[a-z0-9-]+)+[^\s<>`[\]()]*)/gim, (whole, lead: string, raw: string) => {
    const { url, rest } = trimUrl(raw);
    const href = hrefFor(url);
    return href ? `${lead}[${url}](${href})${rest}` : whole;
  });
  return out;
}

/**
 * One stretch of a reply between code blocks, as HTML: linked, escaped, then
 * formatted by the dashboard's renderer, which expects escaped text (the
 * dashboard escapes first too). Markup a reply contains is shown as text,
 * never run. A `>` opening a line is the quote marker, which the renderer
 * reads and drops, so it is let through; anywhere else a lone `>` is text.
 */
export function proseHtml(markdown: string): string {
  return renderProse(escapeHtml(linkifyReply(markdown)).replace(/^&gt;/gm, ">"));
}
