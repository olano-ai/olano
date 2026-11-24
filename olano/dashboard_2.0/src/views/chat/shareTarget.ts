// Receiving the OS share sheet.
//
// An installed PWA can register as a share target; the manifest
// (public/manifest.json) points ours at `/chat` with a GET and the
// params named below, so sharing a link or a note from another app lands here
// with the payload in the query string.
//
// GET rather than POST on purpose. A POST share target can carry files, but
// only by having the service worker intercept the form post, stash the body and
// redirect — which means the worker has to touch a navigation it would
// otherwise ignore, for a payload it must then hold somewhere. Text and links
// are the share that matters for a chat composer, and they need none of that.
//
// This is Android and desktop Chromium only. iOS implements no Web Share Target
// at all, which is why the iPhone story is a Shortcut posting to the API rather
// than an entry in this manifest.
//
// Like the Cortex hand-off next door, a shared payload is a DRAFT: it lands in
// the composer and waits. Nothing is ever sent on the user's behalf.

/** Query parameters the manifest's share_target maps its fields onto. */
export const SHARE_PARAMS = ["share_title", "share_text", "share_url"] as const;

export interface SharedPayload {
  title?: string | null;
  text?: string | null;
  url?: string | null;
}

/**
 * Fold a share payload into composer text, or "" when nothing usable arrived.
 *
 * The three fields overlap in practice and the overlap is not consistent
 * between apps: Chrome on Android puts a shared link in `text` AND `url`, some
 * apps repeat the page title inside `text`, and a plain text selection arrives
 * with `text` alone. Concatenating blindly produces the URL twice, which is
 * exactly the sort of thing a person then has to edit out by hand. So each part
 * is added only when it is not already carried by another.
 */
export function composeSharedDraft(payload: SharedPayload): string {
  const title = (payload.title ?? "").trim();
  const text = (payload.text ?? "").trim();
  const url = (payload.url ?? "").trim();

  const parts: string[] = [];
  if (title && !text.includes(title)) parts.push(title);
  if (text) parts.push(text);
  if (url && !text.includes(url) && !title.includes(url)) parts.push(url);
  return parts.join("\n");
}

/** Read a share payload straight off a URLSearchParams. */
export function readSharedPayload(params: URLSearchParams): SharedPayload {
  return {
    title: params.get("share_title"),
    text: params.get("share_text"),
    url: params.get("share_url"),
  };
}

/**
 * The same query string with every share parameter removed, or null when there
 * was nothing to remove.
 *
 * Stripping them is what makes a share one-shot: the params are the whole
 * payload, so a refresh (or a back-button) onto a URL that still carried them
 * would re-draft the same text over whatever the person had since typed.
 */
export function stripShareParams(params: URLSearchParams): URLSearchParams | null {
  if (!SHARE_PARAMS.some((key) => params.has(key))) return null;
  const next = new URLSearchParams(params);
  for (const key of SHARE_PARAMS) next.delete(key);
  return next;
}
