import { useCallback, useEffect, useRef, useState, type KeyboardEvent } from "react";
import { ArrowLeft, Pencil, Plus, StickyNote, Trash2 } from "lucide-react";
import { subscribeStream } from "../../core/agentStream";
import { usePersistentState } from "../../core/usePersistentState";
import { useUrlParam, type UrlHistory } from "../../core/useUrlParam";
import { useCurrentAgent } from "../../core/currentAgent";
import { useAuth } from "../../core/auth";
import { api, ApiError } from "../../core/api";
import { useT } from "../../core/i18n";
import { useMediaQuery, MOBILE_QUERY } from "../../core/useMediaQuery";
import { BrandWordmark } from "../../ui/BrandMark";
import { AgentPicker } from "../nav/AgentPicker";
import { Markdown } from "../../ui/Markdown";
import { useToast } from "../../ui/toast";
import { STR } from "./strings";

// Shapes of GET /api/notes and GET /api/notes/entry — see olano/runtime/api.py
// (the dashboard surface over olano/memory/knowledge_vault.py NotesStore).
interface NoteMeta {
  title: string;
  tags: string[];
  aliases: string[];
  auto_inject: boolean;
  created: string;
  updated: string;
  snippet: string;
}

interface NotesListResponse {
  notes: NoteMeta[];
  count: number;
  link_count: number;
  query: string;
}

interface NoteEntry extends NoteMeta {
  content: string;
  sources: string[];
  outlinks: string[];
  backlinks: string[];
}

// /api/stream SSE envelope. notes_updated carries {agent, title}; we refresh
// when the event targets the agent currently in view.
interface StreamEvent {
  type: string;
  data?: { agent?: string | null; title?: string };
}

// Editor draft — comma-joined list fields, matching how the notes_add tool
// takes them; split/trimmed on save.
interface Draft {
  isNew: boolean;
  title: string;
  content: string;
  tags: string;
  aliases: string;
  sources: string;
  autoInject: boolean;
}

type T = (key: string, vars?: Record<string, string>) => string;

function relativeTime(iso: string, t: T): string {
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "";
  const secs = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (secs < 5) return t("time.justNow");
  if (secs < 60) return t("time.secs", { n: String(secs) });
  const mins = Math.round(secs / 60);
  if (mins < 60) return t("time.mins", { n: String(mins) });
  const hrs = Math.round(mins / 60);
  if (hrs < 48) return t("time.hrs", { n: String(hrs) });
  return t("time.days", { n: String(Math.round(hrs / 24)) });
}

function splitCsv(value: string): string[] {
  return value
    .split(",")
    .map((v) => v.trim())
    .filter(Boolean);
}

// List snippets come as raw markdown; strip the noisiest tokens (headings,
// emphasis, wikilink brackets, bullets) so the preview reads as prose.
function cleanSnippet(text: string): string {
  return text
    .replace(/[#*`>]/g, "")
    .replace(/\[\[([^\]|]+)(?:\|[^\]]+)?\]\]/g, "$1")
    .replace(/^\s*[-•]\s*/gm, "")
    .replace(/\s+/g, " ")
    .trim();
}

export function NotesView() {
  const toast = useToast();
  const t = useT(STR);
  const isMobile = useMediaQuery(MOBILE_QUERY);

  // Agent selection comes from the app-wide picker in the sidebar; notes are
  // per-workspace, same scoping as the Files and Memory views.
  const { agent, current } = useCurrentAgent();

  // Non-admin members in a multi-user install may view notes only; the server
  // blocks their writes centrally, so the UI just hides the write affordances.
  const { user, multiUser } = useAuth();
  const readOnly = multiUser && !!user && !user.is_admin;

  // ── list state ──────────────────────────────────────────────────────────
  const [notes, setNotes] = useState<NoteMeta[]>([]);
  const [count, setCount] = useState(0);
  const [linkCount, setLinkCount] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [liveFlash, setLiveFlash] = useState(false);

  // Search box value + the debounced query actually sent to the server.
  const [query, setQuery] = usePersistentState("notes.query", "");
  const [debouncedQuery, setDebouncedQuery] = useState(query);
  useEffect(() => {
    const id = window.setTimeout(() => setDebouncedQuery(query.trim()), 300);
    return () => window.clearTimeout(id);
  }, [query]);

  // ── detail state ────────────────────────────────────────────────────────
  // The open note is the URL's `?note=<title>` (notes are addressed by title),
  // so a reload or a copied link reopens it and Back steps out of it. Opening
  // a note (a row, a wikilink, a backlink) is a Back step; landing on the
  // note just saved, or leaving one just deleted, is not.
  const [noteParam, setNoteParam] = useUrlParam("note");
  const selected = noteParam ?? "";
  const setSelected = useCallback(
    (title: string, history: UrlHistory = "push") => setNoteParam(title || null, { history }),
    [setNoteParam],
  );
  const [entry, setEntry] = useState<NoteEntry | null>(null);
  const [entryLoading, setEntryLoading] = useState(false);
  const [entryError, setEntryError] = useState<string | null>(null);

  // ── editor state ────────────────────────────────────────────────────────
  const [draft, setDraft] = useState<Draft | null>(null);
  const [saving, setSaving] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const titleRef = useRef<HTMLInputElement | null>(null);
  const contentRef = useRef<HTMLTextAreaElement | null>(null);

  // Keep the latest agent in a ref so the long-lived SSE handler can compare
  // against it without re-subscribing on every agent switch.
  const agentRef = useRef(agent);
  useEffect(() => {
    agentRef.current = agent;
  }, [agent]);

  // The entry and any open editor belong to the agent they were opened for:
  // drop them when the agent changes. (The selection needs no reset: switching
  // agent moves to /notes/<other>, which names no note.)
  useEffect(() => {
    setEntry(null);
    setEntryError(null);
    setDraft(null);
    setConfirmDelete(false);
  }, [agent]);

  // ── load the note list (or search results) ──────────────────────────────
  const loadNotes = useCallback(() => {
    if (!agent) {
      setNotes([]);
      setCount(0);
      setLinkCount(0);
      setError(null);
      return;
    }
    setLoading(true);
    setError(null);
    const q = debouncedQuery ? `&q=${encodeURIComponent(debouncedQuery)}` : "";
    api
      .get<NotesListResponse>(`/api/notes?agent=${encodeURIComponent(agent)}${q}`)
      .then((res) => {
        setNotes(res.notes ?? []);
        setCount(res.count ?? 0);
        setLinkCount(res.link_count ?? 0);
      })
      .catch((err) => {
        const detail = err instanceof ApiError ? err.detail : t("error.load");
        setError(detail);
        setNotes([]);
      })
      .finally(() => setLoading(false));
  }, [agent, debouncedQuery, t]);

  useEffect(loadNotes, [loadNotes]);

  // ── load the selected note ──────────────────────────────────────────────
  const loadEntry = useCallback(() => {
    if (!agent || !selected) {
      setEntry(null);
      setEntryError(null);
      return;
    }
    setEntryLoading(true);
    setEntryError(null);
    api
      .get<NoteEntry>(
        `/api/notes/entry?agent=${encodeURIComponent(agent)}&title=${encodeURIComponent(selected)}`,
      )
      .then(setEntry)
      .catch((err) => {
        setEntry(null);
        if (err instanceof ApiError && err.status === 404) {
          setEntryError(t("toast.noteMissing", { title: selected }));
        } else {
          setEntryError(err instanceof ApiError ? err.detail : t("error.loadEntry"));
        }
      })
      .finally(() => setEntryLoading(false));
  }, [agent, selected, t]);

  useEffect(loadEntry, [loadEntry]);

  // ── live updates over the global SSE channel ────────────────────────────
  // notes_updated fires on every dashboard write; agent-tool writes surface on
  // the next refresh. Reloading never clobbers an open editor — the draft is
  // its own state, not derived from `entry`.
  useEffect(() => {
    return subscribeStream((frame) => {
      const event = frame as StreamEvent;
      if (event.type !== "notes_updated") return;
      const evtAgent = event.data?.agent ?? null;
      if (evtAgent && agentRef.current && evtAgent !== agentRef.current) return;
      setLiveFlash(true);
      window.setTimeout(() => setLiveFlash(false), 1500);
      loadNotes();
      loadEntry();
    });
  }, [loadNotes, loadEntry]);

  // ── actions ─────────────────────────────────────────────────────────────
  const openNote = useCallback(
    (title: string) => {
      setDraft(null);
      setConfirmDelete(false);
      setSelected(title);
    },
    [setSelected],
  );

  const startNew = useCallback(
    (title = "") => {
      if (readOnly) return;
      if (!agent) {
        toast.push(t("toast.selectAgent"), "error");
        return;
      }
      setConfirmDelete(false);
      setDraft({
        isNew: true,
        title,
        content: "",
        tags: "",
        aliases: "",
        sources: "",
        autoInject: false,
      });
      window.setTimeout(() => titleRef.current?.focus(), 0);
    },
    [agent, readOnly, toast, t],
  );

  const startEdit = useCallback(() => {
    if (readOnly || !entry) return;
    setConfirmDelete(false);
    setDraft({
      isNew: false,
      title: entry.title,
      content: entry.content,
      tags: entry.tags.join(", "),
      aliases: entry.aliases.join(", "),
      sources: entry.sources.join(", "),
      autoInject: entry.auto_inject,
    });
    window.setTimeout(() => contentRef.current?.focus(), 0);
  }, [entry, readOnly]);

  const saveDraft = useCallback(async () => {
    if (readOnly || !draft) return;
    if (!agent) {
      toast.push(t("toast.selectAgent"), "error");
      return;
    }
    const title = draft.title.trim();
    if (!title) {
      toast.push(t("toast.enterTitle"), "error");
      titleRef.current?.focus();
      return;
    }
    setSaving(true);
    try {
      await api.post<{ ok: boolean }>(`/api/notes?agent=${encodeURIComponent(agent)}`, {
        title,
        content: draft.content,
        tags: splitCsv(draft.tags),
        aliases: splitCsv(draft.aliases),
        sources: splitCsv(draft.sources),
        auto_inject: draft.autoInject,
      });
      setDraft(null);
      setSelected(title, "replace");
      toast.push(t("toast.saved", { title }));
      loadNotes();
      loadEntry();
    } catch (err) {
      const detail = err instanceof ApiError ? err.detail : t("toast.saveFailed");
      toast.push(detail, "error");
    } finally {
      setSaving(false);
    }
  }, [agent, draft, readOnly, toast, t, loadNotes, loadEntry]);

  // Cmd/Ctrl+Enter saves from any editor field.
  const onDraftKeyDown = useCallback(
    (e: KeyboardEvent<HTMLElement>) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
        e.preventDefault();
        void saveDraft();
      }
    },
    [saveDraft],
  );

  const deleteSelected = useCallback(async () => {
    if (readOnly || !agent || !entry) return;
    const title = entry.title;
    setDeleting(true);
    try {
      await api.del<{ ok: boolean }>(
        `/api/notes/entry?agent=${encodeURIComponent(agent)}&title=${encodeURIComponent(title)}`,
      );
      setConfirmDelete(false);
      setSelected("", "replace");
      setEntry(null);
      toast.push(t("toast.deleted", { title }));
      loadNotes();
    } catch (err) {
      const detail = err instanceof ApiError ? err.detail : t("toast.deleteFailed");
      toast.push(detail, "error");
    } finally {
      setDeleting(false);
    }
  }, [agent, entry, readOnly, toast, t, loadNotes]);

  const selectedAgent = current;
  const editing = draft !== null;
  // Mobile is single-pane: the list yields to the reader/editor and a back
  // button returns; desktop shows both side by side.
  const showList = !isMobile || (!selected && !editing);
  const showDetail = !isMobile || !!selected || editing;

  const linkChips = useCallback(
    (titles: string[], label: string) =>
      titles.length > 0 && (
        <div style={{ marginTop: 10 }}>
          <span className="muted" style={{ fontSize: 12, marginRight: 8 }}>
            {label}
          </span>
          <span className="row" style={{ display: "inline-flex", flexWrap: "wrap", gap: 6 }}>
            {titles.map((title) => (
              <button
                key={title}
                type="button"
                className="badge"
                style={{
                  cursor: "pointer",
                  background: "var(--accent-soft)",
                  color: "var(--accent)",
                  border: "1px solid transparent",
                }}
                onClick={() => openNote(title)}
              >
                [[{title}]]
              </button>
            ))}
          </span>
        </div>
      ),
    [openNote],
  );

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
          {/* The app-wide agent picker, repeated here for the same reason as on
              Memory: the sidebar copy is a closed drawer on a phone. */}
          <AgentPicker variant="inline" />
          <button className="btn" onClick={loadNotes} disabled={loading || !agent}>
            {loading ? <span className="spinner" /> : t("btn.refresh")}
          </button>
        </div>
      </div>

      <div className="page" style={{ maxWidth: 1100 }}>
        <h1>{t("title")}</h1>
        <p className="muted">
          {agent ? (
            <>
              {t("intro.prefix")}
              <code>notes/</code>
              {t("intro.suffix", { agent: selectedAgent?.name ?? agent })}
            </>
          ) : (
            t("intro.none")
          )}
          {readOnly && t("intro.readonly")}
        </p>

        {/* ── toolbar: search + stats + new ─────────────────────────────── */}
        <div className="row" style={{ gap: 10, marginTop: 12, flexWrap: "wrap" }}>
          <input
            className="input"
            style={{ maxWidth: 320, flex: "1 1 200px" }}
            placeholder={t("search.placeholder")}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            disabled={!agent}
          />
          {query && (
            <button className="btn" onClick={() => setQuery("")}>
              {t("search.clear")}
            </button>
          )}
          <span className="badge">
            {count === 1
              ? t("stat.notes", { n: String(count) })
              : t("stat.notes.plural", { n: String(count) })}
          </span>
          <span className="badge">
            {linkCount === 1
              ? t("stat.links", { n: String(linkCount) })
              : t("stat.links.plural", { n: String(linkCount) })}
          </span>
          {!readOnly && (
            <button
              className="btn primary"
              style={{ marginLeft: "auto", display: "inline-flex", alignItems: "center", gap: 5 }}
              onClick={() => startNew()}
              disabled={!agent}
            >
              <Plus size={13} strokeWidth={1.75} />
              {t("btn.new")}
            </button>
          )}
        </div>

        <div
          style={{
            display: "grid",
            gridTemplateColumns: isMobile ? "1fr" : "minmax(240px, 320px) 1fr",
            gap: 16,
            marginTop: 16,
            alignItems: "start",
          }}
        >
          {/* ── note list ──────────────────────────────────────────────── */}
          {showList && (
            <section className="card" style={{ padding: 0, overflow: "hidden" }}>
              {!agent ? (
                <p className="muted" style={{ padding: 16, margin: 0 }}>
                  {t("empty.noAgent")}
                </p>
              ) : loading && notes.length === 0 ? (
                <div className="row" style={{ padding: 16, gap: 8 }}>
                  <span className="spinner" />
                  <span className="muted">{t("loading")}</span>
                </div>
              ) : error ? (
                <div style={{ padding: 16 }}>
                  <p className="error-text" style={{ marginTop: 0 }}>
                    {error}
                  </p>
                  <button className="btn" onClick={loadNotes}>
                    {t("btn.tryAgain")}
                  </button>
                </div>
              ) : notes.length === 0 ? (
                <div style={{ padding: "28px 16px", textAlign: "center" }}>
                  <div style={{ marginBottom: 10, color: "var(--text-3)" }}>
                    <StickyNote size={30} strokeWidth={1.5} />
                  </div>
                  {debouncedQuery ? (
                    <p className="muted" style={{ margin: 0, fontSize: 13 }}>
                      {t("empty.search", { q: debouncedQuery })}
                    </p>
                  ) : (
                    <>
                      <p style={{ margin: "0 0 4px", fontWeight: 600 }}>{t("empty.title")}</p>
                      <p className="muted" style={{ margin: "0 0 14px", fontSize: 13 }}>
                        {t("empty.body", { agent: selectedAgent?.name ?? agent })}
                      </p>
                      {!readOnly && (
                        <button className="btn primary" onClick={() => startNew()}>
                          {t("btn.new")}
                        </button>
                      )}
                    </>
                  )}
                </div>
              ) : (
                <div style={{ maxHeight: "62vh", overflow: "auto" }}>
                  {debouncedQuery && (
                    <div
                      className="muted"
                      style={{
                        fontSize: 12,
                        padding: "8px 14px",
                        borderBottom: "1px solid var(--border)",
                      }}
                    >
                      {notes.length === 1
                        ? t("search.results", { n: "1", q: debouncedQuery })
                        : t("search.results.plural", {
                            n: String(notes.length),
                            q: debouncedQuery,
                          })}
                    </div>
                  )}
                  {notes.map((n) => {
                    const active = n.title === selected && !editing;
                    return (
                      <div
                        key={n.title}
                        role="button"
                        tabIndex={0}
                        onClick={() => openNote(n.title)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter" || e.key === " ") {
                            e.preventDefault();
                            openNote(n.title);
                          }
                        }}
                        style={{
                          padding: "10px 14px",
                          borderBottom: "1px solid var(--border)",
                          cursor: "pointer",
                          background: active ? "var(--accent-soft)" : "transparent",
                        }}
                      >
                        <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
                          <strong style={{ fontSize: 14, wordBreak: "break-word" }}>
                            {n.title}
                          </strong>
                          {n.auto_inject && (
                            <span className="badge" title={t("badge.autoInject.title")}>
                              {t("badge.autoInject")}
                            </span>
                          )}
                        </div>
                        {n.snippet && (
                          <div
                            className="muted"
                            style={{
                              fontSize: 12,
                              marginTop: 3,
                              display: "-webkit-box",
                              WebkitLineClamp: 2,
                              WebkitBoxOrient: "vertical",
                              overflow: "hidden",
                              wordBreak: "break-word",
                            }}
                          >
                            {cleanSnippet(n.snippet)}
                          </div>
                        )}
                        <div className="row" style={{ gap: 6, marginTop: 5, flexWrap: "wrap" }}>
                          {n.tags.map((tag) => (
                            <span key={tag} className="badge" style={{ fontSize: 10 }}>
                              {tag}
                            </span>
                          ))}
                          <span className="muted" style={{ fontSize: 11 }}>
                            {t("list.updated", { time: relativeTime(n.updated, t) })}
                          </span>
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </section>
          )}

          {/* ── reader / editor pane ───────────────────────────────────── */}
          {showDetail && (
            <section className="card" style={{ padding: 0, overflow: "hidden" }}>
              {editing ? (
                /* ── editor ─────────────────────────────────────────────── */
                <div style={{ padding: 18, display: "flex", flexDirection: "column", gap: 12 }}>
                  <div className="row" style={{ justifyContent: "space-between", gap: 8 }}>
                    <h2 style={{ margin: 0, fontSize: 15 }}>
                      {draft.isNew
                        ? t("form.heading.new")
                        : t("form.heading.edit", { title: draft.title })}
                    </h2>
                    {isMobile && (
                      <button className="btn" onClick={() => setDraft(null)}>
                        {t("btn.cancel")}
                      </button>
                    )}
                  </div>
                  <div className="field">
                    <label>{t("form.title.label")}</label>
                    <input
                      ref={titleRef}
                      className="input"
                      value={draft.title}
                      placeholder={t("form.title.placeholder")}
                      disabled={!draft.isNew}
                      onChange={(e) => setDraft({ ...draft, title: e.target.value })}
                      onKeyDown={onDraftKeyDown}
                    />
                    <span className="muted" style={{ fontSize: 11 }}>
                      {t("form.title.help")}
                    </span>
                  </div>
                  <div className="field">
                    <label>{t("form.content.label")}</label>
                    <textarea
                      ref={contentRef}
                      className="input"
                      style={{
                        minHeight: 180,
                        resize: "vertical",
                        fontFamily: "var(--font-mono)",
                        fontSize: 13,
                      }}
                      value={draft.content}
                      placeholder={t("form.content.placeholder")}
                      onChange={(e) => setDraft({ ...draft, content: e.target.value })}
                      onKeyDown={onDraftKeyDown}
                    />
                    <span className="muted" style={{ fontSize: 11 }}>
                      {t("form.content.help")}
                    </span>
                  </div>
                  <div
                    style={{
                      display: "grid",
                      gridTemplateColumns: isMobile ? "1fr" : "1fr 1fr",
                      gap: 12,
                    }}
                  >
                    <div className="field">
                      <label>{t("form.tags.label")}</label>
                      <input
                        className="input"
                        value={draft.tags}
                        placeholder="infra, runbook"
                        onChange={(e) => setDraft({ ...draft, tags: e.target.value })}
                        onKeyDown={onDraftKeyDown}
                      />
                      <span className="muted" style={{ fontSize: 11 }}>
                        {t("form.tags.help")}
                      </span>
                    </div>
                    <div className="field">
                      <label>{t("form.aliases.label")}</label>
                      <input
                        className="input"
                        value={draft.aliases}
                        placeholder="k8s"
                        onChange={(e) => setDraft({ ...draft, aliases: e.target.value })}
                        onKeyDown={onDraftKeyDown}
                      />
                      <span className="muted" style={{ fontSize: 11 }}>
                        {t("form.aliases.help")}
                      </span>
                    </div>
                  </div>
                  <div className="field">
                    <label>{t("form.sources.label")}</label>
                    <input
                      className="input"
                      value={draft.sources}
                      placeholder="https://kubernetes.io/docs, docs/runbook.md"
                      onChange={(e) => setDraft({ ...draft, sources: e.target.value })}
                      onKeyDown={onDraftKeyDown}
                    />
                    <span className="muted" style={{ fontSize: 11 }}>
                      {t("form.sources.help")}
                    </span>
                  </div>
                  <div className="field">
                    <label
                      className="row"
                      style={{ gap: 8, cursor: "pointer", alignItems: "center" }}
                    >
                      <input
                        type="checkbox"
                        checked={draft.autoInject}
                        onChange={(e) => setDraft({ ...draft, autoInject: e.target.checked })}
                      />
                      {t("form.autoInject.label")}
                    </label>
                    <span className="muted" style={{ fontSize: 11 }}>
                      {t("form.autoInject.help")}
                    </span>
                  </div>
                  <div className="row" style={{ justifyContent: "flex-end", gap: 8 }}>
                    <button className="btn" onClick={() => setDraft(null)} disabled={saving}>
                      {t("btn.cancel")}
                    </button>
                    <button
                      className="btn primary"
                      onClick={() => void saveDraft()}
                      disabled={saving || !draft.title.trim()}
                    >
                      {saving ? t("btn.saving") : t("btn.save")}
                    </button>
                  </div>
                </div>
              ) : !selected ? (
                <div style={{ padding: "56px 16px", textAlign: "center" }}>
                  <div style={{ marginBottom: 10, color: "var(--text-3)" }}>
                    <StickyNote size={30} strokeWidth={1.5} />
                  </div>
                  <p className="muted" style={{ margin: 0, fontSize: 13 }}>
                    {t("detail.placeholder")}
                  </p>
                </div>
              ) : entryLoading ? (
                <div className="row" style={{ padding: 16, gap: 8 }}>
                  <span className="spinner" />
                  <span className="muted">{t("loading")}</span>
                </div>
              ) : entryError ? (
                <div style={{ padding: 16 }}>
                  <p className="error-text" style={{ marginTop: 0 }}>
                    {entryError}
                  </p>
                  <div className="row" style={{ gap: 8 }}>
                    <button className="btn" onClick={() => setSelected("")}>
                      {t("btn.back")}
                    </button>
                    <button className="btn" onClick={loadEntry}>
                      {t("btn.tryAgain")}
                    </button>
                    {!readOnly && (
                      <button className="btn primary" onClick={() => startNew(selected)}>
                        {t("btn.new")}
                      </button>
                    )}
                  </div>
                </div>
              ) : entry ? (
                /* ── reader ─────────────────────────────────────────────── */
                <>
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
                    <div className="row" style={{ gap: 10, flexWrap: "wrap" }}>
                      {isMobile && (
                        <button
                          className="btn"
                          style={{ display: "inline-flex", alignItems: "center", gap: 5 }}
                          onClick={() => setSelected("")}
                        >
                          <ArrowLeft size={13} strokeWidth={1.75} />
                          {t("btn.back")}
                        </button>
                      )}
                      <strong style={{ fontSize: 15, wordBreak: "break-word" }}>
                        {entry.title}
                      </strong>
                      {entry.auto_inject && (
                        <span className="badge" title={t("badge.autoInject.title")}>
                          {t("badge.autoInject")}
                        </span>
                      )}
                      <span className="muted" style={{ fontSize: 12 }}>
                        {t("list.updated", { time: relativeTime(entry.updated, t) })}
                      </span>
                    </div>
                    {!readOnly && (
                      <div className="row" style={{ gap: 6 }}>
                        <button
                          className="btn"
                          style={{ display: "inline-flex", alignItems: "center", gap: 5 }}
                          onClick={startEdit}
                        >
                          <Pencil size={13} strokeWidth={1.75} />
                          {t("btn.edit")}
                        </button>
                        <button
                          className="btn danger"
                          style={{ display: "inline-flex", alignItems: "center", gap: 5 }}
                          onClick={() => setConfirmDelete(true)}
                        >
                          <Trash2 size={13} strokeWidth={1.75} />
                          {t("btn.delete")}
                        </button>
                      </div>
                    )}
                  </div>
                  <div style={{ padding: "14px 18px", maxHeight: "58vh", overflow: "auto" }}>
                    <div className="row" style={{ gap: 6, flexWrap: "wrap", marginBottom: 8 }}>
                      {entry.tags.map((tag) => (
                        <span key={tag} className="badge">
                          {tag}
                        </span>
                      ))}
                    </div>
                    <Markdown text={entry.content} />
                    {entry.aliases.length > 0 && (
                      <p className="muted" style={{ fontSize: 12, marginBottom: 0 }}>
                        {t("detail.aliases")}: {entry.aliases.join(", ")}
                      </p>
                    )}
                    {entry.sources.length > 0 && (
                      <p className="muted" style={{ fontSize: 12, marginBottom: 0 }}>
                        {t("detail.sources")}: {entry.sources.join(", ")}
                      </p>
                    )}
                    {linkChips(entry.outlinks, t("detail.links"))}
                    {linkChips(entry.backlinks, t("detail.backlinks"))}
                  </div>
                </>
              ) : null}
            </section>
          )}
        </div>
      </div>

      {/* ── confirm-delete modal (shared .modal-overlay pattern) ─────────── */}
      {confirmDelete && entry && (
        <div className="modal-overlay" onClick={() => setConfirmDelete(false)}>
          <div
            className="card"
            onClick={(e) => e.stopPropagation()}
            style={{ width: 380, maxWidth: "100%", padding: 22, boxShadow: "var(--shadow-lg)" }}
          >
            <h2 style={{ margin: "0 0 8px", fontSize: 16 }}>
              {t("confirm.delete.title", { title: entry.title })}
            </h2>
            <p className="muted" style={{ marginTop: 0 }}>
              {t("confirm.delete.body")}
            </p>
            <div className="row" style={{ justifyContent: "flex-end", gap: 8, marginTop: 8 }}>
              <button className="btn" onClick={() => setConfirmDelete(false)} disabled={deleting}>
                {t("confirm.keep")}
              </button>
              <button
                className="btn danger"
                onClick={() => void deleteSelected()}
                disabled={deleting}
              >
                {deleting ? t("btn.saving") : t("confirm.delete")}
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
