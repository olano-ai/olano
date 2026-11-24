// Lazy facade over the real highlighter (highlightImpl.ts — hljs core + a
// curated grammar set, ~230 KB of source). The impl chunk is fetched via
// dynamic import the first time a code block renders, keeping hljs out of the
// initial bundle. Until it lands, highlight() returns escaped plaintext;
// CodeBlock kicks off loadHighlighter() and re-renders when it resolves.

type HighlightFn = (code: string, lang: string) => string;

let impl: HighlightFn | null = null;
let loading: Promise<void> | null = null;

export function highlighterReady(): boolean {
  return impl !== null;
}

// Idempotent: starts the dynamic import on first call; resolves when the
// highlighter is usable. A failed chunk fetch (offline, deploy skew) clears the
// promise so a later code block can retry.
export function loadHighlighter(): Promise<void> {
  if (!loading) {
    loading = import("./highlightImpl")
      .then((m) => {
        impl = m.highlight;
      })
      .catch(() => {
        loading = null;
      });
  }
  return loading;
}

// Highlighted HTML (hljs-* spans) once the impl is loaded; escaped plaintext
// before that. Both outputs are safe to inject as innerHTML.
export function highlight(code: string, lang: string): string {
  return impl ? impl(code, lang) : escapeHtml(code);
}

// The one escapeHtml. Exported because its output is XSS-load-bearing
// (dangerouslySetInnerHTML consumers) — one copy means a hardening change
// can't miss a private duplicate.
export function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}
