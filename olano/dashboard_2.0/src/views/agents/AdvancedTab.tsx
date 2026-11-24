// Advanced config tab — a schema-driven editor for EVERY agent.json5 field.
//
// The backend hands us the AgentConfig JSON Schema (GET …/config/schema) plus
// which fields to hide (read_only) and which already have a dedicated, richer
// tab (managed). We render an appropriate control for every remaining field, so
// the moment a new field is added to AgentConfig it becomes editable here — no
// per-field UI work, and nothing is ever stranded behind the raw-JSON tab. The
// "generic" badge on each row is also a discovery aid: it flags fields that are
// still driven by a generic control and could be promoted to a bespoke one.

import { useCallback, useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../../core/api";
import { useT } from "../../core/i18n";
import { useToast } from "../../ui/toast";
import { Toggle, KvRow } from "./Toggle";
import { STR } from "./strings";
import type {
  AgentConfig,
  AgentConfigSchemaResponse,
  DetailTab,
  JsonSchemaProp,
  JsonSchema,
} from "./types";

type FieldKind = "boolean" | "integer" | "number" | "string" | "enum" | "stringlist" | "json";

interface Resolved {
  kind: FieldKind;
  nullable: boolean;
  enumValues: (string | number)[];
}

function deref(spec: JsonSchemaProp, defs: Record<string, JsonSchemaProp>): JsonSchemaProp {
  if (spec.$ref) {
    const key = spec.$ref.split("/").pop();
    if (key && defs[key]) return defs[key];
  }
  return spec;
}

// Collapse the JSON-Schema shapes Pydantic emits (anyOf for Optional[T], $ref /
// allOf for nested models, inline enum, typed arrays) into a single widget kind.
function resolveField(spec: JsonSchemaProp, defs: Record<string, JsonSchemaProp>): Resolved {
  let nullable = false;
  let s = spec;
  if (s.anyOf && s.anyOf.length) {
    const nonNull = s.anyOf.filter((b) => b.type !== "null");
    nullable = nonNull.length !== s.anyOf.length;
    s = nonNull[0] ?? s.anyOf[0];
  }
  if (s.allOf && s.allOf.length === 1) s = s.allOf[0];
  s = deref(s, defs);
  if (Array.isArray(s.enum)) {
    return { kind: "enum", nullable, enumValues: s.enum as (string | number)[] };
  }
  const type = Array.isArray(s.type) ? s.type.find((x) => x !== "null") : s.type;
  if (type === "boolean") return { kind: "boolean", nullable, enumValues: [] };
  if (type === "integer") return { kind: "integer", nullable, enumValues: [] };
  if (type === "number") return { kind: "number", nullable, enumValues: [] };
  if (type === "array") {
    const items = s.items ? deref(s.items, defs) : undefined;
    const itemType = items && (Array.isArray(items.type) ? items.type[0] : items.type);
    if (itemType === "string" && !items?.enum) {
      return { kind: "stringlist", nullable, enumValues: [] };
    }
    return { kind: "json", nullable, enumValues: [] };
  }
  if (type === "string") return { kind: "string", nullable, enumValues: [] };
  return { kind: "json", nullable, enumValues: [] };
}

const inputStyle: React.CSSProperties = { minWidth: 180, maxWidth: 320 };

// ── Per-kind rows ───────────────────────────────────────────────────────────

function ScalarInputRow({
  label,
  hint,
  value,
  numeric,
  onSave,
}: {
  label: React.ReactNode;
  hint?: string;
  value: unknown;
  numeric: boolean;
  onSave: (v: unknown) => void;
}) {
  const initial = value === undefined || value === null ? "" : String(value);
  const [draft, setDraft] = useState(initial);
  useEffect(() => setDraft(initial), [initial]);
  const commit = () => {
    if (draft === initial) return;
    if (draft === "") {
      onSave(null);
      return;
    }
    if (numeric) {
      const n = Number(draft);
      if (Number.isNaN(n)) return; // let re-render restore the last good value
      onSave(n);
    } else {
      onSave(draft);
    }
  };
  return (
    <KvRow label={label} hint={hint}>
      <input
        className="input"
        style={inputStyle}
        type={numeric ? "number" : "text"}
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => e.key === "Enter" && e.currentTarget.blur()}
      />
    </KvRow>
  );
}

function StringListRow({
  label,
  hint,
  value,
  onSave,
}: {
  label: React.ReactNode;
  hint?: string;
  value: unknown;
  onSave: (v: unknown) => void;
}) {
  const t = useT(STR);
  const arr = Array.isArray(value) ? (value as string[]) : [];
  const initial = arr.join(", ");
  const [draft, setDraft] = useState(initial);
  useEffect(() => setDraft(initial), [initial]);
  const commit = () => {
    if (draft === initial) return;
    const next = draft
      .split(/[,\n]/)
      .map((s) => s.trim())
      .filter(Boolean);
    onSave(next);
  };
  return (
    <KvRow label={label} hint={hint}>
      <input
        className="input"
        style={inputStyle}
        value={draft}
        placeholder={t("adv.stringListPlaceholder")}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => e.key === "Enter" && e.currentTarget.blur()}
      />
    </KvRow>
  );
}

function JsonRow({
  label,
  hint,
  value,
  onSave,
}: {
  label: React.ReactNode;
  hint?: string;
  value: unknown;
  onSave: (v: unknown) => void;
}) {
  const t = useT(STR);
  const initial = value === undefined ? "" : JSON.stringify(value, null, 2);
  const [draft, setDraft] = useState(initial);
  const [err, setErr] = useState(false);
  useEffect(() => {
    setDraft(initial);
    setErr(false);
  }, [initial]);
  const commit = () => {
    if (draft === initial) return;
    if (draft.trim() === "") {
      setErr(false);
      onSave(null);
      return;
    }
    try {
      onSave(JSON.parse(draft));
      setErr(false);
    } catch {
      setErr(true);
    }
  };
  return (
    <div style={{ padding: "9px 0", borderBottom: "1px solid var(--border)" }}>
      <div style={{ fontSize: 13, marginBottom: 4 }}>{label}</div>
      {hint ? (
        <div className="muted" style={{ fontSize: 11, marginBottom: 6, lineHeight: 1.4 }}>
          {hint}
        </div>
      ) : null}
      <textarea
        className="input"
        style={{ width: "100%", minHeight: 84, fontFamily: "var(--font-mono, monospace)", fontSize: 12 }}
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        spellCheck={false}
      />
      {err ? (
        <div style={{ color: "var(--danger)", fontSize: 11, marginTop: 4 }}>{t("adv.invalidJson")}</div>
      ) : null}
    </div>
  );
}

// ── One schema field → the right control ────────────────────────────────────

function SchemaField({
  fieldKey,
  spec,
  defs,
  value,
  onSave,
}: {
  fieldKey: string;
  spec: JsonSchemaProp;
  defs: Record<string, JsonSchemaProp>;
  value: unknown;
  onSave: (v: unknown) => void;
}) {
  const t = useT(STR);
  const r = useMemo(() => resolveField(spec, defs), [spec, defs]);
  const label = spec.title || fieldKey;
  const hint = spec.description;
  const current = value !== undefined ? value : spec.default;

  // A small "generic" badge doubles as a discovery aid — it marks fields that
  // still use a generic control and are candidates for a bespoke one later.
  const labelNode = (
    <span className="row" style={{ gap: 7, alignItems: "baseline", flexWrap: "wrap" }}>
      <span>{label}</span>
      <code style={{ fontSize: 10, opacity: 0.6 }}>{fieldKey}</code>
    </span>
  );

  if (r.kind === "boolean") {
    return (
      <KvRow label={labelNode} hint={hint}>
        <Toggle checked={!!current} onChange={(v) => onSave(v)} />
      </KvRow>
    );
  }
  if (r.kind === "enum") {
    return (
      <KvRow label={labelNode} hint={hint}>
        <select
          className="input"
          style={inputStyle}
          value={current === undefined || current === null ? "" : String(current)}
          onChange={(e) => onSave(e.target.value)}
        >
          {r.nullable ? <option value="">{t("adv.unset")}</option> : null}
          {r.enumValues.map((v) => (
            <option key={String(v)} value={String(v)}>
              {String(v)}
            </option>
          ))}
        </select>
      </KvRow>
    );
  }
  if (r.kind === "integer" || r.kind === "number") {
    return <ScalarInputRow label={labelNode} hint={hint} value={current} numeric onSave={onSave} />;
  }
  if (r.kind === "string") {
    return <ScalarInputRow label={labelNode} hint={hint} value={current} numeric={false} onSave={onSave} />;
  }
  if (r.kind === "stringlist") {
    return <StringListRow label={labelNode} hint={hint} value={current} onSave={onSave} />;
  }
  return <JsonRow label={labelNode} hint={hint} value={current} onSave={onSave} />;
}

// ── The tab ─────────────────────────────────────────────────────────────────

export function AdvancedTab({
  agentName,
  cfg,
  savePatch,
  onGoToTab,
}: {
  agentName: string;
  cfg: AgentConfig;
  savePatch: (patch: Partial<AgentConfig>, msg?: string) => Promise<boolean>;
  onGoToTab: (tab: DetailTab) => void;
}) {
  const t = useT(STR);
  const toast = useToast();
  const [meta, setMeta] = useState<AgentConfigSchemaResponse | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setMeta(null);
    setErr(null);
    api
      .get<AgentConfigSchemaResponse>(`/api/agents/${encodeURIComponent(agentName)}/config/schema`)
      .then((r) => alive && setMeta(r))
      .catch((e) => alive && setErr(e instanceof ApiError ? e.detail : String(e)));
    return () => {
      alive = false;
    };
  }, [agentName]);

  const save = useCallback(
    (key: string, v: unknown) => {
      void savePatch({ [key]: v } as Partial<AgentConfig>, t("adv.saved", { field: key }));
    },
    [savePatch, t],
  );

  const groups = useMemo(() => {
    if (!meta) return null;
    const schema: JsonSchema = meta.schema || {};
    const props = schema.properties || {};
    const defs = schema.$defs || {};
    const readOnly = new Set(meta.read_only || []);
    const managed = meta.managed || {};
    const editable: [string, JsonSchemaProp][] = [];
    const managedRows: [string, string][] = [];
    for (const [key, spec] of Object.entries(props)) {
      if (readOnly.has(key)) continue;
      if (key in managed) managedRows.push([key, managed[key]]);
      else editable.push([key, spec]);
    }
    editable.sort((a, b) => a[0].localeCompare(b[0]));
    managedRows.sort((a, b) => a[0].localeCompare(b[0]));
    return { editable, managedRows, defs };
  }, [meta]);

  if (err) {
    return (
      <div className="card" style={{ padding: 16, color: "var(--danger)" }}>
        {err}
      </div>
    );
  }
  if (!meta || !groups) {
    return (
      <div className="card" style={{ padding: 16 }}>
        <div className="spinner" />
      </div>
    );
  }

  return (
    <div className="card" style={{ padding: "12px 16px" }}>
      <div style={{ marginBottom: 8 }}>
        <strong style={{ fontSize: 13 }}>{t("adv.title")}</strong>
        <div className="muted" style={{ fontSize: 12, marginTop: 2, lineHeight: 1.5 }}>
          {t("adv.subtitle")}
        </div>
      </div>

      {groups.editable.map(([key, spec]) => (
        <SchemaField
          key={key}
          fieldKey={key}
          spec={spec}
          defs={groups.defs}
          value={cfg[key]}
          onSave={(v) => save(key, v)}
        />
      ))}

      {groups.managedRows.length ? (
        <div style={{ marginTop: 18 }}>
          <div className="muted" style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: 0.4, marginBottom: 4 }}>
            {t("adv.managedHeader")}
          </div>
          <div className="muted" style={{ fontSize: 12, marginBottom: 8, lineHeight: 1.5 }}>
            {t("adv.managedHint")}
          </div>
          {groups.managedRows.map(([key, tab]) => (
            <KvRow key={key} label={key}>
              <button
                className="btn ghost"
                style={{ padding: "3px 10px", fontSize: 12 }}
                onClick={() => onGoToTab(tab as DetailTab)}
              >
                {t("adv.editInTab", { tab })}
              </button>
            </KvRow>
          ))}
        </div>
      ) : null}

      <div className="muted" style={{ fontSize: 11, marginTop: 14, lineHeight: 1.5 }}>
        {t("adv.restartNote")}{" "}
        <button
          className="btn ghost"
          style={{ padding: "2px 8px", fontSize: 11 }}
          onClick={() => {
            void navigator.clipboard.writeText(JSON.stringify(cfg, null, 2));
            toast.push(t("adv.copied"));
          }}
        >
          {t("adv.copyJson")}
        </button>
      </div>
    </div>
  );
}
