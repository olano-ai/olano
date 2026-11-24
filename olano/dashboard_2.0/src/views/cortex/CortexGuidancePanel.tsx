import { useCallback, useEffect, useRef, useState, type KeyboardEvent } from "react";
import { ChevronRight, Eye, Pencil, Sparkles } from "lucide-react";
import { subscribeStream } from "../../core/agentStream";
import { usePersistentState } from "../../core/usePersistentState";
import { useAuth } from "../../core/auth";
import { api, ApiError } from "../../core/api";
import { useT } from "../../core/i18n";
import { useToast } from "../../ui/toast";
import { ConfirmModal, Markdown } from "./ui";
import { STR } from "./strings";

// The optional creative-direction file steering the Cortex Engine's autonomous
// phases. Mirrors olano/cognition/guidance.py (CORTEX_FILE / CORTEX_TEMPLATE):
// substance-free by default (only headings + commented hints) so the engine
// ignores it until the user or agent adds real bullets.
const CORTEX_FILE = "CORTEX.md";

// Seed shown in the editor when the file is missing/empty — kept in sync with
// the backend template so a first-time edit starts from the same scaffold.
const CORTEX_TEMPLATE = `# CORTEX.md — Creative direction for the Cortex Engine

<!--
OPTIONAL. This file steers what your agent pursues when it thinks on its OWN —
the Cortex Engine's generative phases: Pulse, Brainstorming, Ideas, Prototypes,
and Advance. It does NOT change normal chat (that's IDENTITY.md / SOUL.md).

Fill in any section below to nudge autonomous generation toward what matters to
you; leave a section empty to ignore it. The agent may also add to this file
during self-learning as it understands your direction better.
-->

## Goals
<!-- Concrete things to work toward, e.g.
- Ship a personal-finance dashboard this quarter
- Grow the newsletter to 1,000 subscribers
-->

## Dreams
<!-- Aspirational, north-star directions for ideas & brainstorms, e.g.
- Build a tool that makes tax filing effortless
-->

## Avoid
<!-- Topics or kinds of suggestions you do NOT want, e.g.
- No crypto-trading ideas
- Nothing that requires paid advertising
-->
`;

// GET /api/files/{path} → { path, content, size }.
interface FileResponse {
  content: string;
  size?: number;
}

// Strip HTML comments (the seed's inline hints) so the rendered preview stays
// clean; the raw editor keeps them so the scaffold guides first-time edits.
function stripComments(text: string): string {
  return text.replace(/<!--[\s\S]*?-->/g, "");
}

// True when the comment-stripped text has real content (a bullet or a
// non-heading line), not just the seeded section headings. Matches the
// _has_substance check the backend reader uses to decide whether to inject it.
function hasSubstance(stripped: string): boolean {
  for (const line of stripped.split("\n")) {
    const s = line.trim();
    if (!s || s.startsWith("#")) continue;
    return true;
  }
  return false;
}

// The "Creative direction" sub-section of the Cortex settings card — reads
// CORTEX.md for the selected agent, renders a Markdown preview by default, and
// lets the user edit & save it in place.
//
// It used to be a peer CARD beside the Autonomy panel, which is the confusion
// this consolidation removes: two top-level headings ("Autonomous Cortex" and
// "Customize your Cortex Engine") that both promise engine settings, so the
// reader had to open both to learn which one held the knob they wanted. Same
// controls, one place: what the engine may do, and what it should aim at.
//
// `sectionActive` is the parent card's open state. The fetch is gated on both,
// so a collapsed card costs no request even when this sub-section's own
// disclosure was left open on a previous visit.
export function CortexGuidancePanel({
  agent,
  sectionActive = true,
}: {
  agent: string;
  sectionActive?: boolean;
}) {
  const t = useT(STR);
  const toast = useToast();

  // Non-admin members in a multi-user install may view but not write (the
  // server enforces this with 403s on PUT /api/files); hide the edit affordances.
  const { user, multiUser } = useAuth();
  const readOnly = multiUser && !!user && !user.is_admin;

  // Collapsed by default — it's a secondary/advanced control, like the
  // Autonomy panel's phase grid. Persisted so expand/collapse survives nav.
  const [open, setOpen] = usePersistentState("cortex.guidance.open", false);

  const [content, setContent] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Inline editor: `editing` swaps the preview for a raw textarea bound to
  // `draft`; saving PUTs the whole file.
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const [confirmCancel, setConfirmCancel] = useState(false);
  // A file_updated event (e.g. the agent editing CORTEX.md during self-learning)
  // arrived while editing — suppress the live reload, warn that saving overwrites.
  const [stale, setStale] = useState(false);
  const editorRef = useRef<HTMLTextAreaElement | null>(null);
  const dirty = editing && draft !== content;

  const previewText = stripComments(content).trim();
  const customized = hasSubstance(stripComments(content));

  const load = useCallback(() => {
    if (!agent) return;
    setLoading(true);
    setError(null);
    api
      .get<FileResponse>(`/api/files/${CORTEX_FILE}?agent=${encodeURIComponent(agent)}`)
      .then((res) => setContent(res.content ?? ""))
      .catch((err) => {
        // A missing file is a normal "not customized yet" state, not an error —
        // fresh workspaces seed it, but be defensive if it was deleted.
        if (err instanceof ApiError && err.status === 404) {
          setContent("");
          return;
        }
        setError(err instanceof ApiError ? err.detail : t("cx.loadFailed"));
        setContent("");
      })
      .finally(() => setLoading(false));
  }, [agent, t]);

  // Load lazily — only when both the card and this sub-section are open (and on
  // agent switch while they are).
  useEffect(() => {
    if (open && sectionActive && agent) load();
  }, [open, sectionActive, agent, load]);

  // Abandon any in-progress edit when the agent changes (the draft belongs to
  // the previous agent).
  useEffect(() => {
    setEditing(false);
    setConfirmCancel(false);
    setStale(false);
  }, [agent]);

  // Live updates over the global SSE channel — refresh when CORTEX.md changes
  // on disk for the agent in view. Guarded against clobbering an open draft.
  const editingRef = useRef(editing);
  editingRef.current = editing;
  const agentRef = useRef(agent);
  agentRef.current = agent;
  const openRef = useRef(open);
  openRef.current = open;
  useEffect(() => {
    return subscribeStream((frame) => {
      const ev = frame as { type?: string; data?: { agent?: string | null; path?: string } };
      if (ev.type !== "file_updated") return;
      if (ev.data?.path && ev.data.path !== CORTEX_FILE) return;
      const evtAgent = ev.data?.agent ?? null;
      if (evtAgent && agentRef.current && evtAgent !== agentRef.current) return;
      if (!openRef.current) return;
      if (editingRef.current) {
        setStale(true);
        return;
      }
      load();
    });
  }, [load]);

  const startEditing = useCallback(() => {
    if (readOnly) return;
    // Seed a missing/empty file with the scaffold so the user has structure to
    // fill in; otherwise edit the file as-is (comments preserved).
    setDraft(content.trim() ? content : CORTEX_TEMPLATE);
    setStale(false);
    setEditing(true);
    window.setTimeout(() => editorRef.current?.focus(), 0);
  }, [content, readOnly]);

  const save = useCallback(async () => {
    if (readOnly || !agent) return;
    setSaving(true);
    try {
      await api.put<{ size?: number }>(
        `/api/files/${CORTEX_FILE}?agent=${encodeURIComponent(agent)}`,
        { content: draft },
      );
      setContent(draft);
      setEditing(false);
      setStale(false);
      toast.push(t("cx.saved"));
    } catch (err) {
      toast.push(err instanceof ApiError ? err.detail : t("cx.saveFailed"), "error");
    } finally {
      setSaving(false);
    }
  }, [agent, draft, readOnly, toast, t]);

  const onEditorKeyDown = useCallback(
    (e: KeyboardEvent<HTMLTextAreaElement>) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
        e.preventDefault();
        if (dirty) void save();
      }
    },
    [dirty, save],
  );

  const requestCancel = useCallback(() => {
    if (dirty) setConfirmCancel(true);
    else {
      setEditing(false);
      setStale(false);
    }
  }, [dirty]);

  return (
    <section className="cortex-subsection">
      <details open={open}>
        <summary
          className="phase-disclosure cortex-subsection-toggle"
          onClick={(e) => {
            e.preventDefault();
            setOpen((o) => !o);
          }}
        >
          <ChevronRight size={15} strokeWidth={2.25} aria-hidden />
          <Sparkles size={15} strokeWidth={1.75} aria-hidden style={{ color: "var(--accent)" }} />
          {t("cx.title")}
          {customized ? (
            <span className="badge" style={{ marginLeft: 2 }}>
              {t("cx.badge.customized")}
            </span>
          ) : null}
        </summary>

        {open ? (
          <div style={{ marginTop: 12 }}>
            {/* how-to help */}
            <div
              style={{
                background: "var(--surface-2)",
                border: "1px solid var(--border)",
                borderRadius: "var(--r-sm)",
                padding: "12px 14px",
                fontSize: 12.5,
                lineHeight: 1.55,
                marginBottom: 14,
              }}
            >
              <div className="muted" style={{ marginBottom: 8 }}>
                {t("cx.help.lead")}
              </div>
              <ul style={{ margin: 0, paddingLeft: 18, display: "flex", flexDirection: "column", gap: 4 }}>
                <li>
                  <strong>{t("cx.help.goals.h")}</strong> — {t("cx.help.goals.d")}
                </li>
                <li>
                  <strong>{t("cx.help.dreams.h")}</strong> — {t("cx.help.dreams.d")}
                </li>
                <li>
                  <strong>{t("cx.help.avoid.h")}</strong> — {t("cx.help.avoid.d")}
                </li>
              </ul>
              <div className="muted" style={{ marginTop: 8 }}>
                {t("cx.help.selflearn")}
              </div>
            </div>

            {/* toolbar */}
            <div
              className="row"
              style={{ justifyContent: "space-between", alignItems: "center", gap: 8, marginBottom: 10, flexWrap: "wrap" }}
            >
              <div className="row" style={{ gap: 8 }}>
                <strong style={{ fontSize: 13, fontFamily: "var(--font-mono)" }}>{CORTEX_FILE}</strong>
                <span className="muted" style={{ fontSize: 12, display: "inline-flex", alignItems: "center", gap: 5 }}>
                  {editing ? <Pencil size={12} strokeWidth={1.75} /> : <Eye size={12} strokeWidth={1.75} />}
                  {editing ? t("cx.mode.edit") : t("cx.mode.preview")}
                </span>
              </div>
              <div className="row" style={{ gap: 6 }}>
                {editing ? (
                  <>
                    <button className="btn" onClick={requestCancel} disabled={saving}>
                      {t("cx.btn.cancel")}
                    </button>
                    <button className="btn primary" onClick={() => void save()} disabled={saving || !dirty}>
                      {saving ? t("cx.btn.saving") : t("cx.btn.save")}
                    </button>
                  </>
                ) : (
                  <>
                    <button className="btn" onClick={load} disabled={loading}>
                      {t("cx.btn.refresh")}
                    </button>
                    {!readOnly ? (
                      <button
                        className="btn"
                        onClick={startEditing}
                        disabled={loading || !!error}
                        style={{ display: "inline-flex", alignItems: "center", gap: 5 }}
                      >
                        <Pencil size={13} strokeWidth={1.75} />
                        {t("cx.btn.edit")}
                      </button>
                    ) : null}
                  </>
                )}
              </div>
            </div>

            {/* body */}
            {loading ? (
              <div className="spinner" />
            ) : error ? (
              <div>
                <p className="error-text" style={{ marginTop: 0 }}>
                  {error}
                </p>
                <button className="btn" onClick={load}>
                  {t("cx.btn.refresh")}
                </button>
              </div>
            ) : editing ? (
              <div>
                {stale ? (
                  <div
                    className="row"
                    style={{
                      justifyContent: "space-between",
                      gap: 10,
                      flexWrap: "wrap",
                      padding: "8px 12px",
                      fontSize: 12,
                      background: "var(--warning-soft, var(--accent-soft))",
                      color: "var(--warning, var(--accent))",
                      borderRadius: "var(--r-sm)",
                      marginBottom: 8,
                    }}
                  >
                    <span>{t("cx.stale")}</span>
                    <button
                      className="btn"
                      style={{ padding: "3px 9px", fontSize: 12 }}
                      onClick={load}
                      disabled={saving}
                    >
                      {t("cx.reload")}
                    </button>
                  </div>
                ) : null}
                <textarea
                  ref={editorRef}
                  className="input"
                  value={draft}
                  onChange={(e) => setDraft(e.target.value)}
                  onKeyDown={onEditorKeyDown}
                  spellCheck={false}
                  placeholder={t("cx.edit.placeholder")}
                  style={{
                    display: "block",
                    width: "100%",
                    boxSizing: "border-box",
                    minHeight: "40vh",
                    resize: "vertical",
                    fontFamily: "var(--font-mono)",
                    fontSize: 13,
                    lineHeight: 1.55,
                  }}
                />
                <div className="row" style={{ justifyContent: "space-between", marginTop: 8, gap: 10 }}>
                  <span className="muted" style={{ fontSize: 12 }}>
                    {t("cx.edit.hint")}
                  </span>
                  <span className="muted" style={{ fontSize: 12 }}>
                    {t("cx.chars", { n: String([...draft].length) })}
                    {dirty ? ` · ${t("cx.unsaved")}` : ""}
                  </span>
                </div>
              </div>
            ) : customized ? (
              <div
                style={{
                  maxHeight: "45vh",
                  overflow: "auto",
                  padding: "6px 14px",
                  background: "var(--surface)",
                  border: "1px solid var(--border)",
                  borderRadius: "var(--r-sm)",
                }}
              >
                <Markdown source={previewText} />
              </div>
            ) : (
              <div
                style={{
                  textAlign: "center",
                  padding: "28px 16px",
                  background: "var(--surface)",
                  border: "1px dashed var(--border)",
                  borderRadius: "var(--r-sm)",
                }}
              >
                <div style={{ color: "var(--text-3)", marginBottom: 8 }}>
                  <Sparkles size={26} strokeWidth={1.5} />
                </div>
                <p style={{ margin: "0 0 4px", fontWeight: 600, fontSize: 14 }}>{t("cx.empty.title")}</p>
                <p className="muted" style={{ margin: "0 0 14px", fontSize: 13 }}>
                  {t("cx.empty.body")}
                </p>
                {!readOnly ? (
                  <button className="btn primary" onClick={startEditing} disabled={loading}>
                    {t("cx.empty.cta")}
                  </button>
                ) : null}
              </div>
            )}

            {readOnly ? (
              <p className="muted" style={{ fontSize: 12, marginTop: 8 }}>
                {t("cx.readOnly")}
              </p>
            ) : null}
          </div>
        ) : null}
      </details>

      {confirmCancel ? (
        <ConfirmModal
          title={t("cx.confirm.cancel.title")}
          confirmLabel={t("cx.confirm.discard")}
          danger
          message={t("cx.confirm.cancel.body")}
          onCancel={() => {
            setConfirmCancel(false);
            window.setTimeout(() => editorRef.current?.focus(), 0);
          }}
          onConfirm={() => {
            setConfirmCancel(false);
            setEditing(false);
            setStale(false);
          }}
        />
      ) : null}
    </section>
  );
}
