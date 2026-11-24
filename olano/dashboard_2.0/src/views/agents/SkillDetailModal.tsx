// Skill viewer — opens a skill from the Skills tab and shows what it actually
// says: SKILL.md rendered, plus the scripts, forms, references and assets
// bundled beside it, each previewable in place.
//
// It has its own read endpoints (`/api/catalog/skills/detail|file|file-raw`)
// rather than reusing the Files view, because the common pack lives in
// `<home>/skills` — outside every agent workspace, and so unreachable through
// `/api/files`, which refuses anything resolving out of the workspace root.
// That is also why editing is not offered here: a skill inside the workspace
// links out to the Files view, which owns the write path, and everything else
// is honestly labelled read-only instead of given a second, weaker one.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { FileCode2, FileText, Folder, Image as ImageIcon, Lock, Pencil, Zap } from "lucide-react";
import { api, ApiError } from "../../core/api";
import { useMediaQuery, MOBILE_QUERY } from "../../core/useMediaQuery";
import { useT } from "../../core/i18n";
import { Markdown } from "../../ui/Markdown";
import { Modal } from "./Modal";
import { STR } from "./strings";
import { scopeLabelKey, scopeHintKey } from "./helpers";
import type { SkillDetail, SkillEntry, SkillFileContent, SkillFileEntry } from "./types";

const SKILL_MD = "SKILL.md";

function errText(e: unknown, fallback: string): string {
  return e instanceof ApiError ? e.detail : e instanceof Error ? e.message : fallback;
}

function query(params: Record<string, string>): string {
  return new URLSearchParams(params).toString();
}

/** Bytes → a short human size, matching the Files view's terse column. */
function humanSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function fileGlyph(entry: SkillFileEntry) {
  if (entry.type === "directory") return <Folder size={14} strokeWidth={1.75} />;
  if (entry.kind === "image") return <ImageIcon size={14} strokeWidth={1.75} />;
  if (entry.kind === "markdown") return <FileText size={14} strokeWidth={1.75} />;
  return <FileCode2 size={14} strokeWidth={1.75} />;
}

/**
 * Split a SKILL.md's leading YAML frontmatter off its body.
 *
 * Markdown renderers have no idea what `---\nname: …\n---` means, so it came
 * out as a horizontal rule followed by the name and description as prose —
 * directly under the header that already shows both. The block is still the
 * skill's contract (tier, trigger, allowed tools), so it is kept behind a
 * disclosure rather than dropped.
 */
function splitFrontmatter(text: string): { frontmatter: string | null; body: string } {
  const lines = text.split("\n");
  if (lines[0]?.trim() !== "---") return { frontmatter: null, body: text };
  const end = lines.findIndex((line, i) => i > 0 && line.trim() === "---");
  if (end === -1) return { frontmatter: null, body: text };
  return {
    frontmatter: lines.slice(1, end).join("\n").trim(),
    body: lines.slice(end + 1).join("\n").replace(/^\n+/, ""),
  };
}

export function SkillDetailModal({
  skill,
  agent,
  onClose,
}: {
  skill: SkillEntry;
  agent: string;
  onClose: () => void;
}) {
  const t = useT(STR);
  const isMobile = useMediaQuery(MOBILE_QUERY);
  const location = skill.location || "";

  const [detail, setDetail] = useState<SkillDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string>(SKILL_MD);
  const [file, setFile] = useState<SkillFileContent | null>(null);
  const [fileError, setFileError] = useState<string | null>(null);
  const [fileLoading, setFileLoading] = useState(false);
  // Which file the preview is *for*. Clicking down a folder faster than the
  // fetches resolve would otherwise let an earlier response land last and show
  // the wrong file under the highlighted row.
  const pending = useRef(SKILL_MD);

  useEffect(() => {
    let alive = true;
    setDetail(null);
    setError(null);
    setSelected(SKILL_MD);
    pending.current = SKILL_MD;
    setFile(null);
    api
      .get<SkillDetail>(`/api/catalog/skills/detail?${query({ location, agent })}`)
      .then((d) => {
        if (!alive) return;
        setDetail(d);
        // SKILL.md rides along with the detail response — the file every reader
        // wants first renders without a second round trip.
        setFile(d.content);
      })
      .catch((e) => alive && setError(errText(e, t("skillView.loadFailed"))));
    return () => {
      alive = false;
    };
  }, [location, agent, t]);

  const openFile = useCallback(
    (entry: SkillFileEntry) => {
      if (entry.type === "directory") return;
      setSelected(entry.path);
      pending.current = entry.path;
      setFileError(null);
      if (entry.path === SKILL_MD && detail) {
        setFile(detail.content);
        setFileLoading(false);
        return;
      }
      setFile(null);
      setFileLoading(true);
      api
        .get<SkillFileContent>(
          `/api/catalog/skills/file?${query({ location, agent, path: entry.path })}`,
        )
        .then((f) => {
          if (pending.current !== entry.path) return;
          setFile(f);
        })
        .catch((e) => {
          if (pending.current !== entry.path) return;
          setFileError(errText(e, t("skillView.fileFailed")));
        })
        .finally(() => {
          if (pending.current === entry.path) setFileLoading(false);
        });
    },
    [location, agent, detail, t],
  );

  const meta = detail?.skill ?? skill;
  const scope = meta.scope ?? null;
  const editable = meta.editable === true;
  const workspacePath = meta.workspace_path ?? null;

  // Files under their folder, folders in path order — a skill's own layout is
  // the fastest way to see it bundles a script or a form, so it is kept rather
  // than flattened into one alphabetical list.
  const entries = useMemo(() => {
    const files = detail?.files ?? [];
    const rank = (e: SkillFileEntry) => (e.path === SKILL_MD ? 0 : 1);
    return [...files].sort((a, b) => rank(a) - rank(b) || a.path.localeCompare(b.path));
  }, [detail]);

  const rawSrc = file
    ? `/api/catalog/skills/file-raw?${query({ location, agent, path: file.path })}`
    : "";

  // Phones stack the two panes rather than swapping between them: a skill
  // bundles a handful of files, so a short capped list above the content costs
  // less than a pane switch and a back button to read the file you opened the
  // skill for.
  const fileList = (
    <div
      style={{
        borderRight: isMobile ? "none" : "1px solid var(--border)",
        borderBottom: isMobile ? "1px solid var(--border)" : "none",
        paddingRight: isMobile ? 0 : 10,
        paddingBottom: isMobile ? 8 : 0,
        // On a phone the list is a short index above the text, capped so it
        // never pushes the thing you opened the skill to read off-screen.
        maxHeight: isMobile ? 160 : undefined,
        overflowY: "auto",
        minHeight: 0,
      }}
    >
      {entries.map((entry) => {
        const active = entry.path === selected;
        const depth = entry.path.split("/").length - 1;
        return (
          <button
            key={entry.path}
            type="button"
            className="menu-item"
            aria-current={active || undefined}
            disabled={entry.type === "directory"}
            onClick={() => openFile(entry)}
            style={{
              width: "100%",
              gap: 7,
              paddingLeft: 8 + depth * 12,
              minHeight: isMobile ? 40 : undefined,
              fontFamily: "var(--font-mono)",
              fontSize: 12,
              background: active ? "var(--surface-3)" : undefined,
              color: entry.type === "directory" ? "var(--text-3)" : undefined,
              cursor: entry.type === "directory" ? "default" : "pointer",
            }}
          >
            <span style={{ display: "inline-flex", color: "var(--text-4)" }}>{fileGlyph(entry)}</span>
            <span style={{ flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis" }}>
              {entry.name}
            </span>
            {entry.type === "file" ? (
              <span className="muted" style={{ fontSize: 10 }}>
                {humanSize(entry.size)}
              </span>
            ) : null}
          </button>
        );
      })}
      {detail?.truncated ? (
        <div className="muted" style={{ fontSize: 11, padding: "8px 10px", lineHeight: 1.45 }}>
          {t("skillView.truncatedFiles")}
        </div>
      ) : null}
    </div>
  );

  const preview = (
    <div style={{ overflowY: "auto", minHeight: 0 }}>
      {fileError ? (
        <div className="muted" style={{ fontSize: 13 }}>
          {fileError}
        </div>
      ) : fileLoading || !file ? (
        <div className="spinner" style={{ margin: 24 }} />
      ) : file.kind === "image" ? (
        <div style={{ display: "grid", placeItems: "center", padding: 12 }}>
          <img src={rawSrc} alt={file.name} style={{ maxWidth: "100%", objectFit: "contain" }} />
        </div>
      ) : file.kind === "binary" ? (
        <div className="muted" style={{ fontSize: 13, lineHeight: 1.55 }}>
          {t("skillView.binary", { size: humanSize(file.size) })}{" "}
          <a href={rawSrc} target="_blank" rel="noreferrer">
            {t("skillView.openRaw")}
          </a>
        </div>
      ) : file.kind === "markdown" ? (
        <MarkdownPane text={file.content ?? ""} frontmatterLabel={t("skillView.frontmatter")} />
      ) : (
        <pre
          style={{
            margin: 0,
            padding: 14,
            background: "var(--code-bg)",
            borderRadius: "var(--r-sm)",
            fontFamily: "var(--font-mono)",
            fontSize: 13,
            lineHeight: 1.65,
            // A script wraps rather than scrolling sideways: the pane is a
            // reading surface, and a horizontal scrollbar hides the ends of
            // exactly the long lines you opened it to read.
            whiteSpace: "pre-wrap",
            wordBreak: "break-word",
          }}
        >
          {file.content ?? ""}
        </pre>
      )}
      {file?.truncated ? (
        <div className="muted" style={{ fontSize: 11, marginTop: 8 }}>
          {t("skillView.truncatedFile")}
        </div>
      ) : null}
    </div>
  );

  return (
    <Modal
      title={meta.name || t("skills.unnamed")}
      subtitle={meta.description || meta.location}
      onClose={onClose}
      // Wide enough to read a runbook in, not to skim a summary: this dialog
      // exists to show a document. The Modal shell caps it to the viewport.
      width={1140}
      // Actions only. The scope explanation is prose about the skill, so it
      // lives in the body: as a footer it wrapped to four lines on a phone and
      // pushed the buttons of a bottom sheet against the edge of the screen.
      footer={
        <>
          {editable && workspacePath ? (
            <Link
              to={`/files?path=${encodeURIComponent(workspacePath)}&agent=${encodeURIComponent(agent)}`}
              className="btn"
              style={{ marginRight: "auto" }}
            >
              {t("skillView.openInFiles")}
            </Link>
          ) : null}
          <button className="btn" onClick={onClose}>
            {t("skillView.close")}
          </button>
        </>
      }
    >
      {error ? (
        <div className="muted" style={{ fontSize: 13, padding: "18px 0" }}>
          {error}
        </div>
      ) : !detail ? (
        <div className="spinner" style={{ margin: 32 }} />
      ) : (
        <>
          <div className="row" style={{ gap: 6, flexWrap: "wrap", marginBottom: 8 }}>
            <SkillBadges skill={meta} />
          </div>
          {/* Every skill says where it lives and what that means for editing —
              not just the read-only ones, so the badge is never the whole
              explanation. */}
          <div
            className="muted row"
            style={{
              gap: 5,
              alignItems: "flex-start",
              fontSize: 11.5,
              lineHeight: 1.45,
              marginBottom: 12,
            }}
          >
            {editable ? (
              <Pencil size={12} strokeWidth={1.75} style={{ flexShrink: 0, marginTop: 2 }} />
            ) : (
              <Lock size={12} strokeWidth={1.75} style={{ flexShrink: 0, marginTop: 2 }} />
            )}
            <span>{t(scopeHintKey(scope))}</span>
          </div>
          <div
            style={{
              display: "grid",
              gridTemplateColumns: isMobile ? "1fr" : "minmax(200px, 260px) 1fr",
              gap: 14,
              // A SKILL.md is a document, so the reading pane takes the height
              // the dialog can actually have rather than a fixed box: the card
              // caps itself at the viewport (.modal-card), and this is what
              // makes it *reach* that cap instead of shrink-wrapping a short
              // file. The subtrahend is the chrome above and below — head,
              // badge row, scope hint, footer. Both columns then scroll
              // independently; overshooting it only makes the body scroll too,
              // since .modal-card caps itself at the viewport regardless.
              height: isMobile ? "auto" : "max(380px, calc(100vh - 285px))",
              minHeight: 0,
            }}
          >
            {fileList}
            {preview}
          </div>
        </>
      )}
    </Modal>
  );
}

/** Rendered markdown, with any YAML frontmatter folded into a disclosure. */
function MarkdownPane({ text, frontmatterLabel }: { text: string; frontmatterLabel: string }) {
  const { frontmatter, body } = splitFrontmatter(text);
  return (
    // Reading type, not UI type — this is the document the agent follows. The
    // measure cap is what the width buys: a wider dialog should give longer
    // *documents* more room, not stretch every line past comfortable reading.
    <div
      data-testid="skill-doc"
      style={{ lineHeight: 1.7, fontSize: 14.5, maxWidth: "80ch", paddingRight: 4 }}
    >
      {frontmatter ? (
        <details style={{ marginBottom: 12 }}>
          <summary className="muted" style={{ fontSize: 12, cursor: "pointer" }}>
            {frontmatterLabel}
          </summary>
          <pre
            style={{
              margin: "6px 0 0",
              padding: 10,
              background: "var(--code-bg)",
              borderRadius: "var(--r-sm)",
              fontFamily: "var(--font-mono)",
              fontSize: 12,
              lineHeight: 1.5,
              whiteSpace: "pre-wrap",
              wordBreak: "break-word",
            }}
          >
            {frontmatter}
          </pre>
        </details>
      ) : null}
      <Markdown text={body} />
    </div>
  );
}

/**
 * The badge row shared by the skills list and this viewer's header, so a skill
 * reads the same in both places: where it lives, whether it can be edited here,
 * and whether it runs in the prompt or as a delegated subagent.
 */
export function SkillBadges({ skill }: { skill: SkillEntry }) {
  const t = useT(STR);
  const scope = skill.scope ?? null;
  const editable = skill.editable === true;
  return (
    <>
      {scope ? (
        <span className="badge" title={t(scopeHintKey(scope))} style={{ gap: 4 }}>
          <Zap size={11} strokeWidth={2} />
          {t(scopeLabelKey(scope))}
        </span>
      ) : null}
      <span
        className="badge"
        title={editable ? t("skillView.editableHint") : t(scopeHintKey(scope))}
        style={{ gap: 4, color: editable ? "var(--text-2)" : "var(--text-3)" }}
      >
        {editable ? <Pencil size={11} strokeWidth={2} /> : <Lock size={11} strokeWidth={2} />}
        {editable ? t("skillView.editable") : t("skillView.readOnly")}
      </span>
      {skill.tier === "subagent" ? (
        <span className="badge" title={t("skillView.subagentHint")}>
          {t("skillView.subagent")}
        </span>
      ) : null}
      {/* Only ever a negative badge: "on" is the default and needs no label,
          while "off" is the state someone will otherwise spend a while
          wondering about. Rendered here rather than only in the list so the
          viewer header agrees with the row that opened it. */}
      {skill.enabled === false ? (
        <span className="badge" title={t("skillView.offHint")} style={{ color: "var(--text-3)" }}>
          {t("skillView.off")}
        </span>
      ) : null}
      {/* Gated by `requires_tools`: not a switch state, a "not written for
          this agent" — the title names the tools that would change that. */}
      {skill.gated ? (
        <span
          className="badge"
          title={t("skillView.gatedHint", { tools: skill.gated })}
          style={{ color: "var(--text-3)" }}
        >
          {t("skillView.gated")}
        </span>
      ) : null}
    </>
  );
}
