import { test } from "node:test";
import assert from "node:assert/strict";
import { batchPercent, partitionBySize, UPLOAD_MAX_BYTES, uploadUrl } from "./upload.ts";

test("the upload URL carries the agent, folder and name, encoded", () => {
  const url = new URL(uploadUrl("itus", "reports/q3", "PM Trade Blotter & co.xlsx"), "http://x");
  assert.equal(url.pathname, "/api/files/upload");
  assert.equal(url.searchParams.get("agent"), "itus");
  assert.equal(url.searchParams.get("dir"), "reports/q3");
  assert.equal(url.searchParams.get("name"), "PM Trade Blotter & co.xlsx");
  assert.equal(url.searchParams.has("overwrite"), false);
  assert.equal(new URL(uploadUrl("a", "", "f", true), "http://x").searchParams.get("overwrite"), "true");
});

test("files over the cap are set aside before anything is sent", () => {
  const files = [{ size: 10 }, { size: UPLOAD_MAX_BYTES + 1 }, { size: UPLOAD_MAX_BYTES }];
  const { ok, tooLarge } = partitionBySize(files);
  assert.deepEqual(ok, [files[0], files[2]]);
  assert.deepEqual(tooLarge, [files[1]]);
});

test("batch progress is weighted by size", () => {
  // A 90-byte file half-sent after a 10-byte one: (10 + 45) / 100.
  assert.equal(batchPercent([10, 90], 1, 45), 55);
  assert.equal(batchPercent([10, 90], 2, 0), 100);
  // Empty files still advance by count.
  assert.equal(batchPercent([0, 0], 1, 0), 50);
});
