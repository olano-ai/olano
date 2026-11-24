/**
 * Names and types of files shared into Olano (see shareIn.ts). Pure, so
 * `node --test` covers it.
 */

const MIME_BY_EXTENSION: Record<string, string> = {
  pdf: "application/pdf",
  doc: "application/msword",
  docx: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  xls: "application/vnd.ms-excel",
  xlsx: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  ppt: "application/vnd.ms-powerpoint",
  pptx: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
  csv: "text/csv",
  txt: "text/plain",
  md: "text/markdown",
  json: "application/json",
  png: "image/png",
  jpg: "image/jpeg",
  jpeg: "image/jpeg",
  heic: "image/heic",
  gif: "image/gif",
  webp: "image/webp",
  mp3: "audio/mpeg",
  m4a: "audio/mp4",
  wav: "audio/wav",
  mp4: "video/mp4",
  mov: "video/quicktime",
};

/** The file name of a file:// URL, decoded ("Q3%20plan.pdf" is "Q3 plan.pdf"). */
export function fileNameOf(url: string): string {
  const path = url.replace(/^[a-z][a-z0-9+.-]*:\/\//i, "").split(/[?#]/)[0];
  const last = path.split("/").filter(Boolean).pop() || "shared file";
  try {
    return decodeURIComponent(last);
  } catch {
    return last;
  }
}

export function mimeOf(name: string): string {
  const ext = name.includes(".") ? name.split(".").pop()!.toLowerCase() : "";
  return MIME_BY_EXTENSION[ext] || "application/octet-stream";
}
