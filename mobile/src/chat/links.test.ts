import { strict as assert } from "node:assert";
import { test } from "node:test";

import { hrefFor, linkifyReply, proseHtml as render, splitLinks, trimUrl } from "./links.ts";

test("a sentence's punctuation stays out of the link; a URL's own parenthesis stays in", () => {
  assert.deepEqual(trimUrl("https://x.dev/a."), { url: "https://x.dev/a", rest: "." });
  assert.deepEqual(trimUrl("https://x.dev/a),"), { url: "https://x.dev/a", rest: ")," });
  assert.deepEqual(trimUrl("https://en.wikipedia.org/wiki/Mercury_(planet)."), {
    url: "https://en.wikipedia.org/wiki/Mercury_(planet)",
    rest: ".",
  });
});

test("www addresses open over https; anything that is not a web address opens nothing", () => {
  assert.equal(hrefFor("www.olano.ai/docs"), "https://www.olano.ai/docs");
  assert.equal(hrefFor("https://acme.olano.run/api/connect/abc"), "https://acme.olano.run/api/connect/abc");
  assert.equal(hrefFor("http://localhost:3000/x"), "http://localhost:3000/x");
  assert.equal(hrefFor("https://nodot"), null);
  assert.equal(hrefFor("javascript:alert(1)"), null);
  assert.equal(hrefFor('https://x.dev/"onmouseover="a'), "https://x.dev/%22onmouseover=%22a");
});

test("a message's own text is cut into words and links", () => {
  assert.deepEqual(splitLinks("Look at https://x.dev/a, and www.olano.ai."), [
    { text: "Look at " },
    { text: "https://x.dev/a", href: "https://x.dev/a" },
    { text: ", and " },
    { text: "www.olano.ai", href: "https://www.olano.ai" },
    { text: "." },
  ]);
  assert.deepEqual(splitLinks("no links here"), [{ text: "no links here" }]);
});

test("a reply's bare URLs and Markdown links are links", () => {
  assert.match(render("See https://x.dev/a."), /<a href="https:\/\/x\.dev\/a"[^>]*>https:\/\/x\.dev\/a<\/a>\./);
  assert.match(render("[Connect](https://acme.olano.run/api/connect/abc)"), /<a href="https:\/\/acme\.olano\.run\/api\/connect\/abc"[^>]*>Connect<\/a>/);
});

test("a URL in code formatting and a www. address become links too", () => {
  assert.match(render("Open `https://acme.olano.run/api/connect/abc` to sign in"), /<a href="https:\/\/acme\.olano\.run\/api\/connect\/abc"/);
  assert.match(render("Docs at www.olano.ai/docs."), /<a href="https:\/\/www\.olano\.ai\/docs"[^>]*>www\.olano\.ai\/docs<\/a>\./);
  assert.match(render("(see www.olano.ai)"), /\(see <a href="https:\/\/www\.olano\.ai"[^>]*>www\.olano\.ai<\/a>\)/);
  // Longer code stays code; a link's own label is left alone.
  assert.match(render("Run `curl https://x.dev/a`"), /<code>curl https:\/\/x\.dev\/a<\/code>/);
  assert.equal(linkifyReply("[www.olano.ai](https://www.olano.ai)"), "[www.olano.ai](https://www.olano.ai)");
});

test("markup in a reply is shown as text, never run", () => {
  const html = render('Use <img src=x onerror="alert(1)"> and <b>bold</b>');
  assert.ok(!html.includes("<img"), html);
  assert.ok(!html.includes("<b>"), html);
  assert.match(html, /&lt;b&gt;bold&lt;\/b&gt;/);
});

test("a quote inside a URL cannot add attributes to its link", () => {
  const html = render('see https://x.dev/"onmouseover="alert(1)"x and [a](https://x.dev/"onclick="b)');
  assert.ok(!/ onmouseover=| onclick=|"onmouseover|"onclick/.test(html), html);
  assert.match(html, /href="https:\/\/x\.dev\/%22onmouseover=%22alert\(1\)%22x"/);
});

test("a quote opening a line still renders as a quote", () => {
  assert.match(render("> Quoted words\n\nAfter"), /<blockquote>Quoted words<\/blockquote>/);
  assert.match(render("1 > 0"), /1 &gt; 0/);
});
