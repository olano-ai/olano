/**
 * Getting photos into a message: the camera or the photo library, then one
 * pass that makes every photo the same kind of file.
 *
 * - **Picking.** On a phone, the Camera plugin: the system camera, and the
 *   system photo picker (several at once, no library permission needed on
 *   either platform). In a browser (`npm run dev`, the end-to-end tests) a
 *   file input does the same job.
 * - **Preparing.** Every photo, whatever it came as (HEIC from an iPhone
 *   camera, a PNG screenshot, a 48-megapixel original), is drawn upright onto
 *   a canvas no larger than MAX_EDGE on its longest side and saved as JPEG.
 *   That keeps a message small enough to send on a phone connection, under the
 *   runtime's per-file cap, and leaves out what the original carried that the
 *   agent does not need: the location and the rest of its metadata.
 *
 * Photos are prepared one at a time: ten full-size photos decoded at once
 * would hold more memory than an older phone gives a web view.
 */

import { Camera, CameraErrorCode, MediaTypeSelection } from "@capacitor/camera";

import { fitWithin, JPEG_QUALITY, MAX_EDGE, MAX_PHOTO_BYTES, type PreparedPhoto } from "../chat/photos.ts";
import { isNative, messageOf } from "./shell.ts";

export type PhotoSource = "camera" | "library";

/** Picking failed for a reason the person can act on. */
export class PhotoError extends Error {
  readonly reason: "denied" | "no_camera" | "unreadable" | "too_large" | "failed";
  constructor(reason: PhotoError["reason"], message: string) {
    super(message);
    this.name = "PhotoError";
    this.reason = reason;
  }
}

const CANCELLED = new Set<string>([
  CameraErrorCode.TakePhotoCancelled,
  CameraErrorCode.ChooseMediaCancelled,
  CameraErrorCode.EditPhotoCancelled,
]);

function denied(source: PhotoSource): PhotoError {
  return new PhotoError(
    "denied",
    source === "camera"
      ? "Olano isn't allowed to use the camera. You can allow it in Settings, under Olano."
      : "Olano isn't allowed to open your photos. You can allow it in Settings, under Olano.",
  );
}

/**
 * Opens the camera or the photo library and resolves the chosen photos, at
 * most `limit` of them. Resolves empty when the person cancels.
 */
export async function pickPhotos(source: PhotoSource, limit: number): Promise<Blob[]> {
  if (limit <= 0) return [];
  return isNative() ? pickNative(source, limit) : pickInBrowser(source, limit);
}

async function pickNative(source: PhotoSource, limit: number): Promise<Blob[]> {
  // The plugin scales and turns the photo upright itself, which keeps what
  // crosses into the web view small; `preparePhoto` still has the last word.
  const shape = { quality: 90, targetWidth: MAX_EDGE, targetHeight: MAX_EDGE, correctOrientation: true };
  let paths: string[];
  try {
    if (source === "camera") {
      const result = await Camera.takePhoto({ ...shape, saveToGallery: false });
      paths = result.webPath ? [result.webPath] : [];
    } else {
      const { results } = await Camera.chooseFromGallery({
        ...shape,
        mediaType: MediaTypeSelection.Photo,
        allowMultipleSelection: limit > 1,
        limit,
      });
      paths = results.map((r) => r.webPath).filter((p): p is string => !!p);
    }
  } catch (error) {
    const code = (error as { code?: string } | null)?.code ?? "";
    const message = messageOf(error, "");
    if (CANCELLED.has(code) || /cancel/i.test(message)) return [];
    if (code === CameraErrorCode.CameraPermissionDenied || code === CameraErrorCode.GalleryPermissionDenied) {
      throw denied(source);
    }
    if (/denied|not authori[sz]ed|permission/i.test(message)) throw denied(source);
    if (code === CameraErrorCode.NoCameraAvailable) {
      throw new PhotoError("no_camera", "This device has no camera to take a photo with.");
    }
    throw new PhotoError("failed", message || "The photo could not be opened.");
  }
  const blobs: Blob[] = [];
  for (const path of paths.slice(0, limit)) {
    // The plugin's file is served on the app's own origin (capacitor:// on
    // iOS, https://localhost on Android), so fetch() reads it.
    const response = await fetch(path);
    if (response.ok) blobs.push(await response.blob());
  }
  return blobs;
}

function pickInBrowser(source: PhotoSource, limit: number): Promise<Blob[]> {
  return new Promise((resolve) => {
    const input = document.createElement("input");
    input.type = "file";
    input.accept = "image/*";
    input.multiple = source === "library" && limit > 1;
    if (source === "camera") input.setAttribute("capture", "environment");
    input.className = "olano-hidden-input";
    let settled = false;
    const finish = (files: Blob[]) => {
      if (settled) return;
      settled = true;
      input.remove();
      resolve(files.slice(0, limit));
    };
    input.addEventListener("change", () => finish(Array.from(input.files ?? [])));
    input.addEventListener("cancel", () => finish([]));
    document.body.appendChild(input);
    input.click();
  });
}

/** The image files a paste carries (a screenshot pasted on an iPad, say). */
export function imagesFromClipboard(data: DataTransfer | null): Blob[] {
  if (!data) return [];
  const out: Blob[] = [];
  for (const item of Array.from(data.items ?? [])) {
    if (item.kind === "file" && item.type.startsWith("image/")) {
      const file = item.getAsFile();
      if (file) out.push(file);
    }
  }
  return out;
}

interface Decoded {
  source: CanvasImageSource;
  width: number;
  height: number;
  close(): void;
}

async function decode(blob: Blob): Promise<Decoded> {
  if (typeof createImageBitmap === "function") {
    try {
      const bitmap = await createImageBitmap(blob, { imageOrientation: "from-image" });
      return { source: bitmap, width: bitmap.width, height: bitmap.height, close: () => bitmap.close() };
    } catch {
      // Older WebKit: fall back to an <img>, which also applies the orientation.
    }
  }
  const url = URL.createObjectURL(blob);
  try {
    const img = new Image();
    img.decoding = "async";
    img.src = url;
    await img.decode();
    return { source: img, width: img.naturalWidth, height: img.naturalHeight, close: () => URL.revokeObjectURL(url) };
  } catch {
    URL.revokeObjectURL(url);
    throw new PhotoError("unreadable", "That photo's format can't be read. Try a JPEG or PNG.");
  }
}

function toJpeg(canvas: HTMLCanvasElement, quality: number): Promise<Blob> {
  return new Promise((resolve, reject) => {
    canvas.toBlob((blob) => (blob ? resolve(blob) : reject(new Error("encode"))), "image/jpeg", quality);
  });
}

function toBase64(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error);
    reader.onload = () => {
      const result = String(reader.result || "");
      resolve(result.slice(result.indexOf(",") + 1));
    };
    reader.readAsDataURL(blob);
  });
}

let queue: Promise<unknown> = Promise.resolve();

/** Re-encodes one photo for sending (see the top of this file). One at a time. */
export function preparePhoto(blob: Blob, name: string): Promise<PreparedPhoto> {
  const run = queue.then(() => prepareNow(blob, name));
  queue = run.catch(() => undefined);
  return run;
}

async function prepareNow(blob: Blob, name: string): Promise<PreparedPhoto> {
  const image = await decode(blob);
  try {
    const { width, height } = fitWithin(image.width, image.height, MAX_EDGE);
    if (!width || !height) throw new PhotoError("unreadable", "That photo is empty.");
    const canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = height;
    const ctx = canvas.getContext("2d");
    if (!ctx) throw new PhotoError("failed", "The photo could not be prepared.");
    // A transparent PNG would turn black as a JPEG.
    ctx.fillStyle = "#ffffff";
    ctx.fillRect(0, 0, width, height);
    ctx.imageSmoothingEnabled = true;
    ctx.imageSmoothingQuality = "high";
    ctx.drawImage(image.source, 0, 0, width, height);
    let jpeg = await toJpeg(canvas, JPEG_QUALITY);
    if (jpeg.size > MAX_PHOTO_BYTES) jpeg = await toJpeg(canvas, 0.6);
    // Release the canvas's pixels now rather than whenever the collector runs.
    canvas.width = canvas.height = 0;
    if (jpeg.size > MAX_PHOTO_BYTES) throw new PhotoError("too_large", "That photo is too large to send.");
    return {
      name,
      mime: "image/jpeg",
      data: await toBase64(jpeg),
      bytes: jpeg.size,
      width,
      height,
      url: URL.createObjectURL(jpeg),
    };
  } finally {
    image.close();
  }
}
