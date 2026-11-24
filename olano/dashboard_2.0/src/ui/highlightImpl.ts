// Syntax highlighting via highlight.js core + a curated language set, so the
// bundle stays lean (the full auto-registering build is ~1 MB). Tokens are
// emitted as `hljs-*` classes and themed in globals.css against our CSS vars,
// so highlighting respects the light/dark theme without importing a hljs CSS.
//
// This module is only ever loaded through the dynamic import in highlight.ts —
// keeping hljs and its grammars out of the initial bundle entirely.

import hljs from "highlight.js/lib/core";
import { escapeHtml } from "./highlight";

import bash from "highlight.js/lib/languages/bash";
import c from "highlight.js/lib/languages/c";
import cpp from "highlight.js/lib/languages/cpp";
import css from "highlight.js/lib/languages/css";
import diff from "highlight.js/lib/languages/diff";
import dockerfile from "highlight.js/lib/languages/dockerfile";
import go from "highlight.js/lib/languages/go";
import ini from "highlight.js/lib/languages/ini";
import java from "highlight.js/lib/languages/java";
import javascript from "highlight.js/lib/languages/javascript";
import json from "highlight.js/lib/languages/json";
import markdown from "highlight.js/lib/languages/markdown";
import python from "highlight.js/lib/languages/python";
import ruby from "highlight.js/lib/languages/ruby";
import rust from "highlight.js/lib/languages/rust";
import sql from "highlight.js/lib/languages/sql";
import typescript from "highlight.js/lib/languages/typescript";
import xml from "highlight.js/lib/languages/xml";
import yaml from "highlight.js/lib/languages/yaml";

let registered = false;

function register() {
  if (registered) return;
  registered = true;
  hljs.registerLanguage("bash", bash);
  hljs.registerLanguage("c", c);
  hljs.registerLanguage("cpp", cpp);
  hljs.registerLanguage("css", css);
  hljs.registerLanguage("diff", diff);
  hljs.registerLanguage("dockerfile", dockerfile);
  hljs.registerLanguage("go", go);
  hljs.registerLanguage("ini", ini);
  hljs.registerLanguage("java", java);
  hljs.registerLanguage("javascript", javascript);
  hljs.registerLanguage("json", json);
  hljs.registerLanguage("markdown", markdown);
  hljs.registerLanguage("python", python);
  hljs.registerLanguage("ruby", ruby);
  hljs.registerLanguage("rust", rust);
  hljs.registerLanguage("sql", sql);
  hljs.registerLanguage("typescript", typescript);
  hljs.registerLanguage("xml", xml);
  hljs.registerLanguage("yaml", yaml);
  // Common aliases the model emits in fences.
  hljs.registerLanguage("html", xml);
  hljs.registerLanguage("shell", bash);
  hljs.registerLanguage("sh", bash);
  hljs.registerLanguage("zsh", bash);
  hljs.registerLanguage("js", javascript);
  hljs.registerLanguage("jsx", javascript);
  hljs.registerLanguage("ts", typescript);
  hljs.registerLanguage("tsx", typescript);
  hljs.registerLanguage("py", python);
  hljs.registerLanguage("yml", yaml);
  hljs.registerLanguage("toml", ini);
}

// Returns highlighted HTML (hljs-* spans). `lang` is the fence language; when
// unknown or absent we fall back to plaintext (escaped, no highlighting) — we
// avoid auto-detection because it's slow and noisy on short streamed snippets.
export function highlight(code: string, lang: string): string {
  register();
  const normalized = lang.toLowerCase();
  if (normalized && hljs.getLanguage(normalized)) {
    try {
      return hljs.highlight(code, { language: normalized, ignoreIllegals: true }).value;
    } catch {
      // fall through to escaped plaintext
    }
  }
  return escapeHtml(code);
}
