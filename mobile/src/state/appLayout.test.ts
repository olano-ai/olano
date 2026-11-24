import { strict as assert } from "node:assert";
import { test } from "node:test";

import { DEFAULT_LAYOUT, inboxSections, parseLayout, redirectFor, showsInbox, transcribeOn } from "./appLayout.ts";

test("an empty or old deployment shows everything and transcribes on the deployment", () => {
  assert.deepEqual(parseLayout(undefined), DEFAULT_LAYOUT);
  assert.deepEqual(parseLayout({ cortex_tab: "no", inbox: "yes" }), DEFAULT_LAYOUT);
  assert.equal(DEFAULT_LAYOUT.transcribeOnPhoneDefault, false);
});

test("the deployment's answer is read field by field", () => {
  const layout = parseLayout({
    revision: "abc",
    cortex_tab: false,
    inbox: { enabled: true, approvals: false, alerts: true, issues: false },
    settings_tab: { files: false, credits_usage: false, transcribe_on_phone: false },
    transcribe_on_phone_default: true,
  });
  assert.equal(layout.revision, "abc");
  assert.equal(layout.cortexTab, false);
  assert.deepEqual(inboxSections(layout), ["alerts"]);
  assert.equal(layout.settingsTab.creditsUsage, false);
  assert.equal(layout.transcribeOnPhoneDefault, true);
});

test("the Inbox tab goes when it is off or has no section left", () => {
  assert.equal(showsInbox(DEFAULT_LAYOUT), true);
  const off = parseLayout({ inbox: { enabled: false } });
  assert.deepEqual(inboxSections(off), []);
  assert.equal(showsInbox(off), false);
  const empty = parseLayout({ inbox: { approvals: false, alerts: false, issues: false } });
  assert.equal(showsInbox(empty), false);
});

test("a hidden screen sends the phone somewhere it can be", () => {
  const hidden = parseLayout({
    cortex_tab: false,
    inbox: { issues: false },
    settings_tab: { files: false, credits_usage: false },
  });
  assert.equal(redirectFor("/cortex", hidden), "/chats");
  assert.equal(redirectFor("/cortex/aurelius/item-1", hidden), "/chats");
  assert.equal(redirectFor("/inbox", hidden), null); // approvals and alerts remain
  assert.equal(redirectFor("/inbox/issues/12", hidden), "/chats");
  assert.equal(redirectFor("/settings/files/navi", hidden), "/settings");
  assert.equal(redirectFor("/settings/usage", hidden), "/settings");
  assert.equal(redirectFor("/settings", hidden), null);
  assert.equal(redirectFor("/chats/navi/_files/reports", hidden), "/chats/navi/_info");
  assert.equal(redirectFor("/chats/navi/abc123", hidden), null);
  for (const path of ["/cortex", "/inbox/issues/1", "/settings/files", "/chats/a/_files"]) {
    assert.equal(redirectFor(path, DEFAULT_LAYOUT), null);
  }
  assert.equal(redirectFor("/inbox", parseLayout({ inbox: { enabled: false } })), "/chats");
});

test("voice notes follow the person's choice only while the switch is shown and chosen", () => {
  const chosePhone = { transcribeOn: "phone" as const, transcribeChosen: true };
  const neverChose = { transcribeOn: "phone" as const, transcribeChosen: false };
  // Default: the deployment transcribes, unless the person turned it on.
  assert.equal(transcribeOn(neverChose, DEFAULT_LAYOUT), "deployment");
  assert.equal(transcribeOn(chosePhone, DEFAULT_LAYOUT), "phone");
  // The admin's default applies to anyone who has not chosen.
  const phoneDefault = parseLayout({ transcribe_on_phone_default: true });
  assert.equal(transcribeOn(neverChose, phoneDefault), "phone");
  assert.equal(transcribeOn({ transcribeOn: "deployment", transcribeChosen: true }, phoneDefault), "deployment");
  // Switch hidden: the default for everyone, earlier choices included.
  const hiddenSwitch = parseLayout({ settings_tab: { transcribe_on_phone: false } });
  assert.equal(transcribeOn(chosePhone, hiddenSwitch), "deployment");
});
