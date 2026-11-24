import { strict as assert } from "node:assert";
import { test } from "node:test";

import { filesOf, foldTranscript, photosOf } from "./model.ts";
import {
  fitWithin,
  MAX_PHOTOS,
  photoLayout,
  photoName,
  singlePhotoBox,
  inlinedFileNames,
  stripAttachmentNotes,
  takeRoom,
  wireAttachment,
} from "./photos.ts";

// The runtime writes an em dash in its note; spelled as an escape here.
const DASH = "\u2014";
const NOTE = `[Image attached: photo-1.jpg ${DASH} saved to /home/olano/agents/nova/workspace/uploads/photo-1.jpg (image/jpeg, 412.0 KB)]`;
const GUIDANCE =
  "[Note: the attached file(s) above are saved in your workspace but are NOT shown to you inline. Decide from the user's message what to do with them: to view or analyze an image, call a vision tool such as image_describe with the saved path; to publish, send, or process a file, pass the saved path to the relevant tool; if the user only wants the file kept, no tool call is needed.]";

test("fitWithin scales the longest side down to the limit and never up", () => {
  assert.deepEqual(fitWithin(4032, 3024, 2048), { width: 2048, height: 1536 });
  assert.deepEqual(fitWithin(3024, 4032, 2048), { width: 1536, height: 2048 });
  assert.deepEqual(fitWithin(800, 600, 2048), { width: 800, height: 600 });
  assert.deepEqual(fitWithin(10000, 10, 2048), { width: 2048, height: 2 });
  assert.deepEqual(fitWithin(0, 600, 2048), { width: 0, height: 0 });
});

test("a message's photos are laid out like the platform's own messages", () => {
  assert.deepEqual(photoLayout(1), { kind: "single", shown: 1, more: 0 });
  assert.deepEqual(photoLayout(2), { kind: "pair", shown: 2, more: 0 });
  assert.deepEqual(photoLayout(3), { kind: "trio", shown: 3, more: 0 });
  assert.deepEqual(photoLayout(4), { kind: "grid", shown: 4, more: 0 });
  assert.deepEqual(photoLayout(7), { kind: "grid", shown: 4, more: 3 });
});

test("a single photo keeps its shape within bounds, but never a sliver", () => {
  assert.deepEqual(singlePhotoBox(4000, 3000, 248, 320), { width: 248, height: 186 });
  assert.deepEqual(singlePhotoBox(3000, 4000, 248, 320), { width: 240, height: 320 });
  // A panorama is capped at 2.5:1, a tall screenshot at 1:2.5.
  assert.deepEqual(singlePhotoBox(10000, 1000, 248, 320), { width: 248, height: 99 });
  assert.deepEqual(singlePhotoBox(1000, 10000, 248, 320), { width: 128, height: 320 });
  // Size unknown until it loads: a 4:3 placeholder.
  assert.deepEqual(singlePhotoBox(undefined, undefined, 248, 320), { width: 248, height: 186 });
});

test("the composer takes photos up to the limit and says how many it left out", () => {
  assert.deepEqual(takeRoom(0, 3), { take: 3, dropped: 0 });
  assert.deepEqual(takeRoom(8, 5), { take: 2, dropped: 3 });
  assert.deepEqual(takeRoom(MAX_PHOTOS, 1), { take: 0, dropped: 1 });
});

test("photo names say when they were sent and their place in the message", () => {
  assert.equal(photoName(new Date(2026, 9, 16, 9, 5, 3), 0), "photo-20261016-090503-1.jpg");
  assert.equal(photoName(new Date(2026, 0, 2, 23, 59, 59), 9), "photo-20260102-235959-10.jpg");
});

test("a photo travels as the runtime's image attachment", () => {
  const wire = wireAttachment({
    name: "photo-1.jpg",
    mime: "image/jpeg",
    data: "AAAA",
    bytes: 3,
    width: 1,
    height: 1,
    url: "blob:x",
  });
  assert.deepEqual(wire, { kind: "image", name: "photo-1.jpg", mime: "image/jpeg", data: "AAAA" });
});

test("the runtime's notes about attachments are not the person's words", () => {
  assert.equal(stripAttachmentNotes(`What is on this label?\n\n${NOTE}\n\n${GUIDANCE}`), "What is on this label?");
  assert.equal(stripAttachmentNotes(`${NOTE}\n\n${NOTE}\n\n${GUIDANCE}`), "");
  assert.equal(
    stripAttachmentNotes(
      `Read this\n\n[Document attached: q3.pdf, saved to /w/uploads/q3.pdf (application/pdf, 1.2 MB). Its contents are NOT shown inline: to read it, call document_to_text with this path.]`,
    ),
    "Read this",
  );
  // Words that merely look bracketed stay.
  assert.equal(stripAttachmentNotes("[draft] a plan\n[x] done"), "[draft] a plan\n[x] done");
  assert.equal(stripAttachmentNotes("no notes here"), "no notes here");
});

test("a reloaded photo message shows the photo and the words, not the notes", () => {
  const [message] = foldTranscript([
    {
      role: "user",
      content: `What is on this label?\n\n${NOTE}\n\n${GUIDANCE}`,
      media: [
        { kind: "image", name: "photo-1.jpg", mime: "image/jpeg", url: "/api/files-raw/uploads/photo-1.jpg?agent=nova", path: "uploads/photo-1.jpg" },
        { kind: "document", name: "q3.pdf", mime: "application/pdf", url: "/api/files-raw/uploads/q3.pdf?agent=nova", path: "uploads/q3.pdf" },
      ],
    },
  ]);
  assert.equal(message.body, "What is on this label?");
  assert.deepEqual(
    photosOf(message).map((p) => [p.name, p.url]),
    [["photo-1.jpg", "/api/files-raw/uploads/photo-1.jpg?agent=nova"]],
  );
  assert.deepEqual(
    filesOf(message).map((f) => f.name),
    ["q3.pdf"],
  );
});

test("a photo this phone just sent is drawn from its own copy", () => {
  const photos = photosOf({ photos: [{ key: "k", name: "photo-1.jpg", src: "blob:local" }], media: [] });
  assert.deepEqual(photos, [{ key: "k", name: "photo-1.jpg", src: "blob:local" }]);
});

test("a text file read into the message is a name, not its whole contents", () => {
  const row =
    "Summarise this\n\n\n--- attached: notes.txt (text/plain) ---\nline one\nline two\n--- end notes.txt ---\n\n" +
    "\n--- attached: data.csv (text/csv) ---\na,b\n1,2\n--- end data.csv ---";
  assert.deepEqual(inlinedFileNames(row), ["notes.txt", "data.csv"]);
  assert.equal(stripAttachmentNotes(row), "Summarise this");
  assert.deepEqual(inlinedFileNames("--- attached: a sentence about dashes"), []);
  assert.equal(stripAttachmentNotes("--- attached: x (y) ---\nno end line"), "--- attached: x (y) ---\nno end line");
});
