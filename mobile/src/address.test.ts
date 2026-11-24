import assert from "node:assert/strict";
import { test } from "node:test";

import { hostOf, parseAddress } from "./address.ts";

const release = { allowHttp: false };
const debug = { allowHttp: true };

function origin(input: string, options = release): string {
  const result = parseAddress(input, options);
  assert.equal(result.ok, true, `expected ${JSON.stringify(input)} to parse`);
  return result.ok ? result.origin : "";
}

function error(input: string, options = release): string {
  const result = parseAddress(input, options);
  assert.equal(result.ok, false, `expected ${JSON.stringify(input)} to be refused`);
  return result.ok ? "" : result.error;
}

test("a bare name is a managed deployment", () => {
  assert.equal(origin("acme"), "https://acme.olano.run");
  assert.equal(origin("  Acme-Corp  "), "https://acme-corp.olano.run");
  assert.equal(origin("a1"), "https://a1.olano.run");
});

test("a host gets https", () => {
  assert.equal(origin("agents.example.com"), "https://agents.example.com");
  assert.equal(origin("acme.olano.run/"), "https://acme.olano.run");
  assert.equal(origin("agents.example.com:8443"), "https://agents.example.com:8443");
});

test("a full URL is reduced to its origin", () => {
  assert.equal(origin("https://agents.example.com/chat?x=1#y"), "https://agents.example.com");
  assert.equal(origin("HTTPS://Agents.Example.COM"), "https://agents.example.com");
  assert.equal(origin("https://acme.olano.run:443/"), "https://acme.olano.run");
});

test("plain http is a debug-build convenience only", () => {
  assert.match(error("http://192.168.1.20:8765"), /http/);
  assert.equal(origin("http://192.168.1.20:8765", debug), "http://192.168.1.20:8765");
  assert.equal(origin("http://localhost:8765", debug), "http://localhost:8765");
  const parsed = parseAddress("http://192.168.1.20:8765", debug);
  assert.equal(parsed.ok && parsed.insecure, true);
});

test("refuses what cannot be a deployment address", () => {
  error("");
  error("   ");
  error("acme corp");
  error("ftp://example.com");
  error("javascript:alert(1)");
  error("mailto:someone@example.com");
  error("https://user:secret@example.com");
  error("-acme");
  assert.match(error("https://acme"), /acme\.olano\.run/);
});

test("hostOf shows the host of an origin", () => {
  assert.equal(hostOf("https://acme.olano.run"), "acme.olano.run");
  assert.equal(hostOf("http://192.168.1.20:8765"), "192.168.1.20:8765");
  assert.equal(hostOf("not a url"), "not a url");
});
