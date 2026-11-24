import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { ChevronDown, CornerDownRight, NotebookPen, Pencil, Plus } from "lucide-react";
import { subscribeStream } from "../../core/agentStream";
import { usePersistentState } from "../../core/usePersistentState";
import { useCurrentAgent } from "../../core/currentAgent";
import { useAuth } from "../../core/auth";
import { api, ApiError } from "../../core/api";
import { useT } from "../../core/i18n";
import { BrandWordmark } from "../../ui/BrandMark";
import { AgentPicker } from "../nav/AgentPicker";
import { Markdown } from "../../ui/Markdown";
import { useToast } from "../../ui/toast";
import { STR } from "./strings";
import {
  bulletMatches,
  formatBytes,
  parseMemory,
  sectionMatches,
  type MemorySection,
} from "./memoryParse";

// Shape of GET /api/memory — see olano/runtime/api.py read_memory().
interface GetMemoryResponse {
  content: string;
  size?: number;
}

// POST /api/memory body — MemoryAppendRequest in api.py. A non-empty `section`
// routes to MarkdownMemory.append_fact() (under a ## heading in MEMORY.md);
// an empty section routes to append_daily() (today's memories/YYYY-MM-DD.md).
interface MemoryAppendRequest {
  content: string;
  section: string;
}

// /api/stream SSE envelope. memory_updated carries {agent, size}; we refresh
// the document when the event targets the agent currently in view.
interface StreamEvent {
  type: string;
  data?: { agent?: string | null; size?: number };
  ts?: string;
}

type ViewMode = "structured" | "rendered" | "raw";

// Read-mode toggle identity + translation keys (label/title resolved inside the
// component so they re-translate on language switch).
const VIEW_MODES: { id: ViewMode; labelKey: string; titleKey: string }[] = [
  { id: "structured", labelKey: "view.outline", titleKey: "view.outline.title" },
  { id: "rendered", labelKey: "view.read", titleKey: "view.read.title" },
  { id: "raw", labelKey: "view.raw", titleKey: "view.raw.title" },
];

type T = (key: string, vars?: Record<string, string>) => string;

function relativeTime(iso: string | null, t: T): string {
  if (!iso) return "";
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "";
  const secs = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (secs < 5) return t("time.justNow");
  if (secs < 60) return t("time.secs", { n: String(secs) });
  const mins = Math.round(secs / 60);
  if (mins < 60) return t("time.mins", { n: String(mins) });
  const hrs = Math.round(mins / 60);
  return t("time.hrs", { n: String(hrs) });
}

export function MemoryView() {
  const toast = useToast();
  const t = useT(STR);

  // Agent selection comes from the app-wide picker in the sidebar.
  const { agent, current } = useCurrentAgent();

  // Non-admin members in a multi-user install may view memory only; the server
  // enforces this with 403s, so the UI just hides the write affordances.
  const { user, multiUser } = useAuth();
  const readOnly = multiUser && !!user && !user.is_admin;

  // Memory document state.
  const [content, setContent] = useState("");
  const [size, setSize] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [updatedAt, setUpdatedAt] = useState<string | null>(null);
  const [liveFlash, setLiveFlash] = useState(false);

  // Read-pane controls.
  const [viewMode, setViewMode] = usePersistentState<ViewMode>("memory.viewMode", "structured");
  const [filter, setFilter] = usePersistentState("memory.filter", "");
  const [collapsed, setCollapsed] = usePersistentState<Record<string, boolean>>("memory.collapsed", {});

  // Add-a-fact form state.
  const [section, setSection] = usePersistentState("memory.section", "");
  const [fact, setFact] = usePersistentState("memory.fact", "");
  const [saving, setSaving] = useState(false);
  const factRef = useRef<HTMLTextAreaElement | null>(null);

  // Inline full-document editor state. `editing` swaps the read panes for a
  // raw textarea bound to `draft`; saving PUTs the whole document.
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [savingDoc, setSavingDoc] = useState(false);
  // True when a memory_updated event arrived while editing (the live reload was
  // suppressed to protect the draft) — surfaces a "saving will overwrite" banner.
  const [staleWhileEditing, setStaleWhileEditing] = useState(false);
  const editorRef = useRef<HTMLTextAreaElement | null>(null);
  const dirty = editing && draft !== content;

  // Confirmation modal for the destructive "clear form" action when both
  // fields hold unsaved text (window.confirm would lose focus context).
  const [confirmClear, setConfirmClear] = useState(false);
  // Confirmation modal for abandoning unsaved edits to the document itself.
  const [confirmCancelEdit, setConfirmCancelEdit] = useState(false);

  // Keep the latest agent in a ref so the long-lived SSE handler can compare
  // against it without re-subscribing on every agent switch.
  const agentRef = useRef(agent);
  useEffect(() => {
    agentRef.current = agent;
  }, [agent]);

  // Mirror `editing` into a ref so the long-lived SSE handler can avoid
  // clobbering an in-progress draft when a memory_updated event arrives.
  const editingRef = useRef(editing);
  useEffect(() => {
    editingRef.current = editing;
  }, [editing]);

  // Abandon any in-progress edit when the agent changes — the draft belongs to
  // the agent it was opened for, and loadMemory will repopulate `content`.
  useEffect(() => {
    setEditing(false);
    setConfirmCancelEdit(false);
    setStaleWhileEditing(false);
  }, [agent]);

  // ── load MEMORY.md for the selected agent ───────────────────────────────
  const loadMemory = useCallback(() => {
    if (!agent) {
      setContent("");
      setSize(0);
      setError(null);
      setUpdatedAt(null);
      return;
    }
    setLoading(true);
    setError(null);
    api
      .get<GetMemoryResponse>(`/api/memory?agent=${encodeURIComponent(agent)}`)
      .then((res) => {
        const text = res.content ?? "";
        setContent(text);
        setSize(typeof res.size === "number" ? res.size : text.length);
        setUpdatedAt(new Date().toISOString());
      })
      .catch((err) => {
        const detail = err instanceof ApiError ? err.detail : t("error.loadMemory");
        setError(detail);
        setContent("");
        setSize(0);
      })
      .finally(() => setLoading(false));
  }, [agent, t]);

  useEffect(loadMemory, [loadMemory]);

  // ── live updates over the global SSE channel ────────────────────────────
  // Mirrors the old dashboard: `if (event.type === 'memory_updated' && …)
  // loadMemoryView()`. We additionally pulse a "Live" badge and only react to
  // events for the agent currently on screen.
  useEffect(() => {
    return subscribeStream((frame) => {
      const event = frame as StreamEvent;
      if (event.type !== "memory_updated") return;
      const evtAgent = event.data?.agent ?? null;
      // A null agent means "default workspace" — accept it for whichever agent
      // is in view, matching the backend which scopes by workspace root.
      if (evtAgent && agentRef.current && evtAgent !== agentRef.current) return;
      setLiveFlash(true);
      window.setTimeout(() => setLiveFlash(false), 1500);
      // Don't reload underneath an open editor — it would discard the draft.
      // Instead flag that the file changed on disk so the editor can warn that
      // saving overwrites the newer version and offer a reload escape hatch.
      if (editingRef.current) {
        setStaleWhileEditing(true);
        return;
      }
      loadMemory();
    });
  }, [loadMemory]);

  // ── derived: parsed sections + size + existing section names ────────────
  const parsed = useMemo(() => parseMemory(content), [content]);
  const sectionNames = useMemo(
    () => Array.from(new Set(parsed.sections.map((s) => s.name))).sort(),
    [parsed],
  );

  const filteredSections = useMemo<MemorySection[]>(() => {
    if (!filter.trim()) return parsed.sections;
    const q = filter.trim();
    return parsed.sections
      .filter((s) => sectionMatches(s, q))
      .map((s) => ({
        ...s,
        bullets: s.name.toLowerCase().includes(q.toLowerCase())
          ? s.bullets
          : s.bullets.filter((b) => bulletMatches(b, q)),
      }));
  }, [parsed, filter]);

  const visibleBulletCount = useMemo(
    () => filteredSections.reduce((acc, s) => acc + s.bullets.length, 0),
    [filteredSections],
  );

  // ── actions ─────────────────────────────────────────────────────────────
  const copyContent = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(content);
      toast.push(t("toast.copied"));
    } catch {
      toast.push(t("toast.copyFailed"), "error");
    }
  }, [content, toast, t]);

  const downloadContent = useCallback(() => {
    const blob = new Blob([content], { type: "text/markdown;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `MEMORY-${agent || "agent"}.md`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    toast.push(t("toast.downloadStarted"));
  }, [content, agent, t]);

  const toggleSection = useCallback((name: string) => {
    setCollapsed((prev) => ({ ...prev, [name]: !prev[name] }));
  }, []);

  const setAllCollapsed = useCallback(
    (value: boolean) => {
      const next: Record<string, boolean> = {};
      for (const s of parsed.sections) next[s.name] = value;
      setCollapsed(next);
    },
    [parsed],
  );

  const performSave = useCallback(async () => {
    if (readOnly) return;
    if (!agent) {
      toast.push(t("toast.selectAgent"), "error");
      return;
    }
    const trimmedFact = fact.trim();
    if (!trimmedFact) {
      toast.push(t("toast.enterFact"), "error");
      factRef.current?.focus();
      return;
    }
    const trimmedSection = section.trim();
    const body: MemoryAppendRequest = { content: trimmedFact, section: trimmedSection };
    setSaving(true);
    try {
      await api.post<{ ok: boolean }>(`/api/memory?agent=${encodeURIComponent(agent)}`, body);
      setFact("");
      setSection("");
      toast.push(
        trimmedSection
          ? t("toast.savedSection", { section: trimmedSection })
          : t("toast.savedDaily"),
      );
      loadMemory();
    } catch (err) {
      const detail = err instanceof ApiError ? err.detail : t("toast.saveFailed");
      toast.push(detail, "error");
    } finally {
      setSaving(false);
    }
  }, [agent, fact, section, toast, loadMemory, t, readOnly]);

  const saveFact = useCallback(
    (e: FormEvent) => {
      e.preventDefault();
      void performSave();
    },
    [performSave],
  );

  // Cmd/Ctrl+Enter submits from the textarea.
  const onFactKeyDown = useCallback(
    (e: KeyboardEvent<HTMLTextAreaElement>) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
        e.preventDefault();
        void performSave();
      }
    },
    [performSave],
  );

  const requestClear = useCallback(() => {
    if (fact.trim() || section.trim()) {
      setConfirmClear(true);
    }
  }, [fact, section]);

  const confirmedClear = useCallback(() => {
    setFact("");
    setSection("");
    setConfirmClear(false);
    factRef.current?.focus();
  }, []);

  // ── full-document inline editor ──────────────────────────────────────────
  const startEditing = useCallback(() => {
    if (readOnly) return;
    if (!agent) {
      toast.push(t("toast.selectAgent"), "error");
      return;
    }
    setDraft(content);
    setStaleWhileEditing(false);
    setEditing(true);
    // Focus the textarea once it has mounted.
    window.setTimeout(() => editorRef.current?.focus(), 0);
  }, [agent, content, toast, t, readOnly]);

  // Re-fetch MEMORY.md into the open editor, discarding the current draft. The
  // escape hatch offered by the stale-on-disk banner (the topbar Refresh button
  // is disabled while editing to avoid silently dropping a draft).
  const reloadIntoEditor = useCallback(async () => {
    if (!agent) return;
    try {
      const res = await api.get<GetMemoryResponse>(
        `/api/memory?agent=${encodeURIComponent(agent)}`,
      );
      const text = res.content ?? "";
      setContent(text);
      setSize(typeof res.size === "number" ? res.size : text.length);
      setUpdatedAt(new Date().toISOString());
      setDraft(text);
      setStaleWhileEditing(false);
      window.setTimeout(() => editorRef.current?.focus(), 0);
    } catch (err) {
      const detail = err instanceof ApiError ? err.detail : t("error.loadMemory");
      toast.push(detail, "error");
    }
  }, [agent, toast, t]);

  const saveDocument = useCallback(async () => {
    if (readOnly) return;
    if (!agent) {
      toast.push(t("toast.selectAgent"), "error");
      return;
    }
    setSavingDoc(true);
    try {
      const res = await api.put<{ ok: boolean; size?: number }>(
        `/api/memory?agent=${encodeURIComponent(agent)}`,
        { content: draft },
      );
      setContent(draft);
      setSize(typeof res.size === "number" ? res.size : draft.length);
      setUpdatedAt(new Date().toISOString());
      setStaleWhileEditing(false);
      setEditing(false);
      toast.push(t("toast.docSaved"));
    } catch (err) {
      const detail = err instanceof ApiError ? err.detail : t("toast.docSaveFailed");
      toast.push(detail, "error");
    } finally {
      setSavingDoc(false);
    }
  }, [agent, draft, toast, t, readOnly]);

  // Cmd/Ctrl+Enter saves the document from the editor.
  const onDocKeyDown = useCallback(
    (e: KeyboardEvent<HTMLTextAreaElement>) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
        e.preventDefault();
        if (dirty) void saveDocument();
      }
    },
    [dirty, saveDocument],
  );

  const requestCancelEdit = useCallback(() => {
    if (draft !== content) {
      setConfirmCancelEdit(true);
    } else {
      setEditing(false);
      setStaleWhileEditing(false);
    }
  }, [draft, content]);

  const confirmedCancelEdit = useCallback(() => {
    setEditing(false);
    setConfirmCancelEdit(false);
    setStaleWhileEditing(false);
  }, []);

  // Dismiss the discard prompt while staying in edit mode — return focus to the
  // still-mounted editor, matching the focus-restoration used by the fact form.
  const dismissCancelEdit = useCallback(() => {
    setConfirmCancelEdit(false);
    window.setTimeout(() => editorRef.current?.focus(), 0);
  }, []);

  const selectedAgent = current;

  // ── render ──────────────────────────────────────────────────────────────
  return (
    <>
      <div className="topbar">
        <BrandWordmark />
        <div className="row">
          {liveFlash && (
            <span
              className="badge"
              style={{
                background: "var(--success-soft)",
                color: "var(--success)",
                borderColor: "color-mix(in srgb, var(--success) 35%, transparent)",
                gap: 6,
              }}
            >
              <span
                style={{
                  width: 7,
                  height: 7,
                  borderRadius: "50%",
                  background: "var(--success)",
                  animation: "blink 1s steps(2) infinite",
                }}
              />
              {t("live")}
            </span>
          )}
          {/* The same app-wide picker the sidebar holds: on a phone the sidebar
              is a closed drawer, so the page names — and lets you change — the
              agent whose memory this is. Both write one store. */}
          <AgentPicker variant="inline" />
          <button
            className="btn"
            onClick={() => loadMemory()}
            disabled={loading || !agent || editing}
          >
            {loading ? <span className="spinner" /> : t("btn.refresh")}
          </button>
        </div>
      </div>

      <div className="page" style={{ maxWidth: 960 }}>
        <h1>{t("title")}</h1>
        <p className="muted">
          {agent ? (
            <>
              {t("intro.prefix")}
              <code>MEMORY.md</code>
              {t("intro.suffix", { agent: selectedAgent?.name ?? agent })}
            </>
          ) : (
            t("intro.none")
          )}
          {readOnly && t("intro.readonly")}
        </p>

        {/* ── MEMORY.md document ──────────────────────────────────────── */}
        <section className="card" style={{ padding: 0, marginTop: 16, overflow: "hidden" }}>
          <div
            className="row"
            style={{
              justifyContent: "space-between",
              padding: "12px 16px",
              borderBottom: "1px solid var(--border)",
              flexWrap: "wrap",
              gap: 10,
            }}
          >
            <div className="row" style={{ gap: 10 }}>
              <strong style={{ fontSize: 15 }}>MEMORY.md</strong>
              {/* Derived stats reflect the last-loaded document, not the live
                  draft — hide them while editing (the footer shows a live count),
                  mirroring the filter-bar / Refresh guards. */}
              {agent && !loading && !error && !editing && (
                <>
                  <span className="badge">{formatBytes(size)}</span>
                  <span className="badge">
                    {parsed.sections.length === 1
                      ? t("stat.sections", { n: String(parsed.sections.length) })
                      : t("stat.sections.plural", { n: String(parsed.sections.length) })}
                  </span>
                  <span className="badge">
                    {parsed.totalBullets === 1
                      ? t("stat.facts", { n: String(parsed.totalBullets) })
                      : t("stat.facts.plural", { n: String(parsed.totalBullets) })}
                  </span>
                  {updatedAt && (
                    <span className="muted" style={{ fontSize: 12 }}>
                      {t("stat.updated", { time: relativeTime(updatedAt, t) })}
                    </span>
                  )}
                </>
              )}
            </div>
            <div className="row" style={{ gap: 6 }}>
              {editing ? (
                <>
                  <button className="btn" onClick={requestCancelEdit} disabled={savingDoc}>
                    {t("btn.cancel")}
                  </button>
                  <button
                    className="btn primary"
                    onClick={() => void saveDocument()}
                    disabled={savingDoc || !dirty}
                  >
                    {savingDoc ? t("btn.saving") : t("btn.saveDoc")}
                  </button>
                </>
              ) : (
                <>
                  {/* view-mode toggle */}
                  <div
                    className="row"
                    style={{
                      gap: 0,
                      background: "var(--surface-2)",
                      borderRadius: "var(--r-sm)",
                      overflow: "hidden",
                    }}
                  >
                    {VIEW_MODES.map((m) => (
                      <button
                        key={m.id}
                        onClick={() => setViewMode(m.id)}
                        title={t(m.titleKey)}
                        style={{
                          padding: "5px 11px",
                          border: "none",
                          cursor: "pointer",
                          background: viewMode === m.id ? "var(--accent-soft)" : "transparent",
                          color: viewMode === m.id ? "var(--accent)" : "var(--text-2)",
                          fontWeight: viewMode === m.id ? 600 : 400,
                          transition: "background var(--t-fast) var(--ease)",
                        }}
                      >
                        {t(m.labelKey)}
                      </button>
                    ))}
                  </div>
                  <button
                    className="btn"
                    onClick={() => void copyContent()}
                    disabled={!agent || loading || !!error || !content}
                  >
                    {t("btn.copy")}
                  </button>
                  <button
                    className="btn"
                    onClick={downloadContent}
                    disabled={!agent || loading || !!error || !content}
                  >
                    {t("btn.download")}
                  </button>
                  {!readOnly && (
                    <button
                      className="btn"
                      onClick={startEditing}
                      disabled={!agent || loading || !!error}
                      title={t("btn.edit.title")}
                      style={{ display: "inline-flex", alignItems: "center", gap: 5 }}
                    >
                      <Pencil size={13} strokeWidth={1.75} />
                      {t("btn.edit")}
                    </button>
                  )}
                </>
              )}
            </div>
          </div>

          {/* filter / search bar (structured mode) */}
          {agent &&
            !loading &&
            !error &&
            !editing &&
            viewMode === "structured" &&
            parsed.sections.length > 0 && (
            <div
              className="row"
              style={{
                justifyContent: "space-between",
                padding: "10px 16px",
                borderBottom: "1px solid var(--border)",
                gap: 10,
              }}
            >
              <input
                className="input"
                style={{ maxWidth: 320 }}
                placeholder={t("filter.placeholder")}
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
              />
              <div className="row" style={{ gap: 6 }}>
                {filter && (
                  <span className="muted" style={{ fontSize: 12 }}>
                    {visibleBulletCount === 1
                      ? t("filter.matches", { n: String(visibleBulletCount) })
                      : t("filter.matches.plural", { n: String(visibleBulletCount) })}
                  </span>
                )}
                <button className="btn" onClick={() => setAllCollapsed(true)}>
                  {t("btn.collapseAll")}
                </button>
                <button className="btn" onClick={() => setAllCollapsed(false)}>
                  {t("btn.expandAll")}
                </button>
              </div>
            </div>
          )}

          <div style={{ padding: 0 }}>
            {!agent ? (
              <p className="muted" style={{ padding: 16, margin: 0 }}>
                {t("empty.noAgent")}
              </p>
            ) : loading ? (
              <div className="row" style={{ padding: 16, gap: 8 }}>
                <span className="spinner" />
                <span className="muted">{t("loading")}</span>
              </div>
            ) : error ? (
              <div style={{ padding: 16 }}>
                <p className="error-text" style={{ marginTop: 0 }}>
                  {error}
                </p>
                <button className="btn" onClick={() => loadMemory()}>
                  {t("btn.tryAgain")}
                </button>
              </div>
            ) : editing ? (
              <div style={{ padding: 0 }}>
                {staleWhileEditing && (
                  <div
                    className="row"
                    style={{
                      justifyContent: "space-between",
                      gap: 10,
                      flexWrap: "wrap",
                      padding: "9px 16px",
                      fontSize: 12,
                      background: "var(--warning-soft, var(--accent-soft))",
                      color: "var(--warning, var(--accent))",
                      borderBottom: "1px solid var(--border)",
                    }}
                  >
                    <span>{t("edit.stale")}</span>
                    <button
                      className="btn"
                      style={{ padding: "3px 9px", fontSize: 12 }}
                      onClick={() => void reloadIntoEditor()}
                      disabled={savingDoc}
                    >
                      {t("edit.reload")}
                    </button>
                  </div>
                )}
                <textarea
                  ref={editorRef}
                  className="input"
                  value={draft}
                  onChange={(e) => setDraft(e.target.value)}
                  onKeyDown={onDocKeyDown}
                  spellCheck={false}
                  placeholder={t("edit.placeholder")}
                  style={{
                    display: "block",
                    width: "100%",
                    boxSizing: "border-box",
                    minHeight: "55vh",
                    resize: "vertical",
                    border: "none",
                    borderRadius: 0,
                    fontFamily: "var(--font-mono)",
                    fontSize: 13,
                    lineHeight: 1.55,
                    background: "var(--code-bg)",
                    color: "var(--text)",
                    padding: "14px 16px",
                  }}
                />
                <div
                  className="row"
                  style={{
                    justifyContent: "space-between",
                    padding: "10px 16px",
                    borderTop: "1px solid var(--border)",
                    gap: 10,
                  }}
                >
                  <span className="muted" style={{ fontSize: 12 }}>
                    {t("edit.meta", { n: String([...draft].length) })}
                    {dirty ? ` · ${t("edit.unsaved")}` : ""}
                  </span>
                  <div className="row" style={{ gap: 8 }}>
                    <button className="btn" onClick={requestCancelEdit} disabled={savingDoc}>
                      {t("btn.cancel")}
                    </button>
                    <button
                      className="btn primary"
                      onClick={() => void saveDocument()}
                      disabled={savingDoc || !dirty}
                    >
                      {savingDoc ? t("btn.saving") : t("btn.saveDoc")}
                    </button>
                  </div>
                </div>
              </div>
            ) : !content.trim() ? (
              <div style={{ padding: "32px 16px", textAlign: "center" }}>
                <div style={{ marginBottom: 10, color: "var(--text-3)" }}>
                  <NotebookPen size={30} strokeWidth={1.5} />
                </div>
                <p style={{ margin: "0 0 4px", fontWeight: 600 }}>{t("empty.title")}</p>
                <p className="muted" style={{ margin: "0 0 14px", fontSize: 13 }}>
                  {t("empty.subtitle.prefix", { agent: selectedAgent?.name ?? agent })}
                  <code>MEMORY.md</code>
                  {t("empty.subtitle.suffix")}
                </p>
                {!readOnly && (
                  <button
                    className="btn primary"
                    onClick={() => factRef.current?.focus()}
                  >
                    {t("btn.addFirst")}
                  </button>
                )}
              </div>
            ) : viewMode === "raw" ? (
              <pre
                style={{
                  margin: 0,
                  maxHeight: "55vh",
                  overflow: "auto",
                  whiteSpace: "pre-wrap",
                  wordWrap: "break-word",
                  fontFamily: "var(--font-mono)",
                  fontSize: 13,
                  color: "var(--text-2)",
                  background: "var(--code-bg)",
                  padding: "14px 16px",
                }}
              >
                {content}
              </pre>
            ) : viewMode === "rendered" ? (
              <div style={{ maxHeight: "55vh", overflow: "auto", padding: "14px 18px" }}>
                <Markdown text={content} />
              </div>
            ) : (
              <div style={{ maxHeight: "55vh", overflow: "auto", padding: "8px 0" }}>
                {parsed.intro.length > 0 && (
                  <div
                    className="muted"
                    style={{
                      padding: "4px 16px 10px",
                      fontSize: 12,
                      whiteSpace: "pre-wrap",
                      borderBottom: "1px solid var(--border)",
                      marginBottom: 6,
                    }}
                  >
                    {parsed.intro.join("\n").trim()}
                  </div>
                )}
                {filteredSections.length === 0 ? (
                  <p className="muted" style={{ padding: "8px 16px", margin: 0 }}>
                    {t("filter.noMatch", { query: filter })}
                  </p>
                ) : (
                  filteredSections.map((sec) => {
                    const isCollapsed = !!collapsed[sec.name];
                    return (
                      <div key={sec.name} style={{ padding: "2px 8px" }}>
                        <div
                          role="button"
                          tabIndex={0}
                          onClick={() => toggleSection(sec.name)}
                          onKeyDown={(e) => {
                            if (e.key === "Enter" || e.key === " ") {
                              e.preventDefault();
                              toggleSection(sec.name);
                            }
                          }}
                          style={{
                            display: "flex",
                            alignItems: "center",
                            gap: 8,
                            width: "100%",
                            background: "transparent",
                            cursor: "pointer",
                            padding: "8px",
                            borderRadius: "var(--r-sm)",
                            textAlign: "left",
                          }}
                        >
                          <span
                            style={{
                              display: "inline-flex",
                              transition: "transform var(--t-fast) var(--ease)",
                              transform: isCollapsed ? "rotate(-90deg)" : "none",
                              color: "var(--text-3)",
                            }}
                          >
                            <ChevronDown size={15} strokeWidth={1.75} />
                          </span>
                          <strong style={{ fontSize: 14 }}>{sec.name}</strong>
                          <span className="badge">{sec.bullets.length}</span>
                          {!readOnly && (
                            <button
                              className="btn"
                              style={{
                                marginLeft: "auto",
                                padding: "3px 9px",
                                fontSize: 12,
                                display: "inline-flex",
                                alignItems: "center",
                                gap: 4,
                              }}
                              onClick={(e) => {
                                e.stopPropagation();
                                setSection(sec.name);
                                setViewMode("structured");
                                factRef.current?.focus();
                                toast.push(t("toast.addingTo", { section: sec.name }));
                              }}
                              title={t("section.add.title")}
                            >
                              <Plus size={13} strokeWidth={1.75} />
                              {t("section.add")}
                            </button>
                          )}
                        </div>
                        {!isCollapsed && (
                          <div style={{ padding: "0 8px 8px 30px" }}>
                            {sec.bullets.length === 0 && sec.prose.length === 0 ? (
                              <div className="muted" style={{ fontSize: 13, padding: "2px 0" }}>
                                {t("section.noFacts")}
                              </div>
                            ) : (
                              <>
                                {sec.bullets.map((b, i) => (
                                  <div
                                    key={i}
                                    style={{
                                      display: "flex",
                                      gap: 8,
                                      alignItems: "baseline",
                                      padding: "4px 0",
                                      fontSize: 13,
                                      borderBottom:
                                        i < sec.bullets.length - 1
                                          ? "1px solid var(--border)"
                                          : "none",
                                    }}
                                  >
                                    {b.date && (
                                      <span
                                        className="badge"
                                        style={{
                                          fontFamily: "var(--font-mono)",
                                          fontSize: 10,
                                          flexShrink: 0,
                                        }}
                                      >
                                        {b.date}
                                      </span>
                                    )}
                                    <span style={{ wordBreak: "break-word" }}>{b.body}</span>
                                  </div>
                                ))}
                                {sec.prose.length > 0 && (
                                  <div
                                    className="muted"
                                    style={{
                                      fontSize: 12,
                                      whiteSpace: "pre-wrap",
                                      marginTop: 6,
                                      fontFamily: "var(--font-mono)",
                                    }}
                                  >
                                    {sec.prose.join("\n")}
                                  </div>
                                )}
                              </>
                            )}
                          </div>
                        )}
                      </div>
                    );
                  })
                )}
              </div>
            )}
          </div>
        </section>

        {/* ── Add a fact ──────────────────────────────────────────────── */}
        {agent && !readOnly && (
          <section className="card" style={{ padding: 18, marginTop: 16 }}>
            <h2 style={{ marginTop: 0, fontSize: 15 }}>{t("form.heading")}</h2>
            <form onSubmit={saveFact} style={{ display: "flex", flexDirection: "column", gap: 12 }}>
              <div className="field">
                <label>
                  {t("form.section.label")}{" "}
                  <span className="muted" style={{ fontSize: 11 }}>
                    {t("form.section.hint")}
                  </span>
                </label>
                <input
                  className="input"
                  placeholder={t("form.section.placeholder")}
                  value={section}
                  list="memory-section-options"
                  onChange={(e) => setSection(e.target.value)}
                />
                {/* Existing section names as autocomplete suggestions. */}
                <datalist id="memory-section-options">
                  {sectionNames.map((name) => (
                    <option key={name} value={name} />
                  ))}
                </datalist>
                {sectionNames.length > 0 && (
                  <div className="row" style={{ flexWrap: "wrap", gap: 6, marginTop: 6 }}>
                    {sectionNames.slice(0, 12).map((name) => (
                      <button
                        type="button"
                        key={name}
                        className="badge"
                        style={{
                          cursor: "pointer",
                          border:
                            section === name
                              ? "1px solid var(--accent)"
                              : "1px solid transparent",
                        }}
                        onClick={() => setSection(section === name ? "" : name)}
                      >
                        {name}
                      </button>
                    ))}
                  </div>
                )}
              </div>
              <div className="field">
                <label>{t("form.fact.label")}</label>
                <textarea
                  ref={factRef}
                  className="input"
                  style={{ minHeight: 90, resize: "vertical" }}
                  placeholder={t("form.fact.placeholder")}
                  value={fact}
                  onChange={(e) => setFact(e.target.value)}
                  onKeyDown={onFactKeyDown}
                />
                <span className="muted" style={{ fontSize: 11 }}>
                  {t("form.fact.meta", { n: String(fact.trim().length) })}
                </span>
              </div>
              <div className="row" style={{ justifyContent: "space-between" }}>
                <span
                  className="muted"
                  style={{ fontSize: 12, display: "inline-flex", alignItems: "center", gap: 5 }}
                >
                  <CornerDownRight size={13} strokeWidth={1.75} />
                  {section.trim()
                    ? t("form.dest.section", { section: section.trim() })
                    : t("form.dest.daily")}
                </span>
                <div className="row" style={{ gap: 8 }}>
                  <button
                    type="button"
                    className="btn"
                    onClick={requestClear}
                    disabled={saving || (!fact.trim() && !section.trim())}
                  >
                    {t("btn.clear")}
                  </button>
                  <button className="btn primary" disabled={saving || !fact.trim()}>
                    {saving ? t("btn.saving") : t("btn.save")}
                  </button>
                </div>
              </div>
            </form>
          </section>
        )}
      </div>

      {/* ── confirm-clear modal (shared .modal-overlay — Aurora backdrop) ── */}
      {confirmClear && (
        <div className="modal-overlay" onClick={() => setConfirmClear(false)}>
          <div
            className="card"
            onClick={(e) => e.stopPropagation()}
            style={{ width: 360, maxWidth: "100%", padding: 22, boxShadow: "var(--shadow-lg)" }}
          >
            <h2 style={{ margin: "0 0 8px", fontSize: 16 }}>{t("confirm.title")}</h2>
            <p className="muted" style={{ marginTop: 0 }}>
              {t("confirm.body")}
            </p>
            <div className="row" style={{ justifyContent: "flex-end", gap: 8, marginTop: 8 }}>
              <button className="btn" onClick={() => setConfirmClear(false)}>
                {t("confirm.keep")}
              </button>
              <button className="btn danger" onClick={confirmedClear}>
                {t("confirm.discard")}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ── confirm-discard-edit modal (unsaved MEMORY.md changes) ────────── */}
      {confirmCancelEdit && (
        <div className="modal-overlay" onClick={dismissCancelEdit}>
          <div
            className="card"
            onClick={(e) => e.stopPropagation()}
            style={{ width: 360, maxWidth: "100%", padding: 22, boxShadow: "var(--shadow-lg)" }}
          >
            <h2 style={{ margin: "0 0 8px", fontSize: 16 }}>{t("confirm.cancelEdit.title")}</h2>
            <p className="muted" style={{ marginTop: 0 }}>
              {t("confirm.cancelEdit.body")}
            </p>
            <div className="row" style={{ justifyContent: "flex-end", gap: 8, marginTop: 8 }}>
              <button className="btn" onClick={dismissCancelEdit}>
                {t("confirm.keep")}
              </button>
              <button className="btn danger" onClick={confirmedCancelEdit}>
                {t("confirm.discard")}
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
