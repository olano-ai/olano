import { memo, useEffect, useMemo, useState, type MouseEvent } from "react";
import { CheckIcon, CopyIcon, PlayIcon } from "./icons";
import { escapeHtml, highlight, highlighterReady, loadHighlighter } from "./highlight";
import { renderProse } from "./markdownProse";
import { useT, type LocalDict } from "../core/i18n";
import type { FileLinkIndex } from "../views/chat/fileLinks";

// Component-local strings for the code-block header controls (no view owns
// this component, so it carries its own dict rather than living in a
// shared strings.ts — mirrors ui/ErrorState.tsx).
const STR: LocalDict = {
  en: {
    "run.title":
      "Run this command in the agent's workspace; its output is buffered and sent to the agent with your next message",
    "run.running": "Running…",
    "run.now": "Run Now",
    "copy.title": "Copy to clipboard",
    "copy.copied": "Copied",
    "copy.copy": "Copy",
  },
  zh: {
    "run.title": "在智能体的工作区中运行此命令；其输出会被缓存，并随你的下一条消息发送给智能体",
    "run.running": "运行中…",
    "run.now": "立即运行",
    "copy.title": "复制到剪贴板",
    "copy.copied": "已复制",
    "copy.copy": "复制",
  },
  es: {
    "run.title":
      "Ejecuta este comando en el espacio de trabajo del agente; su salida se almacena en búfer y se envía al agente con tu próximo mensaje",
    "run.running": "Ejecutando…",
    "run.now": "Ejecutar ahora",
    "copy.title": "Copiar al portapapeles",
    "copy.copied": "Copiado",
    "copy.copy": "Copiar",
  },
};

// Dependency-light, XSS-safe markdown. Prose is HTML-escaped then lightly
// formatted; fenced code is rendered as a real React <CodeBlock> (never raw
// innerHTML of model text) with a language header, copy button and highlight.js
// syntax colouring. Streaming-safe: an unclosed ``` renders as a code block in
// progress. Markdown image syntax `![alt](url)` renders inline media
// (image / audio / video) when the URL scheme is safe, or when the URL is a
// workspace path the host indexed (`files` — see views/chat/fileLinks.ts),
// in which case bare mentions of those files become Files-tab links too.

// Languages whose code blocks are runnable shell commands — the inline "Run Now"
// button only appears for these (and only when the host passes an onRun handler).
const SHELL_LANGS = new Set([
  "bash",
  "sh",
  "shell",
  "zsh",
  "console",
  "shell-session",
  "shellsession",
  "terminal",
]);

function isShellLang(lang: string): boolean {
  return SHELL_LANGS.has(lang.toLowerCase());
}

// Turn the fenced code into the command(s) to run. Prompt-style transcripts
// (console / shell-session) interleave commands with output, so keep only the
// `$`/`#` prompt lines; plain bash/sh blocks run verbatim (minus a leading `$`).
function extractCommand(lang: string, code: string): string {
  const l = lang.toLowerCase();
  if (l === "console" || l === "shell-session" || l === "shellsession" || l === "terminal") {
    const cmds = code
      .split("\n")
      .map((ln) => /^\s*[$#]\s+(.*)$/.exec(ln))
      .filter((m): m is RegExpExecArray => m !== null)
      .map((m) => m[1]);
    if (cmds.length) return cmds.join("\n").trim();
  }
  return code.replace(/^\s*\$\s+/, "").trim();
}

const CodeBlock = memo(function CodeBlock({
  raw,
  onRun,
}: {
  raw: string;
  onRun?: (cmd: string) => void | Promise<void>;
}) {
  const t = useT(STR);
  const [copied, setCopied] = useState(false);
  const [running, setRunning] = useState(false);
  // The highlighter chunk loads lazily on the first code block; until then the
  // block renders as escaped plaintext and re-highlights when the chunk lands.
  const [hlReady, setHlReady] = useState(highlighterReady);
  useEffect(() => {
    if (hlReady) return;
    let alive = true;
    void loadHighlighter().then(() => {
      if (alive && highlighterReady()) setHlReady(true);
    });
    return () => {
      alive = false;
    };
  }, [hlReady]);
  const nl = raw.indexOf("\n");
  const first = (nl >= 0 ? raw.slice(0, nl) : raw).trim();
  const hasLang = nl >= 0 && first.length > 0 && first.length < 16 && /^[\w+#.-]+$/.test(first);
  const lang = hasLang ? first : "";
  const code = (hasLang ? raw.slice(nl + 1) : raw).replace(/\n$/, "");
  const runnable = !!onRun && isShellLang(lang);

  function copy() {
    void navigator.clipboard?.writeText(code);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1200);
  }

  async function run() {
    if (!onRun || running) return;
    const cmd = extractCommand(lang, code);
    if (!cmd) return;
    setRunning(true);
    try {
      await onRun(cmd);
    } finally {
      setRunning(false);
    }
  }

  // highlight.js escapes the source and only emits hljs-* span tags, so the
  // output is safe to inject as innerHTML. Memoized: tokenizing is pure in
  // (code, lang), so chat re-renders during streaming don't re-tokenize
  // finished blocks.
  const html = useMemo(() => highlight(code, lang), [code, lang, hlReady]);

  return (
    <div className="code-block">
      <div className="code-head">
        <span>{lang || "code"}</span>
        <span className="code-actions">
          {runnable && (
            <button
              className="code-run"
              onClick={() => void run()}
              type="button"
              disabled={running}
              title={t("run.title")}
            >
              {running ? <span className="code-run-spin" aria-hidden /> : <PlayIcon size={12} />}
              {running ? t("run.running") : t("run.now")}
            </button>
          )}
          <button className="code-copy" onClick={copy} type="button" title={t("copy.title")}>
            {copied ? <CheckIcon size={13} /> : <CopyIcon size={13} />}
            {copied ? t("copy.copied") : t("copy.copy")}
          </button>
        </span>
      </div>
      <pre>
        <code
          className={`hljs${lang ? ` language-${lang}` : ""}`}
          dangerouslySetInnerHTML={{ __html: html }}
        />
      </pre>
    </div>
  );
});

// Memoized: a chat re-render during streaming touches every message, but only
// the streaming message's `text` changes — every other Markdown bails out here
// instead of re-running the regex passes. Hosts must pass a stable
// onRunCommand (useCallback) and a per-message-stable `files` index
// (fileIndexFor) or the memo is defeated; a NEW index is exactly what should
// re-render, since it is what turns the mentions into links. (No inner
// useMemo: this component has no state/context, so the body only runs when a
// prop changed — a cache keyed on exactly those props could never hit.)
export const Markdown = memo(function Markdown({
  text,
  onRunCommand,
  files,
  onNavigate,
}: {
  text: string;
  // When provided, runnable shell code blocks render a "Run Now" button that
  // calls this with the extracted command (see ChatView → inline shell run).
  onRunCommand?: (cmd: string) => void | Promise<void>;
  // Workspace files the text names (views/chat/fileLinks.ts): mentions become
  // Files-tab links, `![alt](path)` draws the file. Compared by identity.
  files?: FileLinkIndex;
  // Where an internal (`data-internal`) link goes on a plain click — the
  // host's router navigate. Modifier and non-primary clicks are left to the
  // browser (open in a new tab still works).
  onNavigate?: (href: string) => void;
}) {
  const segments = text.split("```");
  // Delegated: the anchors live in innerHTML, so the root listens for them.
  const onClick = onNavigate
    ? (e: MouseEvent<HTMLDivElement>) => {
        if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
        const a = (e.target as Element | null)?.closest?.("a[data-internal]");
        if (!(a instanceof HTMLAnchorElement) || !e.currentTarget.contains(a)) return;
        const href = a.getAttribute("href");
        if (!href) return;
        e.preventDefault();
        onNavigate(href);
      }
    : undefined;
  return (
    <div className="md" onClick={onClick}>
      {segments.map((seg, i) =>
        i % 2 === 1 ? (
          <CodeBlock key={i} raw={seg} onRun={onRunCommand} />
        ) : (
          <span key={i} dangerouslySetInnerHTML={{ __html: renderProse(escapeHtml(seg), files) }} />
        ),
      )}
    </div>
  );
});
