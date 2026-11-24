// Unit tests for public/manifest.json. Run with `npm run test:unit`.
//
// Every URL the manifest advertises has to be a route the SPA actually serves.
// Nothing else checks this: app.tsx ends with
// `<Route path="*" element={<Navigate to="/overview" replace />} />`, so a
// manifest pointing at a route that does not exist does not 404 — it silently
// lands the user on Overview. A launcher shortcut that quietly goes somewhere
// else is worse than no shortcut, and it looks fine in every screenshot.
//
// This caught a real one: a "Approvals" → /approvals shortcut, written from the
// assumption that the approvals inbox was its own tab. It is a section inside
// Overview (see the comment in views/Layout.tsx), so the shortcut would have
// redirected on every launch.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, it } from "node:test";

const here = dirname(fileURLToPath(import.meta.url));
const manifest = JSON.parse(
  readFileSync(join(here, "..", "..", "public", "manifest.json"), "utf8"),
);
const appSource = readFileSync(join(here, "..", "app.tsx"), "utf8");

/** Every `path="…"` declared in app.tsx, normalised to a leading slash. A
 *  path whose trailing segments are optional (`chat/:agent?`) also serves the
 *  bare prefix (`/chat`), which is the form a manifest URL uses. */
function declaredRoutes(): Set<string> {
  const routes = new Set<string>(["/"]);
  for (const m of appSource.matchAll(/path="([^"]+)"/g)) {
    let path = m[1];
    while (/\/:[^/]+\?$/.test(path)) path = path.replace(/\/:[^/]+\?$/, "");
    if (path === "*" || path.includes(":")) continue; // catch-all and params
    routes.add(path.startsWith("/") ? path : `/${path}`);
  }
  return routes;
}

/** The path part of a manifest URL, ignoring any query string. */
function pathOf(url: string): string {
  return new URL(url, "https://box.example").pathname;
}

describe("manifest routes", () => {
  it("declares start_url, scope and a maskable icon", () => {
    assert.equal(manifest.start_url, "/");
    assert.equal(manifest.scope, "/");
    assert.equal(manifest.display, "standalone");
    // Chromium's install criteria want 192 and 512; a maskable entry is what
    // stops Android drawing the icon letterboxed inside its own shape.
    const purposes = manifest.icons.map((i: { purpose?: string }) => i.purpose);
    assert.ok(purposes.includes("maskable"), "no maskable icon");
    const sizes = manifest.icons.map((i: { sizes: string }) => i.sizes);
    for (const size of ["192x192", "512x512"]) assert.ok(sizes.includes(size), size);
  });

  it("points every shortcut at a route app.tsx actually serves", () => {
    const routes = declaredRoutes();
    for (const shortcut of manifest.shortcuts ?? []) {
      assert.ok(
        routes.has(pathOf(shortcut.url)),
        `shortcut "${shortcut.name}" → ${shortcut.url} is not a route (would redirect to /overview)`,
      );
    }
  });

  it("points the share target at a route that handles the payload", () => {
    const action = manifest.share_target?.action;
    assert.ok(action, "no share_target");
    assert.ok(declaredRoutes().has(pathOf(action)), `share_target action ${action} is not a route`);
    // The receiving code reads exactly these names; renaming one here without
    // renaming it in views/chat/shareTarget.ts loses the shared payload.
    assert.deepEqual(manifest.share_target.params, {
      title: "share_title",
      text: "share_text",
      url: "share_url",
    });
  });
});
