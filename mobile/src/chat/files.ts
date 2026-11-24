/**
 * Files an agent hands over in a reply ("Saved: reports/x.md" becomes a card
 * under the message), opened inside the app: Markdown rendered, JSON, CSV,
 * logs and code shown as highlighted text, a PDF drawn where the phone can
 * (an iPhone's web view can, Android's cannot), and anything else handed to
 * the phone's own "Open in..." sheet. The bytes come from the deployment's
 * `/api/files-raw/...` with the app's session, so nothing needs a second
 * sign-in the way the dashboard's Files page in a browser did.
 *
 * Pure, for `node --test`.
 */

export type FileView = "markdown" | "text" | "pdf" | "other";

/** Larger than this is offered to other apps only, never decoded here. */
export const MAX_OPEN_BYTES = 20 * 1024 * 1024;
/** Text past this is cut, with a note: a phone scrolls a few hundred KB well. */
export const MAX_TEXT_CHARS = 400_000;
/** Highlighting past this costs more than it helps. */
export const MAX_HIGHLIGHT_CHARS = 120_000;

/** Extension to the highlighter's language name ("" = plain text). */
const LANGUAGES: Record<string, string> = {
  json: "json",
  jsonl: "json",
  yaml: "yaml",
  yml: "yaml",
  toml: "ini",
  ini: "ini",
  cfg: "ini",
  env: "bash",
  csv: "",
  tsv: "",
  txt: "",
  log: "",
  py: "python",
  js: "javascript",
  mjs: "javascript",
  cjs: "javascript",
  jsx: "javascript",
  ts: "typescript",
  tsx: "typescript",
  sh: "bash",
  bash: "bash",
  zsh: "bash",
  html: "xml",
  htm: "xml",
  xml: "xml",
  svg: "xml",
  css: "css",
  scss: "scss",
  sql: "sql",
  go: "go",
  rs: "rust",
  java: "java",
  kt: "kotlin",
  rb: "ruby",
  php: "php",
  swift: "swift",
  c: "c",
  h: "c",
  cpp: "cpp",
  cs: "csharp",
  diff: "diff",
  patch: "diff",
};

function extension(name: string): string {
  const base = (name || "").split("/").pop() || "";
  const dot = base.lastIndexOf(".");
  return dot > 0 ? base.slice(dot + 1).toLowerCase() : "";
}

/** How a file opens, and the highlighter's language for text. */
export function fileView(name: string, mime = ""): { view: FileView; lang: string } {
  const ext = extension(name);
  const type = (mime || "").split(";")[0].trim().toLowerCase();
  if (ext === "md" || ext === "markdown" || type === "text/markdown") return { view: "markdown", lang: "" };
  if (ext === "pdf" || type === "application/pdf") return { view: "pdf", lang: "" };
  if (ext in LANGUAGES) return { view: "text", lang: LANGUAGES[ext] };
  if (
    type.startsWith("text/") ||
    type === "application/json" ||
    type === "application/xml" ||
    type === "application/x-yaml" ||
    type === "application/javascript"
  ) {
    return { view: "text", lang: type === "application/json" ? "json" : "" };
  }
  return { view: "other", lang: "" };
}

/** "820 B", "12 KB", "3.4 MB". */
export function formatSize(bytes: number | undefined): string {
  if (!bytes || bytes < 0) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** JSON laid out to read; anything that does not parse comes back as it was. */
export function readableText(text: string, lang: string): string {
  if (lang !== "json") return text;
  try {
    return JSON.stringify(JSON.parse(text), null, 2);
  } catch {
    return text;
  }
}

/** Text cut to what the viewer shows, and whether it was cut. */
export function clipText(text: string, max = MAX_TEXT_CHARS): { text: string; clipped: boolean } {
  return text.length > max ? { text: text.slice(0, max), clipped: true } : { text, clipped: false };
}

/** A name safe to write to the phone's cache for sharing: no folders, nothing odd. */
export function shareName(name: string): string {
  const base = (name || "").split(/[\\/]/).pop() || "";
  const clean = base.replace(/[^\w.\- ]+/g, "_").replace(/^\.+/, "").trim();
  return clean || "file";
}

/** Base64 to text, as UTF-8 (the deployment writes text files as UTF-8). */
export function decodeText(base64: string): string {
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return new TextDecoder("utf-8").decode(bytes);
}
