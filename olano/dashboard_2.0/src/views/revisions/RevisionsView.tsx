// Revisions — automatic home revision history (<home>/revisions.git).
// The commit log (optionally filtered to one file); select a commit to see
// which files it changed and each file's diff (CommitDetail); a read-only
// viewer for a file at a chosen commit; "Snapshot now"; and, for admins, the
// GitHub sync card (GitHubSync), which can also bring changes made on GitHub
// back into this box through a validated pull.

import { useCallback, useEffect, useState, type ReactNode } from "react";
import {
  Camera,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  ChevronsLeft,
  ChevronsRight,
  Copy,
  Download,
} from "lucide-react";
import { useSearchParams } from "react-router-dom";
import { ApiError, api } from "../../core/api";
import { useAuth } from "../../core/auth";
import { useT } from "../../core/i18n";
import { usePersistentState } from "../../core/usePersistentState";
import { useUrlUpdate, type UrlHistory } from "../../core/useUrlParam";
import { BrandWordmark } from "../../ui/BrandMark";
import { RefreshIcon } from "../../ui/icons";
import { useToast } from "../../ui/toast";
import { CommitDetail } from "./CommitDetail";
import { GitHubSyncCard } from "./GitHubSync";
import { STR } from "./strings";
import { rawUrl } from "./CommitDetail";
import { fmtBytes, relTime } from "./format";
import type { HeldBack, LogEntry, LogPage, RemoteStatus, RevisionFile } from "./types";

// Commits per page of the history list.
const PAGE_SIZE = 10;

function Pager({
  page,
  pages,
  total,
  onPage,
  t,
}: {
  page: number;
  pages: number;
  total: number;
  onPage: (page: number) => void;
  t: ReturnType<typeof useT>;
}) {
  if (total <= PAGE_SIZE) return null;
  const first = page * PAGE_SIZE + 1;
  const last = Math.min(total, (page + 1) * PAGE_SIZE);
  const button = (to: number, label: string, icon: ReactNode, disabled: boolean) => (
    <button
      className="btn sm revisions-pager-btn"
      onClick={() => onPage(to)}
      disabled={disabled}
      aria-label={label}
      title={label}
    >
      {icon}
    </button>
  );
  return (
    <nav className="revisions-pager" aria-label={t("log.title")}>
      <span className="revisions-pager-range">
        {t("log.range", { from: first.toLocaleString(), to: last.toLocaleString(), total: total.toLocaleString() })}
      </span>
      <div className="revisions-pager-buttons">
        {button(0, t("log.first"), <ChevronsLeft size={14} strokeWidth={2} />, page === 0)}
        {button(page - 1, t("log.prev"), <ChevronLeft size={14} strokeWidth={2} />, page === 0)}
        <span className="revisions-pager-page" aria-live="polite">
          {t("log.page", { page: String(page + 1), pages: String(pages) })}
        </span>
        {button(page + 1, t("log.next"), <ChevronRight size={14} strokeWidth={2} />, page >= pages - 1)}
        {button(pages - 1, t("log.last"), <ChevronsRight size={14} strokeWidth={2} />, page >= pages - 1)}
      </div>
    </nav>
  );
}


export function RevisionsView() {
  const t = useT(STR);
  const toast = useToast();
  const { user } = useAuth();
  const isAdmin = !!user?.is_admin;

  const [entries, setEntries] = useState<LogEntry[]>([]);
  const [repoBytes, setRepoBytes] = useState(0);
  const [held, setHeld] = useState<{ files: HeldBack[]; kb: number }>({ files: [], kb: 0 });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [path, setPath] = usePersistentState("revisions.path", "");
  const [selected, setSelected] = usePersistentState<string>("revisions.selected", "");
  // The open revision is the address (?path=<file>&rev=<commit>): a reload or a
  // copied link reopens it, opening one is a Back step and Back closes it. The
  // path box itself is search text and stays out of the URL.
  const [params] = useSearchParams();
  const updateUrl = useUrlUpdate();
  const urlPath = params.get("path");
  const urlRev = params.get("rev");
  const [viewer, setViewer] = useState<RevisionFile | null>(null);
  const [busy, setBusy] = useState(false);
  const [remote, setRemote] = useState<RemoteStatus | null>(null);
  const [total, setTotal] = useState(0);
  // The page belongs to the filter it was chosen under: a new path starts
  // again from the newest commits.
  const [paging, setPaging] = useState({ path, page: 0 });
  const page = paging.path === path ? paging.page : 0;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const goPage = (next: number) => setPaging({ path, page: Math.max(0, Math.min(next, pages - 1)) });

  const refresh = useCallback(() => {
    const params = new URLSearchParams({ limit: String(PAGE_SIZE), skip: String(page * PAGE_SIZE) });
    if (path) params.set("path", path);
    api
      .get<LogPage>(`/api/revisions/log?${params.toString()}`)
      .then((res) => {
        setEntries(res.entries);
        setTotal(res.total ?? res.entries.length);
        setRepoBytes(res.repo_bytes);
        setHeld({ files: res.held_back ?? [], kb: res.max_file_kb ?? 0 });
        setError("");
      })
      .catch((err) => setError(err instanceof ApiError ? err.detail : t("err.load")))
      .finally(() => setLoading(false));
  }, [path, page, t]);

  const refreshRemote = useCallback(() => {
    if (!isAdmin) return;
    api
      .get<RemoteStatus>("/api/revisions/remote")
      .then(setRemote)
      .catch(() => setRemote(null));
  }, [isAdmin]);

  useEffect(refresh, [refresh]);
  useEffect(refreshRemote, [refreshRemote]);

  const [viewerLoading, setViewerLoading] = useState(false);

  const viewFile = (filePath: string, rev: string) => {
    if (!filePath) return;
    updateUrl({ path: filePath, rev }, { history: "push" });
  };
  const closeViewer = (history: UrlHistory = "push") => updateUrl({ path: null, rev: null }, { history });

  // Whatever the address names is what the viewer shows: on a click, a
  // reload, a pasted link and Back/Forward alike.
  useEffect(() => {
    if (!urlPath || !urlRev) {
      setViewer(null);
      return;
    }
    // The log beside the viewer is that file's history.
    setPath(urlPath);
    let cancelled = false;
    setViewerLoading(true);
    api
      .get<RevisionFile>(
        `/api/revisions/file?path=${encodeURIComponent(urlPath)}&rev=${encodeURIComponent(urlRev)}`,
      )
      .then((res) => {
        if (!cancelled) setViewer(res);
      })
      .catch(() => {
        if (!cancelled) toast.push(t("err.file"), "error");
      })
      .finally(() => {
        if (!cancelled) setViewerLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [urlPath, urlRev, setPath, toast, t]);

  const copyContent = async () => {
    if (!viewer || viewer.binary) return;
    try {
      await navigator.clipboard.writeText(viewer.content);
      toast.push(t("toast.copied"));
    } catch {
      toast.push(t("err.copy"), "error");
    }
  };

  const snapshot = async () => {
    setBusy(true);
    try {
      const res = await api.post<{ committed: boolean }>("/api/revisions/snapshot", {
        reason: "manual snapshot (dashboard)",
      });
      toast.push(t(res.committed ? "toast.snapshot.done" : "toast.snapshot.clean"));
      // A new commit is the newest one: show the page it lands on.
      if (page !== 0) goPage(0);
      else refresh();
      refreshRemote();
    } catch (err) {
      toast.push(err instanceof ApiError ? err.detail : t("err.snapshot"), "error");
    } finally {
      setBusy(false);
    }
  };

  const unpushed = new Set(remote?.connected ? remote.unpushed : []);

  return (
    <>
      <div className="topbar">
        <BrandWordmark />
        <div className="row">
          <button
            className="btn"
            onClick={() => {
              refresh();
              refreshRemote();
            }}
            title={t("btn.refresh")}
          >
            <RefreshIcon size={14} strokeWidth={1.75} /> {t("btn.refresh")}
          </button>
          <button className="btn primary" onClick={() => void snapshot()} disabled={busy}>
            <Camera size={14} strokeWidth={1.75} /> {t("btn.snapshot")}
          </button>
        </div>
      </div>
      <div className="page" style={{ maxWidth: 1100 }}>
        <h1>{t("title")}</h1>
        <p className="muted">{t("intro")}</p>

        {isAdmin ? (
          <GitHubSyncCard
            status={remote}
            onStatus={setRemote}
            onHistoryChanged={() => {
              refresh();
              refreshRemote();
            }}
          />
        ) : null}

        <div className="row" style={{ gap: 8, marginBottom: 12, flexWrap: "wrap" }}>
          <input
            className="input"
            style={{ flex: "1 1 320px" }}
            placeholder={t("filter.path.placeholder")}
            value={path}
            onChange={(e) => {
              setPath(e.target.value);
              // A new search leaves the open revision behind (not a Back step).
              if (urlRev) closeViewer("replace");
            }}
          />
          <span className="muted" style={{ fontSize: 12, alignSelf: "center" }}>
            {t(total === 1 ? "stat.commits" : "stat.commits.plural", { n: total.toLocaleString() })}
            {" · "}
            {t("stat.repo", { size: fmtBytes(repoBytes) })}
          </span>
        </div>

        {error ? <p style={{ color: "var(--danger)" }}>{error}</p> : null}
        {held.files.length ? (
          <div className="revisions-warn-block" data-testid="revisions-held-back">
            <div>
              {t("warn.heldBack", { n: String(held.files.length) })}
              <ul className="revisions-pathlist">
                {held.files.map((f) => (
                  <li key={f.path}>
                    <code>{f.path}</code>: {t(`held.${f.reason}`, { kb: String(held.kb) })}
                  </li>
                ))}
              </ul>
            </div>
          </div>
        ) : null}

        {viewer ? (
          <section className="card" style={{ marginBottom: 14 }}>
            <div className="row" style={{ justifyContent: "space-between", flexWrap: "wrap", gap: 8 }}>
              <div style={{ minWidth: 0 }}>
                <strong style={{ overflowWrap: "anywhere" }}>
                  {t("viewer.title", { path: viewer.path, rev: viewer.rev.slice(0, 10) })}
                </strong>
                <div className="muted" style={{ fontSize: 12 }}>
                  {t("viewer.hint")}
                </div>
              </div>
              <div className="row" style={{ gap: 6 }}>
                <a className="btn sm" href={rawUrl(viewer.path, viewer.rev)} download>
                  <Download size={13} strokeWidth={1.75} /> {t("btn.download")}
                </a>
                {!viewer.binary ? (
                  <button className="btn sm" onClick={() => void copyContent()}>
                    <Copy size={13} strokeWidth={1.75} /> {t("btn.copy")}
                  </button>
                ) : null}
                <button className="btn sm" onClick={() => closeViewer()}>
                  {t("btn.close")}
                </button>
              </div>
            </div>
            {viewer.binary ? (
              <p className="muted revisions-small">{t("viewer.binary", { size: fmtBytes(viewer.size) })}</p>
            ) : (
              <pre className="revisions-viewer">{viewer.content}</pre>
            )}
          </section>
        ) : null}

        <section className="card" aria-label={t("log.title")}>
          <div className="revisions-log-head">
            <h3 style={{ margin: 0 }}>{t("log.title")}</h3>
            <Pager page={page} pages={pages} total={total} onPage={goPage} t={t} />
          </div>
          {entries.length > 0 ? <p className="muted revisions-small">{t("log.hint")}</p> : null}
          {loading ? (
            <div className="spinner" />
          ) : entries.length === 0 ? (
            <p className="muted">{t("log.empty")}</p>
          ) : (
            <ul className="revisions-log">
              {entries.map((entry) => {
                const isOpen = selected === entry.commit;
                return (
                  <li key={entry.commit} className={`revisions-item${isOpen ? " open" : ""}`}>
                    <div className="revisions-entry">
                      <button
                        className="revisions-entry-main"
                        aria-expanded={isOpen}
                        onClick={() => setSelected(isOpen ? "" : entry.commit)}
                      >
                        {isOpen ? (
                          <ChevronDown size={14} strokeWidth={1.75} />
                        ) : (
                          <ChevronRight size={14} strokeWidth={1.75} />
                        )}
                        <code className="muted" style={{ fontSize: 11 }}>
                          {entry.commit.slice(0, 10)}
                        </code>
                        <span className="badge">{entry.author}</span>
                        {entry.parents?.length > 1 ? <span className="badge">{t("log.merge")}</span> : null}
                        {unpushed.has(entry.commit) ? (
                          <span className="badge revisions-badge--warn" title={t("log.unpushed.title")}>
                            {t("log.unpushed")}
                          </span>
                        ) : null}
                        <span className="revisions-entry-message">{entry.message}</span>
                      </button>
                      <div className="row" style={{ gap: 8, flex: "0 0 auto" }}>
                        <span
                          className="muted"
                          style={{ fontSize: 11 }}
                          title={new Date(entry.ts).toLocaleString()}
                        >
                          {relTime(t, entry.ts)}
                        </span>
                        {path ? (
                          <button
                            className="btn xs"
                            disabled={viewerLoading}
                            onClick={() => viewFile(path, entry.commit)}
                          >
                            {t("btn.view")}
                          </button>
                        ) : null}
                      </div>
                    </div>
                    {isOpen ? (
                      <CommitDetail
                        rev={entry.commit}
                        onViewFile={(p, rev) => {
                          viewFile(p, rev);
                          window.scrollTo({ top: 0, behavior: "smooth" });
                        }}
                      />
                    ) : null}
                  </li>
                );
              })}
            </ul>
          )}
          {!loading && entries.length > 0 ? (
            <div className="revisions-log-foot">
              <Pager page={page} pages={pages} total={total} onPage={goPage} t={t} />
            </div>
          ) : null}
        </section>
      </div>
    </>
  );
}
