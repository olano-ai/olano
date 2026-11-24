// Per-agent Pages tab.
//
// Lists the pages this agent has published (via the pages_* agent tools) at
// /pages/<slug>/, and lets an admin set / change / remove the HTTP Basic-Auth
// viewer credential that guards them. The credential is per agent: one password
// covers every page the agent publishes, and until it is set those pages answer
// 503. Agents can neither read nor set this password — only this admin surface
// (and the `olano pages set-password --agent` CLI) can.
//
// Pages published with pages_deploy also run a build, a server, or both. The
// page supervisor (olano/pages/services.py) reports what each one is doing in
// the `runtime` block of GET /api/pages; this tab shows it, offers Restart and
// Stop, and reads the build and server logs. It refetches when the supervisor
// emits a `page_state` frame for this agent, so a build finishing or a server
// crashing shows up without a reload.

import { useCallback, useEffect, useRef, useState } from "react";
import {
  Copy,
  CheckCircle2,
  ExternalLink,
  Lock,
  Trash2,
  RotateCw,
  Square,
  ScrollText,
} from "lucide-react";
import { api, ApiError } from "../../core/api";
import { subscribeStream } from "../../core/agentStream";
import { useToast } from "../../ui/toast";
import { Field } from "./Modal";
import { useT } from "../../core/i18n";
import { STR } from "./strings";

interface PageRuntime {
  state?: string;
  detail?: string;
  lane?: string;
  port?: number;
  restarts?: number;
  last_error?: string;
  release?: string;
  build?: {
    state?: string;
    seconds?: number;
    timed_out?: boolean;
    finished_at?: number;
  };
}

interface PageRecord {
  slug: string;
  agent: string;
  directory?: string;
  enabled?: boolean;
  updated_at?: string;
  kind?: string;
  spa?: boolean;
  deploy?: { build?: string; start?: string; output?: string } | null;
  runtime?: PageRuntime | null;
}

interface AuthState {
  configured: boolean;
  username: string;
}

type LogKind = "service" | "build";

const MIN_PASSWORD_LEN = 8;
const STATE_COLOR: Record<string, string> = {
  running: "var(--success)",
  live: "var(--success)",
  failed: "var(--danger)",
  stopped: "var(--text-3)",
};

function errText(e: unknown, fallback: string): string {
  return e instanceof ApiError ? e.detail : e instanceof Error ? e.message : fallback;
}

function pageUrl(slug: string): string {
  try {
    return `${window.location.origin}/pages/${slug}/`;
  } catch {
    return `/pages/${slug}/`;
  }
}

function PageLogs({ slug, hasBuild, hasServer }: { slug: string; hasBuild: boolean; hasServer: boolean }) {
  const t = useT(STR);
  const toast = useToast();
  const [which, setWhich] = useState<LogKind>(hasServer ? "service" : "build");
  const [text, setText] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    setBusy(true);
    api
      .get<{ text: string }>(
        `/api/pages/${encodeURIComponent(slug)}/logs?which=${which}&lines=300`,
      )
      .then((r) => setText(r.text || ""))
      .catch((e) => toast.push(errText(e, t("pages.toast.logsFailed")), "error"))
      .finally(() => setBusy(false));
  }, [slug, which, toast, t]);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <div style={{ width: "100%", display: "flex", flexDirection: "column", gap: 6 }}>
      <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
        {hasServer ? (
          <button
            type="button"
            className={`btn ${which === "service" ? "primary" : "ghost"}`}
            onClick={() => setWhich("service")}
          >
            {t("pages.logs.service")}
          </button>
        ) : null}
        {hasBuild ? (
          <button
            type="button"
            className={`btn ${which === "build" ? "primary" : "ghost"}`}
            onClick={() => setWhich("build")}
          >
            {t("pages.logs.build")}
          </button>
        ) : null}
        <button type="button" className="btn ghost" onClick={load} disabled={busy}>
          {busy ? <span className="spinner" /> : <RotateCw size={14} />} {t("pages.logs.refresh")}
        </button>
      </div>
      <pre
        data-testid={`page-log-${slug}`}
        style={{
          margin: 0,
          maxHeight: 320,
          overflow: "auto",
          fontSize: 12,
          padding: 10,
          borderRadius: 8,
          background: "var(--surface-2)",
          whiteSpace: "pre-wrap",
          wordBreak: "break-word",
        }}
      >
        {text === null ? "…" : text.trim() ? text : t("pages.logs.empty")}
      </pre>
    </div>
  );
}

function PageCard({
  page,
  onChanged,
}: {
  page: PageRecord;
  onChanged: () => void;
}) {
  const t = useT(STR);
  const toast = useToast();
  const [copied, setCopied] = useState(false);
  const [showLogs, setShowLogs] = useState(false);
  const [busy, setBusy] = useState(false);
  const url = pageUrl(page.slug);
  const deployed = !!page.deploy;
  const runtime = page.runtime || {};
  const state = page.enabled ? runtime.state || "pending" : "stopped";
  const kindKey = deployed ? (page.kind === "service" ? "service" : "static") : "files";

  function copy() {
    navigator.clipboard?.writeText(url).then(
      () => {
        setCopied(true);
        setTimeout(() => setCopied(false), 1500);
      },
      () => toast.push(t("pages.toast.copyFailed"), "error"),
    );
  }

  async function act(action: "restart" | "stop") {
    setBusy(true);
    try {
      await api.post(`/api/pages/${encodeURIComponent(page.slug)}/${action}`);
      toast.push(
        action === "restart"
          ? t("pages.toast.restarted", { slug: page.slug })
          : t("pages.toast.stoppedPage", { slug: page.slug }),
        "info",
      );
      onChanged();
    } catch (e) {
      toast.push(errText(e, t("pages.toast.actionFailed")), "error");
    } finally {
      setBusy(false);
    }
  }

  const build = runtime.build || {};
  let buildLine = "";
  if (deployed && build.state === "succeeded") {
    buildLine = t("pages.lastBuildOk", { s: String(build.seconds ?? 0) });
  } else if (deployed && build.state === "failed") {
    buildLine = build.timed_out
      ? t("pages.lastBuildTimedOut")
      : t("pages.lastBuildFailed", { s: String(build.seconds ?? 0) });
  }
  const facts = [
    runtime.port && state === "running" ? t("pages.port", { port: String(runtime.port) }) : "",
    runtime.restarts ? t("pages.restarts", { n: String(runtime.restarts) }) : "",
    runtime.release && page.kind !== "service" ? t("pages.release", { id: runtime.release }) : "",
    buildLine,
  ].filter(Boolean);

  return (
    <div
      className="card"
      data-testid={`page-card-${page.slug}`}
      style={{ padding: 12, display: "flex", flexDirection: "column", gap: 8, minWidth: 0 }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
        <strong style={{ wordBreak: "break-all" }}>{page.slug}</strong>
        <span className="badge">{t(`pages.kind.${kindKey}`)}</span>
        <span
          className="badge"
          data-testid={`page-state-${page.slug}`}
          style={{
            color: STATE_COLOR[state] || "var(--warn)",
            borderColor: STATE_COLOR[state] || "var(--warn)",
          }}
        >
          {deployed ? t(`pages.state.${state}`) : page.enabled ? t("pages.serving") : t("pages.stopped")}
        </span>
        <button
          type="button"
          className="btn ghost"
          style={{ marginLeft: "auto" }}
          onClick={copy}
          title={t("pages.copyUrlTitle")}
        >
          {copied ? <CheckCircle2 size={14} /> : <Copy size={14} />}
        </button>
      </div>
      <a
        href={url}
        target="_blank"
        rel="noreferrer"
        className="muted"
        style={{ fontSize: 12, wordBreak: "break-all", display: "inline-flex", gap: 4 }}
      >
        {url}
        <ExternalLink size={12} />
      </a>
      {deployed ? (
        <div className="muted" style={{ fontSize: 12, display: "flex", flexDirection: "column", gap: 2 }}>
          {page.deploy?.build ? (
            <span style={{ wordBreak: "break-all" }}>
              {t("pages.cmd.build")}: <code>{page.deploy.build}</code>
            </span>
          ) : null}
          {page.deploy?.start ? (
            <span style={{ wordBreak: "break-all" }}>
              {t("pages.cmd.start")}: <code>{page.deploy.start}</code>
            </span>
          ) : null}
          {facts.length ? <span>{facts.join(" · ")}</span> : null}
          {runtime.detail && page.enabled ? <span>{runtime.detail}</span> : null}
        </div>
      ) : null}
      {deployed && runtime.last_error && page.enabled && state !== "running" ? (
        <details>
          <summary style={{ cursor: "pointer", fontSize: 12, color: "var(--danger)" }}>
            {t("pages.lastError")}
          </summary>
          <pre
            style={{
              margin: "6px 0 0",
              fontSize: 12,
              whiteSpace: "pre-wrap",
              wordBreak: "break-word",
              maxHeight: 220,
              overflow: "auto",
            }}
          >
            {runtime.last_error}
          </pre>
        </details>
      ) : null}
      <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
        <button
          type="button"
          className="btn ghost"
          disabled={busy}
          onClick={() => act("restart")}
          title={t("pages.restartTitle")}
        >
          <RotateCw size={14} /> {t("pages.restart")}
        </button>
        {page.enabled ? (
          <button
            type="button"
            className="btn ghost"
            disabled={busy}
            onClick={() => act("stop")}
            title={t("pages.stopTitle")}
          >
            <Square size={14} /> {t("pages.stop")}
          </button>
        ) : null}
        {deployed ? (
          <button type="button" className="btn ghost" onClick={() => setShowLogs((v) => !v)}>
            <ScrollText size={14} /> {showLogs ? t("pages.hideLogs") : t("pages.showLogs")}
          </button>
        ) : null}
      </div>
      {deployed && showLogs ? (
        <PageLogs slug={page.slug} hasBuild={!!page.deploy?.build} hasServer={!!page.deploy?.start} />
      ) : null}
    </div>
  );
}

export function PagesTab({ agentName }: { agentName: string }) {
  const toast = useToast();
  const t = useT(STR);
  const [pages, setPages] = useState<PageRecord[]>([]);
  const [auth, setAuth] = useState<AuthState>({ configured: false, username: "owner" });
  const [loading, setLoading] = useState(true);
  const [username, setUsername] = useState("owner");
  const [password, setPassword] = useState("");
  const [saving, setSaving] = useState(false);
  const refetchTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const authBase = `/api/pages/${encodeURIComponent(agentName)}/auth`;

  const load = useCallback(
    (quiet = false) => {
      if (!quiet) setLoading(true);
      Promise.all([
        api.get<{ pages: PageRecord[] }>("/api/pages"),
        api.get<AuthState & { username: string }>(authBase),
      ])
        .then(([list, a]) => {
          setPages((list.pages || []).filter((p) => p.agent === agentName));
          setAuth({ configured: a.configured, username: a.username || "owner" });
          if (!quiet) setUsername(a.username || "owner");
        })
        .catch((e) => {
          if (!quiet) toast.push(errText(e, t("pages.toast.loadFailed")), "error");
        })
        .finally(() => {
          if (!quiet) setLoading(false);
        });
    },
    [agentName, authBase, toast, t],
  );

  useEffect(() => {
    load();
  }, [load]);

  // Refetch (quietly, coalesced) when the supervisor reports a transition for
  // one of this agent's pages, or a page tool changes one.
  useEffect(() => {
    const unsubscribe = subscribeStream((frame) => {
      const type = (frame as { type?: string }).type;
      if (type !== "page_state" && type !== "pages_changed") return;
      const data = (frame as { data?: { agent?: string } }).data;
      if (data?.agent && data.agent !== agentName) return;
      if (refetchTimer.current) clearTimeout(refetchTimer.current);
      refetchTimer.current = setTimeout(() => load(true), 300);
    });
    return () => {
      unsubscribe();
      if (refetchTimer.current) clearTimeout(refetchTimer.current);
    };
  }, [agentName, load]);

  const canSave = username.trim().length > 0 && password.length >= MIN_PASSWORD_LEN && !saving;

  async function save() {
    if (!canSave) return;
    setSaving(true);
    try {
      await api.post(authBase, { username: username.trim(), password });
      setPassword("");
      toast.push(t("pages.toast.saved"), "success");
      load();
    } catch (e) {
      toast.push(errText(e, t("pages.toast.saveFailed")), "error");
    } finally {
      setSaving(false);
    }
  }

  async function remove() {
    setSaving(true);
    try {
      await api.del(authBase);
      setPassword("");
      toast.push(t("pages.toast.removed"), "info");
      load();
    } catch (e) {
      toast.push(errText(e, t("pages.toast.removeFailed")), "error");
    } finally {
      setSaving(false);
    }
  }

  if (loading) {
    return (
      <div className="muted" style={{ padding: 24 }}>
        <span className="spinner" /> {t("pages.loading")}
      </div>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 20, maxWidth: 640, minWidth: 0 }}>
      {/* ── Published pages ─────────────────────────────────────────── */}
      <section>
        <h3 style={{ margin: "0 0 4px" }}>{t("pages.title")}</h3>
        <p className="muted" style={{ margin: "0 0 6px", fontSize: 13 }}>
          {t("pages.introA")} <code>pages_*</code> {t("pages.introB")}{" "}
          <code>/pages/&lt;slug&gt;/</code> {t("pages.introC")}
        </p>
        <p className="muted" style={{ margin: "0 0 12px", fontSize: 13 }}>
          {t("pages.introDeploy")}
        </p>
        {pages.length === 0 ? (
          <div className="card" style={{ padding: 16 }}>
            <span className="muted">
              {t("pages.emptyA")} <code>pages_start</code>{" "}
              {t("pages.emptyB")}
            </span>
          </div>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {pages.map((p) => (
              <PageCard key={p.slug} page={p} onChanged={() => load(true)} />
            ))}
          </div>
        )}
      </section>

      {/* ── Viewer password ─────────────────────────────────────────── */}
      <section>
        <h3 style={{ margin: "0 0 4px", display: "inline-flex", alignItems: "center", gap: 6 }}>
          <Lock size={15} /> {t("pages.viewerPassword")}
        </h3>
        <p className="muted" style={{ margin: "0 0 12px", fontSize: 13 }}>
          {t("pages.viewerIntroA")} <strong>{t("pages.all")}</strong> {t("pages.viewerIntroB", { agent: agentName })}
        </p>

        <div className="card" style={{ padding: 16 }}>
          <div
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 6,
              marginBottom: 12,
              fontSize: 13,
            }}
          >
            {auth.configured ? (
              <>
                <CheckCircle2 size={15} style={{ color: "var(--success)" }} />
                <span>
                  {t("pages.pwSetA")} <code>{auth.username}</code>
                  {t("pages.pwSetB")}
                </span>
              </>
            ) : (
              <>
                <Lock size={15} style={{ color: "var(--danger)" }} />
                <span style={{ color: "var(--danger)" }}>
                  {t("pages.noPasswordSet")}
                </span>
              </>
            )}
          </div>

          <Field label={t("pages.username")}>
            <input
              className="input"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              placeholder={t("pages.usernamePlaceholder")}
              autoComplete="off"
            />
          </Field>
          <Field
            label={auth.configured ? t("pages.newPassword") : t("pages.passwordLabel")}
            hint={t("pages.passwordHint", { n: String(MIN_PASSWORD_LEN) })}
          >
            <input
              className="input"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="••••••••"
              autoComplete="new-password"
              onKeyDown={(e) => {
                if (e.key === "Enter") save();
              }}
            />
          </Field>

          <div className="row" style={{ gap: 8, marginTop: 4 }}>
            <button type="button" className="btn primary" disabled={!canSave} onClick={save}>
              {saving ? <span className="spinner" /> : null}
              {auth.configured ? t("pages.updatePassword") : t("pages.setPassword")}
            </button>
            {auth.configured ? (
              <button
                type="button"
                className="btn danger"
                disabled={saving}
                onClick={remove}
                title={t("pages.removePasswordTitle")}
              >
                <Trash2 size={14} /> {t("pages.removePassword")}
              </button>
            ) : null}
          </div>
        </div>
      </section>
    </div>
  );
}
