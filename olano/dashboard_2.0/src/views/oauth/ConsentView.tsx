import { useEffect, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { api, ApiError } from "../../core/api";
import { useT } from "../../core/i18n";
import { STR } from "./strings";

/**
 * The page an OAuth sign-in lands on: an outside MCP/A2A client has been sent
 * here by `/api/oauth/authorize`, and an administrator decides which agents it
 * may reach. Rendered inside the auth gate (a login is required) but outside
 * the app shell, so the person sees one decision and nothing else.
 */

interface Pending {
  request_id: string;
  client_id: string;
  client_name: string;
  client_uri?: string | null;
  logo_uri?: string | null;
  redirect_host: string;
  requested_agents: string[];
  requested_all: boolean;
  offered_agents: string[];
  exposed_agents: string[];
  resource?: string | null;
  expires_in: number;
}

type Phase = "loading" | "ready" | "invalid" | "forbidden" | "redirecting";

export function ConsentView() {
  const [sp] = useSearchParams();
  const requestId = sp.get("request") ?? "";
  const navigate = useNavigate();
  const t = useT(STR);
  const [phase, setPhase] = useState<Phase>("loading");
  const [info, setInfo] = useState<Pending | null>(null);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [all, setAll] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!requestId) {
      setPhase("invalid");
      return;
    }
    api
      .get<Pending>(`/api/oauth/consent/${encodeURIComponent(requestId)}`)
      .then((p) => {
        setInfo(p);
        // Preselect what the client asked for (or everything offered when it
        // asked for all); the admin still has to press Allow.
        setPicked(new Set(p.offered_agents));
        setAll(false);
        setPhase("ready");
      })
      .catch((e: unknown) => {
        setPhase(e instanceof ApiError && e.status === 403 ? "forbidden" : "invalid");
      });
  }, [requestId]);

  const minutes = useMemo(() => Math.max(1, Math.round((info?.expires_in ?? 0) / 60)), [info]);

  async function decide(approve: boolean) {
    if (!info) return;
    const agents = all ? ["*"] : Array.from(picked);
    if (approve && agents.length === 0) {
      setError(t("consent.err.pick"));
      return;
    }
    setBusy(true);
    setError("");
    try {
      const res = await api.post<{ redirect_to: string; approved: boolean }>(
        `/api/oauth/consent/${encodeURIComponent(info.request_id)}`,
        { approve, agents },
      );
      setPhase("redirecting");
      // The client's registered redirect URI: it receives the code (or the
      // access_denied error) exactly as OAuth prescribes.
      window.location.href = res.redirect_to;
    } catch (e: unknown) {
      setBusy(false);
      if (e instanceof ApiError && e.status === 404) setPhase("invalid");
      else if (e instanceof ApiError && e.status === 403) setPhase("forbidden");
      else setError(e instanceof ApiError ? e.detail : t("consent.err.generic"));
    }
  }

  if (phase === "loading") {
    return (
      <div className="auth-wrap">
        <div className="spinner" />
      </div>
    );
  }

  if (phase === "invalid" || phase === "forbidden") {
    const key = phase === "invalid" ? "consent.invalid" : "consent.forbidden";
    return (
      <div className="auth-wrap">
        <div className="card auth-card" data-testid="oauth-consent-invalid">
          <h2 style={{ marginTop: 0 }}>{t(`${key}.title`)}</h2>
          <p className="muted">{t(`${key}.body`)}</p>
          <button className="btn" type="button" onClick={() => navigate("/", { replace: true })}>
            {t("consent.back")}
          </button>
        </div>
      </div>
    );
  }

  if (phase === "redirecting" || !info) {
    return (
      <div className="auth-wrap">
        <div className="card auth-card">
          <p className="muted">{t("consent.redirecting", { client: info?.client_name ?? "" })}</p>
          <div className="spinner" />
        </div>
      </div>
    );
  }

  const noneExposed = info.exposed_agents.length === 0;
  return (
    <div className="auth-wrap">
      <div className="card auth-card" data-testid="oauth-consent" style={{ maxWidth: 560 }}>
        <h2 style={{ marginTop: 0 }}>{t("consent.title", { client: info.client_name })}</h2>
        <p className="muted">{t("consent.subtitle")}</p>

        <dl className="consent-facts" style={{ margin: "14px 0", display: "grid", gap: 6 }}>
          <div>
            <dt className="muted" style={{ fontSize: 12 }}>{t("consent.client")}</dt>
            <dd style={{ margin: 0 }}>
              {info.logo_uri && (
                <img
                  src={info.logo_uri}
                  alt=""
                  width={18}
                  height={18}
                  style={{ verticalAlign: "middle", marginRight: 6, borderRadius: 4 }}
                />
              )}
              <strong>{info.client_name}</strong>
              {info.client_uri && (
                <>
                  {" "}
                  · <span className="muted">{info.client_uri}</span>
                </>
              )}
            </dd>
          </div>
          <div>
            <dt className="muted" style={{ fontSize: 12 }}>{t("consent.clientId")}</dt>
            <dd style={{ margin: 0 }}>
              <code>{info.client_id}</code>
            </dd>
          </div>
          <div>
            <dt className="muted" style={{ fontSize: 12 }}>{t("consent.redirect")}</dt>
            <dd style={{ margin: 0 }}>
              <code>{info.redirect_host}</code>
              <div className="muted" style={{ fontSize: 11.5, lineHeight: 1.45, marginTop: 2 }}>
                {t("consent.redirectHint")}
              </div>
            </dd>
          </div>
          {info.resource && (
            <div>
              <dt className="muted" style={{ fontSize: 12 }}>{t("consent.resource")}</dt>
              <dd style={{ margin: 0 }}>
                <code>{info.resource}</code>
              </dd>
            </div>
          )}
        </dl>

        <fieldset style={{ border: 0, padding: 0, margin: "0 0 12px" }}>
          <legend style={{ fontWeight: 600, marginBottom: 4 }}>{t("consent.agents")}</legend>
          <div className="muted" style={{ fontSize: 12, lineHeight: 1.45, marginBottom: 8 }}>
            {t("consent.agentsHint")}
          </div>
          {info.requested_agents.length > 0 && !info.requested_all && (
            <div className="muted" style={{ fontSize: 12, marginBottom: 8 }}>
              {t("consent.requestedNote", { agents: info.requested_agents.join(", ") })}
            </div>
          )}
          {noneExposed ? (
            <p style={{ color: "var(--danger)", fontSize: 13 }}>{t("consent.noneExposed")}</p>
          ) : (
            <div style={{ display: "grid", gap: 6 }}>
              {info.offered_agents.map((a) => (
                <label key={a} style={{ display: "flex", gap: 8, alignItems: "center", opacity: all ? 0.5 : 1 }}>
                  <input
                    type="checkbox"
                    checked={all || picked.has(a)}
                    disabled={all}
                    onChange={(e) => {
                      const next = new Set(picked);
                      if (e.target.checked) next.add(a);
                      else next.delete(a);
                      setPicked(next);
                    }}
                  />
                  <span>{a}</span>
                </label>
              ))}
              {info.requested_all && (
                <label style={{ display: "flex", gap: 8, alignItems: "flex-start", marginTop: 6 }}>
                  <input type="checkbox" checked={all} onChange={(e) => setAll(e.target.checked)} />
                  <span>
                    {t("consent.all")}
                    <div className="muted" style={{ fontSize: 11.5, lineHeight: 1.45 }}>
                      {t("consent.allHint")}
                    </div>
                  </span>
                </label>
              )}
            </div>
          )}
        </fieldset>

        <p className="muted" style={{ fontSize: 12 }}>{t("consent.expires", { minutes: String(minutes) })}</p>
        {error && <p style={{ color: "var(--danger)" }}>{error}</p>}
        <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
          <button className="btn" type="button" disabled={busy} onClick={() => decide(false)}>
            {t("consent.deny")}
          </button>
          <button
            className="btn primary"
            type="button"
            disabled={busy || noneExposed}
            onClick={() => decide(true)}
          >
            {busy ? t("consent.working") : t("consent.approve")}
          </button>
        </div>
      </div>
    </div>
  );
}
