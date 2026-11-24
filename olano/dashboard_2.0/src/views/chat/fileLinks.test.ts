// Unit tests for the workspace-file link index. Run with `npm run test:unit`
// (Node's built-in runner with type stripping — no test framework in the bundle).
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import type { MediaRef } from "../../core/types";
import {
  buildFileIndex,
  fileAnchorHtml,
  fileIndexFor,
  linkifyFileMentions,
  resolveFileLink,
  resolveMarkdownImage,
} from "./fileLinks.ts";

function ref(path: string, extra: Partial<MediaRef> = {}): MediaRef {
  const name = path.split("/").pop() ?? path;
  return {
    kind: "image",
    name,
    path,
    mime: "image/png",
    url: `/api/files-raw/${path}?agent=merli`,
    files_url: `/files?path=${encodeURIComponent(path)}&agent=merli`,
    ...extra,
  };
}

// The renderer's stash: finished HTML is parked behind \x00n\x00 tokens.
function stasher() {
  const stash: string[] = [];
  const keep = (html: string): string => `\x00${stash.push(html) - 1}\x00`;
  return { stash, keep };
}

const X = ref("reports/x.png", { mentions: ["reports/x.png", "`reports/x.png`", "/ws/reports/x.png"] });
const XA = `<a class="md-file" data-internal="1" href="/files?path=reports%2Fx.png&amp;agent=merli">`;

describe("buildFileIndex", () => {
  it("indexes every mention, the path and the bare name, longest first", () => {
    const idx = buildFileIndex([X]);
    assert.deepEqual([...idx.targets.keys()], ["/ws/reports/x.png", "reports/x.png", "x.png"]);
    assert.equal(idx.scan[0].text, "/ws/reports/x.png");
    assert.equal(idx.targets.get("x.png")?.raw, "/api/files-raw/reports/x.png?agent=merli");
    assert.equal(idx.targets.get("x.png")?.kind, "image");
  });

  it("strips the backticks off a code-span mention (the code pass still wraps it)", () => {
    const idx = buildFileIndex([ref("a.csv", { mentions: ["`a.csv`"] })]);
    assert.deepEqual([...idx.targets.keys()], ["a.csv"]);
  });

  it("skips a ref with no files_url — nowhere to link", () => {
    const idx = buildFileIndex([ref("a.csv", { files_url: undefined })]);
    assert.equal(idx.targets.size, 0);
    assert.equal(idx.scan.length, 0);
    assert.equal(fileIndexFor([ref("a.csv", { files_url: undefined })]), undefined);
  });

  it("drops a bare name two files share, keeps their paths", () => {
    const idx = buildFileIndex([ref("a/x.png"), ref("b/x.png")]);
    assert.deepEqual([...idx.targets.keys()].sort(), ["a/x.png", "b/x.png"]);
  });

  it("escapes tokens the way the prose is escaped", () => {
    const idx = buildFileIndex([ref("a&b.png")]);
    assert.equal(idx.scan[0].text, "a&amp;b.png");
  });

  it("empty / absent input is an empty index", () => {
    assert.equal(buildFileIndex(undefined).scan.length, 0);
    assert.equal(buildFileIndex([]).targets.size, 0);
    assert.equal(fileIndexFor(undefined), undefined);
    assert.equal(fileIndexFor([]), undefined);
  });
});

describe("fileIndexFor", () => {
  it("memoizes per array identity", () => {
    const media = [X];
    assert.equal(fileIndexFor(media), fileIndexFor(media));
    assert.notEqual(fileIndexFor(media), fileIndexFor([X]));
  });
});

describe("linkifyFileMentions", () => {
  it("links an exact mention through keep", () => {
    const { stash, keep } = stasher();
    const out = linkifyFileMentions("see reports/x.png now", buildFileIndex([X]), keep);
    assert.equal(out, "see \x000\x00 now");
    assert.equal(stash[0], `${XA}reports/x.png</a>`);
  });

  it("links the bare name and the absolute form too", () => {
    const { stash, keep } = stasher();
    const out = linkifyFileMentions("x.png and /ws/reports/x.png", buildFileIndex([X]), keep);
    assert.equal(out, "\x000\x00 and \x001\x00");
    assert.equal(stash[0], `${XA}x.png</a>`);
    assert.equal(stash[1], `${XA}/ws/reports/x.png</a>`);
  });

  it("longest token wins: one anchor for reports/x.png, not a nested one", () => {
    const { stash, keep } = stasher();
    const out = linkifyFileMentions("reports/x.png", buildFileIndex([X]), keep);
    assert.equal(out, "\x000\x00");
    assert.equal(stash.length, 1);
    assert.equal(stash[0], `${XA}reports/x.png</a>`);
  });

  it("never links a token that sits inside a longer path", () => {
    const { stash, keep } = stasher();
    const idx = buildFileIndex([X]);
    assert.equal(linkifyFileMentions("other/x.png", idx, keep), "other/x.png");
    assert.equal(linkifyFileMentions("/srv/reports/x.png", idx, keep), "/srv/reports/x.png");
    assert.equal(linkifyFileMentions("x.png.bak", idx, keep), "x.png.bak");
    assert.equal(linkifyFileMentions("myx.png", idx, keep), "myx.png");
    assert.equal(stash.length, 0);
  });

  it("keeps sentence punctuation outside the link", () => {
    const { stash, keep } = stasher();
    const out = linkifyFileMentions("(see reports/x.png).", buildFileIndex([X]), keep);
    assert.equal(out, "(see \x000\x00).");
    assert.equal(stash.length, 1);
  });

  it("matches the escaped spelling of a token", () => {
    const { stash, keep } = stasher();
    const out = linkifyFileMentions("open a&amp;b.png", buildFileIndex([ref("a&b.png")]), keep);
    assert.equal(out, "open \x000\x00");
    assert.ok(stash[0].endsWith(">a&amp;b.png</a>"));
  });

  it("never rewrites inside an existing stash token", () => {
    const { stash, keep } = stasher();
    // A file literally named "12" would otherwise match the digits of \x0012\x00.
    const idx = buildFileIndex([ref("12")]);
    assert.equal(linkifyFileMentions("a \x0012\x00 b 12", idx, keep), "a \x0012\x00 b \x000\x00");
    assert.equal(stash.length, 1);
  });

  it("tokens glued to word characters are inside something longer; apart, both link", () => {
    const { stash, keep } = stasher();
    const idx = buildFileIndex([ref("ab", { mentions: [] }), ref("bcd", { mentions: [] })]);
    // "ab" at 0 is followed by a word char, "bcd" at 1 is preceded by one.
    assert.equal(linkifyFileMentions("abcd", idx, keep), "abcd");
    assert.equal(linkifyFileMentions("ab bcd", idx, keep), "\x000\x00 \x001\x00");
    assert.ok(stash[0].endsWith(">ab</a>"));
    assert.ok(stash[1].endsWith(">bcd</a>"));
  });

  it("is a no-op with nothing indexed", () => {
    const { keep } = stasher();
    assert.equal(linkifyFileMentions("reports/x.png", undefined, keep), "reports/x.png");
    assert.equal(linkifyFileMentions("reports/x.png", buildFileIndex([]), keep), "reports/x.png");
  });
});

describe("resolveMarkdownImage / resolveFileLink", () => {
  const idx = buildFileIndex([X, ref("my file.png")]);

  it("returns the files-raw URL for an indexed token", () => {
    assert.equal(resolveMarkdownImage("reports/x.png", idx), "/api/files-raw/reports/x.png?agent=merli");
  });

  it("also tries the token without a leading ./", () => {
    assert.equal(resolveMarkdownImage("./reports/x.png", idx), "/api/files-raw/reports/x.png?agent=merli");
  });

  it("percent-decodes a markdown URL", () => {
    assert.equal(resolveMarkdownImage("my%20file.png", idx), "/api/files-raw/my file.png?agent=merli");
  });

  it("undoes the prose escaping first", () => {
    const amp = buildFileIndex([ref("a&b.png")]);
    assert.equal(resolveFileLink("a&amp;b.png", amp)?.href, "/files?path=a%26b.png&agent=merli");
  });

  it("is null for anything not indexed", () => {
    assert.equal(resolveMarkdownImage("other/y.png", idx), null);
    assert.equal(resolveMarkdownImage("https://example.com/x.png", idx), null);
    assert.equal(resolveMarkdownImage("reports/x.png", undefined), null);
    assert.equal(resolveMarkdownImage("", idx), null);
  });
});

describe("fileAnchorHtml", () => {
  it("escapes the href for an attribute and marks the link internal", () => {
    const t = { href: '/files?path=a"b&agent=m', raw: "/api/files-raw/a", kind: "file" as const, name: "a" };
    assert.equal(
      fileAnchorHtml(t, "lbl"),
      '<a class="md-file" data-internal="1" href="/files?path=a&quot;b&amp;agent=m">lbl</a>',
    );
  });
});
