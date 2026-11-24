/**
 * An agent's files: the folder paths the Files screens move between, what a
 * row opens as, and the address its bytes are read from. Pure, so node's test
 * runner can import it.
 */

import type { WorkspaceEntry } from "../net/types.ts";

/** Searching starts at this many characters (the deployment's own minimum). */
export const SEARCH_MIN = 2;

/** Folders first, then files, each by name as a person reads them. */
export function sortEntries<T extends Pick<WorkspaceEntry, "name" | "type">>(entries: T[]): T[] {
  const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" });
  return [...entries].sort((a, b) => {
    if (a.type !== b.type) return a.type === "directory" ? -1 : 1;
    return collator.compare(a.name, b.name);
  });
}

/** `folder` + `name`, with no leading, trailing or doubled slashes. */
export function joinPath(folder: string, name: string): string {
  return [...splitPath(folder), ...splitPath(name)].join("/");
}

export function splitPath(path: string): string[] {
  return (path || "").split("/").filter((part) => part && part !== ".");
}

/** The last part of a path: the folder's own name ("" at the top). */
export function baseName(path: string): string {
  const parts = splitPath(path);
  return parts[parts.length - 1] ?? "";
}

/** The folder a path sits in ("" at the top). */
export function parentOf(path: string): string {
  return splitPath(path).slice(0, -1).join("/");
}

/**
 * A folder's screen: the whole folder path is one route segment, slashes and
 * all encoded, so every folder is its own page (back climbs one at a time).
 */
export function folderRoute(base: string, path: string): string {
  const clean = splitPath(path).join("/");
  return clean ? `${base}/${encodeURIComponent(clean)}` : base;
}

/** Where a file's bytes are read from (the dashboard's Files view reads the same). */
export function rawUrl(agent: string, path: string): string {
  const encoded = splitPath(path).map(encodeURIComponent).join("/");
  return `/api/files-raw/${encoded}?agent=${encodeURIComponent(agent)}`;
}

const IMAGE = new Set(["png", "jpg", "jpeg", "gif", "webp", "heic", "heif", "bmp", "svg"]);
const AUDIO = new Set(["mp3", "m4a", "wav", "ogg", "oga", "opus", "aac", "flac", "webm"]);
const VIDEO = new Set(["mp4", "mov", "m4v"]);
const DOCUMENT = new Set(["pdf", "md", "markdown", "txt", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "csv", "json"]);

/** What a file is, by its name: a photo opens in the photo viewer, the rest in the file viewer. */
export function fileKind(name: string): "image" | "audio" | "video" | "document" | "file" {
  const dot = name.lastIndexOf(".");
  const ext = dot > 0 ? name.slice(dot + 1).toLowerCase() : "";
  if (IMAGE.has(ext)) return "image";
  if (AUDIO.has(ext)) return "audio";
  if (VIDEO.has(ext)) return "video";
  if (DOCUMENT.has(ext)) return "document";
  return "file";
}

/** A free name beside `taken`: "notes.md" becomes "notes 2.md", then "notes 3.md". */
export function freeName(name: string, taken: Iterable<string>): string {
  const used = new Set(taken);
  if (!used.has(name)) return name;
  const dot = name.lastIndexOf(".");
  const stem = dot > 0 ? name.slice(0, dot) : name;
  const ext = dot > 0 ? name.slice(dot) : "";
  for (let n = 2; ; n++) {
    const candidate = `${stem} ${n}${ext}`;
    if (!used.has(candidate)) return candidate;
  }
}
