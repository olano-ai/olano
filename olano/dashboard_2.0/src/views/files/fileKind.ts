// What the Files preview does with a file, decided from its name and, for
// anything that names no known format, from the bytes the server read.
//
// Pure (no React, no store) so node's `--test` can import it.

export type FileKind = "image" | "video" | "audio" | "pdf" | "binary" | "markdown" | "code" | "text";

export interface FileCategory {
  kind: FileKind;
  /** Highlight-language label for code files, otherwise undefined. */
  lang?: string;
}

export const TEXT_KINDS: ReadonlySet<FileKind> = new Set<FileKind>(["markdown", "code", "text"]);

/** Kinds whose preview fills the pane rather than flowing down it. */
export const FILL_KINDS: ReadonlySet<FileKind> = new Set<FileKind>(["image", "pdf", "video", "audio"]);

// ── extension → category maps ────────────────────────────────────────────
const IMAGE_EXTS = new Set(["png", "jpg", "jpeg", "jfif", "gif", "apng", "webp", "svg", "avif", "bmp", "ico"]);
const VIDEO_EXTS = new Set(["mp4", "webm", "mov", "m4v", "ogv"]);
const AUDIO_EXTS = new Set(["mp3", "wav", "ogg", "oga", "m4a", "aac", "flac", "opus", "weba"]);
const PDF_EXTS = new Set(["pdf"]);
const MD_EXTS = new Set(["md", "markdown"]);
/** Formats with no inline renderer here: archives, Office documents,
 *  executables, databases, fonts, model weights, and media a browser cannot
 *  draw. Opening one never reads it; the pane offers the download. Anything
 *  not listed is still read and sniffed (see `looksBinary`), so this list is a
 *  shortcut, not the only gate. */
const BINARY_EXTS = new Set([
  // archives and packages
  "zip", "gz", "tgz", "bz2", "xz", "zst", "7z", "rar", "tar", "jar", "war", "whl", "egg",
  "apk", "deb", "rpm", "dmg", "iso", "msi", "pkg",
  // office and e-books
  "doc", "docx", "xls", "xlsx", "xlsm", "ppt", "pptx", "odt", "ods", "odp", "pages", "numbers", "key", "epub",
  // executables and objects
  "exe", "dll", "so", "dylib", "bin", "o", "a", "class", "pyc", "pyo", "wasm",
  // data stores and serialized arrays
  "db", "sqlite", "sqlite3", "parquet", "feather", "arrow", "pkl", "pickle", "npy", "npz", "h5", "hdf5",
  "onnx", "pt", "pth", "safetensors", "gguf",
  // fonts
  "ttf", "otf", "woff", "woff2", "eot",
  // media no browser draws inline
  "psd", "ai", "tif", "tiff", "heic", "heif", "raw", "cr2", "nef", "avi", "wmv", "flv", "mkv", "mid", "midi",
]);
const CODE_LANG_BY_EXT: Record<string, string> = {
  js: "javascript",
  mjs: "javascript",
  cjs: "javascript",
  jsx: "javascript",
  ts: "typescript",
  tsx: "typescript",
  py: "python",
  rb: "ruby",
  go: "go",
  rs: "rust",
  java: "java",
  kt: "kotlin",
  swift: "swift",
  c: "c",
  h: "c",
  cpp: "cpp",
  cc: "cpp",
  hpp: "cpp",
  cs: "csharp",
  php: "php",
  sh: "bash",
  bash: "bash",
  zsh: "bash",
  fish: "bash",
  ps1: "powershell",
  sql: "sql",
  html: "xml",
  htm: "xml",
  xml: "xml",
  yaml: "yaml",
  yml: "yaml",
  toml: "ini",
  ini: "ini",
  cfg: "ini",
  json: "json",
  jsonl: "json",
  json5: "json",
  css: "css",
  scss: "scss",
  sass: "scss",
  less: "less",
  dockerfile: "dockerfile",
  makefile: "makefile",
  mk: "makefile",
  diff: "diff",
  patch: "diff",
  graphql: "graphql",
  gql: "graphql",
  env: "bash",
  conf: "ini",
};

// ── helpers ──────────────────────────────────────────────────────────────
export function extOf(name: string): string {
  const parts = name.split(".");
  return parts.length > 1 ? (parts.pop() ?? "").toLowerCase() : "";
}

/** Pick the renderer for a file. Falls back to basename matches for
 *  Dockerfile / Makefile / .env so those extensionless files still highlight. */
export function categorize(name: string): FileCategory {
  const ext = extOf(name);
  if (IMAGE_EXTS.has(ext)) return { kind: "image" };
  if (VIDEO_EXTS.has(ext)) return { kind: "video" };
  if (AUDIO_EXTS.has(ext)) return { kind: "audio" };
  if (PDF_EXTS.has(ext)) return { kind: "pdf" };
  if (BINARY_EXTS.has(ext)) return { kind: "binary" };
  if (MD_EXTS.has(ext)) return { kind: "markdown" };
  if (CODE_LANG_BY_EXT[ext]) return { kind: "code", lang: CODE_LANG_BY_EXT[ext] };
  const base = name.toLowerCase();
  if (base === "dockerfile") return { kind: "code", lang: "dockerfile" };
  if (base === "makefile") return { kind: "code", lang: "makefile" };
  if (base.startsWith(".env")) return { kind: "code", lang: "bash" };
  return { kind: "text" };
}

/** Client-side twin of the runtime's `looks_binary`, for a runtime that
 *  predates the `binary` flag. The server decoded with `errors="replace"`, so
 *  undecodable bytes arrive as U+FFFD; a NUL arrives as itself. */
export function looksBinary(text: string): boolean {
  const sample = text.slice(0, 8192);
  if (!sample) return false;
  if (sample.includes("\u0000")) return true;
  let junk = 0;
  // The last char may be the seam of a head cut inside a multibyte character.
  for (let i = 0; i < sample.length - 1; i++) {
    const c = sample.charCodeAt(i);
    if (c === 0xfffd || (c < 0x20 && c !== 9 && c !== 10 && c !== 13 && c !== 12 && c !== 8 && c !== 0x1b)) junk++;
  }
  return junk > sample.length * 0.1;
}
