// Builds the srcdoc for the Files view's live HTML preview.
//
// The preview iframe runs the page on an opaque origin (sandbox without
// `allow-same-origin`), which is the security boundary that keeps
// agent-authored HTML away from the dashboard session — but it also means the
// framed document cannot fetch its own relative assets: subresource requests
// from an opaque origin carry no session cookie (401) and would drop the
// `?agent=` query anyway, resolving against the wrong workspace. So the
// assets are inlined here, in the parent context, before the document ever
// reaches the frame: stylesheets and scripts as text via the JSON content
// endpoint (open to read-only members), images as data: URIs via the raw
// endpoint (blob: URLs are origin-bound and unusable from the opaque origin).
//
// Best-effort by contract: any per-asset failure (403 for read-only members
// on /api/files-raw, 404, over-budget, timeout) leaves that element untouched
// and never blocks the preview. Not covered: url() inside fetched CSS, page
// fetch()/XHR, root-relative "/…" URLs, srcset, relative module imports.

import { api } from "../../core/api";

/** Files with a live-render Preview mode (parallel to markdown's). */
export function isHtmlFile(name: string): boolean {
  const parts = name.split(".");
  const ext = parts.length > 1 ? (parts.pop() ?? "").toLowerCase() : "";
  return ext === "html" || ext === "htm";
}

export interface HtmlPreviewLimits {
  /** Max elements rewritten, preferring stylesheets, then scripts, then images. */
  maxAssets: number;
  maxAssetBytes: number;
  maxTotalBytes: number;
  /** Overall inlining deadline; whatever landed by then is what renders. */
  timeoutMs: number;
}

const DEFAULT_LIMITS: HtmlPreviewLimits = {
  maxAssets: 24,
  maxAssetBytes: 2 * 1024 * 1024,
  maxTotalBytes: 8 * 1024 * 1024,
  timeoutMs: 8000,
};

// Mirrors FilesView's module-private encodePath/rawUrl — importing them would
// require exporting from the view, a cycle once the view imports this module.
function encodePath(path: string): string {
  return path
    .split("/")
    .map((seg) => encodeURIComponent(seg))
    .join("/");
}

function agentQuery(agent: string): string {
  return agent ? `?agent=${encodeURIComponent(agent)}` : "";
}

const SCHEME_RE = /^[a-zA-Z][a-zA-Z0-9+.-]*:/;

/** Only scheme-less, non-root-relative URLs are workspace files we can inline. */
function isRelativeUrl(url: string): boolean {
  if (!url || url.startsWith("#")) return false;
  if (SCHEME_RE.test(url)) return false; // http:, https:, data:, javascript:, …
  if (url.startsWith("//")) return false; // protocol-relative
  if (url.startsWith("/")) return false; // resolves against the dashboard origin, not the workspace
  return true;
}

/**
 * Resolve a relative URL against the previewed file's directory into a
 * workspace-relative path, or null when it escapes the workspace root (the
 * backend would 403 the fetch regardless).
 */
function resolveWorkspacePath(baseDir: string, url: string): string | null {
  const path = url.split(/[?#]/)[0];
  if (!path) return null;
  const stack = baseDir ? baseDir.split("/") : [];
  for (const rawSeg of path.split("/")) {
    let seg = rawSeg;
    try {
      seg = decodeURIComponent(rawSeg);
    } catch {
      // Malformed % escape — keep the raw segment; the fetch just 404s.
    }
    if (!seg || seg === ".") continue;
    if (seg === "..") {
      if (!stack.length) return null;
      stack.pop();
      continue;
    }
    stack.push(seg);
  }
  return stack.length ? stack.join("/") : null;
}

// A fetched asset containing "</script" (or "</style") would close the inline
// element early and truncate the serialized document; "<\/" is byte-identical
// inside JS strings and CSS.
function escapeInlineClose(text: string): string {
  return text.replace(/<\/(script|style)/gi, "<\\/$1");
}

async function fetchText(path: string, agent: string): Promise<string> {
  const res = await api.get<{ content: string; size: number }>(
    `/api/files/${encodePath(path)}${agentQuery(agent)}`,
  );
  return res.content;
}

async function fetchDataUri(
  path: string,
  agent: string,
  budget: (bytes: number) => boolean,
): Promise<string | null> {
  const res = await fetch(`/api/files-raw/${encodePath(path)}${agentQuery(agent)}`, {
    credentials: "include",
  });
  if (!res.ok) return null;
  const blob = await res.blob();
  if (!budget(blob.size)) return null;
  return await new Promise<string | null>((resolve) => {
    const reader = new FileReader();
    reader.onload = () => resolve(typeof reader.result === "string" ? reader.result : null);
    reader.onerror = () => resolve(null);
    reader.readAsDataURL(blob);
  });
}

/**
 * Turn the previewed file's HTML into a self-contained srcdoc string, inlining
 * relatively-referenced same-workspace stylesheets, scripts and images.
 * Never rejects — any unexpected failure falls back to the input HTML.
 */
export async function buildHtmlSrcdoc(
  html: string,
  filePath: string,
  agent: string,
  limits?: Partial<HtmlPreviewLimits>,
): Promise<string> {
  try {
    const lim = { ...DEFAULT_LIMITS, ...limits };
    const dir = filePath.split("/").slice(0, -1).join("/");
    // Inert: parseFromString never executes scripts, so no sanitizer is needed
    // before the document goes into the sandboxed frame.
    const doc = new DOMParser().parseFromString(html, "text/html");

    let totalBytes = 0;
    const takeBudget = (bytes: number): boolean => {
      if (bytes > lim.maxAssetBytes || totalBytes + bytes > lim.maxTotalBytes) return false;
      totalBytes += bytes;
      return true;
    };

    const jobs: Array<() => Promise<void>> = [];
    const addJob = (el: Element, attr: string, run: (target: string) => Promise<void>): void => {
      if (jobs.length >= lim.maxAssets) return;
      const url = el.getAttribute(attr) ?? "";
      if (!isRelativeUrl(url)) return;
      const target = resolveWorkspacePath(dir, url);
      if (target === null) return;
      jobs.push(async () => {
        try {
          await run(target);
        } catch {
          // Leave the element untouched — a broken asset never blocks the preview.
        }
      });
    };

    for (const link of doc.querySelectorAll('link[rel~="stylesheet" i][href]')) {
      addJob(link, "href", async (target) => {
        const css = await fetchText(target, agent);
        if (!takeBudget(css.length)) return;
        const style = doc.createElement("style");
        style.textContent = escapeInlineClose(css);
        link.replaceWith(style); // in place, so cascade order is preserved
      });
    }
    for (const script of doc.querySelectorAll("script[src]")) {
      addJob(script, "src", async (target) => {
        const js = await fetchText(target, agent);
        if (!takeBudget(js.length)) return;
        // The element stays in place so document-order execution is preserved
        // (inline scripts ignore async/defer — close enough for prototypes).
        script.removeAttribute("src");
        script.textContent = escapeInlineClose(js);
      });
    }
    for (const img of doc.querySelectorAll("img[src]")) {
      addJob(img, "src", async (target) => {
        const uri = await fetchDataUri(target, agent, takeBudget);
        if (uri) img.setAttribute("src", uri);
      });
    }

    // Jobs that land after the deadline mutate a document nobody reads again —
    // serialization below is a snapshot.
    await Promise.race([
      Promise.allSettled(jobs.map((run) => run())),
      new Promise<void>((resolve) => {
        window.setTimeout(resolve, lim.timeoutMs);
      }),
    ]);

    // outerHTML drops the doctype; always prepend one or pages lay out in
    // quirks mode.
    return `<!DOCTYPE html>\n${doc.documentElement.outerHTML}`;
  } catch {
    return html;
  }
}
