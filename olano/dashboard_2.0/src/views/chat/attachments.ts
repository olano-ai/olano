// Attachment ingestion for the chat composer. Mirrors the old dashboard's
// `ingestAttachment()` pipeline: classify a File as image / audio / video /
// text / document, read its bytes (base64 for media and documents, UTF-8 for
// text), and stage a preview. The wire format sent to /api/chat/stream strips
// client-only fields.
//
// "document" is every other file (PDF, Word, Excel, PowerPoint, a zip, ...).
// The backend saves it into the agent's workspace `uploads/` and, for the
// formats it can read, extracts the text inline: the same contract as a file
// sent on a messaging channel (`runtime/chat_attachments.py`). Nothing is
// refused for its type; only the size cap applies.

export type AttachmentKind = "image" | "audio" | "video" | "file" | "document";

export interface Attachment {
  id: string;
  name: string;
  mime: string;
  size: number;
  kind: AttachmentKind;
  // Media (image/audio/video) and documents: base64 of the raw bytes. Text
  // files: UTF-8 text.
  data: string;
  // Object/data URL for inline media display; null for text files.
  preview: string | null;
}

// Wire item accepted by the backend's `_build_multimodal_content`.
export interface WireAttachment {
  kind: AttachmentKind;
  name: string;
  mime: string;
  data: string;
}

export const MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024; // 10 MB

// Text larger than this is saved to the workspace like a document instead of
// pasted into the message: the backend inlines at most 60k characters, so a
// bigger log or CSV would reach the agent cut off. Saved, it reads all of it.
export const INLINE_TEXT_MAX_BYTES = 60_000;

// How much of a file the text sniff reads (mirrors `olano/uploads.py`).
export const TEXT_SNIFF_BYTES = 8192;

const TEXT_LIKE_MIMES =
  /^(text\/|application\/(json|xml|x-yaml|yaml|x-toml|x-sh|javascript))/i;
// A hint only: a name here outranks a media MIME type (browsers type TypeScript
// `.ts` as `video/mp2t`), and the bytes still have to read as text.
const TEXT_LIKE_EXTS =
  /\.(txt|md|markdown|json|jsonl|json5|yaml|yml|toml|csv|tsv|html|htm|xml|js|jsx|mjs|cjs|ts|tsx|mts|cts|py|rb|go|rs|java|c|cc|cpp|h|hpp|sh|bash|zsh|env|log|conf|ini|cfg|sql)$/i;
// Formats the backend converts; decided by name, because a PDF can open with
// an all-ASCII first block and would sniff as text.
const DOCUMENT_EXTS =
  /\.(pdf|docx|xlsx|xlsm|pptx|doc|xls|ppt|odt|ods|odp|rtf|zip|tar|gz|tgz|bz2|xz|7z|rar|jar)$/i;

/** Whether a file's first bytes are UTF-8 text (mirrors `uploads.looks_like_text`):
 *  no NUL, valid UTF-8 (a character cut by the sample's end is fine), and at
 *  most 1% control characters other than whitespace and ESC. */
export function looksLikeText(head: Uint8Array): boolean {
  const sample = head.subarray(0, TEXT_SNIFF_BYTES);
  let control = 0;
  for (const b of sample) {
    if (b === 0) return false;
    if (b < 0x20 && b !== 0x09 && b !== 0x0a && b !== 0x0c && b !== 0x0d && b !== 0x1b) control++;
  }
  if (control > Math.floor(sample.length / 100)) return false;
  try {
    // `stream: true` tolerates a multi-byte character split at the end.
    new TextDecoder("utf-8", { fatal: true }).decode(sample, { stream: true });
  } catch {
    return false;
  }
  return true;
}

/** The kind a file travels as. `head` is its first bytes (null when they could
 *  not be read). The file's content decides "is this text?", not a list of
 *  extensions, so any code, config or log format works; the MIME type decides
 *  media; a large text file is sent as a document so it is saved whole. */
export function classify(
  file: { type: string; name: string; size: number },
  head: Uint8Array | null,
): AttachmentKind {
  const type = file.type || "";
  const media = type.startsWith("image/")
    ? "image"
    : type.startsWith("audio/")
      ? "audio"
      : type.startsWith("video/")
        ? "video"
        : null;
  const namedText = TEXT_LIKE_EXTS.test(file.name) || TEXT_LIKE_MIMES.test(type);
  if (media && !namedText) return media;
  if (DOCUMENT_EXTS.test(file.name)) return "document";
  if (head && looksLikeText(head)) return file.size > INLINE_TEXT_MAX_BYTES ? "document" : "file";
  return media ?? "document";
}

function readAsBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error);
    reader.onload = () => {
      const result = String(reader.result || "");
      // Strip the `data:<mime>;base64,` prefix — keep only the payload.
      const comma = result.indexOf(",");
      resolve(comma >= 0 ? result.slice(comma + 1) : result);
    };
    reader.readAsDataURL(file);
  });
}

function readAsText(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error);
    reader.onload = () => resolve(String(reader.result || ""));
    reader.readAsText(file);
  });
}

function uid(): string {
  try {
    return crypto.randomUUID();
  } catch {
    return `att-${Date.now()}-${Math.round(performance.now())}`;
  }
}

export type IngestResult =
  | { ok: true; attachment: Attachment }
  | { ok: false; reason: "too_large" | "unreadable" };

export async function ingestFile(file: File): Promise<IngestResult> {
  if (file.size > MAX_ATTACHMENT_BYTES) return { ok: false, reason: "too_large" };
  let head: Uint8Array | null = null;
  try {
    head = new Uint8Array(await file.slice(0, TEXT_SNIFF_BYTES).arrayBuffer());
  } catch {
    head = null; // classify falls back to the name and type
  }
  const kind = classify(file, head);

  // A text file keeps a text type even when the browser called it something
  // else (`.ts` as video/mp2t), so nothing downstream treats it as media.
  const mime =
    kind === "file"
      ? TEXT_LIKE_MIMES.test(file.type) ? file.type : "text/plain"
      : file.type || "application/octet-stream";
  const name = file.name || "attachment";

  if (kind === "file") {
    const data = await readAsText(file);
    return { ok: true, attachment: { id: uid(), name, mime, size: file.size, kind, data, preview: null } };
  }

  let data: string;
  try {
    data = await readAsBase64(file);
  } catch {
    // An unreadable file (an iCloud placeholder that never downloaded, a
    // permission error) is the one remaining "can't attach this".
    return { ok: false, reason: "unreadable" };
  }
  if (kind === "document") {
    return { ok: true, attachment: { id: uid(), name, mime, size: file.size, kind, data, preview: null } };
  }
  const preview = `data:${mime};base64,${data}`;
  return { ok: true, attachment: { id: uid(), name, mime, size: file.size, kind, data, preview } };
}

// Strip client-only fields (id, size, preview) for the request body.
export function toWire(attachments: Attachment[]): WireAttachment[] | undefined {
  if (!attachments.length) return undefined;
  return attachments.map((a) => ({ kind: a.kind, name: a.name, mime: a.mime, data: a.data }));
}

export function fmtSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
