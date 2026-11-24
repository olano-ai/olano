/**
 * Turning what a person types into the origin of an Olano deployment.
 *
 * Three spellings are accepted:
 *
 * - a bare name (`acme`): letters, digits and hyphens, which means
 *   `https://acme.olano.run`, the address every managed deployment gets;
 * - a host (`agents.example.com`, `agents.example.com:8443`): https is assumed;
 * - a full URL (`https://agents.example.com/chat`): the path is dropped.
 *
 * The result is always an origin (`scheme://host[:port]`), which is what the
 * native shell stores. The native side validates again (OlanoShell.setServer),
 * so this module is about giving a precise message early, not about security.
 *
 * Pure: no DOM, no storage, so `node --test` can import it.
 */

export const MANAGED_DOMAIN = "olano.run";

/** One DNS label: what a bare deployment name has to be. */
const SLUG = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/i;

export type ParsedAddress =
  | { ok: true; origin: string; host: string; insecure: boolean }
  | { ok: false; error: string };

export interface ParseOptions {
  /** Plain http is only for debug builds pointed at a box on the local network. */
  allowHttp: boolean;
}

export function parseAddress(input: string, options: ParseOptions): ParsedAddress {
  const text = input.trim();
  if (!text) {
    return { ok: false, error: "Enter the address of your Olano deployment." };
  }
  if (/\s/.test(text)) {
    return { ok: false, error: "An address cannot contain spaces." };
  }

  let candidate: string;
  if (/^[a-z][a-z0-9+.-]*:\/\//i.test(text)) {
    candidate = text;
  } else if (SLUG.test(text)) {
    candidate = `https://${text.toLowerCase()}.${MANAGED_DOMAIN}`;
  } else if (/^[a-z][a-z0-9+.-]*:/i.test(text) && !/^[^:/]+:\d+(?:[/?#]|$)/.test(text)) {
    // "mailto:x", "javascript:x" and friends. A host with a port ("box:8443")
    // looks like a scheme too, which is what the second test lets through.
    return { ok: false, error: "Use an https:// address, a host name, or a deployment name." };
  } else {
    candidate = `https://${text}`;
  }

  let url: URL;
  try {
    url = new URL(candidate);
  } catch {
    return { ok: false, error: "That does not look like a web address." };
  }

  const scheme = url.protocol.replace(/:$/, "").toLowerCase();
  if (scheme !== "https" && scheme !== "http") {
    return { ok: false, error: "Only https:// addresses can be opened." };
  }
  if (scheme === "http" && !options.allowHttp) {
    return {
      ok: false,
      error: "Plain http:// addresses are not allowed. Use the https:// address of your deployment.",
    };
  }
  if (url.username || url.password) {
    return { ok: false, error: "Leave the user name and password out of the address." };
  }
  if (!url.hostname) {
    return { ok: false, error: "The address is missing a host name." };
  }
  const hostname = url.hostname.replace(/\.$/, "");
  if (!hostname.includes(".") && hostname !== "localhost" && !hostname.startsWith("[")) {
    // `new URL("https://acme")` parses fine; a single label never resolves
    // outside a LAN, and as a bare name it was already handled above.
    return { ok: false, error: `Did you mean ${hostname}.${MANAGED_DOMAIN}?` };
  }

  return {
    ok: true,
    origin: url.origin,
    host: url.host,
    insecure: scheme === "http",
  };
}

/** The host part of a stored origin, for display. Falls back to the input. */
export function hostOf(origin: string): string {
  try {
    return new URL(origin).host;
  } catch {
    return origin;
  }
}
