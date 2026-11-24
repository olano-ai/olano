// Upload from the user's device into a workspace folder (the Files view's
// Upload button). One request per file to `POST /api/files/upload`, the body
// being the file's raw bytes, so nothing is base64'd or buffered in a form.
// XHR, not fetch: fetch cannot report upload progress, and on a phone a 40 MB
// video over 4G is a minute of nothing without it.
//
// Pure apart from `uploadFile` (no store, no React) so node's --test can
// import it. The `.ts` extensions are what let node resolve these imports.

import { ApiError, apiErrorFrom } from "../../core/api.ts";

/** Mirrors the backend's `routes/files.FILES_UPLOAD_MAX_BYTES`. Checked here
 *  so an oversized file is refused before its bytes leave the device. */
export const UPLOAD_MAX_BYTES = 100 * 1024 * 1024;

export interface UploadedFile {
  path: string;
  size: number;
}

export function uploadUrl(agent: string, dir: string, name: string, overwrite = false): string {
  const q = new URLSearchParams({ agent, dir, name });
  if (overwrite) q.set("overwrite", "true");
  return `/api/files/upload?${q.toString()}`;
}

/** Split a picked batch into what can be sent and what is over the cap. */
export function partitionBySize<F extends { size: number }>(
  files: readonly F[],
  max = UPLOAD_MAX_BYTES,
): { ok: F[]; tooLarge: F[] } {
  const ok: F[] = [];
  const tooLarge: F[] = [];
  for (const f of files) (f.size > max ? tooLarge : ok).push(f);
  return { ok, tooLarge };
}

/** Whole-batch progress, 0..100: finished files count in full, the one in
 *  flight by its loaded bytes. Weighted by size, so one large file among small
 *  ones does not read as nearly done. */
export function batchPercent(sizes: readonly number[], done: number, loadedOfCurrent: number): number {
  const total = sizes.reduce((a, b) => a + b, 0);
  if (total <= 0) return sizes.length ? Math.round((done / sizes.length) * 100) : 100;
  const finished = sizes.slice(0, done).reduce((a, b) => a + b, 0);
  return Math.min(100, Math.round(((finished + loadedOfCurrent) / total) * 100));
}

export function uploadFile(
  agent: string,
  dir: string,
  file: Blob & { name: string },
  opts: { overwrite?: boolean; onProgress?: (loaded: number) => void } = {},
): Promise<UploadedFile> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", uploadUrl(agent, dir, file.name, opts.overwrite));
    xhr.withCredentials = true;
    // Raw bytes, never a form type: the backend refuses those (a cross-site
    // form could otherwise post one without a CORS preflight).
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    if (opts.onProgress) {
      const report = opts.onProgress;
      xhr.upload.onprogress = (e) => report(e.loaded);
    }
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          resolve(JSON.parse(xhr.responseText) as UploadedFile);
        } catch {
          resolve({ path: dir ? `${dir}/${file.name}` : file.name, size: file.size });
        }
      } else {
        reject(apiErrorFrom(xhr.status, xhr.statusText, xhr.responseText));
      }
    };
    // A dropped connection has no status; say so rather than "0".
    xhr.onerror = () => reject(new ApiError(0, "The connection dropped during the upload"));
    xhr.onabort = () => reject(new ApiError(0, "The upload was cancelled"));
    xhr.send(file);
  });
}
