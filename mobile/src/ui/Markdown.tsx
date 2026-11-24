/**
 * A reply's Markdown, rendered by the dashboard's own renderer
 * (`renderProse`) so a reply reads the same in the app and in the dashboard.
 * The renderer formats text that is already escaped (the dashboard escapes
 * first, and so does `proseHtml`): markup a reply contains is shown as text,
 * never run. Addresses the renderer leaves as text (a URL in code
 * formatting, a `www.` address) become links first (chat/links.ts). Code
 * blocks get the dashboard's highlighter and a copy button. Links open in the
 * in-app browser, never inside the app's own web view.
 */

import { Browser } from "@capacitor/browser";
import { Clipboard } from "./clipboard.ts";
import { memo, useEffect, useMemo, useState, type MouseEvent } from "react";

import { proseHtml } from "../chat/links.ts";
import { escapeHtml, highlight, highlighterReady, loadHighlighter } from "../shared/dashboard.ts";
import { isNative } from "../native/shell.ts";
import { session } from "../state/session.ts";

function CodeBlock({ raw }: { raw: string }) {
  const nl = raw.indexOf("\n");
  const first = (nl >= 0 ? raw.slice(0, nl) : raw).trim();
  const hasLang = nl >= 0 && first.length > 0 && first.length < 16 && /^[\w+#.-]+$/.test(first);
  const lang = hasLang ? first : "";
  const code = (hasLang ? raw.slice(nl + 1) : raw).replace(/\n$/, "");
  return <CodeView code={code} lang={lang} />;
}

/**
 * Code with the dashboard's highlighter and a copy button: a block in a
 * reply, or a whole file opened from one. `plain` skips the highlighter for
 * text too long for it to be worth the wait.
 */
export function CodeView({ code, lang, label, plain = false }: { code: string; lang: string; label?: string; plain?: boolean }) {
  const [ready, setReady] = useState(highlighterReady);
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (ready || plain) return;
    let alive = true;
    void loadHighlighter().then(() => alive && highlighterReady() && setReady(true));
    return () => {
      alive = false;
    };
  }, [ready, plain]);
  const html = useMemo(() => (plain ? escapeHtml(code) : highlight(code, lang)), [code, lang, ready, plain]);
  return (
    <div className="olano-code">
      <div className="olano-code-head">
        <span>{label || lang || "code"}</span>
        <button
          type="button"
          onClick={() => {
            void Clipboard.write(code).then(() => {
              setCopied(true);
              window.setTimeout(() => setCopied(false), 1200);
            });
          }}
        >
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
      <pre>
        <code className={`hljs${lang ? ` language-${lang}` : ""}`} dangerouslySetInnerHTML={{ __html: html }} />
      </pre>
    </div>
  );
}

/**
 * Opens a link from a reply: web pages in the in-app browser (Safari's on an
 * iPhone, Chrome's Custom Tab on Android), deployment paths against the
 * deployment, mail and phone links in their apps. Never inside the app's own
 * web view. If the in-app browser cannot open, the phone's browser does.
 */
export async function openLink(href: string): Promise<void> {
  let url: URL;
  try {
    const origin = session.get().origin ?? location.origin;
    url = new URL(href, origin);
  } catch {
    return;
  }
  if (url.protocol !== "https:" && url.protocol !== "http:" && url.protocol !== "mailto:" && url.protocol !== "tel:") return;
  if (isNative() && (url.protocol === "https:" || url.protocol === "http:")) {
    try {
      await Browser.open({ url: url.toString(), presentationStyle: "fullscreen" });
      return;
    } catch {
      // Fall through to the system's own handling.
    }
  }
  window.open(url.toString(), "_blank", "noopener,noreferrer");
}

export const Markdown = memo(function Markdown({ text }: { text: string }) {
  const parts = useMemo(() => text.split("```"), [text]);
  const onClick = (event: MouseEvent<HTMLDivElement>) => {
    const anchor = (event.target as Element | null)?.closest?.("a");
    if (!(anchor instanceof HTMLAnchorElement) || !event.currentTarget.contains(anchor)) return;
    event.preventDefault();
    const href = anchor.getAttribute("href");
    if (href) void openLink(href);
  };
  return (
    <div className="olano-md" onClick={onClick}>
      {parts.map((part, i) =>
        i % 2 === 1 ? (
          <CodeBlock key={i} raw={part} />
        ) : part.trim() ? (
          <div key={i} dangerouslySetInnerHTML={{ __html: proseHtml(part) }} />
        ) : null,
      )}
    </div>
  );
});
