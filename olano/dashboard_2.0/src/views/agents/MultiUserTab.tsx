// Per-agent Multi-user tab — every knob for serving many isolated end-users
// from one deployed agent, grouped in one place: the mode switch (per-user
// memory/file isolation), the prompt extension, the allowed-actions script
// allow-list that replaces shell access, and per-user scheduling with its cost
// caps. Everything here writes ordinary agent.json5 fields via the same PUT
// the Overview uses; tool bindings (per-user tools, execute_action, the
// schedule_my_task trio) are baked at build time, so changes fully apply after
// a restart — hence the note at the bottom.

import { useEffect, useState } from "react";
import { Copy } from "lucide-react";
import { api } from "../../core/api";
import { useT } from "../../core/i18n";
import { useToast } from "../../ui/toast";
import { Toggle, KvRow } from "./Toggle";
import { CustomersCard } from "./CustomersCard";
import { STR } from "./strings";
import type { AgentConfig, CustomerServiceConfig, MessagePolicyConfig } from "./types";

function Card({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="card" style={{ padding: 16, marginBottom: 14 }}>
      <strong style={{ fontSize: 13, display: "block", marginBottom: 10 }}>{title}</strong>
      {children}
    </div>
  );
}

/** Two prompt-extension variants, offered behind copy buttons. The support-desk
 * one also fills the field server-side when it is blank and multi-user mode is
 * on, so the textarea arrives pre-filled rather than empty (see
 * config/customer_service.py); neither ever overwrites wording an operator
 * typed. Moved here from the Overview when multi-user settings got their own
 * tab. */
type SuggestionVariant = "multi_user" | "support";

function CustomerServiceExtension({ value, onCommit }: { value: string; onCommit: (next: string) => void }) {
  const t = useT(STR);
  const toast = useToast();
  const [v, setV] = useState(value);
  // Two starting points: privacy-only (no assumptions about what the agent is
  // for) and the support-desk one (privacy + "decline anything off-topic"
  // guardrails, which would make a job-hunt or bookkeeping agent refuse its own
  // job). The selected variant starts on whichever the server reports as the
  // blank-field default, so the buttons agree with what is already in the box.
  const [suggestions, setSuggestions] = useState<Partial<Record<SuggestionVariant, string>>>({});
  const [variant, setVariant] = useState<SuggestionVariant>("support");
  const [showPreview, setShowPreview] = useState(false);
  useEffect(() => setV(value), [value]);
  const suggested = suggestions[variant] || "";

  // Fetched rather than duplicated in the frontend so the wording has one
  // source of truth (config/customer_service.py).
  useEffect(() => {
    let alive = true;
    api
      .cachedGet<{
        suggested_prompt_extension?: string;
        suggested_prompt_extensions?: Partial<Record<SuggestionVariant, string>>;
        default_prompt_extension_variant?: SuggestionVariant;
      }>("/api/customer-service/defaults", 300000)
      .then((d) => {
        if (!alive) return;
        // An older server sends only the singular key (the support variant).
        setSuggestions(
          d.suggested_prompt_extensions || { support: d.suggested_prompt_extension || "" },
        );
        if (d.default_prompt_extension_variant) setVariant(d.default_prompt_extension_variant);
      })
      .catch(() => {
        /* non-fatal: the field still works, only the suggestion is unavailable */
      });
    return () => {
      alive = false;
    };
  }, []);

  async function copySuggested() {
    try {
      await navigator.clipboard.writeText(suggested);
      toast.push(t("ov.cs.copied"), "success");
    } catch {
      // Clipboard API needs a secure context; the preview gives a manual path.
      setShowPreview(true);
      toast.push(t("ov.cs.copyFailed"), "error");
    }
  }

  return (
    <div style={{ marginTop: 12 }}>
      <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 4 }}>{t("ov.cs.ext")}</div>
      <p className="muted" style={{ fontSize: 11, margin: "0 0 8px", lineHeight: 1.45 }}>
        {t("ov.cs.extHint")}
      </p>
      <textarea
        className="input"
        value={v}
        placeholder={t("ov.cs.extPlaceholder")}
        onChange={(e) => setV(e.target.value)}
        onBlur={() => {
          if (v !== value) onCommit(v);
        }}
        style={{
          minHeight: 180,
          resize: "vertical",
          fontFamily: "var(--font-mono)",
          fontSize: 13,
          lineHeight: 1.5,
        }}
      />
      <div className="row" style={{ justifyContent: "space-between", marginTop: 8, flexWrap: "wrap", gap: 8 }}>
        <span className="muted" style={{ fontSize: 11 }}>
          {t("ov.autosave")}
        </span>
        <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
          {(["multi_user", "support"] as SuggestionVariant[])
            .filter((k) => suggestions[k])
            .map((k) => (
              <button
                key={k}
                className={variant === k ? "btn row" : "btn ghost row"}
                style={{ padding: "3px 8px", fontSize: 12, gap: 5 }}
                aria-pressed={variant === k}
                onClick={() => setVariant(k)}
              >
                {k === "multi_user" ? t("ov.cs.variantPrivacy") : t("ov.cs.variantSupport")}
              </button>
            ))}
          <button
            className="btn ghost row"
            style={{ padding: "3px 8px", fontSize: 12, gap: 5 }}
            disabled={!suggested}
            onClick={() => setShowPreview((x) => !x)}
          >
            {showPreview ? t("ov.cs.hide") : t("ov.cs.preview")}
          </button>
          <button
            className="btn row"
            style={{ padding: "3px 8px", fontSize: 12, gap: 5 }}
            disabled={!suggested}
            onClick={() => void copySuggested()}
          >
            <Copy size={13} strokeWidth={1.75} />
            {t("ov.cs.copySuggested")}
          </button>
        </div>
      </div>
      {showPreview && suggested ? (
        <pre
          style={{
            marginTop: 8,
            padding: 10,
            maxHeight: 260,
            overflow: "auto",
            fontSize: 12,
            lineHeight: 1.5,
            whiteSpace: "pre-wrap",
            wordBreak: "break-word",
            background: "var(--surface-2, rgba(127,127,127,0.08))",
            borderRadius: 6,
          }}
        >
          {suggested}
        </pre>
      ) : null}
    </div>
  );
}

/** Editable name → script rows for `allowed_actions`. Drafted locally and
 * committed with an explicit Save, so half-typed rows never hit the config. */
function ActionsEditor({
  actions,
  onSave,
}: {
  actions: Record<string, string>;
  onSave: (next: Record<string, string>) => void;
}) {
  const t = useT(STR);
  const toast = useToast();
  const toRows = (a: Record<string, string>) =>
    Object.entries(a).map(([name, script]) => ({ name, script }));
  const [rows, setRows] = useState(toRows(actions));
  useEffect(() => setRows(toRows(actions)), [actions]);

  const dirty = JSON.stringify(toRows(actions)) !== JSON.stringify(rows);

  function commit() {
    const next: Record<string, string> = {};
    for (const row of rows) {
      const name = row.name.trim();
      const script = row.script.trim();
      if (!name && !script) continue; // an abandoned empty row is not an error
      if (!name || !script) {
        toast.push(t("mu.actionsIncomplete"), "error");
        return;
      }
      if (next[name] !== undefined) {
        toast.push(t("mu.actionsDuplicate", { name }), "error");
        return;
      }
      next[name] = script;
    }
    onSave(next);
  }

  return (
    <div>
      {rows.length === 0 ? (
        <p className="muted" style={{ fontSize: 12, margin: "0 0 8px" }}>
          {t("mu.actionsEmpty")}
        </p>
      ) : (
        rows.map((row, i) => (
          <div key={i} className="row" style={{ gap: 8, marginBottom: 6, flexWrap: "wrap" }}>
            <input
              className="input"
              style={{ width: 180 }}
              placeholder={t("mu.actionName")}
              value={row.name}
              onChange={(e) =>
                setRows((r) => r.map((x, j) => (j === i ? { ...x, name: e.target.value } : x)))
              }
            />
            <input
              className="input"
              style={{ flex: 1, minWidth: 220 }}
              placeholder={t("mu.actionScript")}
              value={row.script}
              onChange={(e) =>
                setRows((r) => r.map((x, j) => (j === i ? { ...x, script: e.target.value } : x)))
              }
            />
            <button
              className="btn ghost"
              style={{ padding: "3px 8px", fontSize: 12 }}
              onClick={() => setRows((r) => r.filter((_, j) => j !== i))}
            >
              {t("mu.actionRemove")}
            </button>
          </div>
        ))
      )}
      <div className="row" style={{ gap: 8, marginTop: 8 }}>
        <button
          className="btn ghost"
          style={{ padding: "3px 10px", fontSize: 12 }}
          onClick={() => setRows((r) => [...r, { name: "", script: "" }])}
        >
          {t("mu.actionAdd")}
        </button>
        <button
          className="btn primary"
          style={{ padding: "3px 10px", fontSize: 12 }}
          disabled={!dirty}
          onClick={commit}
        >
          {t("mu.actionsSave")}
        </button>
      </div>
    </div>
  );
}

/** `message_policy` — inbound size cap, silence rules, reply ceiling.
 *
 * Lives on this tab rather than behind the multi-user switch above because the
 * problem it solves is strangers, not per-user isolation: an agent whose chat
 * address is public needs these whether or not its memory is partitioned. Every
 * field is off by default, so an untouched card changes nothing.
 *
 * Text lists (patterns, rules) are drafted locally and parsed on blur — the
 * backend refuses a pattern that is not a valid expression, so a half-typed
 * line must never reach a PUT. */
function MessagePolicyCard({
  cfg,
  setCfg,
  savePatch,
}: {
  cfg: AgentConfig;
  setCfg: (c: AgentConfig) => void;
  savePatch: (patch: Partial<AgentConfig>, msg?: string) => Promise<boolean>;
}) {
  const t = useT(STR);
  const mp: MessagePolicyConfig = cfg.message_policy || {};

  function setField(key: string, value: unknown) {
    const next = { ...mp, [key]: value };
    setCfg({ ...cfg, message_policy: next });
    void savePatch({ message_policy: next }, t("mp.saved"));
  }

  // Numbers are drafted so typing doesn't PUT per keystroke. An empty box is a
  // real value here — null, i.e. "no limit" — so it is committed rather than
  // bounced back to the stored number the way a required field would be.
  const [maxIn, setMaxIn] = useState(mp.max_input_chars == null ? "" : String(mp.max_input_chars));
  const [maxOut, setMaxOut] = useState(
    mp.max_output_tokens == null ? "" : String(mp.max_output_tokens),
  );
  useEffect(
    () => setMaxIn(mp.max_input_chars == null ? "" : String(mp.max_input_chars)),
    [mp.max_input_chars],
  );
  useEffect(
    () => setMaxOut(mp.max_output_tokens == null ? "" : String(mp.max_output_tokens)),
    [mp.max_output_tokens],
  );

  function commitNumber(
    draft: string,
    setDraft: (v: string) => void,
    key: string,
    current: number | null | undefined,
    minimum: number,
  ) {
    const trimmed = draft.trim();
    if (!trimmed) {
      setDraft("");
      if (current != null) setField(key, null);
      return;
    }
    const n = Math.max(minimum, Math.floor(Number(trimmed) || 0));
    setDraft(String(n));
    if (n !== current) setField(key, n);
  }

  const linesToText = (v?: string[]) => (v || []).join("\n");
  const [patterns, setPatterns] = useState(linesToText(mp.ignore_patterns));
  const [rules, setRules] = useState(linesToText(mp.ignore_rules));
  useEffect(() => setPatterns(linesToText(mp.ignore_patterns)), [mp.ignore_patterns]);
  useEffect(() => setRules(linesToText(mp.ignore_rules)), [mp.ignore_rules]);

  function commitLines(draft: string, key: string, current: string[] | undefined, msg: string) {
    const parsed = draft
      .split("\n")
      .map((l) => l.trim())
      .filter(Boolean);
    if (JSON.stringify(parsed) === JSON.stringify(current || [])) return;
    const next = { ...mp, [key]: parsed };
    setCfg({ ...cfg, message_policy: next });
    void savePatch({ message_policy: next }, msg);
  }

  const hasInputCap = mp.max_input_chars != null;
  const onOversize = mp.on_oversize || "reject";
  // The "what an ignored sender sees" line only has a subject once something
  // can ignore a message, so it stays hidden until then rather than sitting
  // there inert.
  const canIgnore =
    (mp.ignore_patterns?.length || 0) > 0 ||
    (mp.ignore_rules?.length || 0) > 0 ||
    (hasInputCap && onOversize === "ignore");

  return (
    <Card title={t("mp.title")}>
      <p className="muted" style={{ fontSize: 11, lineHeight: 1.5, margin: "0 0 12px" }}>
        {t("mp.intro")}
      </p>

      <KvRow label={t("mp.maxInput")} hint={t("mp.maxInputHint")}>
        <input
          type="number"
          className="input"
          style={{ width: 110 }}
          min={1}
          placeholder="∞"
          value={maxIn}
          onChange={(e) => setMaxIn(e.target.value)}
          onBlur={() =>
            commitNumber(maxIn, setMaxIn, "max_input_chars", mp.max_input_chars, 1)
          }
        />
      </KvRow>

      {hasInputCap ? (
        <>
          <KvRow label={t("mp.onOversize")} hint={t("mp.onOversizeHint")}>
            <select
              className="input"
              style={{ width: 190 }}
              value={onOversize}
              onChange={(e) => setField("on_oversize", e.target.value)}
            >
              <option value="reject">{t("mp.oversizeReject")}</option>
              <option value="truncate">{t("mp.oversizeTruncate")}</option>
              <option value="ignore">{t("mp.oversizeIgnore")}</option>
            </select>
          </KvRow>
          {onOversize === "reject" ? (
            <div style={{ marginTop: 10 }}>
              <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 4 }}>
                {t("mp.oversizeNotice")}
              </div>
              <p className="muted" style={{ fontSize: 11, margin: "0 0 6px", lineHeight: 1.45 }}>
                {t("mp.oversizeNoticeHint")}
              </p>
              <input
                className="input"
                style={{ width: "100%" }}
                placeholder={t("mp.oversizeNoticePlaceholder")}
                defaultValue={mp.oversize_notice || ""}
                key={mp.oversize_notice || ""}
                onBlur={(e) => {
                  if (e.target.value !== (mp.oversize_notice || ""))
                    setField("oversize_notice", e.target.value.trim());
                }}
              />
            </div>
          ) : null}
        </>
      ) : null}

      <div style={{ marginTop: 14 }}>
        <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 4 }}>{t("mp.patterns")}</div>
        <p className="muted" style={{ fontSize: 11, margin: "0 0 6px", lineHeight: 1.45 }}>
          {t("mp.patternsHint")}
        </p>
        <textarea
          className="input"
          value={patterns}
          placeholder={t("mp.patternsPlaceholder")}
          onChange={(e) => setPatterns(e.target.value)}
          onBlur={() =>
            commitLines(patterns, "ignore_patterns", mp.ignore_patterns, t("mp.patternsSaved"))
          }
          style={{
            minHeight: 70,
            resize: "vertical",
            fontFamily: "var(--font-mono)",
            fontSize: 13,
            lineHeight: 1.5,
          }}
        />
      </div>

      <div style={{ marginTop: 14 }}>
        <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 4 }}>{t("mp.rules")}</div>
        <p className="muted" style={{ fontSize: 11, margin: "0 0 6px", lineHeight: 1.45 }}>
          {t("mp.rulesHint")}
        </p>
        <textarea
          className="input"
          value={rules}
          placeholder={t("mp.rulesPlaceholder")}
          onChange={(e) => setRules(e.target.value)}
          onBlur={() => commitLines(rules, "ignore_rules", mp.ignore_rules, t("mp.rulesSaved"))}
          style={{ minHeight: 70, resize: "vertical", fontSize: 13, lineHeight: 1.5 }}
        />
      </div>

      {canIgnore ? (
        <div style={{ marginTop: 14 }}>
          <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 4 }}>
            {t("mp.ignoreNotice")}
          </div>
          <p className="muted" style={{ fontSize: 11, margin: "0 0 6px", lineHeight: 1.45 }}>
            {t("mp.ignoreNoticeHint")}
          </p>
          <input
            className="input"
            style={{ width: "100%" }}
            placeholder={t("mp.ignoreNoticePlaceholder")}
            defaultValue={mp.ignore_notice || ""}
            key={mp.ignore_notice || ""}
            onBlur={(e) => {
              if (e.target.value !== (mp.ignore_notice || ""))
                setField("ignore_notice", e.target.value.trim());
            }}
          />
        </div>
      ) : null}

      <div style={{ marginTop: 14 }}>
        <KvRow label={t("mp.maxOutput")} hint={t("mp.maxOutputHint")}>
          <input
            type="number"
            className="input"
            style={{ width: 110 }}
            min={16}
            placeholder="∞"
            value={maxOut}
            onChange={(e) => setMaxOut(e.target.value)}
            onBlur={() =>
              commitNumber(maxOut, setMaxOut, "max_output_tokens", mp.max_output_tokens, 16)
            }
          />
        </KvRow>
      </div>

      <p className="muted" style={{ fontSize: 11, lineHeight: 1.5, margin: "12px 0 0" }}>
        {t("mp.restartNote")}
      </p>
    </Card>
  );
}

export function MultiUserTab({
  cfg,
  setCfg,
  savePatch,
}: {
  cfg: AgentConfig;
  setCfg: (c: AgentConfig) => void;
  savePatch: (patch: Partial<AgentConfig>, msg?: string) => Promise<boolean>;
}) {
  const t = useT(STR);
  // Enabling the switch also flips memory.per_customer server side
  // (AgentConfig._sync_customer_service_mode), so we only ever write the
  // customer_service block and let the reloaded config carry the rest back.
  const cs: CustomerServiceConfig = cfg.customer_service || {};
  const enabled = cs.enabled ?? false;
  function setCsField(key: string, value: boolean | string | number | string[]) {
    const next = { ...cs, [key]: value };
    // user_scheduling / connections / user_credentials all require multi-user
    // mode (AgentConfig validates each pair), so switching the mode off must
    // drop them in the same patch — a save that trips the backend validator
    // would surface as a cryptic toast.
    if (key === "enabled" && value === false) {
      next.user_scheduling = false;
      next.connections = false;
      next.user_credentials = false;
    }
    setCfg({ ...cfg, customer_service: next });
    void savePatch({ customer_service: next }, key === "enabled" ? undefined : t("ov.cs.saved"));
  }

  // Numeric caps: drafted locally so typing doesn't PUT per keystroke;
  // committed on blur, clamped to the backend's minimums.
  const [maxJobs, setMaxJobs] = useState(String(cs.max_jobs_per_user ?? 3));
  const [floorMin, setFloorMin] = useState(String(cs.min_job_interval_minutes ?? 60));
  useEffect(() => setMaxJobs(String(cs.max_jobs_per_user ?? 3)), [cs.max_jobs_per_user]);
  useEffect(
    () => setFloorMin(String(cs.min_job_interval_minutes ?? 60)),
    [cs.min_job_interval_minutes],
  );
  const [connMax, setConnMax] = useState(String(cs.max_connections_per_user ?? 25));
  const [connDaily, setConnDaily] = useState(String(cs.max_connection_sends_per_day ?? 20));
  const [connTtl, setConnTtl] = useState(String(cs.connection_invite_ttl_hours ?? 168));
  const [connContact, setConnContact] = useState(cs.connection_contact_info || "");
  useEffect(
    () => setConnMax(String(cs.max_connections_per_user ?? 25)),
    [cs.max_connections_per_user],
  );
  useEffect(
    () => setConnDaily(String(cs.max_connection_sends_per_day ?? 20)),
    [cs.max_connection_sends_per_day],
  );
  useEffect(
    () => setConnTtl(String(cs.connection_invite_ttl_hours ?? 168)),
    [cs.connection_invite_ttl_hours],
  );
  useEffect(() => setConnContact(cs.connection_contact_info || ""), [cs.connection_contact_info]);
  // Declared credential names, drafted as free text (one per line or commas)
  // and parsed on blur so half-typed lists never hit the config.
  const namesToText = (names?: string[]) => (names || []).join("\n");
  const [credNames, setCredNames] = useState(namesToText(cs.customer_credential_names));
  useEffect(
    () => setCredNames(namesToText(cs.customer_credential_names)),
    [cs.customer_credential_names],
  );
  function commitCredNames() {
    const parsed = Array.from(
      new Set(
        credNames
          .split(/[\n,]/)
          .map((n) => n.trim())
          .filter(Boolean),
      ),
    );
    if (JSON.stringify(parsed) !== JSON.stringify(cs.customer_credential_names || [])) {
      const next = { ...cs, customer_credential_names: parsed };
      setCfg({ ...cfg, customer_service: next });
      void savePatch({ customer_service: next }, t("mu.credNamesSaved"));
    }
  }
  function numericCommit(
    draft: string,
    setDraft: (v: string) => void,
    key: string,
    current: number,
    fallback: number,
    minimum: number,
  ) {
    const n = Math.max(minimum, Math.floor(Number(draft) || 0) || fallback);
    setDraft(String(n));
    if (n !== current) setCsField(key, n);
  }

  return (
    <div style={{ maxWidth: 720 }}>
      {/* Applies to every agent, so it sits above — and outside — the
          multi-user switch: the abuse it guards against is strangers in a
          public chat, which has nothing to do with per-user isolation. */}
      <MessagePolicyCard cfg={cfg} setCfg={setCfg} savePatch={savePatch} />

      <Card title={t("ov.cs.title")}>
        <KvRow label={t("ov.cs.enabled")} hint={t("ov.cs.enabledHint")}>
          <Toggle checked={enabled} onChange={(v) => setCsField("enabled", v)} />
        </KvRow>
        {enabled ? (
          <CustomerServiceExtension
            value={cs.prompt_extension || ""}
            onCommit={(next) => setCsField("prompt_extension", next)}
          />
        ) : null}
      </Card>

      {enabled ? (
        <>
          <CustomersCard agent={cfg.name} />

          <Card title={t("mu.actionsTitle")}>
            <p className="muted" style={{ fontSize: 11, lineHeight: 1.5, margin: "0 0 10px" }}>
              {t("mu.actionsHint")}
            </p>
            <ActionsEditor
              actions={cfg.allowed_actions || {}}
              onSave={(next) => {
                setCfg({ ...cfg, allowed_actions: next });
                void savePatch({ allowed_actions: next }, t("mu.actionsSaved"));
              }}
            />
          </Card>

          <Card title={t("mu.schedTitle")}>
            <KvRow label={t("mu.schedEnable")} hint={t("mu.schedEnableHint")}>
              <Toggle
                checked={cs.user_scheduling ?? false}
                onChange={(v) => setCsField("user_scheduling", v)}
              />
            </KvRow>
            {cs.user_scheduling ? (
              <>
                <KvRow label={t("mu.schedMax")} hint={t("mu.schedMaxHint")}>
                  <input
                    type="number"
                    className="input"
                    style={{ width: 90 }}
                    min={1}
                    value={maxJobs}
                    onChange={(e) => setMaxJobs(e.target.value)}
                    onBlur={() => {
                      const n = Math.max(1, Math.floor(Number(maxJobs) || 0) || 3);
                      setMaxJobs(String(n));
                      if (n !== (cs.max_jobs_per_user ?? 3)) setCsField("max_jobs_per_user", n);
                    }}
                  />
                </KvRow>
                <KvRow label={t("mu.schedFloor")} hint={t("mu.schedFloorHint")}>
                  <input
                    type="number"
                    className="input"
                    style={{ width: 90 }}
                    min={0}
                    value={floorMin}
                    onChange={(e) => setFloorMin(e.target.value)}
                    onBlur={() => {
                      const n = Math.max(0, Math.floor(Number(floorMin) || 0));
                      setFloorMin(String(n));
                      if (n !== (cs.min_job_interval_minutes ?? 60))
                        setCsField("min_job_interval_minutes", n);
                    }}
                  />
                </KvRow>
              </>
            ) : null}
            <p className="muted" style={{ fontSize: 11, lineHeight: 1.5, margin: "10px 0 0" }}>
              {t("mu.fanoutNote")}
            </p>
          </Card>

          <Card title={t("mu.connTitle")}>
            <KvRow label={t("mu.connEnable")} hint={t("mu.connEnableHint")}>
              <Toggle
                checked={cs.connections ?? false}
                onChange={(v) => setCsField("connections", v)}
              />
            </KvRow>
            {cs.connections ? (
              <>
                <KvRow label={t("mu.connMax")} hint={t("mu.connMaxHint")}>
                  <input
                    type="number"
                    className="input"
                    style={{ width: 90 }}
                    min={1}
                    value={connMax}
                    onChange={(e) => setConnMax(e.target.value)}
                    onBlur={() =>
                      numericCommit(
                        connMax,
                        setConnMax,
                        "max_connections_per_user",
                        cs.max_connections_per_user ?? 25,
                        25,
                        1,
                      )
                    }
                  />
                </KvRow>
                <KvRow label={t("mu.connDaily")} hint={t("mu.connDailyHint")}>
                  <input
                    type="number"
                    className="input"
                    style={{ width: 90 }}
                    min={1}
                    value={connDaily}
                    onChange={(e) => setConnDaily(e.target.value)}
                    onBlur={() =>
                      numericCommit(
                        connDaily,
                        setConnDaily,
                        "max_connection_sends_per_day",
                        cs.max_connection_sends_per_day ?? 20,
                        20,
                        1,
                      )
                    }
                  />
                </KvRow>
                <KvRow label={t("mu.connTtl")} hint={t("mu.connTtlHint")}>
                  <input
                    type="number"
                    className="input"
                    style={{ width: 90 }}
                    min={1}
                    value={connTtl}
                    onChange={(e) => setConnTtl(e.target.value)}
                    onBlur={() =>
                      numericCommit(
                        connTtl,
                        setConnTtl,
                        "connection_invite_ttl_hours",
                        cs.connection_invite_ttl_hours ?? 168,
                        168,
                        1,
                      )
                    }
                  />
                </KvRow>
                <div style={{ marginTop: 10 }}>
                  <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 4 }}>
                    {t("mu.connContact")}
                  </div>
                  <p className="muted" style={{ fontSize: 11, margin: "0 0 6px", lineHeight: 1.45 }}>
                    {t("mu.connContactHint")}
                  </p>
                  <input
                    className="input"
                    style={{ width: "100%" }}
                    placeholder={t("mu.connContactPlaceholder")}
                    value={connContact}
                    onChange={(e) => setConnContact(e.target.value)}
                    onBlur={() => {
                      if (connContact !== (cs.connection_contact_info || ""))
                        setCsField("connection_contact_info", connContact.trim());
                    }}
                  />
                </div>
              </>
            ) : null}
          </Card>

          <Card title={t("mu.credTitle")}>
            <KvRow label={t("mu.credEnable")} hint={t("mu.credEnableHint")}>
              <Toggle
                checked={cs.user_credentials ?? false}
                onChange={(v) => setCsField("user_credentials", v)}
              />
            </KvRow>
            {cs.user_credentials ? (
              <div style={{ marginTop: 10 }}>
                <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 4 }}>
                  {t("mu.credNames")}
                </div>
                <p className="muted" style={{ fontSize: 11, margin: "0 0 6px", lineHeight: 1.45 }}>
                  {t("mu.credNamesHint")}
                </p>
                <textarea
                  className="input"
                  value={credNames}
                  placeholder={t("mu.credNamesPlaceholder")}
                  onChange={(e) => setCredNames(e.target.value)}
                  onBlur={commitCredNames}
                  style={{
                    minHeight: 70,
                    resize: "vertical",
                    fontFamily: "var(--font-mono)",
                    fontSize: 13,
                    lineHeight: 1.5,
                  }}
                />
              </div>
            ) : null}
          </Card>
        </>
      ) : (
        <p className="muted" style={{ fontSize: 12 }}>
          {t("mu.disabledNote")}
        </p>
      )}

      <p className="muted" style={{ fontSize: 12, marginTop: 4 }}>
        {t("mu.restartNote")}
      </p>
    </div>
  );
}
