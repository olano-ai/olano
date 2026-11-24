import { useCallback, useEffect, useRef, useState } from "react";
import { X } from "lucide-react";
import { api, ApiError } from "../../core/api";
import { useT } from "../../core/i18n";
import { useToast } from "../../ui/toast";
import { STR } from "./strings";

// ── types ─────────────────────────────────────────────────────────────────
type Mode = "everyone" | "none" | "list";
type VisMode = "none" | "all" | "list";
interface Direction {
  mode: Mode;
  agents: string[];
}
interface Visibility {
  mode: VisMode;
  agents: string[];
}
interface OrgEntry {
  title: string | null;
  manager: string | null;
  reports: string[];
  coworkers: string[];
  input: Direction;
  output: Direction;
  autogenerate: boolean;
  mutating_from_manager_only: boolean;
}
interface RosterAgent {
  name: string;
  description?: string;
  emoji?: string;
}

function normalize(o: Partial<OrgEntry> | undefined): OrgEntry {
  return {
    title: o?.title ?? null,
    manager: o?.manager ?? null,
    reports: o?.reports ?? [],
    coworkers: o?.coworkers ?? [],
    input: { mode: o?.input?.mode ?? "everyone", agents: o?.input?.agents ?? [] },
    output: { mode: o?.output?.mode ?? "everyone", agents: o?.output?.agents ?? [] },
    autogenerate: !!o?.autogenerate,
    mutating_from_manager_only: !!o?.mutating_from_manager_only,
  };
}

const CSS = `
.org-tab { max-width: 720px; }
.org-tab-intro { font-size: 13px; color: var(--text-3); line-height: 1.55; margin-bottom: 18px; }
.org-section-title { font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--text-3); font-weight: 700; margin: 22px 0 10px; padding-bottom: 6px; border-bottom: 1px solid var(--border); }
.org-field { margin-bottom: 16px; }
.org-field-label { font-size: 12.5px; font-weight: 600; color: var(--text); margin-bottom: 3px; }
.org-hint { font-size: 11.5px; color: var(--text-4); line-height: 1.45; margin-bottom: 7px; }
.org-control { display: flex; flex-wrap: wrap; align-items: center; gap: 7px; }
.org-chips { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
.org-empty { font-size: 12px; color: var(--text-4); }
.org-chip2 { display: inline-flex; align-items: center; gap: 4px; font-size: 12px; font-family: var(--font-mono); padding: 3px 4px 3px 9px; border-radius: var(--r-pill); background: var(--surface-2); border: 1px solid var(--border); color: var(--text-2); }
.org-chip2.ro { padding: 3px 9px; opacity: 0.85; }
.org-chip2.ok { color: var(--success); border-color: color-mix(in srgb, var(--success) 45%, var(--border)); }
.org-chip2.warn { color: #d9a04a; border-color: color-mix(in srgb, #d9a04a 45%, var(--border)); }
.org-chip-x { display: inline-flex; align-items: center; justify-content: center; width: 16px; height: 16px; border: 0; border-radius: 50%; background: transparent; color: inherit; cursor: pointer; opacity: 0.7; }
.org-chip-x:hover { opacity: 1; background: color-mix(in srgb, var(--text) 12%, transparent); }
.org-pick { position: relative; min-width: 180px; flex: 1; }
.org-pick-input { width: 100%; box-sizing: border-box; font-size: 12.5px; padding: 6px 9px; border: 1px solid var(--border); border-radius: var(--r-sm); background: var(--surface); color: var(--text); }
.org-pick-input:focus { border-color: var(--accent); outline: none; }
.org-pick-menu { position: absolute; z-index: 30; top: calc(100% + 4px); left: 0; right: 0; max-height: 260px; overflow: auto; background: var(--bg-elev); border: 1px solid var(--border); border-radius: var(--r-sm); box-shadow: var(--shadow); padding: 4px; }
.org-pick-item { display: flex; flex-direction: column; gap: 1px; width: 100%; text-align: left; border: 0; background: transparent; padding: 6px 8px; border-radius: var(--r-sm); cursor: pointer; }
.org-pick-item:hover { background: var(--surface-2); }
.org-pick-name { font-size: 12.5px; font-weight: 600; color: var(--text); font-family: var(--font-mono); }
.org-pick-desc { font-size: 11px; color: var(--text-4); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.org-text { width: 100%; box-sizing: border-box; font-size: 12.5px; padding: 6px 9px; border: 1px solid var(--border); border-radius: var(--r-sm); background: var(--surface); color: var(--text); }
.org-text:focus { border-color: var(--accent); outline: none; }
.org-auto { display: inline-flex; align-items: center; gap: 8px; font-size: 12.5px; color: var(--text); cursor: pointer; user-select: none; margin-bottom: 12px; }
.org-derived { margin-bottom: 16px; }
.org-actions { display: flex; gap: 10px; margin-top: 24px; padding-top: 16px; border-top: 1px solid var(--border); }
.vis-modes { display: inline-flex; gap: 4px; padding: 3px; background: var(--surface-2); border: 1px solid var(--border); border-radius: var(--r-sm); }
.vis-mode-btn { font-size: 12.5px; font-weight: 600; padding: 5px 14px; border: 1px solid transparent; border-radius: calc(var(--r-sm) - 2px); background: transparent; color: var(--text-2); cursor: pointer; }
.vis-mode-btn:hover { color: var(--text); }
.vis-mode-btn.active { background: var(--surface); border-color: var(--border); color: var(--text); box-shadow: var(--shadow-sm, 0 1px 2px rgba(0,0,0,0.06)); }
.vis-note { font-size: 11.5px; color: var(--text-4); line-height: 1.5; margin: 14px 0 4px; padding: 9px 11px; border-left: 2px solid color-mix(in srgb, var(--accent) 55%, var(--border)); background: color-mix(in srgb, var(--accent) 5%, transparent); border-radius: 0 var(--r-sm) var(--r-sm) 0; }
`;

// ── pieces ──────────────────────────────────────────────────────────────────
function AgentPicker({
  roster,
  exclude,
  onPick,
  placeholder,
}: {
  roster: RosterAgent[];
  exclude: string[];
  onPick: (name: string) => void;
  placeholder: string;
}) {
  const [q, setQ] = useState("");
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement | null>(null);
  const excl = new Set(exclude.map((e) => e.toLowerCase()));
  const ql = q.trim().toLowerCase();
  const matches = roster
    .filter((a) => !excl.has(a.name.toLowerCase()))
    .filter((a) => !ql || `${a.name} ${a.description || ""}`.toLowerCase().includes(ql))
    .slice(0, 8);

  useEffect(() => {
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, []);

  return (
    <div className="org-pick" ref={ref}>
      <input
        className="org-pick-input"
        value={q}
        placeholder={placeholder}
        onFocus={() => setOpen(true)}
        onChange={(e) => {
          setQ(e.target.value);
          setOpen(true);
        }}
      />
      {open && matches.length > 0 && (
        <div className="org-pick-menu">
          {matches.map((a) => (
            <button
              key={a.name}
              type="button"
              className="org-pick-item"
              onClick={() => {
                onPick(a.name);
                setQ("");
                setOpen(false);
              }}
            >
              <span className="org-pick-name">{a.emoji ? `${a.emoji} ` : ""}{a.name}</span>
              {a.description && <span className="org-pick-desc">{a.description}</span>}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function Chip({ label, onRemove, tone }: { label: string; onRemove: () => void; tone?: string }) {
  const t = useT(STR);
  return (
    <span className={`org-chip2 ${tone || ""}`}>
      {label}
      <button type="button" className="org-chip-x" onClick={onRemove} aria-label={t("org.removeAria")}>
        <X size={11} />
      </button>
    </span>
  );
}

function RelationField({
  label,
  hint,
  values,
  single,
  roster,
  selfName,
  placeholder,
  onChange,
}: {
  label: string;
  hint: string;
  values: string[];
  single?: boolean;
  roster: RosterAgent[];
  selfName: string;
  placeholder: string;
  onChange: (next: string[]) => void;
}) {
  const add = (n: string) => {
    if (single) return onChange([n]);
    if (values.some((v) => v.toLowerCase() === n.toLowerCase())) return;
    onChange([...values, n]);
  };
  const remove = (n: string) => onChange(values.filter((v) => v.toLowerCase() !== n.toLowerCase()));
  return (
    <div className="org-field">
      <div className="org-field-label">{label}</div>
      <div className="org-hint">{hint}</div>
      <div className="org-control">
        <div className="org-chips">
          {values.length === 0 && <span className="org-empty">—</span>}
          {values.map((v) => (
            <Chip key={v} label={v} onRemove={() => remove(v)} />
          ))}
        </div>
        <AgentPicker roster={roster} exclude={[selfName, ...values]} onPick={add} placeholder={placeholder} />
      </div>
    </div>
  );
}

function DirectionEditor({
  label,
  hint,
  dir,
  roster,
  selfName,
  t,
  onChange,
}: {
  label: string;
  hint: string;
  dir: Direction;
  roster: RosterAgent[];
  selfName: string;
  t: (k: string) => string;
  onChange: (d: Direction) => void;
}) {
  const addAgent = (n: string) => {
    if (dir.agents.some((a) => a.toLowerCase() === n.toLowerCase())) return;
    onChange({ mode: "list", agents: [...dir.agents, n] });
  };
  const removeAgent = (n: string) => {
    const agents = dir.agents.filter((a) => a.toLowerCase() !== n.toLowerCase());
    onChange(agents.length ? { mode: "list", agents } : { mode: "everyone", agents: [] });
  };
  return (
    <div className="org-field">
      <div className="org-field-label">{label}</div>
      <div className="org-hint">{hint}</div>
      <div className="org-control">
        <div className="org-chips">
          {dir.mode === "everyone" && (
            <Chip label={t("org.everyone")} tone="ok" onRemove={() => onChange({ mode: "none", agents: [] })} />
          )}
          {dir.mode === "none" && (
            <Chip label={t("org.noone")} tone="warn" onRemove={() => onChange({ mode: "everyone", agents: [] })} />
          )}
          {dir.mode === "list" && dir.agents.map((a) => <Chip key={a} label={a} onRemove={() => removeAgent(a)} />)}
        </div>
        <AgentPicker roster={roster} exclude={[selfName, ...dir.agents]} onPick={addAgent} placeholder={t("org.addAgent")} />
      </div>
    </div>
  );
}

const TALK_TOOL = "talk_to_agent";

// ── visibility section ──────────────────────────────────────────────────────
// Awareness (which peers this agent is told exist) is stored in agent.json5
// (`agent_visibility`) and peer-messaging on/off maps to `disabled_tools`
// containing `talk_to_agent`. Both persist via PUT /api/agents/{name} — a
// different endpoint than the org allow-lists — so this section owns its own
// load + auto-save, independent of the org Save button below.
function VisibilitySection({
  agentName,
  roster,
  t,
}: {
  agentName: string;
  roster: RosterAgent[];
  t: (k: string) => string;
}) {
  const toast = useToast();
  const [vis, setVis] = useState<Visibility | null>(null);
  const [peerMsg, setPeerMsg] = useState(true);
  // Full disabled_tools list preserved so we never clobber non-talk_to entries.
  const [disabledTools, setDisabledTools] = useState<string[]>([]);

  useEffect(() => {
    let alive = true;
    api
      .get<{ agent_visibility?: Partial<Visibility>; disabled_tools?: string[] }>(
        `/api/agents/${encodeURIComponent(agentName)}`,
      )
      .then((cfg) => {
        if (!alive) return;
        const av = cfg.agent_visibility || {};
        setVis({ mode: (av.mode as VisMode) || "none", agents: av.agents || [] });
        const dt = cfg.disabled_tools || [];
        setDisabledTools(dt);
        setPeerMsg(!dt.includes(TALK_TOOL));
      })
      .catch(() => {
        if (alive) setVis({ mode: "none", agents: [] });
      });
    return () => {
      alive = false;
    };
  }, [agentName]);

  const persist = async (patch: Record<string, unknown>, okMsg: string) => {
    try {
      await api.put(`/api/agents/${encodeURIComponent(agentName)}`, patch);
      toast.push(okMsg);
    } catch (e) {
      toast.push(e instanceof ApiError ? e.message : t("vis.saveFail"), "error");
    }
  };

  const saveVis = (next: Visibility) => {
    setVis(next);
    void persist({ agent_visibility: next }, t("vis.saved"));
  };

  const togglePeerMsg = (on: boolean) => {
    setPeerMsg(on);
    const set = new Set(disabledTools);
    if (on) set.delete(TALK_TOOL);
    else set.add(TALK_TOOL);
    const next = [...set].sort();
    setDisabledTools(next);
    void persist({ disabled_tools: next }, t("vis.saved"));
  };

  if (!vis) return <div className="spinner" style={{ margin: "20px auto" }} />;

  const setMode = (mode: VisMode) => saveVis({ ...vis, mode });
  const addAgent = (n: string) => {
    if (vis.agents.some((a) => a.toLowerCase() === n.toLowerCase())) return;
    saveVis({ mode: "list", agents: [...vis.agents, n] });
  };
  const removeAgent = (n: string) =>
    saveVis({ ...vis, agents: vis.agents.filter((a) => a.toLowerCase() !== n.toLowerCase()) });

  const MODES: { key: VisMode; label: string; hint: string }[] = [
    { key: "none", label: t("vis.none"), hint: t("vis.noneHint") },
    { key: "all", label: t("vis.all"), hint: t("vis.allHint") },
    { key: "list", label: t("vis.list"), hint: t("vis.listHint") },
  ];
  const active = MODES.find((m) => m.key === vis.mode) || MODES[0];

  return (
    <>
      <div className="org-section-title">{t("vis.section")}</div>
      <div className="org-tab-intro" style={{ marginBottom: 12 }}>{t("vis.intro")}</div>

      <div className="org-field">
        <div className="org-field-label">{t("vis.modeLabel")}</div>
        <div className="vis-modes">
          {MODES.map((m) => (
            <button
              key={m.key}
              type="button"
              className={`vis-mode-btn${vis.mode === m.key ? " active" : ""}`}
              onClick={() => setMode(m.key)}
            >
              {m.label}
            </button>
          ))}
        </div>
        <div className="org-hint" style={{ marginTop: 7 }}>{active.hint}</div>
      </div>

      {vis.mode === "list" && (
        <div className="org-field">
          <div className="org-control">
            <div className="org-chips">
              {vis.agents.length === 0 && <span className="org-empty">—</span>}
              {vis.agents.map((a) => (
                <Chip key={a} label={a} onRemove={() => removeAgent(a)} />
              ))}
            </div>
            <AgentPicker
              roster={roster}
              exclude={[agentName, ...vis.agents]}
              onPick={addAgent}
              placeholder={t("vis.pickAgents")}
            />
          </div>
        </div>
      )}

      <div className="vis-note">{t("vis.awarenessNote")}</div>

      <label className="org-auto" style={{ marginTop: 16, marginBottom: 4 }}>
        <input type="checkbox" checked={peerMsg} onChange={(e) => togglePeerMsg(e.target.checked)} />
        <span>{t("vis.peerMessaging")}</span>
      </label>
      <div className="org-hint" style={{ marginBottom: 4 }}>{t("vis.peerMessagingHint")}</div>
    </>
  );
}

// ── tab ──────────────────────────────────────────────────────────────────
export function OrgTab({ agentName }: { agentName: string }) {
  const t = useT(STR);
  const toast = useToast();
  const [org, setOrg] = useState<OrgEntry | null>(null);
  const [roster, setRoster] = useState<RosterAgent[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);

  const reload = useCallback(() => {
    setLoading(true);
    Promise.all([
      api.get<{ org: OrgEntry }>(`/api/agents/${encodeURIComponent(agentName)}/org`),
      api.get<{ agents: RosterAgent[] }>("/api/org-chart"),
    ])
      .then(([o, chart]) => {
        setOrg(normalize(o.org));
        setRoster((chart.agents || []).filter((a) => a.name !== agentName));
      })
      .catch(() => toast.push(t("org.loadFail"), "error"))
      .finally(() => setLoading(false));
  }, [agentName, toast, t]);
  useEffect(reload, [reload]);

  const save = async () => {
    if (!org) return;
    setSaving(true);
    try {
      const res = await api.put<{ org: OrgEntry; touched: string[] }>(
        `/api/agents/${encodeURIComponent(agentName)}/org`,
        org,
      );
      setOrg(normalize(res.org));
      const n = res.touched?.length || 0;
      toast.push(n ? t("org.savedReciprocal").replace("{n}", String(n)) : t("org.saved"));
    } catch (e) {
      toast.push(e instanceof ApiError ? e.message : t("org.saveFail"), "error");
    } finally {
      setSaving(false);
    }
  };

  if (loading || !org) return <div className="spinner" style={{ margin: "40px auto" }} />;

  const patch = (p: Partial<OrgEntry>) => setOrg({ ...org, ...p });
  const derived = [...(org.manager ? [org.manager] : []), ...org.reports, ...org.coworkers];

  return (
    <div className="org-tab">
      <style>{CSS}</style>
      <div className="org-tab-intro">{t("org.intro")}</div>

      <VisibilitySection agentName={agentName} roster={roster} t={t} />

      <div className="org-section-title">{t("org.structure")}</div>
      <RelationField
        label={t("org.manager")}
        hint={t("org.managerHint")}
        single
        values={org.manager ? [org.manager] : []}
        roster={roster}
        selfName={agentName}
        placeholder={t("org.pickManager")}
        onChange={(v) => patch({ manager: v[0] ?? null })}
      />
      <RelationField
        label={t("org.reports")}
        hint={t("org.reportsHint")}
        values={org.reports}
        roster={roster}
        selfName={agentName}
        placeholder={t("org.addAgent")}
        onChange={(v) => patch({ reports: v })}
      />
      <RelationField
        label={t("org.coworkers")}
        hint={t("org.coworkersHint")}
        values={org.coworkers}
        roster={roster}
        selfName={agentName}
        placeholder={t("org.addAgent")}
        onChange={(v) => patch({ coworkers: v })}
      />
      <div className="org-field">
        <div className="org-field-label">{t("org.title")}</div>
        <div className="org-hint">{t("org.titleHint")}</div>
        <input
          className="org-text"
          value={org.title || ""}
          placeholder={t("org.titlePlaceholder")}
          onChange={(e) => patch({ title: e.target.value || null })}
        />
      </div>

      <div className="org-section-title">{t("org.messaging")}</div>
      <label className="org-auto">
        <input
          type="checkbox"
          checked={org.autogenerate}
          onChange={(e) => patch({ autogenerate: e.target.checked })}
        />
        <span>{t("org.autogen")}</span>
      </label>
      {org.autogenerate ? (
        <div className="org-derived">
          <div className="org-hint">{t("org.autogenHint")}</div>
          <div className="org-chips">
            {derived.length === 0 ? (
              <span className="org-empty">{t("org.autogenEmpty")}</span>
            ) : (
              derived.map((d, i) => (
                <span key={`${d}:${i}`} className="org-chip2 ro">{d}</span>
              ))
            )}
          </div>
        </div>
      ) : (
        <>
          <DirectionEditor
            label={t("org.canBeMessaged")}
            hint={t("org.canBeMessagedHint")}
            dir={org.input}
            roster={roster}
            selfName={agentName}
            t={t}
            onChange={(d) => patch({ input: d })}
          />
          <DirectionEditor
            label={t("org.canMessage")}
            hint={t("org.canMessageHint")}
            dir={org.output}
            roster={roster}
            selfName={agentName}
            t={t}
            onChange={(d) => patch({ output: d })}
          />
        </>
      )}

      <label className="org-auto" style={{ marginTop: 6 }}>
        <input
          type="checkbox"
          checked={org.mutating_from_manager_only}
          onChange={(e) => patch({ mutating_from_manager_only: e.target.checked })}
        />
        <span>{t("org.managerOnly")}</span>
      </label>
      <div className="org-hint" style={{ marginBottom: 4 }}>
        {org.mutating_from_manager_only && !org.manager
          ? t("org.managerOnlyNoManager")
          : t("org.managerOnlyHint").replace("{manager}", org.manager || "—")}
      </div>

      <div className="org-actions">
        <button className="btn primary row" style={{ gap: 6 }} disabled={saving} onClick={() => void save()}>
          {saving && <span className="spinner" />} {t("org.save")}
        </button>
        <button className="btn ghost" disabled={saving} onClick={reload}>
          {t("org.reset")}
        </button>
      </div>
    </div>
  );
}
