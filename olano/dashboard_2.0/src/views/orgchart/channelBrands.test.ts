import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  CARD_CHROME_X,
  CHIP_GAP,
  CHIP_SIZE,
  activityMatchesChannel,
  channelBrand,
  channelDisplayName,
  channelSlug,
  channelStatus,
  chipLayout,
  worstStatus,
} from "./channelBrands.ts";

describe("channelBrand", () => {
  it("maps legacy and platform spellings of one messenger to the same mark", () => {
    assert.equal(channelBrand("telegram"), "telegram");
    assert.equal(channelBrand("platform:telegram"), "telegram");
    assert.equal(channelBrand("whatsapp"), "whatsapp");
    assert.equal(channelBrand("platform:whatsapp"), "whatsapp");
  });

  it("gives the official WhatsApp Business routes the WhatsApp mark", () => {
    assert.equal(channelBrand("platform:whatsapp_twilio"), "whatsapp");
    assert.equal(channelBrand("platform:whatsapp_meta_api"), "whatsapp");
  });

  it("folds aliases onto one mark and unknown types onto the generic chip", () => {
    assert.equal(channelBrand("imap"), "email");
    assert.equal(channelBrand("platform:bluebubbles"), "imessage");
    assert.equal(channelBrand("platform:weixin"), "wechat");
    assert.equal(channelBrand("platform:wecom_callback"), "wecom");
    assert.equal(channelBrand("platform:something_new"), "generic");
    assert.equal(channelBrand(""), "generic");
  });
});

describe("channelSlug / channelDisplayName", () => {
  it("strips the platform prefix and lower-cases", () => {
    assert.equal(channelSlug("platform:Slack"), "slack");
    assert.equal(channelSlug("discord"), "discord");
  });

  it("keeps product casing and spells out what a capitalize would get wrong", () => {
    assert.equal(channelDisplayName("platform:whatsapp"), "WhatsApp");
    assert.equal(channelDisplayName("platform:whatsapp_twilio"), "WhatsApp (Twilio)");
    assert.equal(channelDisplayName("telegram"), "Telegram");
    assert.equal(channelDisplayName("imap"), "Email (IMAP)");
    assert.equal(channelDisplayName("platform:homeassistant"), "Home Assistant");
    assert.equal(channelDisplayName(""), "");
  });
});

describe("activityMatchesChannel", () => {
  it("matches a live turn's bare channel to the configured type", () => {
    assert.equal(activityMatchesChannel("telegram", "platform:telegram"), true);
    assert.equal(activityMatchesChannel("telegram", "telegram"), true);
    assert.equal(activityMatchesChannel("email", "imap"), true);
  });

  it("never lights a sibling route of the same messenger", () => {
    assert.equal(activityMatchesChannel("whatsapp_twilio", "platform:whatsapp"), false);
    assert.equal(activityMatchesChannel("whatsapp", "platform:whatsapp_meta_api"), false);
    assert.equal(activityMatchesChannel("", "platform:whatsapp"), false);
  });
});

describe("channelStatus / worstStatus", () => {
  it("reads an unknown wire value as connected (no badge)", () => {
    assert.equal(channelStatus({ type: "x", status: "weird" }), "connected");
    assert.equal(channelStatus({ type: "x", status: "error" }), "error");
  });

  it("ranks error over connecting over stopped over connected", () => {
    const c = (status: string) => ({ type: "t", status });
    assert.equal(worstStatus([]), "connected");
    assert.equal(worstStatus([c("connected"), c("stopped")]), "stopped");
    assert.equal(worstStatus([c("stopped"), c("connecting")]), "connecting");
    assert.equal(worstStatus([c("connecting"), c("error"), c("connected")]), "error");
  });
});

describe("chipLayout", () => {
  const widthFor = (slots: number) => CARD_CHROME_X + slots * CHIP_SIZE + (slots - 1) * CHIP_GAP;

  it("shows every chip when they fit", () => {
    assert.deepEqual(chipLayout(0, 176), { shown: 0, overflow: 0 });
    assert.deepEqual(chipLayout(3, 176), { shown: 3, overflow: 0 });
    // The default 176px card fits exactly 7 chips.
    assert.deepEqual(chipLayout(7, 176), { shown: 7, overflow: 0 });
  });

  it("gives the +N chip a slot of its own when the row overflows", () => {
    assert.deepEqual(chipLayout(8, 176), { shown: 6, overflow: 2 });
    assert.deepEqual(chipLayout(5, widthFor(4)), { shown: 3, overflow: 2 });
    assert.deepEqual(chipLayout(4, widthFor(4)), { shown: 4, overflow: 0 });
  });

  it("degrades to a lone +N on a card too narrow for anything else", () => {
    assert.deepEqual(chipLayout(3, 10), { shown: 0, overflow: 3 });
    assert.deepEqual(chipLayout(1, 10), { shown: 1, overflow: 0 });
  });
});
