// Unit tests for the chat markdown prose renderer. Run with `npm run test:unit`.
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { escapeHtml } from "./highlight.ts";
import type { MediaRef } from "../core/types";
import { buildFileIndex } from "../views/chat/fileLinks.ts";
import { renderProse } from "./markdownProse.ts";

const render = (md: string): string => renderProse(escapeHtml(md));
const count = (html: string, tag: string): number => html.split(tag).length - 1;

describe("renderProse ordered lists", () => {
  it("keeps a tight list in one <ol>", () => {
    assert.equal(render("1. a\n2. b\n3. c"), "<ol><li>a</li><li>b</li><li>c</li></ol>");
  });

  it("keeps a loose list with indented descriptions in one <ol>", () => {
    // The shape that rendered as "1. 1. 1." in chat: a numbered heading, a
    // blank line, an indented description, a blank line, the next number.
    const html = render(
      [
        "1. Get to know you",
        "",
        "   Ask for your name.",
        "",
        "2. Understand your business",
        "",
        "   Research the website.",
      ].join("\n"),
    );
    assert.equal(count(html, "<ol"), 1);
    assert.equal(count(html, "<li>"), 2);
    assert.equal(
      html,
      "<ol><li>Get to know you<p>Ask for your name.</p></li>" +
        "<li>Understand your business<p>Research the website.</p></li></ol>",
    );
  });

  it("continues the numbering when a paragraph splits the list", () => {
    const html = render("1. a\n\nnot indented\n\n2. b\n3. c");
    assert.equal(html, '<ol><li>a</li></ol><p>not indented</p><ol start="2"><li>b</li><li>c</li></ol>');
  });

  it("honours the first number of a list that starts mid-sequence", () => {
    // A fenced code block between items reaches renderProse as two calls.
    assert.equal(render("4. d\n5. e"), '<ol start="4"><li>d</li><li>e</li></ol>');
  });

  it("nests indented bullets inside the item instead of ending the list", () => {
    const html = render("1. a\n   - x\n   - y\n2. b");
    assert.equal(html, "<ol><li>a<ul><li>x</li><li>y</li></ul></li><li>b</li></ol>");
  });

  it("treats an unindented line straight under an item as part of it", () => {
    assert.equal(render("1. **Name**\nDescription\n2. b"), "<ol><li><strong>Name</strong><br>Description</li><li>b</li></ol>");
  });

  it("still ends the list at a heading or a different list type", () => {
    assert.equal(render("1. a\n# H\n- b"), "<ol><li>a</li></ol><h1>H</h1><ul><li>b</li></ul>");
  });

  it("leaves plain paragraphs alone", () => {
    assert.equal(render("one\n\ntwo"), "<p>one</p><p>two</p>");
  });
});

// The attribute names a browser reads off the first `<a …>` tag. A
// double-quoted value runs to the next `"`, so a quote that leaked out of a
// URL shows up here as an extra name.
function anchorAttrs(html: string): string[] {
  const tag = /<a\s([^>]*)>/.exec(html);
  assert.ok(tag, html);
  return [...tag[1].matchAll(/([^\s"'=<>/]+)(?:="[^"]*")?/g)].map((m) => m[1]);
}

const LINK_ATTRS = ["href", "target", "rel"];

describe("renderProse link attributes", () => {
  it("percent-encodes quotes in a bare URL's href and keeps its text", () => {
    const html = render('see https://x.dev/"onmouseover="alert(1)"x');
    assert.deepEqual(anchorAttrs(html), LINK_ATTRS, html);
    assert.equal(
      html,
      '<p>see <a href="https://x.dev/%22onmouseover=%22alert(1)%22x" target="_blank" rel="noreferrer">' +
        'https://x.dev/"onmouseover="alert(1)"x</a></p>',
    );
  });

  it("percent-encodes quotes in a markdown link's href", () => {
    const html = render('[a](https://x.dev/"onmouseover="alert(1))');
    assert.deepEqual(anchorAttrs(html), LINK_ATTRS, html);
    assert.equal(
      html,
      '<p><a href="https://x.dev/%22onmouseover=%22alert(1" target="_blank" rel="noreferrer">a</a>)</p>',
    );
  });

  it("percent-encodes a single quote and a backtick too", () => {
    assert.equal(
      render("https://x.dev/a'b"),
      '<p><a href="https://x.dev/a%27b" target="_blank" rel="noreferrer">https://x.dev/a\'b</a></p>',
    );
    assert.equal(
      render("[t](https://x.dev/a`b)"),
      '<p><a href="https://x.dev/a%60b" target="_blank" rel="noreferrer">t</a></p>',
    );
  });

  it("ends a bare URL at HTML an earlier pass stashed", () => {
    // The code span and the markdown link are stashed before the bare-URL
    // pass runs; a URL that ran into the token put that HTML in its href.
    assert.equal(
      render("see https://x.dev/`code` now"),
      '<p>see <a href="https://x.dev/" target="_blank" rel="noreferrer">https://x.dev/</a><code>code</code> now</p>',
    );
    const html = render("https://x.dev/[a](https://y.dev/)");
    assert.equal(
      html,
      '<p><a href="https://x.dev/" target="_blank" rel="noreferrer">https://x.dev/</a>' +
        '<a href="https://y.dev/" target="_blank" rel="noreferrer">a</a></p>',
    );
    for (const href of html.matchAll(/href="([^"]*)"/g)) assert.ok(!href[1].includes("<"), html);
  });

  it("still trims trailing punctuation and entities off a bare URL", () => {
    assert.equal(
      render("see https://x.dev/a."),
      '<p>see <a href="https://x.dev/a" target="_blank" rel="noreferrer">https://x.dev/a</a>.</p>',
    );
    assert.equal(
      render("(https://x.dev/a)"),
      '<p>(<a href="https://x.dev/a" target="_blank" rel="noreferrer">https://x.dev/a</a>)</p>',
    );
    assert.equal(
      render("go to https://x.dev/a&"),
      '<p>go to <a href="https://x.dev/a" target="_blank" rel="noreferrer">https://x.dev/a</a>&amp;</p>',
    );
  });

  it("keeps an escaped ampersand inside a URL as an entity", () => {
    assert.equal(
      render("https://x.dev/?a=1&b=2"),
      '<p><a href="https://x.dev/?a=1&amp;b=2" target="_blank" rel="noreferrer">https://x.dev/?a=1&amp;b=2</a></p>',
    );
  });

  it("still closes emphasis around a bare URL", () => {
    assert.equal(
      render("**bold https://x.dev** more"),
      '<p><strong>bold <a href="https://x.dev" target="_blank" rel="noreferrer">https://x.dev</a></strong> more</p>',
    );
  });

  it("still percent-encodes a quote in a media src", () => {
    assert.equal(
      render('![pic](https://x.dev/a"b.png)'),
      '<p><img class="md-img" alt="pic" src="https://x.dev/a%22b.png" loading="lazy" /></p>',
    );
  });
});

describe("renderProse workspace file mentions", () => {
  const files = buildFileIndex([
    {
      kind: "document",
      name: "BUSINESS_PROFILE.md",
      path: "templates/BUSINESS_PROFILE.md",
      url: "/api/files-raw/templates/BUSINESS_PROFILE.md",
      files_url: "/files?path=templates/BUSINESS_PROFILE.md",
      mentions: ["`templates/BUSINESS_PROFILE.md`"],
    } as MediaRef,
  ]);

  it("links a backticked path inside its code span, never leaving a stash token", () => {
    // The link is stashed before the code-span pass wraps it, so the code
    // span's stash entry holds a token of its own. A one-pass restore left
    // "\x000\x00" in the HTML, which the chat drew as a bare "0".
    const html = renderProse(escapeHtml("- come from `templates/BUSINESS_PROFILE.md`."), files);
    assert.ok(!html.includes("\x00"), html);
    assert.equal(
      html,
      '<ul><li>come from <code><a class="md-file" data-internal="1" ' +
        'href="/files?path=templates/BUSINESS_PROFILE.md">templates/BUSINESS_PROFILE.md</a></code>.</li></ul>',
    );
  });

  it("links a bare path in prose", () => {
    const html = renderProse(escapeHtml("see templates/BUSINESS_PROFILE.md now"), files);
    assert.ok(!html.includes("\x00"), html);
    assert.ok(html.includes('class="md-file"'), html);
  });
});

describe("renderProse tables", () => {
  const table = [
    "| Property | Asking price | Details |",
    "|---|--:|---|",
    "| [531 Serangoon North Avenue 4](https://example.com/a) | S$688,000 | " +
      "3 bed, 2 bath; 1,108 sqft; S$620.94 psf, 590 m from Serangoon North MRT. |",
  ].join("\n");

  it("classes every cell of a column by its longest cell", () => {
    const html = render(table);
    assert.equal(count(html, 'class="md-col-m"'), 2, html);
    assert.equal(count(html, 'class="md-col-s"'), 2, html);
    assert.equal(count(html, 'class="md-col-l"'), 2, html);
  });

  it("measures a link by its label, not its URL", () => {
    const html = render("| A |\n|---|\n| [short](https://example.com/a/very/long/path/that/goes/on/and/on) |");
    assert.equal(count(html, 'class="md-col-s"'), 2, html);
  });

  it("keeps the alignment next to the column class", () => {
    assert.ok(render(table).includes('<td class="md-col-s" style="text-align:right">S$688,000</td>'));
  });
});
