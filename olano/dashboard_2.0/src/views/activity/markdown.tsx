import { useEffect, useMemo, useRef, type CSSProperties } from "react";
import { useT } from "../../core/i18n";
import { STR } from "./strings";

// User-facing labels for the per-code-block Copy button. Defaults keep
// renderMarkdown usable outside React; the Markdown component passes localized
// values from useT.
interface CopyLabels {
  copy: string;
  copied: string;
  failed: string;
}
const DEFAULT_LABELS: CopyLabels = { copy: "Copy", copied: "Copied", failed: "Failed" };

// ──────────────────────────────────────────────────────────────────────────
// Faithful port of the original dashboard's `renderMarkdown` (index.html).
// It supports headings (h1-h3), unordered/ordered lists, GFM tables, fenced
// code blocks (with a Copy button), inline code, **bold**, *italic*, and
// [text](https://link) — exactly the subset the A2A conversation bubbles use.
//
// We emit a SAFE HTML string (everything is escaped, links are forced to
// https + noopener) and inject it via dangerouslySetInnerHTML, mirroring the
// original which did the same. A small wrapper component wires up the
// per-code-block Copy buttons after render.
// ──────────────────────────────────────────────────────────────────────────

function escapeHtml(s = ""): string {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function transformSafe(s: string): string {
  let t = escapeHtml(s);
  t = t.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  t = t.replace(/(?<!\*)\*([^*]+)\*(?!\*)/g, "<em>$1</em>");
  t = t.replace(
    /\[([^\]]+)\]\((https?:[^)]+)\)/g,
    '<a href="$2" target="_blank" rel="noopener">$1</a>',
  );
  return t;
}

function inline(s: string): string {
  // Process code spans first so their contents aren't transformed.
  let r = "";
  let rest = s;
  while (rest.length) {
    const m = /`([^`]+)`/.exec(rest);
    if (!m) {
      r += transformSafe(rest);
      break;
    }
    r += transformSafe(rest.slice(0, m.index));
    r += `<code>${escapeHtml(m[1])}</code>`;
    rest = rest.slice(m.index + m[0].length);
  }
  return r;
}

export function renderMarkdown(text: string, copyLabel: string = DEFAULT_LABELS.copy): string {
  if (!text) return "";
  // Normalise literal <br> tags — LLMs sometimes emit them inline instead of
  // real newlines, which would break line-based parsing.
  const src = text.replace(/<br\s*\/?>/gi, "\n");

  let out = "";
  const lines = src.split("\n");
  let i = 0;
  let inCode = false;
  let codeLang = "";
  let codeBuf: string[] = [];
  let inList: "ul" | "ol" | null = null;
  let tableRows: string[][] = [];

  const flushList = (): void => {
    if (inList) {
      out += `</${inList}>`;
      inList = null;
    }
  };

  const parseTableRow = (line: string): string[] => {
    const parts = line.split("|");
    return parts.slice(1, parts.length - 1);
  };

  const flushTable = (): void => {
    if (!tableRows.length) return;
    const sepIdx = tableRows.findIndex((r) => r.every((c) => /^[\s:\-]+$/.test(c)));
    let html = "<table>";
    const hasHead = sepIdx > 0;
    if (hasHead) {
      html += "<thead>";
      for (let r = 0; r < sepIdx; r++) {
        html += "<tr>" + tableRows[r].map((c) => `<th>${inline(c.trim())}</th>`).join("") + "</tr>";
      }
      html += "</thead>";
    }
    html += "<tbody>";
    const bodyStart = hasHead ? sepIdx + 1 : 0;
    for (let r = bodyStart; r < tableRows.length; r++) {
      html += "<tr>" + tableRows[r].map((c) => `<td>${inline(c.trim())}</td>`).join("") + "</tr>";
    }
    html += "</tbody></table>";
    // Wrap in a horizontally-scrollable container so a wide table scrolls
    // inside the bubble instead of overflowing the conversation card.
    out += `<div class="md-table-wrap">${html}</div>`;
    tableRows = [];
  };

  while (i < lines.length) {
    const line = lines[i];
    if (line.startsWith("```")) {
      if (!inCode) {
        flushList();
        flushTable();
        inCode = true;
        codeLang = line.slice(3).trim();
        codeBuf = [];
      } else {
        const code = escapeHtml(codeBuf.join("\n"));
        out +=
          `<pre><button class="code-copy" type="button">${escapeHtml(copyLabel)}</button>` +
          `<code class="lang-${escapeHtml(codeLang)}">${code}</code></pre>`;
        inCode = false;
        codeBuf = [];
      }
      i++;
      continue;
    }

    if (inCode) {
      codeBuf.push(line);
      i++;
      continue;
    }

    // GFM table rows — lines that start with | (optional leading whitespace).
    if (/^\s*\|/.test(line)) {
      flushList();
      tableRows.push(parseTableRow(line));
      i++;
      continue;
    }

    // Any non-table line flushes a pending table block.
    flushTable();

    const h = /^(#{1,3})\s+(.*)/.exec(line);
    if (h) {
      flushList();
      out += `<h${h[1].length}>${inline(h[2])}</h${h[1].length}>`;
      i++;
      continue;
    }

    const ul = /^[-*]\s+(.*)/.exec(line);
    const ol = /^(\d+)\.\s+(.*)/.exec(line);
    if (ul) {
      if (inList !== "ul") {
        flushList();
        out += "<ul>";
        inList = "ul";
      }
      out += `<li>${inline(ul[1])}</li>`;
      i++;
      continue;
    }
    if (ol) {
      if (inList !== "ol") {
        flushList();
        // Keep the item's number: a list split by a blank line or a
        // paragraph would otherwise restart at 1 in every new <ol>.
        const start = Number(ol[1]);
        out += start !== 1 ? `<ol start="${start}">` : "<ol>";
        inList = "ol";
      }
      out += `<li>${inline(ol[2])}</li>`;
      i++;
      continue;
    }

    flushList();
    if (line.trim() === "") {
      i++;
      continue;
    }
    // Accumulate a paragraph.
    const para = [line];
    while (
      i + 1 < lines.length &&
      lines[i + 1].trim() !== "" &&
      !/^\s*\|/.test(lines[i + 1]) &&
      !lines[i + 1].startsWith("```") &&
      !/^#{1,3}\s+/.test(lines[i + 1]) &&
      !/^[-*]\s+/.test(lines[i + 1]) &&
      !/^\d+\.\s+/.test(lines[i + 1])
    ) {
      i++;
      para.push(lines[i]);
    }
    out += `<p>${inline(para.join("\n").replace(/\n/g, "<br>"))}</p>`;
    i++;
  }

  flushList();
  flushTable();
  if (inCode) out += `<pre><code>${escapeHtml(codeBuf.join("\n"))}</code></pre>`;
  return out;
}

// Renders markdown HTML and wires Copy buttons on fenced code blocks. The
// `style` is applied to the wrapping <div> so callers can pass the bubble
// body's per-color border / background tokens.
export function Markdown({ text, style }: { text: string; style?: CSSProperties }) {
  const t = useT(STR);
  const ref = useRef<HTMLDivElement>(null);
  const labels: CopyLabels = {
    copy: t("code.copy"),
    copied: t("code.copied"),
    failed: t("code.failed"),
  };
  // Parsing is pure in (text, copy label) — memoize so a re-render of the
  // bubble (e.g. its sibling event got a reply) doesn't re-run the regexes,
  // and the unchanged `html` also keeps the copy-button effect from rewiring.
  const html = useMemo(() => renderMarkdown(text, labels.copy), [text, labels.copy]);

  useEffect(() => {
    const root = ref.current;
    if (!root) return;
    const buttons = Array.from(root.querySelectorAll<HTMLButtonElement>("button.code-copy"));
    const cleanups: Array<() => void> = [];
    for (const btn of buttons) {
      const handler = (): void => {
        const pre = btn.parentElement;
        if (!pre) return;
        const code = pre.querySelector("code")?.textContent ?? "";
        void navigator.clipboard
          .writeText(code)
          .then(() => {
            btn.textContent = labels.copied;
            window.setTimeout(() => {
              btn.textContent = labels.copy;
            }, 1200);
          })
          .catch(() => {
            btn.textContent = labels.failed;
            window.setTimeout(() => {
              btn.textContent = labels.copy;
            }, 1200);
          });
      };
      btn.addEventListener("click", handler);
      cleanups.push(() => btn.removeEventListener("click", handler));
    }
    return () => {
      for (const c of cleanups) c();
    };
  }, [html, labels.copy, labels.copied, labels.failed]);

  return <div ref={ref} className="md-body" style={style} dangerouslySetInnerHTML={{ __html: html }} />;
}
