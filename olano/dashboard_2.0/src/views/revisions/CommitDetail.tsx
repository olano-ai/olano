// One commit, expanded under its row in the history: who and why, a summary
// of the files it changed (added / modified / deleted, lines in and out), and
// each file's diff on demand, side by side or unified.

import { useEffect, useState } from "react";
import { ChevronDown, ChevronRight, Columns2, Copy, Download, FileText, Rows2 } from "lucide-react";
import type { ViewType } from "react-diff-view";
import { ApiError, api } from "../../core/api";
import { useT } from "../../core/i18n";
import { MOBILE_QUERY, useMediaQuery } from "../../core/useMediaQuery";
import { usePersistentState } from "../../core/usePersistentState";
import { useToast } from "../../ui/toast";
import { DiffView } from "./DiffView";
import { STR } from "./strings";
import type { CommitDetails, CommitFile, FileDiffResponse } from "./types";

const FULL_CONTEXT = 100000;

/** A file's exact bytes at a commit, served as a download. */
export function rawUrl(path: string, rev: string): string {
  return `/api/revisions/raw?path=${encodeURIComponent(path)}&rev=${encodeURIComponent(rev)}`;
}

/** The diff layout: the user's last choice, else split on desktop and
 *  unified on a phone (two columns of code at 390px are unreadable). */
export function useDiffViewType(): [ViewType, (v: ViewType) => void] {
  const mobile = useMediaQuery(MOBILE_QUERY);
  const [chosen, setChosen] = usePersistentState<ViewType | "">("revisions.diffView", "");
  return [chosen || (mobile ? "unified" : "split"), setChosen];
}

export function DiffLayoutToggle({ value, onChange }: { value: ViewType; onChange: (v: ViewType) => void }) {
  const t = useT(STR);
  return (
    <div className="revisions-seg" role="group" aria-label={t("diff.view")}>
      <button
        className={`btn xs${value === "split" ? " active" : ""}`}
        aria-pressed={value === "split"}
        onClick={() => onChange("split")}
      >
        <Columns2 size={12} strokeWidth={1.75} /> {t("diff.split")}
      </button>
      <button
        className={`btn xs${value === "unified" ? " active" : ""}`}
        aria-pressed={value === "unified"}
        onClick={() => onChange("unified")}
      >
        <Rows2 size={12} strokeWidth={1.75} /> {t("diff.unified")}
      </button>
    </div>
  );
}

function FileDiff({
  rev,
  file,
  onViewFile,
}: {
  rev: string;
  file: CommitFile;
  onViewFile: (path: string, rev: string) => void;
}) {
  const t = useT(STR);
  const [viewType, setViewType] = useDiffViewType();
  const [full, setFull] = useState(false);
  const [diff, setDiff] = useState<FileDiffResponse | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let live = true;
    setError("");
    const params = new URLSearchParams({ rev, path: file.path, context: String(full ? FULL_CONTEXT : 3) });
    api
      .get<FileDiffResponse>(`/api/revisions/diff?${params.toString()}`)
      .then((res) => live && setDiff(res))
      .catch((err) => live && setError(err instanceof ApiError ? err.detail : t("diff.err")));
    return () => {
      live = false;
    };
  }, [rev, file.path, full, t]);

  return (
    <div className="revisions-file-diff">
      <div className="revisions-diff-toolbar">
        <DiffLayoutToggle value={viewType} onChange={setViewType} />
        <label className="revisions-check" title={t("diff.full.title")}>
          <input type="checkbox" checked={full} onChange={(e) => setFull(e.target.checked)} /> {t("diff.full")}
        </label>
        {file.status !== "deleted" ? (
          <button className="btn xs" onClick={() => onViewFile(file.path, rev)}>
            <FileText size={12} strokeWidth={1.75} /> {t("diff.open")}
          </button>
        ) : null}
      </div>
      {error ? (
        <p className="error-text">{error}</p>
      ) : !diff ? (
        <div className="row muted" style={{ gap: 8, fontSize: 12 }}>
          <div className="spinner" /> {t("diff.loading")}
        </div>
      ) : diff.truncated ? (
        <p className="muted revisions-diff-empty">{t("diff.truncated")}</p>
      ) : diff.binary || file.binary ? (
        <div className="revisions-diff-empty">
          <p className="muted">{t("diff.binary")}</p>
          <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
            {file.status !== "added" && diff.base && !/^4b825dc/.test(diff.base) ? (
              <a className="btn xs" href={rawUrl(file.path, diff.base)} download>
                <Download size={12} strokeWidth={1.75} /> {t("diff.downloadOld")}
              </a>
            ) : null}
            {file.status !== "deleted" ? (
              <a className="btn xs" href={rawUrl(file.path, rev)} download>
                <Download size={12} strokeWidth={1.75} /> {t("diff.downloadNew")}
              </a>
            ) : null}
          </div>
        </div>
      ) : (
        <DiffView
          patch={diff.patch}
          viewType={viewType}
          emptyText={t("diff.empty")}
          labels={[t("diff.old"), t("diff.new")]}
        />
      )}
    </div>
  );
}

function StatusBadge({ status }: { status: CommitFile["status"] }) {
  const t = useT(STR);
  const letter = status === "added" ? "A" : status === "deleted" ? "D" : "M";
  return (
    <span className={`revisions-status revisions-status--${status}`} title={t(`status.${status}`)}>
      {letter}
    </span>
  );
}

function ChangeBar({ additions, deletions }: { additions: number; deletions: number }) {
  // Five cells like GitHub's: proportions at a glance, exact counts beside it.
  const total = additions + deletions;
  const adds = total === 0 ? 0 : Math.round((additions / total) * 5);
  const cells = Array.from({ length: 5 }, (_, i) => (total === 0 ? "none" : i < adds ? "add" : "del"));
  return (
    <span className="revisions-bar" aria-hidden="true">
      {cells.map((c, i) => (
        <span key={i} className={`revisions-bar-cell revisions-bar-cell--${c}`} />
      ))}
    </span>
  );
}

export function CommitDetail({
  rev,
  onViewFile,
}: {
  rev: string;
  onViewFile: (path: string, rev: string) => void;
}) {
  const t = useT(STR);
  const toast = useToast();
  const [details, setDetails] = useState<CommitDetails | null>(null);
  const [error, setError] = useState("");
  const [open, setOpen] = useState<Set<string>>(new Set());

  useEffect(() => {
    let live = true;
    setDetails(null);
    setError("");
    api
      .get<CommitDetails>(`/api/revisions/commit?rev=${encodeURIComponent(rev)}`)
      .then((res) => {
        if (!live) return;
        setDetails(res);
        // A one-file commit (the usual agent turn) opens straight to its diff.
        setOpen(new Set(res.files.length === 1 ? [res.files[0].path] : []));
      })
      .catch((err) => live && setError(err instanceof ApiError ? err.detail : t("commit.err")));
    return () => {
      live = false;
    };
  }, [rev, t]);

  if (error) return <p className="error-text revisions-commit">{error}</p>;
  if (!details) {
    return (
      <div className="revisions-commit row muted" style={{ gap: 8, fontSize: 12 }}>
        <div className="spinner" /> {t("commit.loading")}
      </div>
    );
  }

  const s = details.stats;
  const toggle = (path: string) =>
    setOpen((cur) => {
      const next = new Set(cur);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  const copyId = async () => {
    try {
      await navigator.clipboard.writeText(details.commit);
      toast.push(t("toast.copied"));
    } catch {
      toast.push(t("err.copy"), "error");
    }
  };

  return (
    <div className="revisions-commit" data-testid="revisions-commit-detail">
      <dl className="revisions-meta">
        <div>
          <dt>{t("commit.id")}</dt>
          <dd className="row" style={{ gap: 6 }}>
            <code>{details.commit}</code>
            <button className="btn xs ghost" onClick={() => void copyId()} title={t("commit.copyId")} aria-label={t("commit.copyId")}>
              <Copy size={12} strokeWidth={1.75} />
            </button>
          </dd>
        </div>
        <div>
          <dt>{t("commit.author")}</dt>
          <dd>{details.author}</dd>
        </div>
        <div>
          <dt>{t("commit.date")}</dt>
          <dd>{new Date(details.ts).toLocaleString()}</dd>
        </div>
      </dl>
      {details.message ? <pre className="revisions-message">{details.message}</pre> : null}
      {details.parents.length > 1 ? <p className="admin-form-note">{t("commit.mergeNote")}</p> : null}
      {details.parents.length === 0 ? <p className="admin-form-note">{t("commit.rootNote")}</p> : null}

      <div className="revisions-summary" data-testid="revisions-commit-summary">
        <strong>{t(s.files === 1 ? "commit.summary.files" : "commit.summary.files.plural", { n: String(s.files) })}</strong>
        <span className="revisions-plus">+{s.additions}</span>
        <span className="revisions-minus">−{s.deletions}</span>
        {s.added ? <span className="badge revisions-badge--added">{t("commit.summary.added", { n: String(s.added) })}</span> : null}
        {s.modified ? (
          <span className="badge revisions-badge--modified">{t("commit.summary.modified", { n: String(s.modified) })}</span>
        ) : null}
        {s.deleted ? (
          <span className="badge revisions-badge--deleted">{t("commit.summary.deleted", { n: String(s.deleted) })}</span>
        ) : null}
      </div>

      {details.files.length === 0 ? (
        <p className="muted">{t("commit.noFiles")}</p>
      ) : (
        <ul className="revisions-files">
          {details.files.map((file) => {
            const isOpen = open.has(file.path);
            return (
              <li key={file.path} className="revisions-file">
                <button
                  className="revisions-file-row"
                  aria-expanded={isOpen}
                  aria-label={t("file.expand", { path: file.path })}
                  onClick={() => toggle(file.path)}
                >
                  {isOpen ? <ChevronDown size={14} strokeWidth={1.75} /> : <ChevronRight size={14} strokeWidth={1.75} />}
                  <StatusBadge status={file.status} />
                  <span className="revisions-file-path">{file.path}</span>
                  <span className="revisions-file-stats">
                    {file.binary ? (
                      <span className="badge">{t("file.binary")}</span>
                    ) : (
                      <>
                        <span className="revisions-plus">+{file.additions}</span>
                        <span className="revisions-minus">−{file.deletions}</span>
                        <ChangeBar additions={file.additions} deletions={file.deletions} />
                      </>
                    )}
                  </span>
                </button>
                {isOpen ? <FileDiff rev={details.commit} file={file} onViewFile={onViewFile} /> : null}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
