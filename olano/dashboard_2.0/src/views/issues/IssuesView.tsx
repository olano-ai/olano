// Issues — GitHub's issues UX, deliberately: a list page (Open/Closed toggles
// in the list header, filter dropdowns on the right, rows with state icon +
// title + label chips + assignee/comment-count) and a detail page (title +
// state pill, conversation timeline with event lines, right sidebar for
// Assignee/Type/Labels, comment box with Comment / Close-with-comment).
// Selection lives in the URL (?issue=N) so links are shareable; live updates
// ride the shared SSE stream's issue_updated frames.

import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent, useMemo, } from "react";
import { useSearchParams } from "react-router-dom";
import {
  ArrowLeft,
  Bot,
  Check,
  CheckCircle2,
  CircleDot,
  Copy,
  MessageSquare,
  Shapes,
  Tag,
  User,
  UserRound,
  Link as LinkIcon,
} from "lucide-react";
import { ApiError, api } from "../../core/api";
import { hueVarFor } from "../../core/hues";
import { subscribeStream } from "../../core/agentStream";
import { useT } from "../../core/i18n";
import { usePersistentState } from "../../core/usePersistentState";
import { useUrlUpdate, type UrlHistory } from "../../core/useUrlParam";
import { useMediaQuery, MOBILE_QUERY } from "../../core/useMediaQuery";
import {
  DEFAULT_FILTERS,
  SORTS,
  apiQuery,
  filtersActive,
  filtersKey,
  hasFilterParams,
  isDefault,
  parseFilters,
  shareUrl,
  writeFilters,
  type IssueFilters,
} from "./filters";
import { BrandWordmark } from "../../ui/BrandMark";
import { ErrorState } from "../../ui/ErrorState";
import { Markdown } from "../../ui/Markdown";
import { PlusIcon, RefreshIcon } from "../../ui/icons";
import { useToast } from "../../ui/toast";
import { issueMarkdown } from "./copyText";
import { Field, Modal } from "./Modal";
import { STR } from "./strings";

interface Issue {
  id: number;
  title: string;
  body: string;
  author: string;
  assignee: string;
  state: "open" | "closed";
  type: string;
  labels: string[];
  created_at: string;
  updated_at: string;
  closed_at: string | null;
  comment_count?: number;
}

interface IssueEvent {
  id: number;
  ts: string;
  actor: string;
  kind: string;
  payload: Record<string, unknown>;
}

interface IssuesMeta {
  types: string[];
  states: string[];
  agents: string[];
  humans: string[];
  labels: string[];
  counts?: { open: number; closed: number };
}

const EMPTY_META: IssuesMeta = { types: [], states: [], agents: [], humans: [], labels: [] };

type T = (k: string, v?: Record<string, string>) => string;

function relTime(t: T, iso: string): string {
  const ms = Date.now() - Date.parse(iso);
  if (!Number.isFinite(ms) || ms < 60_000) return t("time.justNow");
  const mins = Math.floor(ms / 60_000);
  if (mins < 60) return t("time.mins", { n: String(mins) });
  const hrs = Math.floor(mins / 60);
  if (hrs < 48) return t("time.hrs", { n: String(hrs) });
  return t("time.days", { n: String(Math.floor(hrs / 24)) });
}

function Time({ t, iso }: { t: T; iso: string }) {
  return <span title={new Date(iso).toLocaleString()}>{relTime(t, iso)}</span>;
}

// Principal chip: ai-* renders with a bot glyph, human-* with a person glyph.
function Principal({ name, muted }: { name: string; muted?: boolean }) {
  if (!name) return null;
  const isAgent = name.startsWith("ai-");
  const bare = isAgent ? name.slice(3) : name.startsWith("human-") ? name.slice(6) : name;
  const Icon = isAgent ? Bot : User;
  return (
    <span
      className={muted ? "muted" : undefined}
      title={name}
      style={{ display: "inline-flex", alignItems: "center", gap: 4, whiteSpace: "nowrap" }}
    >
      <Icon size={12} strokeWidth={1.75} aria-hidden />
      {bare}
    </span>
  );
}

// GitHub-style colored label chip. We store no label colors, so the name picks a
// slot in the theme's channel-hue ramp (core/hues.ts) — the same ramp the
// platform badges use, which every theme defines for its own ground.
//
// It used to compute `hsl(<hash of the name> 75% 62%)` directly. A fixed
// lightness is theme-blind: on a pale page those chips measured 1.16:1 (kaki
// green) to 2.39:1 — text you cannot read, against a 4.5:1 floor. Going through
// the ramp puts the worst slot on the worst theme at 4.86:1.
//
// The three mixes mirror `.thread-channel` in globals.css, tuned one step
// further toward --text because a label chip is 11px where a badge is 12px.
function LabelChip({ name }: { name: string }) {
  const hue = hueVarFor(name);
  return (
    <span
      className="issue-label-chip"
      style={{
        background: `color-mix(in srgb, ${hue} 13%, transparent)`,
        borderColor: `color-mix(in srgb, ${hue} 34%, transparent)`,
        color: `color-mix(in srgb, ${hue} 66%, var(--text))`,
      }}
    >
      {name}
    </span>
  );
}

// Assignee + comment count — the row's right-hand column on desktop, part of
// the meta line on a phone. One component so both placements stay identical.
function IssueRowSide({ issue }: { issue: Issue }) {
  const comments = issue.comment_count ?? 0;
  if (!issue.assignee && comments === 0) return null;
  return (
    <span className="issues-row-side">
      {issue.assignee ? <Principal name={issue.assignee} muted /> : null}
      {comments > 0 ? (
        <span className="row muted" style={{ gap: 3, fontSize: 12 }}>
          <MessageSquare size={13} strokeWidth={1.75} aria-hidden />
          {comments}
        </span>
      ) : null}
    </span>
  );
}

function StateIcon({ state, size = 16 }: { state: "open" | "closed"; size?: number }) {
  return state === "open" ? (
    <CircleDot
      size={size}
      strokeWidth={2}
      style={{ color: "var(--success)", flex: "0 0 auto" }}
      aria-hidden
    />
  ) : (
    <CheckCircle2
      size={size}
      strokeWidth={2}
      style={{ color: "var(--info)", flex: "0 0 auto" }}
      aria-hidden
    />
  );
}

// Copies one card's Markdown source (what was typed, not the rendered text, so
// a list or a code block pastes intact). Confirms in place for a beat, like the
// copy under a chat reply; a refused clipboard (a non-secure origin, a denied
// permission) says so instead of failing silently.
function CopyTextButton({ text, label, onFail }: { text: string; label: string; onFail: () => void }) {
  const [copied, setCopied] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => () => void (timer.current && clearTimeout(timer.current)), []);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      if (timer.current) clearTimeout(timer.current);
      timer.current = setTimeout(() => setCopied(false), 1500);
    } catch {
      onFail();
    }
  };
  return (
    <button
      type="button"
      className={`issue-copy${copied ? " copied" : ""}`}
      onClick={() => void copy()}
      title={label}
      aria-label={label}
    >
      {copied ? <Check size={14} strokeWidth={1.75} aria-hidden /> : <Copy size={14} strokeWidth={1.75} aria-hidden />}
    </button>
  );
}

export function IssuesView() {
  const t = useT(STR);
  const toast = useToast();
  const [searchParams, setSearchParams] = useSearchParams();
  const isMobile = useMediaQuery(MOBILE_QUERY);

  const [issues, setIssues] = useState<Issue[]>([]);
  const [meta, setMeta] = useState<IssuesMeta>(EMPTY_META);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  // GitHub model: the detail is a page, not a pane. ?issue=N addresses it,
  // and the URL is the only copy: Back/Forward and an issue link clicked while
  // this view is already open (the bell) both change what is shown. Opening an
  // issue, or going back to the list, is a Back step; the filters live in the
  // same query string and only ever replace.
  const issueParam = Number(searchParams.get("issue"));
  const selectedId = Number.isInteger(issueParam) && issueParam > 0 ? issueParam : null;
  const [detail, setDetail] = useState<{ issue: Issue; events: IssueEvent[] } | null>(null);
  const [showNew, setShowNew] = useState(false);
  const updateUrl = useUrlUpdate();
  // Only the `issue` key: the filters must survive opening and closing an
  // issue (as must `?focus=`, which the settings palette writes on every route).
  const setSelectedId = useCallback(
    (id: number | null, history: UrlHistory = "push") =>
      updateUrl({ issue: id === null ? null : String(id) }, { history }),
    [updateUrl],
  );
  useEffect(() => setDetail(null), [selectedId]);

  // Filters — the URL is the source of truth (a shared link reopens the same
  // list), and one remembered set seeds a bare `/issues` landing so a left-nav
  // round-trip keeps the operator's working set. See filters.ts.
  const [remembered, setRemembered] = usePersistentState<IssueFilters>("issues.filters", DEFAULT_FILTERS);
  const filters = useMemo(() => parseFilters(searchParams), [searchParams]);
  const fKey = filtersKey(filters);
  const { q, state: listState, type, assignee, label, sort } = filters;

  // Landing with no filter in the address: write the remembered set into it,
  // once, so the address bar is truthful from the first paint. A notification's
  // `?issue=N` link lands here too — `issue` survives because `prev` is copied.
  const landedRef = useRef(false);
  useEffect(() => {
    if (landedRef.current) return;
    landedRef.current = true;
    if (hasFilterParams(searchParams) || isDefault(remembered)) return;
    setSearchParams((prev) => writeFilters(new URLSearchParams(prev), remembered), { replace: true });
  }, [searchParams, remembered, setSearchParams]);

  // A pasted or Back/Forward URL becomes the remembered set too.
  useEffect(() => {
    setRemembered(filters);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fKey]);

  const setFilters = useCallback(
    (patch: Partial<IssueFilters>) => {
      setSearchParams(
        (prev) => writeFilters(new URLSearchParams(prev), { ...parseFilters(prev), ...patch }),
        { replace: true },
      );
    },
    [setSearchParams],
  );

  // The search box is debounced into the URL. `qWritten` is what this box last
  // wrote, so the URL→draft echo below only applies to a query that arrived
  // from outside (a pasted link, Back) and never clobbers fast typing.
  const [qDraft, setQDraft] = useState(q);
  const qWritten = useRef(q);
  useEffect(() => {
    if (qDraft === qWritten.current) return;
    const timer = setTimeout(() => {
      qWritten.current = qDraft;
      setFilters({ q: qDraft });
    }, 300);
    return () => clearTimeout(timer);
  }, [qDraft, setFilters]);
  useEffect(() => {
    if (q !== qWritten.current) {
      qWritten.current = q;
      setQDraft(q);
    }
  }, [q]);
  const hasFilters = filtersActive(filters);

  const filtersRef = useRef(filters);
  filtersRef.current = filters;
  const selectedRef = useRef<number | null>(null);
  selectedRef.current = selectedId;

  const refreshList = useCallback(() => {
    api
      .get<{ issues: Issue[] }>(`/api/issues?${apiQuery(filtersRef.current)}`)
      .then((res) => {
        setIssues(res.issues);
        setError("");
      })
      .catch((err) => setError(err instanceof ApiError ? err.detail : t("err.load")))
      .finally(() => setLoading(false));
  }, [t]);

  const refreshDetail = useCallback(() => {
    const id = selectedRef.current;
    if (id === null) return;
    api
      .get<{ issue: Issue; events: IssueEvent[] }>(`/api/issues/${id}`)
      .then(setDetail)
      .catch((err) => {
        setDetail(null);
        // A deleted issue's address names nothing: replace it, so Back does not
        // return to it either.
        if (err instanceof ApiError && err.status === 404) setSelectedId(null, "replace");
        else toast.push(t("err.detail"), "error");
      });
  }, [t, toast, setSelectedId]);

  const loadMeta = useCallback(() => {
    api
      .get<IssuesMeta>("/api/issues/meta")
      .then(setMeta)
      .catch(() => undefined); // list still works without vocabulary
  }, []);

  useEffect(loadMeta, [loadMeta]);
  // Keyed on the filters' identity, not on searchParams: opening an issue
  // changes the URL but not the list.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(refreshList, [refreshList, fKey]);
  useEffect(refreshDetail, [refreshDetail, selectedId]);

  const copyLink = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(shareUrl(window.location.origin, filtersRef.current));
      toast.push(t("toast.linkCopied"));
    } catch {
      toast.push(t("err.copy"), "error");
    }
  }, [t, toast]);

  useEffect(
    () =>
      subscribeStream((frame) => {
        if ((frame as { type?: string }).type !== "issue_updated") return;
        refreshList();
        refreshDetail();
        loadMeta();
      }),
    [refreshList, refreshDetail, loadMeta],
  );

  const mutate = useCallback(
    async (path: string, body: Record<string, unknown>, okKey: string) => {
      try {
        await api.post(path, body);
        if (okKey) toast.push(t(okKey));
        refreshList();
        refreshDetail();
        loadMeta();
        return true;
      } catch (err) {
        toast.push(err instanceof ApiError ? err.detail : String(err), "error");
        return false;
      }
    },
    [t, toast, refreshList, refreshDetail, loadMeta],
  );

  const principals = [...meta.agents, ...meta.humans];

  return (
    <>
      <div className="topbar">
        <BrandWordmark />
        <div className="row">
          <button className="btn" onClick={() => refreshList()} title={t("btn.refresh")}>
            <RefreshIcon size={14} strokeWidth={1.75} /> {t("btn.refresh")}
          </button>
          <button className="btn primary" onClick={() => setShowNew(true)}>
            <PlusIcon size={14} strokeWidth={1.75} /> {t("btn.new")}
          </button>
        </div>
      </div>
      <div className="page" style={{ maxWidth: 1040 }}>
        {selectedId !== null ? (
          <IssuePage
            // The effect that clears `detail` runs after paint, so a detail
            // fetched for the previous id must not be drawn under the new one.
            detail={detail && detail.issue.id === selectedId ? detail : null}
            meta={meta}
            principals={principals}
            mutate={mutate}
            onBack={() => setSelectedId(null)}
            t={t}
          />
        ) : (
          <>
            <h1>{t("title")}</h1>
            <p className="muted">{t("intro")}</p>

            <div className="row" style={{ gap: 8, marginBottom: 12, flexWrap: "wrap" }}>
              <input
                className="input"
                style={{ flex: 1, minWidth: 180 }}
                placeholder={t(isMobile ? "search.placeholder.short" : "search.placeholder")}
                value={qDraft}
                onChange={(e) => setQDraft(e.target.value)}
              />
              {hasFilters ? (
                // One group, so the pair wraps under the search box together on
                // a phone instead of stranding the second button on its own line.
                <span className="row" style={{ gap: 4, flex: "0 0 auto" }}>
                  <button
                    className="btn ghost sm"
                    onClick={() => {
                      qWritten.current = "";
                      setQDraft("");
                      setFilters({ q: "", type: "", assignee: "", label: "" });
                    }}
                  >
                    {t("btn.clearFilters")}
                  </button>
                  {/* The address bar is hidden on a phone; this is how the
                      filtered list gets shared from there. */}
                  <button className="btn ghost sm" onClick={() => void copyLink()} title={t("btn.copyLink")}>
                    <LinkIcon size={13} strokeWidth={1.75} aria-hidden /> {t("btn.copyLink")}
                  </button>
                </span>
              ) : null}
            </div>

            {error ? (
              <ErrorState message={t("err.load")} detail={error} onRetry={refreshList} />
            ) : null}

            <div className="issues-listbox">
              <div className="issues-list-header">
                <div className="row" style={{ gap: 4 }}>
                  <button
                    className={`issues-state-toggle${listState === "open" ? " active" : ""}`}
                    onClick={() => setFilters({ state: "open" })}
                  >
                    <CircleDot size={14} strokeWidth={2} aria-hidden />
                    {t("state.openCount", { n: String(meta.counts?.open ?? 0) })}
                  </button>
                  <button
                    className={`issues-state-toggle${listState === "closed" ? " active" : ""}`}
                    onClick={() => setFilters({ state: "closed" })}
                  >
                    <CheckCircle2 size={14} strokeWidth={2} aria-hidden />
                    {t("state.closedCount", { n: String(meta.counts?.closed ?? 0) })}
                  </button>
                </div>
                {/* Compact selects: inline on desktop, a 2×2 grid on a phone
                    (see .issues-filters). Their first option doubles as the
                    label — "Type: Any" — since a select shows only its value. */}
                <div className="issues-filters">
                  <select
                    className="input sm"
                    value={type}
                    onChange={(e) => setFilters({ type: e.target.value })}
                    aria-label={t("filter.type")}
                  >
                    <option value="">{`${t("filter.type")}: ${t("filter.any")}`}</option>
                    {meta.types.map((ty) => (
                      <option key={ty} value={ty}>
                        {ty}
                      </option>
                    ))}
                  </select>
                  <select
                    className="input sm"
                    value={assignee}
                    onChange={(e) => setFilters({ assignee: e.target.value })}
                    aria-label={t("filter.assignee")}
                  >
                    <option value="">{`${t("filter.assignee")}: ${t("filter.any")}`}</option>
                    {principals.map((p) => (
                      <option key={p} value={p}>
                        {p}
                      </option>
                    ))}
                  </select>
                  <select
                    className="input sm"
                    value={label}
                    onChange={(e) => setFilters({ label: e.target.value })}
                    aria-label={t("filter.label")}
                  >
                    <option value="">{`${t("filter.label")}: ${t("filter.any")}`}</option>
                    {meta.labels.map((l) => (
                      <option key={l} value={l}>
                        {l}
                      </option>
                    ))}
                  </select>
                  <select
                    className="input sm"
                    value={sort}
                    onChange={(e) => setFilters({ sort: e.target.value as IssueFilters["sort"] })}
                    aria-label={t("sort.label")}
                  >
                    {SORTS.map((s) => (
                      <option key={s} value={s}>
                        {t(`sort.${s}`)}
                      </option>
                    ))}
                  </select>
                </div>
              </div>

              {loading ? (
                <div className="spinner" style={{ margin: 16 }} />
              ) : issues.length === 0 ? (
                <div style={{ textAlign: "center", padding: "28px 12px" }}>
                  <p className="muted">{t(hasFilters ? "list.empty" : "list.emptyFresh")}</p>
                  {!hasFilters ? (
                    <button className="btn primary sm" onClick={() => setShowNew(true)}>
                      <PlusIcon size={13} strokeWidth={1.75} /> {t("list.emptyCta")}
                    </button>
                  ) : null}
                </div>
              ) : (
                <ul className="issues-rows">
                  {issues.map((issue) => (
                    <li key={issue.id}>
                      <button className="issues-row" onClick={() => setSelectedId(issue.id)}>
                        <StateIcon state={issue.state} />
                        {/* Title on its own line, labels on their own row beneath it
                            (inline they ran into the title on a phone), then the
                            meta line. Assignee and comment count sit at the right
                            on desktop and join the meta line on a phone, where a
                            side column would squeeze the title. */}
                        <span className="issues-row-main">
                          <span className="issues-row-title">{issue.title}</span>
                          {issue.labels.length > 0 ? (
                            <span className="issue-labels">
                              {issue.labels.map((l) => (
                                <LabelChip key={l} name={l} />
                              ))}
                            </span>
                          ) : null}
                          <span className="issues-row-meta muted">
                            <span>
                              #{issue.id} · {t("row.opened")} <Time t={t} iso={issue.created_at} />{" "}
                              {t("row.by")} <Principal name={issue.author} muted />
                              {issue.type !== "task" ? <> · {issue.type}</> : null}
                            </span>
                            {isMobile ? <IssueRowSide issue={issue} /> : null}
                          </span>
                        </span>
                        {!isMobile ? <IssueRowSide issue={issue} /> : null}
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </>
        )}
      </div>

      {showNew ? (
        <NewIssueModal
          meta={meta}
          principals={principals}
          onClose={() => setShowNew(false)}
          onCreated={(id) => {
            setShowNew(false);
            toast.push(t("toast.created", { id: String(id) }));
            setSelectedId(id);
            refreshList();
          }}
        />
      ) : null}
    </>
  );
}

// ── detail page (GitHub conversation layout) ──────────────────────────────

function IssuePage({
  detail,
  meta,
  principals,
  mutate,
  onBack,
  t,
}: {
  detail: { issue: Issue; events: IssueEvent[] } | null;
  meta: IssuesMeta;
  principals: string[];
  mutate: (path: string, body: Record<string, unknown>, okKey: string) => Promise<boolean>;
  onBack: () => void;
  t: T;
}) {
  const toast = useToast();
  const [comment, setComment] = useState("");
  const [labelsDraft, setLabelsDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const issueId = detail?.issue.id;
  const issueLabels = detail?.issue.labels.join(", ") ?? "";

  useEffect(() => {
    setLabelsDraft(issueLabels);
    setComment("");
  }, [issueId, issueLabels]);

  if (detail === null) {
    return (
      <>
        <button className="btn ghost sm" onClick={onBack} style={{ marginBottom: 12 }}>
          <ArrowLeft size={14} strokeWidth={1.75} /> {t("btn.back")}
        </button>
        <div className="spinner" />
      </>
    );
  }
  const { issue, events } = detail;
  const comments = events.filter((e) => e.kind === "comment").length;

  const run = async (path: string, body: Record<string, unknown>, okKey: string) => {
    setBusy(true);
    try {
      return await mutate(`/api/issues/${issue.id}${path}`, body, okKey);
    } finally {
      setBusy(false);
    }
  };

  // GitHub semantics: text in the box + Close = comment first, then close.
  const closeIssue = async () => {
    if (comment.trim()) {
      const ok = await run("/comment", { body: comment }, "");
      if (!ok) return;
      setComment("");
    }
    await run("/close", {}, "toast.closed");
  };

  const submitComment = () =>
    void run("/comment", { body: comment }, "toast.commented").then((ok) => ok && setComment(""));

  const copyFailed = () => toast.push(t("err.copyText"), "error");
  const copyIssue = async () => {
    try {
      await navigator.clipboard.writeText(issueMarkdown(issue, events, t));
      toast.push(t("toast.issueCopied", { id: String(issue.id) }));
    } catch {
      copyFailed();
    }
  };

  return (
    <>
      <div className="row" style={{ justifyContent: "space-between", gap: 8, marginBottom: 12 }}>
        <button className="btn ghost sm" onClick={onBack}>
          <ArrowLeft size={14} strokeWidth={1.75} /> {t("btn.back")}
        </button>
        <button className="btn ghost sm" onClick={() => void copyIssue()} title={t("btn.copyIssue.title")}>
          <Copy size={13} strokeWidth={1.75} aria-hidden /> {t("btn.copyIssue")}
        </button>
      </div>

      <h1 style={{ marginBottom: 6 }}>
        {issue.title}{" "}
        <span className="muted" style={{ fontWeight: 400 }}>
          #{issue.id}
        </span>
      </h1>
      <div className="row" style={{ gap: 10, flexWrap: "wrap", marginBottom: 18 }}>
        <span className={`issue-state-pill ${issue.state}`}>
          <StateIcon state={issue.state} size={14} />
          {t(`state.${issue.state}`)}
        </span>
        <span className="muted" style={{ fontSize: 13 }}>
          <Principal name={issue.author} muted /> {t("detail.openedThis")}{" "}
          <Time t={t} iso={issue.created_at} /> ·{" "}
          {t(comments === 1 ? "detail.commentCount" : "detail.commentCount.plural", {
            n: String(comments),
          })}
        </span>
      </div>

      <div className="issue-layout">
        <div style={{ minWidth: 0 }}>
          <div className="issue-comment-card">
            <div className="issue-comment-head">
              <Principal name={issue.author} />
              <span className="issue-comment-head-side muted">
                <Time t={t} iso={issue.created_at} />
                {issue.body ? (
                  <CopyTextButton text={issue.body} label={t("copy.description")} onFail={copyFailed} />
                ) : null}
              </span>
            </div>
            <div className="issue-comment-body">
              {issue.body ? (
                <Markdown text={issue.body} />
              ) : (
                <span className="muted" style={{ fontStyle: "italic" }}>
                  {t("detail.noDescription")}
                </span>
              )}
            </div>
          </div>

          <ul className="issue-timeline">
            {events.map((event) =>
              event.kind === "comment" ? (
                <li key={event.id}>
                  <div className="issue-comment-card">
                    <div className="issue-comment-head">
                      <Principal name={event.actor} />
                      <span className="issue-comment-head-side muted">
                        <Time t={t} iso={event.ts} />
                        <CopyTextButton
                          text={String(event.payload.body ?? "")}
                          label={t("copy.comment")}
                          onFail={copyFailed}
                        />
                      </span>
                    </div>
                    <div className="issue-comment-body">
                      <Markdown text={String(event.payload.body ?? "")} />
                    </div>
                  </div>
                </li>
              ) : (
                <li key={event.id} className="issue-timeline-event muted">
                  <EventIcon kind={event.kind} />
                  <span>
                    <Principal name={event.actor} muted /> {eventText(t, event)}{" "}
                    <Time t={t} iso={event.ts} />
                  </span>
                </li>
              ),
            )}
          </ul>

          <div className="issue-comment-card" style={{ marginTop: 14 }}>
            <div className="issue-comment-body">
              <textarea
                className="input"
                style={{ width: "100%", minHeight: 80 }}
                placeholder={t("detail.comment.placeholder")}
                value={comment}
                disabled={busy}
                onChange={(e) => setComment(e.target.value)}
                onKeyDown={(e: ReactKeyboardEvent) => {
                  if ((e.metaKey || e.ctrlKey) && e.key === "Enter" && comment.trim()) {
                    e.preventDefault();
                    submitComment();
                  }
                }}
              />
              <div className="row" style={{ justifyContent: "flex-end", gap: 8, marginTop: 8 }}>
                {issue.state === "open" ? (
                  <button className="btn" disabled={busy} onClick={() => void closeIssue()}>
                    <CheckCircle2 size={14} strokeWidth={1.75} style={{ color: "var(--info)" }} />
                    {comment.trim() ? t("btn.closeWith") : t("btn.closeIssue")}
                  </button>
                ) : (
                  <button
                    className="btn"
                    disabled={busy}
                    onClick={() => void run("/reopen", {}, "toast.reopened")}
                  >
                    <CircleDot size={14} strokeWidth={1.75} style={{ color: "var(--success)" }} />
                    {t("btn.reopen")}
                  </button>
                )}
                <button
                  className="btn primary"
                  disabled={busy || !comment.trim()}
                  onClick={submitComment}
                >
                  {t("btn.comment")}
                </button>
              </div>
            </div>
          </div>
        </div>

        <aside className="issue-sidebar">
          <div className="issue-sidebar-section">
            <label>{t("detail.assignee")}</label>
            <select
              className="input sm"
              value={issue.assignee}
              disabled={busy}
              onChange={(e) => void run("/assign", { assignee: e.target.value }, "toast.assigned")}
            >
              <option value="">{t("detail.unassigned")}</option>
              {principals.map((p) => (
                <option key={p} value={p}>
                  {p}
                </option>
              ))}
            </select>
          </div>
          <div className="issue-sidebar-section">
            <label>{t("detail.type")}</label>
            <select
              className="input sm"
              value={issue.type}
              disabled={busy}
              onChange={(e) => void run("/type", { type: e.target.value }, "toast.type")}
            >
              {meta.types.map((ty) => (
                <option key={ty} value={ty}>
                  {ty}
                </option>
              ))}
            </select>
          </div>
          <div className="issue-sidebar-section">
            <label>{t("detail.labels")}</label>
            {issue.labels.length ? (
              <div className="issue-labels" style={{ marginBottom: 6 }}>
                {issue.labels.map((l) => (
                  <LabelChip key={l} name={l} />
                ))}
              </div>
            ) : null}
            <div className="row" style={{ gap: 6 }}>
              <input
                className="input sm"
                style={{ flex: 1, minWidth: 0 }}
                placeholder={t("detail.labels.placeholder")}
                value={labelsDraft}
                disabled={busy}
                onChange={(e) => setLabelsDraft(e.target.value)}
              />
              <button
                className="btn sm"
                disabled={busy || labelsDraft === issue.labels.join(", ")}
                onClick={() =>
                  void run(
                    "/labels",
                    {
                      labels: labelsDraft
                        .split(",")
                        .map((s) => s.trim())
                        .filter(Boolean),
                    },
                    "toast.labels",
                  )
                }
              >
                {t("btn.save")}
              </button>
            </div>
          </div>
        </aside>
      </div>
    </>
  );
}

function EventIcon({ kind }: { kind: string }) {
  const props = { size: 14, strokeWidth: 1.75, "aria-hidden": true } as const;
  if (kind === "close") return <CheckCircle2 {...props} style={{ color: "var(--info)" }} />;
  if (kind === "reopen") return <CircleDot {...props} style={{ color: "var(--success)" }} />;
  if (kind === "assign") return <UserRound {...props} />;
  if (kind === "label") return <Tag {...props} />;
  return <Shapes {...props} />;
}

function eventText(t: T, event: IssueEvent): string {
  const payload = event.payload;
  switch (event.kind) {
    case "assign":
      return payload.assignee
        ? t("event.assign", { assignee: String(payload.assignee) })
        : t("event.unassign");
    case "label":
      return t("event.label", {
        labels: (payload.labels as string[] | undefined)?.join(", ") ?? "",
      });
    case "type":
      return t("event.type", { type: String(payload.type ?? "") });
    default:
      return `${t(`event.${event.kind}`)}${payload.reason ? ` — ${payload.reason}` : ""}`;
  }
}

function NewIssueModal({
  meta,
  principals,
  onClose,
  onCreated,
}: {
  meta: IssuesMeta;
  principals: string[];
  onClose: () => void;
  onCreated: (id: number) => void;
}) {
  const t = useT(STR);
  const toast = useToast();
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [type, setType] = useState("task");
  const [assignee, setAssignee] = useState("");
  const [labels, setLabels] = useState("");
  const [busy, setBusy] = useState(false);
  const [similar, setSimilar] = useState<{ id: number; title: string; score: number }[]>([]);

  // GitHub-style duplicate hint: as the title settles, ask for semantically
  // similar open issues. Best-effort — empty when embeddings are unavailable.
  useEffect(() => {
    const text = title.trim();
    if (text.length < 8) {
      setSimilar([]);
      return;
    }
    const timer = setTimeout(() => {
      api
        .get<{ similar: { id: number; title: string; score: number }[] }>(
          `/api/issues/similar?q=${encodeURIComponent(`${text}\n${body.slice(0, 500)}`)}`,
        )
        .then((res) => setSimilar(res.similar))
        .catch(() => setSimilar([]));
    }, 400);
    return () => clearTimeout(timer);
  }, [title, body]);

  const submit = async () => {
    setBusy(true);
    try {
      const res = await api.post<{ issue: Issue }>("/api/issues", {
        title,
        body,
        type,
        assignee,
        labels: labels
          .split(",")
          .map((s) => s.trim())
          .filter(Boolean),
      });
      onCreated(res.issue.id);
    } catch (err) {
      toast.push(err instanceof ApiError ? err.detail : String(err), "error");
    } finally {
      setBusy(false);
    }
  };

  const submitKey = (e: ReactKeyboardEvent) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter" && title.trim() && !busy) {
      e.preventDefault();
      void submit();
    }
  };

  return (
    <Modal title={t("new.title")} subtitle={t("new.subtitle")} onClose={onClose}>
      <div onKeyDown={submitKey}>
        <Field label={t("new.field.title")} hint={t("new.field.title.hint")}>
          <input className="input" value={title} onChange={(e) => setTitle(e.target.value)} autoFocus />
        </Field>
        {similar.length > 0 ? (
          <div className="issue-similar-hint">
            <strong style={{ fontSize: 12 }}>{t("new.similar.title")}</strong>
            <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>
              {similar.map((s) => (
                <li key={s.id} style={{ fontSize: 12 }}>
                  #{s.id} {s.title}
                </li>
              ))}
            </ul>
            <span className="muted" style={{ fontSize: 11 }}>
              {t("new.similar.hint")}
            </span>
          </div>
        ) : null}
        <Field label={t("new.field.body")} hint={t("new.field.body.hint")}>
          <textarea
            className="input"
            style={{ minHeight: 90 }}
            value={body}
            onChange={(e) => setBody(e.target.value)}
          />
        </Field>
        <div className="row" style={{ gap: 10, flexWrap: "wrap" }}>
          <Field label={t("new.field.type")}>
            <select className="input" value={type} onChange={(e) => setType(e.target.value)}>
              {meta.types.map((ty) => (
                <option key={ty} value={ty}>
                  {ty}
                </option>
              ))}
            </select>
          </Field>
          <Field label={t("new.field.assignee")} hint={t("new.field.assignee.hint")}>
            <select className="input" value={assignee} onChange={(e) => setAssignee(e.target.value)}>
              <option value="">{t("detail.unassigned")}</option>
              {principals.map((p) => (
                <option key={p} value={p}>
                  {p}
                </option>
              ))}
            </select>
          </Field>
        </div>
        <Field label={t("new.field.labels")} hint={t("new.field.labels.hint")}>
          <input className="input" value={labels} onChange={(e) => setLabels(e.target.value)} />
        </Field>
        <div className="row" style={{ justifyContent: "flex-end", gap: 8, marginTop: 6 }}>
          <button className="btn" onClick={onClose} disabled={busy}>
            {t("btn.cancel")}
          </button>
          <button className="btn primary" onClick={() => void submit()} disabled={busy || !title.trim()}>
            {t("btn.create")}
          </button>
        </div>
      </div>
    </Modal>
  );
}
