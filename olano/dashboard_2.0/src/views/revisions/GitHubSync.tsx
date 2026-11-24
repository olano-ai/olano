// GitHub sync for the revision history (olano/revisions/remote.py): connect an
// empty repository and push the history to it, push and pull by hand or push
// automatically (daily / after every commit), and resolve a pull whose changes
// collide with this box's, file by file. Admin-only, like its API.

import { useEffect, useRef, useState, type ReactNode } from "react";
import {
  ArrowDownToLine,
  ArrowUpFromLine,
  Check,
  CircleAlert,
  CircleCheck,
  Clock,
  CloudDownload,
  CloudUpload,
  ExternalLink,
  GitBranch,
  GitMerge,
  KeyRound,
  Link2,
  RefreshCw,
  TriangleAlert,
  Unplug,
  X,
} from "lucide-react";
import { Link } from "react-router-dom";
import { ApiError, api } from "../../core/api";
import { useT } from "../../core/i18n";
import { useConfirm } from "../../ui/confirm";
import { useDialogA11y } from "../../ui/dialogA11y";
import { useToast } from "../../ui/toast";
import { DiffLayoutToggle, useDiffViewType } from "./CommitDetail";
import { DiffView } from "./DiffView";
import { fmtBytes, relTime } from "./format";
import { STR } from "./strings";
import { SyncProgress } from "./SyncProgress";
import type {
  AuthMode,
  MergeFileDetail,
  MergeSummary,
  PullResult,
  RemoteOperation,
  RemoteStatus,
  SyncMode,
} from "./types";

type T = ReturnType<typeof useT>;
const SYNC_MODES: SyncMode[] = ["manual", "daily", "on_commit"];
const MARKER = /^(?:<{7}|>{7})(?: |$)/m;

function errText(err: unknown, t: T): string {
  return err instanceof ApiError ? err.detail : t("gh.err.generic");
}

function when(iso: string | undefined, t: T): string {
  if (!iso) return t("gh.never");
  const d = new Date(iso);
  return Number.isFinite(d.getTime()) ? d.toLocaleString() : iso;
}

function Dialog({
  title,
  onClose,
  children,
  footer,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
  footer: ReactNode;
}) {
  const t = useT(STR);
  const panelRef = useRef<HTMLDivElement>(null);
  useDialogA11y(panelRef, onClose);
  return (
    <div className="modal-overlay" onClick={onClose}>
      <div
        className="card modal-card"
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onClick={(e) => e.stopPropagation()}
        style={{ width: 600, maxWidth: "calc(100vw - 40px)", display: "flex", flexDirection: "column" }}
      >
        <div className="modal-head">
          <strong style={{ fontSize: 16 }}>{title}</strong>
          <button className="btn" onClick={onClose} aria-label={t("btn.close")} style={{ padding: "4px 9px", flexShrink: 0 }}>
            <X size={15} strokeWidth={1.75} />
          </button>
        </div>
        <div className="modal-body">{children}</div>
        <div className="modal-foot">{footer}</div>
      </div>
    </div>
  );
}

function Field({ label, help, children }: { label: string; help: ReactNode; children: ReactNode }) {
  return (
    <div className="field revisions-field">
      <label>{label}</label>
      {children}
      <div className="revisions-help">{help}</div>
    </div>
  );
}

// ── connect ────────────────────────────────────────────────────────────────

function ConnectDialog({
  status,
  running,
  onStart,
  onClose,
  onConnected,
}: {
  status: RemoteStatus;
  /** The connection under way, as the card's poll last saw it. */
  running: RemoteOperation | null;
  onStart: (on: boolean) => void;
  onClose: () => void;
  onConnected: (next: RemoteStatus) => void;
}) {
  const t = useT(STR);
  const toast = useToast();
  const conn = status.connection;
  const [repo, setRepo] = useState("");
  // A deployment that already signed in to GitHub (Connections page) uses
  // that account unless the person picks a token instead.
  const [auth, setAuth] = useState<AuthMode>(conn.available ? "connection" : "token");
  const [token, setToken] = useState("");
  const [branch, setBranch] = useState("main");
  const [sync, setSync] = useState<SyncMode>("manual");
  const [allowPublic, setAllowPublic] = useState(false);
  const [askPublic, setAskPublic] = useState(false);
  const [busy, setBusy] = useState(false);
  const [startedAt, setStartedAt] = useState("");
  const [error, setError] = useState("");

  const submit = async () => {
    if (!repo.trim()) return setError(t("connect.err.repo"));
    if (auth === "token" && !token.trim()) return setError(t("connect.err.token"));
    setBusy(true);
    setStartedAt(new Date().toISOString());
    onStart(true);
    setError("");
    try {
      const res = await api.post<{ remote: RemoteStatus }>("/api/revisions/remote/connect", {
        repo: repo.trim(),
        auth,
        token: auth === "token" ? token.trim() : "",
        branch: branch.trim() || "main",
        sync,
        allow_public: allowPublic,
      });
      toast.push(t("gh.toast.connected"));
      onConnected(res.remote);
    } catch (err) {
      if (err instanceof ApiError && err.code === "public_repo") setAskPublic(true);
      setError(errText(err, t));
    } finally {
      setBusy(false);
      onStart(false);
    }
  };

  // Until the first poll answers, draw the plan with nothing started yet.
  const live: RemoteOperation | null = busy
    ? running?.op === "connect"
      ? running
      : { op: "connect", by: "", since: startedAt, progress: { steps: CONNECT_STEPS, step: null, percent: null } }
    : null;

  const authOptions: AuthMode[] = conn.available ? ["connection", "token"] : ["token", "connection"];

  return (
    <Dialog
      title={t("connect.title")}
      onClose={onClose}
      footer={
        busy ? (
          <>
            <span className="muted gh-foot-note">{t("connect.background")}</span>
            <button className="btn" onClick={onClose}>
              {t("connect.hide")}
            </button>
          </>
        ) : (
          <>
            <button className="btn" onClick={onClose}>
              {t("connect.cancel")}
            </button>
            <button className="btn primary" onClick={() => void submit()} data-testid="revisions-connect-submit">
              <CloudUpload size={14} strokeWidth={1.75} /> {t("connect.submit")}
            </button>
          </>
        )
      }
    >
      {live ? <SyncProgress op={live} testId="revisions-connect-progress" /> : null}
      <p className="muted" style={{ marginTop: live ? 14 : 0, fontSize: 13 }}>
        {t("connect.lead")}
      </p>
      <fieldset className="gh-fieldset" disabled={busy}>
        <Field
          label={t("connect.repo")}
          help={
            <>
              {t("connect.repo.help")}{" "}
              <a href="https://github.com/new" target="_blank" rel="noreferrer">
                {t("connect.newRepo")} <ExternalLink size={11} strokeWidth={1.75} />
              </a>
            </>
          }
        >
          <input
            className="input"
            value={repo}
            placeholder={t("connect.repo.placeholder")}
            onChange={(e) => setRepo(e.target.value)}
            autoFocus
            spellCheck={false}
            autoCapitalize="off"
            data-testid="revisions-connect-repo"
          />
        </Field>
        <Field
          label={t("connect.auth")}
          help={
            <>
              {t("connect.auth.help")}{" "}
              {conn.available ? (
                conn.account ? (
                  t("connect.auth.connectionReady", { account: conn.account })
                ) : (
                  t("connect.auth.connectionReadyNoName")
                )
              ) : (
                <>
                  {t("connect.auth.connectionMissing")} <Link to="/connections">{t("connect.auth.openConnections")}</Link>
                </>
              )}
            </>
          }
        >
          <select
            className="input"
            value={auth}
            onChange={(e) => setAuth(e.target.value as AuthMode)}
            data-testid="revisions-connect-auth"
          >
            {authOptions.map((mode) =>
              mode === "connection" ? (
                <option key={mode} value="connection" disabled={!conn.available}>
                  {conn.available && conn.account
                    ? t("connect.auth.connectionAs", { account: conn.account })
                    : t("connect.auth.connection")}
                </option>
              ) : (
                <option key={mode} value="token">
                  {t("connect.auth.token")}
                </option>
              ),
            )}
          </select>
        </Field>
        {auth === "token" ? (
          <Field
            label={t("connect.token")}
            help={
              <>
                {t("connect.token.help")}{" "}
                <a href="https://github.com/settings/personal-access-tokens/new" target="_blank" rel="noreferrer">
                  {t("connect.newToken")} <ExternalLink size={11} strokeWidth={1.75} />
                </a>
              </>
            }
          >
            <input
              className="input"
              type="password"
              value={token}
              placeholder={t("connect.token.placeholder")}
              onChange={(e) => setToken(e.target.value)}
              autoComplete="off"
              spellCheck={false}
            />
          </Field>
        ) : null}
        <Field label={t("connect.branch")} help={t("connect.branch.help")}>
          <input
            className="input"
            value={branch}
            onChange={(e) => setBranch(e.target.value)}
            spellCheck={false}
            autoCapitalize="off"
          />
        </Field>
        <Field label={t("gh.sync.label")} help={t("gh.sync.help")}>
          <select className="input" value={sync} onChange={(e) => setSync(e.target.value as SyncMode)}>
            {SYNC_MODES.map((m) => (
              <option key={m} value={m}>
                {t(`gh.sync.${m}`)}
              </option>
            ))}
          </select>
        </Field>
        {askPublic ? (
          <div className="field revisions-field">
            <label className="revisions-check">
              <input type="checkbox" checked={allowPublic} onChange={(e) => setAllowPublic(e.target.checked)} />{" "}
              {t("connect.public")}
            </label>
            <div className="revisions-help">{t("connect.public.help")}</div>
          </div>
        ) : null}
      </fieldset>
      <p className="admin-form-note">{t("connect.what")}</p>
      {error ? (
        <p className="error-text" role="alert">
          {error}
        </p>
      ) : null}
    </Dialog>
  );
}

// ── pull result ────────────────────────────────────────────────────────────

function PathList({ paths }: { paths: string[] }) {
  return (
    <ul className="revisions-pathlist">
      {paths.map((p) => (
        <li key={p}>
          <code>{p}</code>
        </li>
      ))}
    </ul>
  );
}

function PullSummary({ result, onDismiss }: { result: PullResult; onDismiss: () => void }) {
  const t = useT(STR);
  const toast = useToast();
  const [restarted, setRestarted] = useState<Set<string>>(new Set());
  const written = result.written ?? [];
  const deleted = result.deleted ?? [];
  const kept = result.kept ?? [];
  const ignored = result.ignored ?? [];
  const warnings = result.warnings ?? [];
  const agents = result.agents_changed ?? [];

  const restart = async (name: string) => {
    try {
      await api.post(`/api/agents/${encodeURIComponent(name)}/restart`);
      setRestarted((cur) => new Set(cur).add(name));
      toast.push(t("pull.restarted", { name }));
    } catch {
      toast.push(t("pull.restartErr", { name }), "error");
    }
  };

  return (
    <div className="revisions-panel" data-testid="revisions-pull-summary">
      <div className="row" style={{ justifyContent: "space-between", gap: 8 }}>
        <strong>{t("pull.title")}</strong>
        <button className="btn xs" onClick={onDismiss}>
          {t("pull.dismiss")}
        </button>
      </div>
      {result.kind ? <p className="muted revisions-small">{t(`pull.kind.${result.kind}`)}</p> : null}
      {written.length + deleted.length === 0 ? <p className="muted revisions-small">{t("pull.nothing")}</p> : null}
      {written.length ? (
        <>
          <h4>{t("pull.written")}</h4>
          <PathList paths={written} />
        </>
      ) : null}
      {deleted.length ? (
        <>
          <h4>{t("pull.deleted")}</h4>
          <PathList paths={deleted} />
        </>
      ) : null}
      {kept.length ? (
        <>
          <h4>{t("pull.kept")}</h4>
          <p className="revisions-help">{t("pull.kept.help")}</p>
          <PathList paths={kept} />
        </>
      ) : null}
      {ignored.length ? (
        <>
          <h4>{t("pull.ignored")}</h4>
          <p className="revisions-help">{t("pull.ignored.help")}</p>
          <PathList paths={ignored.map((i) => i.path)} />
        </>
      ) : null}
      {warnings.length ? (
        <>
          <h4>{t("pull.warnings")}</h4>
          <ul className="revisions-pathlist">
            {warnings.map((w) => (
              <li key={w}>{w}</li>
            ))}
          </ul>
        </>
      ) : null}
      {result.config_changed ? <p className="revisions-small">{t("pull.config")}</p> : null}
      {agents.length ? (
        <>
          <p className="revisions-small">{t("pull.agents")}</p>
          <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
            {agents.map((name) => (
              <button
                key={name}
                className="btn sm"
                disabled={restarted.has(name)}
                onClick={() => void restart(name)}
              >
                <RefreshCw size={13} strokeWidth={1.75} /> {t("pull.restart", { name })}
              </button>
            ))}
          </div>
        </>
      ) : null}
    </div>
  );
}

// ── merge resolver ─────────────────────────────────────────────────────────

type Decision = { action: "ours" | "theirs" | "delete" } | { action: "content"; content: string };

function ConflictFile({
  file,
  decision,
  onDecide,
}: {
  file: MergeSummary["files"][number];
  decision: Decision | undefined;
  onDecide: (d: Decision) => void;
}) {
  const t = useT(STR);
  const [open, setOpen] = useState(false);
  const [detail, setDetail] = useState<MergeFileDetail | null>(null);
  const [error, setError] = useState("");
  const [viewType, setViewType] = useDiffViewType();

  useEffect(() => {
    if (!open || detail) return;
    api
      .get<MergeFileDetail>(`/api/revisions/remote/merge/file?path=${encodeURIComponent(file.path)}`)
      .then(setDetail)
      .catch((err) => setError(errText(err, t)));
  }, [open, detail, file.path, t]);

  const oneSideDeleted = file.ours_deleted || file.theirs_deleted;
  const editing = decision?.action === "content";
  // Edited text still carrying markers is not a decision yet.
  const settled = !!decision && !(decision.action === "content" && MARKER.test(decision.content));
  const choose = (action: Decision["action"]) => {
    if (action === "content") onDecide({ action, content: detail?.merged ?? "" });
    else onDecide({ action });
  };

  return (
    <li className="revisions-conflict">
      <button className="revisions-file-row" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
        <GitMerge size={14} strokeWidth={1.75} />
        <span className="revisions-file-path">{file.path}</span>
        <span className={`badge ${settled ? "revisions-badge--added" : "revisions-badge--warn"}`}>
          {settled ? t("merge.resolved") : t("merge.unresolved")}
        </span>
      </button>
      {open ? (
        <div className="revisions-file-diff">
          {file.ours_deleted ? <p className="revisions-small">{t("merge.oursDeleted")}</p> : null}
          {file.theirs_deleted ? <p className="revisions-small">{t("merge.theirsDeleted")}</p> : null}
          {error ? <p className="error-text">{error}</p> : null}
          {!detail && !error ? (
            <div className="row muted" style={{ gap: 8, fontSize: 12 }}>
              <div className="spinner" /> {t("merge.loading")}
            </div>
          ) : null}
          {detail ? (
            <>
              {detail.binary ? (
                <p className="revisions-small">
                  {t("merge.binary", {
                    ours: detail.sizes.ours === null ? "-" : fmtBytes(detail.sizes.ours),
                    theirs: detail.sizes.theirs === null ? "-" : fmtBytes(detail.sizes.theirs),
                  })}
                </p>
              ) : (
                <>
                  <div className="revisions-diff-toolbar">
                    <span className="muted revisions-small">{t("merge.diffTitle")}</span>
                    <DiffLayoutToggle value={viewType} onChange={setViewType} />
                  </div>
                  <DiffView
                    patch={detail.patch}
                    viewType={viewType}
                    emptyText={t("diff.empty")}
                    labels={[t("merge.side.ours"), t("merge.side.theirs")]}
                  />
                </>
              )}
              <div className="revisions-choices" role="radiogroup">
                {!file.ours_deleted ? (
                  <button
                    className={`btn sm${decision?.action === "ours" ? " primary" : ""}`}
                    role="radio"
                    aria-checked={decision?.action === "ours"}
                    onClick={() => choose("ours")}
                  >
                    {t("merge.ours")}
                  </button>
                ) : null}
                {!file.theirs_deleted ? (
                  <button
                    className={`btn sm${decision?.action === "theirs" ? " primary" : ""}`}
                    role="radio"
                    aria-checked={decision?.action === "theirs"}
                    onClick={() => choose("theirs")}
                  >
                    {t("merge.theirs")}
                  </button>
                ) : null}
                {detail.binary && !oneSideDeleted ? null : oneSideDeleted ? (
                  <button
                    className={`btn sm${decision?.action === "delete" ? " primary" : ""}`}
                    role="radio"
                    aria-checked={decision?.action === "delete"}
                    onClick={() => choose("delete")}
                  >
                    {t("merge.delete")}
                  </button>
                ) : (
                  <button
                    className={`btn sm${editing ? " primary" : ""}`}
                    role="radio"
                    aria-checked={editing}
                    onClick={() => choose("content")}
                  >
                    {t("merge.edit")}
                  </button>
                )}
              </div>
              {decision?.action === "content" ? (
                <Field label={t("merge.editor")} help={t("merge.editor.help")}>
                  <textarea
                    className="input revisions-merge-editor"
                    value={decision.content}
                    onChange={(e) => onDecide({ action: "content", content: e.target.value })}
                    spellCheck={false}
                    rows={14}
                  />
                  {MARKER.test(decision.content) ? (
                    <span className="revisions-warn">
                      <TriangleAlert size={12} strokeWidth={1.75} /> {t("merge.markersLeft")}
                    </span>
                  ) : null}
                </Field>
              ) : null}
            </>
          ) : null}
        </div>
      ) : null}
    </li>
  );
}

function MergeResolver({
  merge,
  onFinished,
  onStatus,
}: {
  merge: MergeSummary;
  onFinished: (result: PullResult | null) => void;
  onStatus: (s: RemoteStatus) => void;
}) {
  const t = useT(STR);
  const toast = useToast();
  const confirm = useConfirm();
  const [decisions, setDecisions] = useState<Record<string, Decision>>({});
  const [busy, setBusy] = useState(false);

  const allDecided = merge.files.every((f) => decisions[f.path]);
  const markersLeft = Object.values(decisions).some((d) => d.action === "content" && MARKER.test(d.content));

  const complete = async () => {
    setBusy(true);
    try {
      const res = await api.post<{ result: PullResult; remote: RemoteStatus }>(
        "/api/revisions/remote/merge/resolve",
        { resolutions: decisions },
      );
      onStatus(res.remote);
      if (res.result.status === "conflicts") {
        setDecisions({});
        toast.push(t("merge.toast.again"), "error");
      } else {
        toast.push(t("merge.toast.done"));
        onFinished(res.result);
      }
    } catch (err) {
      toast.push(errText(err, t), "error");
    } finally {
      setBusy(false);
    }
  };

  const abort = async () => {
    const ok = await confirm({
      title: t("merge.confirm.abort.title"),
      message: t("merge.confirm.abort"),
      confirmLabel: t("merge.abort"),
      danger: true,
    });
    if (!ok) return;
    setBusy(true);
    try {
      onStatus(await api.post<RemoteStatus>("/api/revisions/remote/merge/abort"));
      toast.push(t("merge.toast.aborted"));
      onFinished(null);
    } catch (err) {
      toast.push(errText(err, t), "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="revisions-panel revisions-panel--merge" data-testid="revisions-merge">
      <strong className="row" style={{ gap: 6 }}>
        <GitMerge size={15} strokeWidth={1.75} /> {t("merge.title")}
      </strong>
      <p className="revisions-small">{t("merge.lead")}</p>
      {merge.created_at ? (
        <p className="muted revisions-small">{t("merge.started", { when: when(merge.created_at, t) })}</p>
      ) : null}
      <ul className="revisions-files">
        {merge.files.map((file) => (
          <ConflictFile
            key={`${merge.theirs}-${file.path}`}
            file={file}
            decision={decisions[file.path]}
            onDecide={(d) => setDecisions((cur) => ({ ...cur, [file.path]: d }))}
          />
        ))}
      </ul>
      <div className="row revisions-actions">
        <button className="btn danger sm" onClick={() => void abort()} disabled={busy}>
          {t("merge.abort")}
        </button>
        <button
          className="btn primary sm"
          onClick={() => void complete()}
          disabled={busy || !allDecided || markersLeft}
        >
          {busy ? t("gh.working") : t("merge.complete")}
        </button>
      </div>
    </div>
  );
}

// ── the card ───────────────────────────────────────────────────────────────

const CONNECT_STEPS = ["check", "snapshot", "upload", "save"];
const PLAN: Record<string, string[]> = {
  push: ["upload"],
  pull: ["snapshot", "download", "apply"],
  fetch: ["download"],
};

type Tone = "ok" | "info" | "warn";

function Tile({ label, iso, t }: { label: string; iso: string | undefined; t: T }) {
  return (
    <div className="gh-tile">
      <dt>{label}</dt>
      <dd title={iso ? when(iso, t) : undefined}>{iso ? relTime(t, iso) : t("gh.never")}</dd>
    </div>
  );
}

export function GitHubSyncCard({
  status,
  onStatus,
  onHistoryChanged,
}: {
  status: RemoteStatus | null;
  onStatus: (s: RemoteStatus) => void;
  onHistoryChanged: () => void;
}) {
  const t = useT(STR);
  const toast = useToast();
  const confirm = useConfirm();
  const [connecting, setConnecting] = useState(false);
  const [connectRunning, setConnectRunning] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [busySince, setBusySince] = useState("");
  const [pull, setPull] = useState<PullResult | null>(null);

  // While anything runs (this person's push, another admin's pull, the
  // automatic push, a connection), ask for the status every second: it
  // carries the operation's progress, and the buttons come back the moment
  // the slot frees up.
  const running = busy !== null || connectRunning || !!status?.busy;
  useEffect(() => {
    if (!running) return;
    const tick = () =>
      api
        .get<RemoteStatus>("/api/revisions/remote")
        .then(onStatus)
        .catch(() => undefined);
    const id = window.setInterval(tick, 1000);
    return () => window.clearInterval(id);
  }, [running, onStatus]);

  if (!status) return null;

  const run = async <R,>(op: string, fn: () => Promise<R>): Promise<R | undefined> => {
    setBusy(op);
    setBusySince(new Date().toISOString());
    try {
      return await fn();
    } finally {
      setBusy(null);
    }
  };

  const push = async (force = false): Promise<void> => {
    try {
      const res = await run("push", () =>
        api.post<{ result: { pushed: boolean }; remote: RemoteStatus }>("/api/revisions/remote/push", { force }),
      );
      if (!res) return;
      onStatus(res.remote);
      onHistoryChanged();
      toast.push(t(res.result.pushed ? "gh.toast.pushed" : "gh.toast.nothing"));
    } catch (err) {
      if (err instanceof ApiError && err.code === "rejected" && !force) {
        const ok = await confirm({
          title: t("gh.confirm.force.title"),
          message: t("gh.confirm.force"),
          confirmLabel: t("gh.confirm.force.ok"),
          danger: true,
        });
        if (ok) return push(true);
        void reload();
        return;
      }
      toast.push(errText(err, t), "error");
      void reload();
    }
  };

  const doPull = async () => {
    try {
      const res = await run("pull", () =>
        api.post<{ result: PullResult; remote: RemoteStatus }>("/api/revisions/remote/pull"),
      );
      if (!res) return;
      onStatus(res.remote);
      onHistoryChanged();
      if (res.result.status === "conflicts") toast.push(t("gh.toast.conflicts"), "error");
      else if (res.result.status === "up_to_date") toast.push(t("gh.toast.upToDate"));
      else {
        setPull(res.result);
        toast.push(t("gh.toast.pulled"));
      }
    } catch (err) {
      toast.push(errText(err, t), "error");
      void reload();
    }
  };

  const reload = async (fetch = false) => {
    try {
      onStatus(await api.get<RemoteStatus>(`/api/revisions/remote${fetch ? "?fetch=true" : ""}`));
      if (fetch) toast.push(t("gh.toast.checked"));
    } catch (err) {
      toast.push(errText(err, t), "error");
    }
  };

  const setSync = async (sync: SyncMode) => {
    if (sync === status.sync) return;
    try {
      const next = await run("sync", () => api.put<RemoteStatus>("/api/revisions/remote", { sync }));
      if (next) onStatus(next);
      toast.push(t("gh.toast.saved"));
    } catch (err) {
      toast.push(errText(err, t), "error");
    }
  };

  const disconnect = async () => {
    const ok = await confirm({
      title: t("gh.confirm.disconnect.title"),
      message: t("gh.confirm.disconnect", { repo: status.repo }),
      confirmLabel: t("gh.disconnect"),
      danger: true,
    });
    if (!ok) return;
    try {
      const next = await run("disconnect", () => api.del<RemoteStatus>("/api/revisions/remote"));
      if (next) onStatus(next);
      setPull(null);
      toast.push(t("gh.toast.disconnected"));
    } catch (err) {
      toast.push(errText(err, t), "error");
    }
  };

  // What to draw for the operation under way: the server's record once a
  // poll has seen it, this page's own plan for the second before that.
  const live: RemoteOperation | null =
    status.busy ??
    (busy && PLAN[busy]
      ? { op: busy, by: "", since: busySince, progress: { steps: PLAN[busy], step: null, percent: null } }
      : null);
  const someoneElse = !!status.busy && busy === null && !connectRunning;

  if (!status.connected) {
    const conn = status.connection;
    return (
      <section className="card revisions-gh gh-empty" aria-label={t("gh.title")} data-testid="revisions-github">
        <div className="gh-empty-icon" aria-hidden>
          <CloudUpload size={22} strokeWidth={1.75} />
        </div>
        <div className="gh-empty-body">
          <div className="gh-eyebrow">{t("gh.title")}</div>
          <h3>{t("gh.empty.title")}</h3>
          <p className="muted">{t("gh.intro")}</p>
          <ul className="gh-benefits">
            <li>
              <Check size={13} strokeWidth={2.25} /> {t("gh.empty.b1")}
            </li>
            <li>
              <Check size={13} strokeWidth={2.25} /> {t("gh.empty.b2")}
            </li>
            <li>
              <Check size={13} strokeWidth={2.25} /> {t("gh.empty.b3")}
            </li>
          </ul>
          {conn.available ? (
            <p className="gh-ready">
              <CircleCheck size={14} strokeWidth={2} />{" "}
              {conn.account ? t("gh.empty.ready", { account: conn.account }) : t("gh.empty.readyNoName")}
            </p>
          ) : null}
          {live && live.op === "connect" && !connecting ? (
            <SyncProgress op={live} showBy={someoneElse} testId="revisions-github-progress" />
          ) : (
            <button className="btn primary" onClick={() => setConnecting(true)} disabled={!!status.busy}>
              <Github16 /> {t("gh.connect")}
            </button>
          )}
        </div>
        {connecting ? (
          <ConnectDialog
            status={status}
            running={status.busy}
            onStart={setConnectRunning}
            onClose={() => setConnecting(false)}
            onConnected={(next) => {
              setConnecting(false);
              onStatus(next);
              onHistoryChanged();
            }}
          />
        ) : null}
      </section>
    );
  }

  const st = status.state ?? {};
  const { ahead, behind } = status;
  const anyBusy = running;
  const merging = !!status.merge;

  let tone: Tone = "ok";
  let StateIcon = CircleCheck;
  let stateLine = t("gh.state.synced");
  let stateHint = st.last_fetch_at
    ? t("gh.state.syncedHint", { when: relTime(t, st.last_fetch_at) })
    : t("gh.state.behindHint");
  if (st.needs_pull) {
    tone = "warn";
    StateIcon = TriangleAlert;
    stateLine = t("gh.state.needsPull.short");
    stateHint = t("gh.state.needsPull");
  } else if (ahead && behind) {
    tone = "warn";
    StateIcon = GitMerge;
    stateLine = t("gh.state.diverged");
    stateHint = t("gh.state.behindHint");
  } else if (ahead) {
    tone = "info";
    StateIcon = CloudUpload;
    stateLine = t(ahead === 1 ? "gh.state.ahead" : "gh.state.ahead.plural", { n: String(ahead) });
    stateHint = status.sync === "manual" ? t("gh.state.aheadHint.manual") : t(`gh.state.aheadHint.${status.sync}`);
  } else if (behind) {
    tone = "info";
    StateIcon = CloudDownload;
    stateLine = t(behind === 1 ? "gh.state.behind" : "gh.state.behind.plural", { n: String(behind) });
    stateHint = t("gh.state.behindHint");
  }
  const pushFirst = !!ahead && !behind && !st.needs_pull;
  const pullFirst = !!behind || !!st.needs_pull;
  const authLabel =
    status.auth === "connection"
      ? status.connection.account
        ? t("gh.auth.chip.connectionAs", { account: status.connection.account })
        : t("gh.auth.chip.connection")
      : t("gh.auth.chip.token");

  return (
    <section className="card revisions-gh" aria-label={t("gh.title")} data-testid="revisions-github">
      <header className="gh-head">
        <div className="gh-head-main">
          <span className="gh-logo" aria-hidden>
            <Github16 size={20} />
          </span>
          <div style={{ minWidth: 0 }}>
            <div className="gh-eyebrow">{t("gh.title")}</div>
            <a className="gh-repo" href={status.web_url} target="_blank" rel="noreferrer" title={t("gh.openRepo")}>
              {status.repo} <ExternalLink size={13} strokeWidth={1.75} />
            </a>
            <div className="gh-chips">
              <span className="gh-chip" title={t("gh.chip.branch.title")}>
                <GitBranch size={12} strokeWidth={2} /> {status.branch}
              </span>
              <span className="gh-chip" title={t("gh.auth.using", { how: authLabel })}>
                {status.auth === "connection" ? (
                  <Link2 size={12} strokeWidth={2} />
                ) : (
                  <KeyRound size={12} strokeWidth={2} />
                )}{" "}
                {authLabel}
              </span>
              <span className="gh-chip" title={t("gh.sync.label")}>
                <Clock size={12} strokeWidth={2} /> {t(`gh.sync.chip.${status.sync}`)}
              </span>
            </div>
          </div>
        </div>
        <div className="gh-actions">
          <button
            className={`btn sm${pushFirst ? " primary" : ""}`}
            onClick={() => void push()}
            disabled={anyBusy || merging}
            title={t("gh.push.title")}
          >
            <ArrowUpFromLine size={13} strokeWidth={1.75} /> {t("gh.push")}
          </button>
          <button
            className={`btn sm${pullFirst ? " primary" : ""}`}
            onClick={() => void doPull()}
            disabled={anyBusy || merging}
            title={t("gh.pull.title")}
          >
            <ArrowDownToLine size={13} strokeWidth={1.75} /> {t("gh.pull")}
          </button>
          <button
            className="btn sm ghost"
            onClick={() => void run("fetch", () => reload(true))}
            disabled={anyBusy}
            title={t("gh.check.title")}
          >
            <RefreshCw size={13} strokeWidth={1.75} className={busy === "fetch" ? "gh-spin" : undefined} />{" "}
            {t("gh.check")}
          </button>
        </div>
      </header>

      {!status.enabled ? (
        <p className="revisions-warn-block">
          <TriangleAlert size={13} strokeWidth={1.75} /> {t("gh.disabled")}
        </p>
      ) : null}

      {live ? (
        <SyncProgress
          op={live}
          showBy={someoneElse}
          testId={someoneElse ? "revisions-github-busy" : "revisions-github-progress"}
        />
      ) : (
        <div className={`gh-status gh-status--${tone}`} data-testid="revisions-github-state">
          <span className="gh-status-icon" aria-hidden>
            <StateIcon size={18} strokeWidth={2} />
          </span>
          <div style={{ minWidth: 0 }}>
            <strong>{stateLine}</strong>
            <div className="gh-status-hint">{stateHint}</div>
          </div>
        </div>
      )}

      {st.last_error ? (
        <div className="gh-error" role="alert">
          <CircleAlert size={14} strokeWidth={2} />
          <div>
            <strong>{t("gh.lastError", { op: t(`gh.op.${st.last_error_op || "push"}`) })}</strong>
            {st.last_error_at ? <span className="muted"> · {relTime(t, st.last_error_at)}</span> : null}
            <div>{st.last_error}</div>
          </div>
        </div>
      ) : null}

      <dl className="gh-tiles">
        <Tile label={t("gh.last.push")} iso={st.last_push_at} t={t} />
        <Tile label={t("gh.last.pull")} iso={st.last_pull_at} t={t} />
        <Tile label={t("gh.last.fetch")} iso={st.last_fetch_at} t={t} />
        <Tile label={t("gh.connectedSince")} iso={st.connected_at} t={t} />
      </dl>

      <div className="gh-setting">
        <div className="gh-setting-text">
          <span className="gh-setting-label" id="revisions-sync-label">
            {t("gh.sync.label")}
          </span>
          <span className="gh-setting-note">{t(`gh.sync.note.${status.sync}`)}</span>
        </div>
        <div className="seg-control" role="radiogroup" aria-labelledby="revisions-sync-label">
          {SYNC_MODES.map((m) => (
            <button
              key={m}
              type="button"
              role="radio"
              aria-checked={status.sync === m}
              className={status.sync === m ? "active" : undefined}
              disabled={anyBusy}
              onClick={() => void setSync(m)}
            >
              {t(`gh.sync.short.${m}`)}
            </button>
          ))}
        </div>
      </div>
      <details className="gh-more">
        <summary>{t("gh.sync.more")}</summary>
        <p>{t("gh.sync.help")}</p>
      </details>

      {status.merge ? (
        <MergeResolver
          merge={status.merge}
          onStatus={onStatus}
          onFinished={(result) => {
            onHistoryChanged();
            if (result) setPull(result);
          }}
        />
      ) : null}
      {pull ? <PullSummary result={pull} onDismiss={() => setPull(null)} /> : null}

      <footer className="gh-foot">
        <span className="muted">{t("gh.foot.note")}</span>
        <button className="btn ghost sm gh-disconnect" onClick={() => void disconnect()} disabled={anyBusy}>
          <Unplug size={13} strokeWidth={1.75} /> {t("gh.disconnect")}
        </button>
      </footer>
    </section>
  );
}

// GitHub's mark (lucide 1.x dropped brand icons).
function Github16({ size = 14 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="currentColor" aria-hidden>
      <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z" />
    </svg>
  );
}
