/**
 * Photos and recordings on the deployment (a sent photo or voice note after a
 * reload, an image or a spoken reply an agent made) need the session cookie,
 * so `<img src>` and `<audio src>` cannot fetch them: native code does, and
 * each becomes an object URL kept for the session. Object URLs rather than
 * data: URLs, so a conversation full of media holds bytes, not megabytes of
 * base64 strings in the page. The bytes stay at hand too, for drawing a voice
 * note's waveform without a second download.
 */

import { useEffect, useState } from "react";

import { session } from "../state/session.ts";

export interface LoadedMedia {
  url: string;
  blob: Blob;
}

const cache = new Map<string, Promise<LoadedMedia | null>>();

function dataUrlToBlob(dataUrl: string): Blob {
  const comma = dataUrl.indexOf(",");
  const type = /^data:([^;,]+)/.exec(dataUrl)?.[1] ?? "application/octet-stream";
  const binary = atob(dataUrl.slice(comma + 1));
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return new Blob([bytes], { type });
}

export function loadMedia(path: string): Promise<LoadedMedia | null> {
  let pending = cache.get(path);
  if (!pending) {
    const api = session.get().api;
    pending = api
      ? api
          .image(path)
          .then((dataUrl) => {
            if (!dataUrl) return null;
            const blob = dataUrlToBlob(dataUrl);
            return { url: URL.createObjectURL(blob), blob };
          })
          .catch(() => null)
      : Promise.resolve(null);
    cache.set(path, pending);
    // A failure is not remembered: the next screen that shows it tries again.
    void pending.then((value) => {
      if (value === null) cache.delete(path);
    });
  }
  return pending;
}

/** Forgets every photo and recording (sign out, switch deployment). */
export function clearMediaCache(): void {
  for (const pending of cache.values()) {
    void pending.then((media) => media && URL.revokeObjectURL(media.url));
  }
  cache.clear();
}

/** Something drawn or played from either the phone's own copy (`src`) or the deployment (`url`). */
export interface MediaSource {
  src?: string;
  url?: string;
}

/** The address to draw or play a message's media from, once it is available. */
export function useMediaSrc(item: MediaSource | undefined): string | null {
  return useLoadedMedia(item)?.url ?? null;
}

/** Like useMediaSrc, with the bytes of a deployment file (null for the phone's own copies). */
export function useLoadedMedia(item: MediaSource | undefined): { url: string; blob: Blob | null } | null {
  const direct = item?.src ?? null;
  const path = direct ? null : (item?.url ?? null);
  const [loaded, setLoaded] = useState<{ path: string; media: LoadedMedia | null } | null>(null);
  useEffect(() => {
    if (!path) return;
    let alive = true;
    void loadMedia(path).then((media) => alive && setLoaded({ path, media }));
    return () => {
      alive = false;
    };
  }, [path]);
  if (direct) return { url: direct, blob: null };
  return loaded && loaded.path === path && loaded.media ? loaded.media : null;
}
