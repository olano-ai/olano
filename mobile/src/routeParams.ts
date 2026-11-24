/**
 * Route parameters, decoded.
 *
 * Every link in the app encodes what it puts in a path segment (a thread id
 * like `acp::aria::nova` or `scheduled_nova_1a2b@20261016T100000-0001`), and
 * Ionic's router hands those segments to the page still encoded. Read raw,
 * an id with `:` or `@` was encoded a second time on its way to the
 * deployment, which answered "not found", and the conversation opened empty.
 */

import { useParams } from "react-router-dom";

/** One path segment as it was before the link encoded it. */
export function decodeSegment(value: string): string {
  try {
    return decodeURIComponent(value);
  } catch {
    // A stray "%" that is not an escape: the segment was never encoded.
    return value;
  }
}

/**
 * Whether `pathname` (the app's current address) is this conversation page's
 * own, `/chats/<agent>/<thread>`, the segments compared decoded so the
 * encoding a link chose does not matter. Pages left on the stack keep
 * rendering with the app's location, so one of them asking is not enough.
 */
export function isPageFor(pathname: string, agent: string, thread: string): boolean {
  const parts = pathname.split("/").filter(Boolean).map(decodeSegment);
  return parts.length === 3 && parts[0] === "chats" && parts[1] === agent && parts[2] === thread;
}

export function useRouteParams(): Record<string, string> {
  const params = useParams();
  const out: Record<string, string> = {};
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined) out[key] = decodeSegment(value);
  }
  return out;
}
