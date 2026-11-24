import { test } from "node:test";
import assert from "node:assert/strict";
import {
  CONNECTOR_DEFINITIONS,
  applyConnectorToolSwitches,
  buildConnectorConfig,
  connectorToolSwitches,
  connectorDefinitionForConfig,
  connectorVariantName,
  getConnectorDefinition,
  getConnectorDefinitionById,
  initialValues,
  pendingVaultSecrets,
  supportedCatalogChannels,
} from "./connectorCatalog.ts";

// Lark is Feishu's international edition on the same adapter: its own picker
// row, `platform:feishu` on the wire, `extra.domain: "lark"` preset. These
// pin the variant machinery that brings that row back.

test("the static list offers Lark as its own row under the Feishu type", () => {
  const lark = getConnectorDefinitionById("platform:feishu@lark");
  assert.ok(lark, "Lark row missing");
  assert.equal(lark.type, "platform:feishu");
  assert.equal(lark.label, "catalog.lark.defLabel");
  assert.deepEqual(lark.variant, { id: "lark", name: "catalog.lark.name", preset: { domain: "lark" } });
  // The base row keeps its own id and label, and owns the wire type.
  const feishu = getConnectorDefinition("platform:feishu");
  assert.ok(feishu && !feishu.variant);
  assert.equal(feishu.id, "platform:feishu");
  assert.equal(getConnectorDefinitionById("platform:feishu"), feishu);
  // Every ordinary row's id is its type.
  for (const d of CONNECTOR_DEFINITIONS) {
    if (!d.variant) assert.equal(d.id, d.type);
  }
});

test("a new Lark connector starts with the domain preset; a Feishu one leaves it to the default", () => {
  const lark = getConnectorDefinitionById("platform:feishu@lark")!;
  assert.equal(initialValues(lark, {}).domain, "lark");
  const feishu = getConnectorDefinition("platform:feishu")!;
  assert.equal(initialValues(feishu, {}).domain, "");
  // A saved value always beats the preset, editing never rewrites the config.
  assert.equal(initialValues(lark, { extra: { domain: "feishu" } }).domain, "feishu");
});

test("a saved connector opens as the row it was created from", () => {
  assert.equal(connectorDefinitionForConfig("platform:feishu", { extra: { domain: "lark" } })?.id, "platform:feishu@lark");
  assert.equal(connectorDefinitionForConfig("platform:feishu", { extra: { domain: "feishu" } })?.id, "platform:feishu");
  assert.equal(connectorDefinitionForConfig("platform:feishu", {})?.id, "platform:feishu");
  assert.equal(connectorDefinitionForConfig("platform:telegram", {})?.id, "platform:telegram");
  assert.equal(connectorDefinitionForConfig("platform:nope", {}), undefined);
  // The connector list prints "Lark" for that row and keeps its own name otherwise.
  assert.equal(connectorVariantName("platform:feishu", { extra: { domain: "lark" } }), "catalog.lark.name");
  assert.equal(connectorVariantName("platform:feishu", {}), undefined);
});

test("the live catalog expands a row's variants and carries the server labels", () => {
  const rows = supportedCatalogChannels([
    { type: "platform:feishu", label: "Feishu / Lark (platform adapter)", variants: [{ id: "lark", label: "Lark (platform adapter)", preset: { domain: "lark" } }] },
    { type: "platform:telegram", label: "Telegram (platform adapter)" },
    { type: "platform:unknown", label: "Nothing this build knows" },
  ]);
  assert.deepEqual(
    rows.map((r) => [r.id, r.label]),
    [
      ["platform:feishu", "Feishu / Lark (platform adapter)"],
      ["platform:feishu@lark", "Lark (platform adapter)"],
      ["platform:telegram", "Telegram (platform adapter)"],
    ],
  );
  // A live row without variants offers no Lark, even though the static list has one.
  const bare = supportedCatalogChannels([{ type: "platform:feishu", label: "Feishu" }]);
  assert.deepEqual(bare.map((r) => r.id), ["platform:feishu"]);
});

// WhatsApp over Twilio: the adapter reads only `extra`, and the first form
// saved its plain fields at the top level, where `PlatformConfig.from_dict`
// drops them, so a connector set up in the dashboard never saw its Account SID.

test("the WhatsApp over Twilio form saves every plain field under extra", () => {
  const def = getConnectorDefinition("platform:whatsapp_twilio")!;
  const values = initialValues(def, {});
  Object.assign(values, {
    account_sid: "AC0123",
    from_number: "+14155238886",
    service_sid: "IS1",
    mcs_region: "ie1",
    allowed_users: "+15551234567",
  });
  const built = buildConnectorConfig(def, values, (k) => k, { omitVaultSecrets: true });
  assert.equal(built.error, undefined);
  assert.deepEqual(Object.keys(built.config!).sort(), ["extra"], "nothing at the top level");
  assert.deepEqual(built.config!.extra, {
    account_sid: "AC0123",
    from_number: "+14155238886",
    service_sid: "IS1",
    mcs_region: "ie1",
    allowed_users: ["+15551234567"],
    // The untouched chat-approvals toggle saves its off state, the backend
    // default; an empty Owner numbers list saves nothing.
    chat_approvals: false,
  });
  // No field asks for a webhook: the connector registers its own.
  assert.ok(!def.fields.some((f) => /webhook/.test(f.key)));
  // Nor for a template: outside the 24-hour window the agent picks an approved
  // one itself, with its WhatsApp template tools.
  assert.ok(!def.fields.some((f) => /template/.test(f.key)));
});

test("a WhatsApp over Twilio connector saved by the old form still opens with its values", () => {
  const def = getConnectorDefinition("platform:whatsapp_twilio")!;
  const legacy = { account_sid: "AC-old", from_number: "+1555", extra: { service_sid: "IS-new" } };
  const values = initialValues(def, legacy);
  assert.equal(values.account_sid, "AC-old");
  assert.equal(values.from_number, "+1555");
  assert.equal(values.service_sid, "IS-new");
  // `extra` wins when both exist.
  assert.equal(initialValues(def, { account_sid: "AC-old", extra: { account_sid: "AC-new" } }).account_sid, "AC-new");
});

// A credential typed into a vault field is form state until Save, which stores
// it in the agent's vault before saving the connector. It used to be the
// field's own draft behind a separate "Save to vault" button, so the form's
// Save dropped it and the connector would not start for want of it.

test("Save stores exactly the credentials that were typed, trimmed", () => {
  const def = getConnectorDefinition("platform:whatsapp_twilio")!;
  const values = initialValues(def, {});
  assert.deepEqual(pendingVaultSecrets(def, values), [], "nothing typed, nothing to store");

  Object.assign(values, { account_sid: "AC0123", from_number: "+14155238886", auth_token: "  tok-123  " });
  assert.deepEqual(
    pendingVaultSecrets(def, values).map((p) => [p.vaultKey, p.value]),
    [["TWILIO_AUTH_TOKEN", "tok-123"]],
  );
  // Blank means "keep what the vault has", never "store an empty value".
  values.auth_token = "   ";
  assert.deepEqual(pendingVaultSecrets(def, values), []);
});

test("every typed credential of a connector is stored, and none rides in the payload", () => {
  const def = getConnectorDefinition("platform:slack")!;
  const values = initialValues(def, {});
  const vaultFields = def.fields.filter((f) => f.kind === "vaultSecret");
  assert.ok(vaultFields.length >= 2, "Slack has a bot token and an app token");
  for (const f of vaultFields) values[f.key] = `secret-${f.key}`;

  const pending = pendingVaultSecrets(def, values);
  assert.deepEqual(
    pending.map((p) => p.vaultKey).sort(),
    vaultFields.map((f) => f.vaultKey).sort(),
  );
  const built = buildConnectorConfig(def, values, (k) => k, { omitVaultSecrets: true });
  assert.ok(!JSON.stringify(built.config).includes("secret-"), "credentials never go into agent.json5");
});

// The connector panel lists the connector's tools with a switch each. The two
// rules below are what the switches read from and write to the agent's config.

test("the WhatsApp over Twilio panel lists the toolkit, everyday tools first", () => {
  const def = getConnectorDefinition("platform:whatsapp_twilio")!;
  assert.deepEqual(
    def.tools.filter((d) => !d.admin).map((d) => d.name),
    ["whatsapp_template_list", "whatsapp_template_send", "whatsapp_message_status", "whatsapp_window_status"],
  );
  assert.equal(def.tools.filter((d) => d.admin).length, 8);
  assert.deepEqual(getConnectorDefinition("platform:telegram")!.tools, []);
});

test("switches read from tools/disabled_tools and write back only what was flipped", () => {
  const def = getConnectorDefinition("platform:whatsapp_twilio")!;
  const cfg = { tools: ["web_search", "whatsapp_template_create"], disabled_tools: ["whatsapp_template_send", "memory_save"] };
  const state = connectorToolSwitches(def.tools, cfg);
  assert.equal(state.whatsapp_template_list, true, "everyday: on unless disabled");
  assert.equal(state.whatsapp_template_send, false);
  assert.equal(state.whatsapp_template_create, true, "admin: on only when listed");
  assert.equal(state.whatsapp_template_delete, false);

  const next = applyConnectorToolSwitches(def.tools, cfg, {
    ...state,
    whatsapp_template_send: true,
    whatsapp_window_status: false,
    whatsapp_template_create: false,
    whatsapp_template_status: true,
  });
  assert.deepEqual(next.tools, ["web_search", "whatsapp_template_status"], "other tools are kept");
  assert.deepEqual(next.disabled_tools, ["memory_save", "whatsapp_window_status"], "other disables are kept");
});
