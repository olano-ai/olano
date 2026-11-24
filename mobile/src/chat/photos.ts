/**
 * Photos in a conversation: what the app sends, how a message lays them out,
 * and how a reloaded message is told apart from the runtime's notes about it.
 *
 * The runtime takes photos as the dashboard sends them: `attachments` on
 * `POST /api/chat/stream`, each `{kind: "image", name, mime, data}` with the
 * bytes in base64 (runtime/chat_attachments.py). It saves each one in the
 * agent's workspace (`uploads/`) and adds a line to the message telling the
 * agent where: "[Image attached: <name> <dash> saved to <path> (...)]", plus one
 * note on how to use it. A reloaded conversation gets that text back, with the
 * photo itself as a media item; `stripAttachmentNotes` leaves the person's own
 * words.
 *
 * Pure: no DOM, no network, so `node --test` covers it.
 */

/** Photos one message can carry. */
export const MAX_PHOTOS = 10;
/** Longest side of a photo as sent, in pixels: what vision models read at full detail. */
export const MAX_EDGE = 2048;
/** JPEG quality of a photo as sent. */
export const JPEG_QUALITY = 0.85;
/** The runtime's cap for one attachment (chat_attachments.MAX_MEDIA_BYTES). */
export const MAX_PHOTO_BYTES = 10 * 1024 * 1024;

/** A photo as the composer holds it until the message is sent. */
export interface StagedPhoto {
  id: string;
  /** An object URL of the original, shown while the photo is prepared. */
  preview: string;
  status: "preparing" | "ready" | "failed";
  /** Set when ready. */
  prepared?: PreparedPhoto;
  /** Why it failed, for the person. */
  error?: string;
}

/** A photo ready to send: re-encoded, its location data gone. */
export interface PreparedPhoto {
  name: string;
  mime: "image/jpeg";
  /** Base64 of the JPEG bytes. */
  data: string;
  bytes: number;
  width: number;
  height: number;
  /** An object URL of the prepared JPEG, for the sent message. */
  url: string;
}

/** A photo in a message: a local one just sent, or one on the deployment. */
export interface MessagePhoto {
  key: string;
  name: string;
  /** Shown directly (an object URL of a photo this phone sent). */
  src?: string;
  /** Fetched with the session (a deployment path like /api/files-raw/...). */
  url?: string;
  width?: number;
  height?: number;
}

/** The wire item the runtime takes for one photo. */
export function wireAttachment(photo: PreparedPhoto): { kind: "image"; name: string; mime: string; data: string } {
  return { kind: "image", name: photo.name, mime: photo.mime, data: photo.data };
}

/** Scales `width` x `height` down (never up) so its longest side is at most `max`. */
export function fitWithin(width: number, height: number, max = MAX_EDGE): { width: number; height: number } {
  if (!(width > 0) || !(height > 0)) return { width: 0, height: 0 };
  const scale = Math.min(1, max / Math.max(width, height));
  return { width: Math.max(1, Math.round(width * scale)), height: Math.max(1, Math.round(height * scale)) };
}

/** A photo's file name: when it was taken (sent), and its place in the message. */
export function photoName(date: Date, index: number): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  const stamp = `${date.getFullYear()}${pad(date.getMonth() + 1)}${pad(date.getDate())}-${pad(date.getHours())}${pad(date.getMinutes())}${pad(date.getSeconds())}`;
  return `photo-${stamp}-${index + 1}.jpg`;
}

/** How many photos fit the remaining room in a message, and how many did not. */
export function takeRoom(have: number, picked: number, max = MAX_PHOTOS): { take: number; dropped: number } {
  const room = Math.max(0, max - have);
  const take = Math.min(room, Math.max(0, picked));
  return { take, dropped: Math.max(0, picked - take) };
}

/**
 * How a message lays out its photos, like the platform's own messages:
 * one photo at its own shape; two side by side; three as one large and two
 * stacked; four or more as a 2 x 2 grid whose last tile counts the rest.
 */
export interface PhotoLayout {
  kind: "single" | "pair" | "trio" | "grid";
  /** Tiles drawn (at most four). */
  shown: number;
  /** Photos behind the last tile ("+3"). */
  more: number;
}

export function photoLayout(count: number): PhotoLayout {
  if (count <= 1) return { kind: "single", shown: Math.max(0, count), more: 0 };
  if (count === 2) return { kind: "pair", shown: 2, more: 0 };
  if (count === 3) return { kind: "trio", shown: 3, more: 0 };
  return { kind: "grid", shown: 4, more: count - 4 };
}

/**
 * The box a single photo is drawn in: its own aspect ratio, within
 * `maxWidth` x `maxHeight`, and never so thin it stops reading as a photo.
 */
export function singlePhotoBox(
  width: number | undefined,
  height: number | undefined,
  maxWidth: number,
  maxHeight: number,
): { width: number; height: number } {
  if (!width || !height) return { width: maxWidth, height: Math.round(maxWidth * 0.75) };
  const ratio = Math.min(2.5, Math.max(0.4, width / height));
  let w = maxWidth;
  let h = w / ratio;
  if (h > maxHeight) {
    h = maxHeight;
    w = h * ratio;
  }
  return { width: Math.round(w), height: Math.round(h) };
}

// The runtime's notes about an attachment, one per line. "Image attached: x
// <dash> saved to ...", the documents' variants, and the one note on how to use
// what was saved (olano/uploads.py MEDIA_GUIDANCE_NOTE). Written for the
// agent, never for the person who sent the photo.
const NOTE_LINE = [
  /^\[(?:Image|Audio|Video) attached: .*\]$/,
  /^\[Document attached: .*\]$/,
  /^\[Attachment: .*\]$/,
  /^\[Note: the attached file\(s\) above .*\]$/,
];

// A text file sent with a message is inlined into it whole, between these two
// lines (runtime/api.py _build_multimodal_content).
const INLINED_FILE = /\n?--- attached: (.+?) \([^)\n]*\) ---\n[\s\S]*?\n--- end \1 ---/g;

/** The names of the text files inlined into a reloaded message. */
export function inlinedFileNames(text: string): string[] {
  if (!text || !text.includes("--- attached: ")) return [];
  return [...text.matchAll(INLINED_FILE)].map((m) => m[1]);
}

/** A message's own words, without the runtime's notes about its attachments. */
export function stripAttachmentNotes(text: string): string {
  if (text && text.includes("--- attached: ")) text = text.replace(INLINED_FILE, "").trim();
  if (!text || !text.includes("[")) return text;
  const kept = text.split("\n").filter((line) => !NOTE_LINE.some((re) => re.test(line.trim())));
  return kept
    .join("\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}
