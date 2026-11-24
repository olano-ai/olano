// The prose half of ui/Markdown.tsx: escaped markdown text in, HTML out. A
// pure module (no React) so node's `--test` can import it; Markdown.tsx owns
// the fenced code blocks and hands every stretch between them to renderProse.
import { escapeHtml } from "./highlight.ts";
import { fileAnchorHtml, linkifyFileMentions, resolveFileLink, type FileLinkIndex } from "../views/chat/fileLinks.ts";

// A URL bound for a double-quoted attribute (every href and src built here).
// The text is escaped, but escapeHtml leaves quotes alone, so a `"` in a URL
// would close the attribute and let the rest of the URL write new ones.
// Percent-encoding keeps the URL pointing at the same resource and leaves the
// attribute nothing to end on; `'` and the backtick go too, so the value is
// safe whatever quoting it lands in. Only the attribute gets this: link text
// is text content and stays as written.
function attrUrl(url: string): string {
  return url.replace(/["'`]/g, (c) => `%${c.charCodeAt(0).toString(16).toUpperCase()}`);
}

// Only allow URLs we can safely drop into a src attribute. Quotes are
// percent-encoded by attrUrl (the inline regex already forbids whitespace).
// Root-relative `/api/files-raw/` paths are the runtime's own workspace-file
// endpoint (same-origin, session cookie): what a resolved workspace mention loads.
function safeMediaUrl(url: string, kinds?: RegExp): string | null {
  const clean = attrUrl(url);
  if (/^https?:\/\//i.test(clean)) return clean;
  if (/^\/api\/files-raw\//.test(clean)) return clean;
  if (kinds && kinds.test(clean)) return clean; // data: URL of the right media type
  return null;
}

// `alt` is escaped prose, but escapeHtml leaves quotes alone and this one
// lands inside a double-quoted attribute.
function altAttr(alt: string): string {
  return alt.replace(/"/g, "&quot;");
}

function mediaTag(alt: string, url: string, files?: FileLinkIndex): string {
  // A workspace path (`![chart](reports/chart.png)`): draw the file the
  // backend resolved — its bytes for media, a Files-tab link for anything
  // else — and fall through to the scheme rules for a path it doesn't know.
  if (files && !/^(https?:|data:)/i.test(url)) {
    const target = resolveFileLink(url, files);
    if (target) {
      const src = safeMediaUrl(target.raw);
      if (src && target.kind === "audio") return `<audio class="md-audio" controls src="${src}"></audio>`;
      if (src && target.kind === "video") return `<video class="md-video" controls src="${src}"></video>`;
      if (src && target.kind === "image") {
        return `<img class="md-img" alt="${altAttr(alt)}" src="${src}" loading="lazy" />`;
      }
      return fileAnchorHtml(target, alt || escapeHtml(target.name));
    }
  }
  // data: URLs and file extensions both decide the media element.
  if (/^data:audio\//i.test(url) || /\.(mp3|wav|ogg|m4a|aac|flac)(\?|$)/i.test(url)) {
    const src = safeMediaUrl(url, /^data:audio\//i);
    return src ? `<audio class="md-audio" controls src="${src}"></audio>` : alt;
  }
  if (/^data:video\//i.test(url) || /\.(mp4|webm|mov|m4v|ogv)(\?|$)/i.test(url)) {
    const src = safeMediaUrl(url, /^data:video\//i);
    return src ? `<video class="md-video" controls src="${src}"></video>` : alt;
  }
  const src = safeMediaUrl(url, /^data:image\//i);
  return src ? `<img class="md-img" alt="${altAttr(alt)}" src="${src}" loading="lazy" />` : alt;
}

// Punctuation a sentence hangs on a URL's tail ("see https://x.dev." / a URL
// inside (parens)) — kept out of the generated link. `;` is excluded so an
// HTML entity ending an escaped URL (`&amp;`) is trimmed as a unit below.
// `*`/`_`/`~` follow GFM's autolink extension: at a URL's tail they are almost
// always markdown formatting ("**bold https://x.dev** more"), and since this
// pass stashes the URL before the bold/em passes run, an absorbed `**` would
// otherwise leave the emphasis unclosed.
const TRAILING_PUNCT = /[.,:!?)\]}'»”*_~]+$/;
const TRAILING_ENTITY = /(?:&(?:quot|gt|lt|amp|#39);)+$/;

function autolink(text: string, keep: (html: string) => string): string {
  // The text is already HTML-escaped, so `<`/`>` only appear as entities and a
  // run of non-space characters after a scheme is the URL candidate. The run
  // also stops at a stash token: by now earlier passes have stashed finished
  // HTML (a code span, a markdown link) behind `\x00n\x00`, and a URL that
  // swallowed one put that HTML, quotes and all, inside its href on restore.
  return text.replace(/https?:\/\/[^\s\x00]+/g, (raw) => {
    let url = raw;
    let rest = "";
    for (;;) {
      const ent = TRAILING_ENTITY.exec(url);
      if (ent) {
        url = url.slice(0, -ent[0].length);
        rest = ent[0] + rest;
        continue;
      }
      const punct = TRAILING_PUNCT.exec(url);
      if (punct) {
        url = url.slice(0, -punct[0].length);
        rest = punct[0] + rest;
        continue;
      }
      break;
    }
    if (!/^https?:\/\/[^/]+/i.test(url)) return raw;
    return keep(`<a href="${attrUrl(url)}" target="_blank" rel="noreferrer">${url}</a>`) + rest;
  });
}

// `files` is threaded explicitly from the component through renderProse —
// never module state — so two Markdowns rendering in the same tick can't
// read each other's index.
function inline(text: string, files?: FileLinkIndex): string {
  // Finished HTML is stashed behind \x00n\x00 tokens so later passes (bold/em,
  // the bare-URL pass) can never rewrite inside an already-built tag — the
  // token characters cannot occur in escaped text.
  const stash: string[] = [];
  const keep = (html: string): string => `\x00${stash.push(html) - 1}\x00`;
  let out = text
    .replace(/\x00/g, "") // a literal NUL in model text must not address the stash
    .replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, (_m, alt: string, url: string) => keep(mediaTag(alt, url, files)))
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, (_m, label: string, url: string) =>
      keep(`<a href="${attrUrl(url)}" target="_blank" rel="noreferrer">${label}</a>`),
    );
  if (files) {
    // Workspace files the reply names: `[label](reports/x.md)` links first,
    // then bare mentions (`reports/x.md`, `x.md`, the absolute path). After
    // the image/link passes, so a path inside `![](…)` is already stashed;
    // before the code-span pass, so `` `reports/x.md` `` keeps its code
    // styling around the link.
    out = out.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (m, label: string, url: string) => {
      const target = resolveFileLink(url, files);
      return target ? keep(fileAnchorHtml(target, label)) : m;
    });
    out = linkifyFileMentions(out, files, keep);
  }
  out = out.replace(/`([^`]+)`/g, (_m, code: string) => keep(`<code>${code}</code>`));
  // Bare URLs become links too — the "not even clickable" fix for the OAuth
  // sign-in links agents paste into chat.
  out = autolink(out, keep);
  out = out
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
  // Restore recursively: a stash entry can hold another token (the code-span
  // pass wraps a file link already stashed inside backticks, so
  // `` `reports/x.md` `` stashes `<code>\x00n\x00</code>`). A single pass
  // left the inner token in the HTML, which a browser draws as a bare "n".
  // An entry only ever names entries stashed before it, so this terminates.
  const restore = (s: string): string =>
    s.replace(/\x00(\d+)\x00/g, (_m, i: string) => restore(stash[Number(i)] ?? ""));
  return restore(out);
}

// Split a table row "| a | b |" into trimmed cells, tolerating missing edge pipes.
function tableCells(line: string): string[] {
  let s = line.trim();
  if (s.startsWith("|")) s = s.slice(1);
  if (s.endsWith("|")) s = s.slice(0, -1);
  return s.split("|").map((c) => c.trim());
}

// A "|---|:--:|" separator row marks the line above as a table header.
function isTableSeparator(line: string): boolean {
  const s = line.trim();
  return /^\|?[\s:|-]+\|?$/.test(s) && s.includes("-") && s.includes("|");
}

// How many characters a raw cell shows once rendered: a link or image counts
// by its label, an entity as one character, emphasis and code marks not at all.
function cellTextLength(raw: string): number {
  return raw
    .replace(/!?\[([^\]]*)\]\([^)\s]*\)/g, "$1")
    .replace(/&(?:[a-z]+|#\d+);/gi, "x")
    .replace(/[*_`~]/g, "").length;
}

// A column's width class, from its longest cell. A browser sizes a table to
// the narrowest each column can get, and inside a chat bubble (which wraps
// anywhere) that was one character: an "Asking price" column read one letter
// per line. Short columns (prices, numbers, dates) never wrap; text columns
// keep a readable minimum width. The table wrapper scrolls sideways when the
// columns need more room than the bubble has.
function columnClass(cells: string[]): string {
  const longest = Math.max(0, ...cells.map(cellTextLength));
  if (longest <= 18) return "md-col-s";
  if (longest <= 48) return "md-col-m";
  return "md-col-l";
}

function alignOf(spec: string): string {
  const s = spec.trim();
  const left = s.startsWith(":");
  const right = s.endsWith(":");
  if (left && right) return " style=\"text-align:center\"";
  if (right) return " style=\"text-align:right\"";
  if (left) return " style=\"text-align:left\"";
  return "";
}

type ListType = "ul" | "ol";

interface SubList {
  type: ListType;
  start: number;
  items: string[];
}

// One top-level item: its finished blocks (the item's own line first, then
// any continuation paragraphs and nested lists), the paragraph still being
// written, and a nested list still taking items.
interface ListItem {
  blocks: { html: string; list: boolean }[];
  para: string | null;
  sub: SubList | null;
}

interface List {
  type: ListType;
  start: number;
  items: ListItem[];
}

// `start` is what keeps the numbering when one list is split in two. Without
// it every <ol> counts from 1, so "1. a … 2. b" broken up by an unindented
// paragraph or a fenced code block rendered as "1. a … 1. b".
function openTag(type: ListType, start: number): string {
  return type === "ol" && start !== 1 ? `<ol start="${start}">` : `<${type}>`;
}

function listHtml(type: ListType, start: number, items: string[]): string {
  return `${openTag(type, start)}${items.map((i) => `<li>${i}</li>`).join("")}</${type}>`;
}

function closeSub(item: ListItem): void {
  if (!item.sub) return;
  pushPara(item);
  item.blocks.push({ html: listHtml(item.sub.type, item.sub.start, item.sub.items), list: true });
  item.sub = null;
}

function pushPara(item: ListItem): void {
  if (item.para === null) return;
  item.blocks.push({ html: item.para, list: false });
  item.para = null;
}

function itemHtml(item: ListItem): string {
  closeSub(item);
  pushPara(item);
  // The item's own line stays bare text; later paragraphs are real <p>s so a
  // loose item ("1. Title" + a blank line + an indented description) keeps
  // its description as a separate paragraph inside the same <li>.
  return item.blocks
    .map((b, k) => (k === 0 || b.list ? b.html : `<p>${b.html}</p>`))
    .join("");
}

// Leading whitespace width, a tab counting as four columns.
function indentOf(line: string): number {
  let n = 0;
  for (const ch of line) {
    if (ch === " ") n += 1;
    else if (ch === "\t") n += 4;
    else break;
  }
  return n;
}

const UL_ITEM = /^[-*]\s+(.*)$/;
const OL_ITEM = /^(\d+)\.\s+(.*)$/;

// `block` is already HTML-escaped.
export function renderProse(block: string, files?: FileLinkIndex): string {
  const lines = block.split("\n");
  const out: string[] = [];
  let list: List | null = null;
  // A blank line inside a list does not end it: the next line decides. Another
  // item or an indented line continues the list; anything else closes it.
  let blankSinceItem = false;
  const flush = () => {
    if (list) {
      const l: List = list;
      out.push(listHtml(l.type, l.start, l.items.map(itemHtml)));
      list = null;
    }
    blankSinceItem = false;
  };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i].trimEnd();
    const heading = /^(#{1,3})\s+(.*)$/.exec(line);
    const ul = UL_ITEM.exec(line);
    const ol = OL_ITEM.exec(line);
    const quote = /^>\s?(.*)$/.exec(line);

    // GFM table: a "| … |" header followed by a "|---|---|" separator.
    if (line.trim().startsWith("|") && i + 1 < lines.length && isTableSeparator(lines[i + 1])) {
      flush();
      const headers = tableCells(line);
      const aligns = tableCells(lines[i + 1]).map(alignOf);
      const rows: string[][] = [];
      let j = i + 2;
      for (; j < lines.length && lines[j].trim().startsWith("|"); j++) {
        rows.push(tableCells(lines[j]));
      }
      const cols = headers.map((h, k) => ` class="${columnClass([h, ...rows.map((r) => r[k] ?? "")])}"`);
      const thead = `<thead><tr>${headers
        .map((h, k) => `<th${cols[k]}${aligns[k] || ""}>${inline(h, files)}</th>`)
        .join("")}</tr></thead>`;
      const tbody = `<tbody>${rows
        .map(
          (r) =>
            `<tr>${headers
              .map((_, k) => `<td${cols[k]}${aligns[k] || ""}>${inline(r[k] ?? "", files)}</td>`)
              .join("")}</tr>`,
        )
        .join("")}</tbody>`;
      out.push(`<div class="md-table-wrap"><table class="md-table">${thead}${tbody}</table></div>`);
      i = j - 1;
      continue;
    }

    if (line.trim() === "") {
      if (list) blankSinceItem = true;
      continue;
    }

    // An indented line under an open list belongs to its last item: a nested
    // bullet or number starts (or extends) a sub-list, anything else is
    // continuation text (a new paragraph after a blank line, a line break
    // otherwise).
    const current: List | null = list;
    if (current && current.items.length && indentOf(line) >= 2) {
      const item = current.items[current.items.length - 1];
      const text = line.trim();
      const subUl = UL_ITEM.exec(text);
      const subOl = OL_ITEM.exec(text);
      if (subUl || subOl) {
        const type: ListType = subUl ? "ul" : "ol";
        if (item.sub && item.sub.type !== type) closeSub(item);
        if (!item.sub) {
          pushPara(item);
          item.sub = { type, start: subOl ? Number(subOl[1]) : 1, items: [] };
        }
        item.sub.items.push(inline(subUl ? subUl[1] : subOl![2], files));
      } else if (item.sub && !blankSinceItem) {
        const last = item.sub.items.length - 1;
        item.sub.items[last] += `<br>${inline(text, files)}`;
      } else {
        closeSub(item);
        if (item.para !== null && !blankSinceItem) item.para += `<br>${inline(text, files)}`;
        else {
          pushPara(item);
          item.para = inline(text, files);
        }
      }
      blankSinceItem = false;
      continue;
    }

    if (heading) {
      flush();
      out.push(`<h${heading[1].length}>${inline(heading[2], files)}</h${heading[1].length}>`);
    } else if (ul || ol) {
      const type: ListType = ul ? "ul" : "ol";
      if (!list || list.type !== type) {
        flush();
        list = { type, start: ol ? Number(ol[1]) : 1, items: [] };
      }
      list.items.push({ blocks: [{ html: inline(ul ? ul[1] : ol![2], files), list: false }], para: null, sub: null });
      blankSinceItem = false;
    } else if (quote) {
      flush();
      out.push(`<blockquote>${inline(quote[1], files)}</blockquote>`);
    } else if (list && !blankSinceItem) {
      // A plain line straight under an item, no blank between ("1. **Name**"
      // then "Description"), is that item's text, as CommonMark reads it.
      const l: List = list;
      const item = l.items[l.items.length - 1];
      closeSub(item);
      const last = item.blocks[item.blocks.length - 1];
      if (item.para !== null) item.para += `<br>${inline(line, files)}`;
      else if (last.list) item.para = inline(line, files);
      else last.html += `<br>${inline(line, files)}`;
    } else {
      flush();
      out.push(`<p>${inline(line, files)}</p>`);
    }
  }
  flush();
  return out.join("");
}
