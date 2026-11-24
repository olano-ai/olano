import { useCallback, useEffect, useMemo, useRef, useState, type MouseEvent } from "react";
import { useNavigate } from "react-router-dom";
import { agentDetailPath, chatPath } from "../../core/routeUrls";
import { Bot, Plus, X } from "lucide-react";
import { api } from "../../core/api";
import { subscribeStream, subscribeStreamState, getStreamState } from "../../core/agentStream";
import { useT } from "../../core/i18n";
import { usePersistentState } from "../../core/usePersistentState";
import { useToast } from "../../ui/toast";
import { ErrorState } from "../../ui/ErrorState";
import type { Agent } from "../../core/types";
import {
  PlaygroundEngine,
  type ActivityState,
  type ActivityStatus,
  type AgentLike,
  type HoverInfo,
} from "./engine";
import { STR } from "./strings";

// The /api/agents payload carries an extra `sprites` map that the shared
// `Agent` type does not model, so we widen it locally.
interface PlaygroundAgent extends Agent {
  sprites?: Record<string, string> | null;
}

interface StreamEvent {
  type: string;
  data: Record<string, unknown> | null;
  ts?: string;
}

const WORKING_TAIL_MS = 14000;

// Sprite generation streams two `agent_sprites_ready` events that reuse the
// same on-disk filenames: a full-canvas first pass (shown while page 2 renders)
// then a tightly-cropped final pass saved over the same paths. This used to
// need a client-side cache-buster, because both events carried byte-for-byte
// identical URLs and the browser kept serving the first pass — the canvas drew
// the character at ~half size until a manual reload. The API now stamps every
// sprite URL with `?v=<content>`, so the second pass arrives as a genuinely
// different URL and the swap happens on its own. A timestamp buster on top
// would defeat the caching those stamps exist to enable.

export function PlaygroundView() {
  const toast = useToast();
  const navigate = useNavigate();
  const t = useT(STR);

  const [agents, setAgents] = useState<PlaygroundAgent[] | null>(null);
  const [activity, setActivity] = useState<Record<string, ActivityState>>({});
  const [connected, setConnected] = useState(false);
  const [hovering, setHovering] = useState(false);
  const [hover, setHover] = useState<HoverInfo | null>(null);
  // Persisted so a deliberate hide of the legend guide sticks across tab switches.
  const [legendOpen, setLegendOpen] = usePersistentState("playground.legendOpen", true);

  const hostRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const engineRef = useRef<PlaygroundEngine | null>(null);

  // stable ref so the engine click callback always navigates with the latest
  // router instance even though the engine is created once.
  const navRef = useRef(navigate);
  navRef.current = navigate;

  // ── data: initial agent roster ──────────────────────────────────────────────
  const [loadError, setLoadError] = useState<string | null>(null);
  const loadRoster = useCallback(() => {
    setLoadError(null);
    api
      .cachedGet<PlaygroundAgent[]>("/api/agents")
      .then(setAgents)
      .catch((e: unknown) => {
        // The roster IS the playground — surface an inline error + retry, not
        // an empty world that looks like "no agents".
        setLoadError(e instanceof Error ? e.message : t("toast.loadFailed"));
      });
  }, [t]);
  useEffect(loadRoster, [loadRoster]);

  // ── engine lifecycle ────────────────────────────────────────────────────────
  useEffect(() => {
    const canvas = canvasRef.current;
    const host = hostRef.current;
    if (!canvas || !host) return;

    const engine = new PlaygroundEngine(canvas, {
      onCharClick: (name, running) => {
        // Running agents → chat with them; configured agents → their detail.
        // Both URLs name the agent, so each view opens on it.
        navRef.current(running ? chatPath(name) : agentDetailPath(name));
      },
    });
    engineRef.current = engine;

    const resize = () => {
      const rect = host.getBoundingClientRect();
      const w = Math.max(320, Math.floor(rect.width));
      const h = Math.max(280, Math.floor(rect.height));
      engine.resize(w, h);
    };
    resize();
    engine.start();

    const ro = new ResizeObserver(resize);
    ro.observe(host);
    window.addEventListener("resize", resize);

    return () => {
      ro.disconnect();
      window.removeEventListener("resize", resize);
      engine.stop();
      engineRef.current = null;
    };
  }, []);

  // ── re-read design tokens when the app theme flips ───────────────────────────
  // The canvas can't see CSS classes, so the engine snapshots the colour tokens.
  // Watch the <html data-theme> attribute and tell the engine to re-resolve.
  useEffect(() => {
    const root = document.documentElement;
    const mo = new MutationObserver(() => engineRef.current?.refreshTheme());
    mo.observe(root, { attributes: true, attributeFilter: ["data-theme"] });
    return () => mo.disconnect();
  }, []);

  // ── feed the engine the latest roster ────────────────────────────────────────
  useEffect(() => {
    if (!agents) return;
    engineRef.current?.setRoster(agents as AgentLike[]);
  }, [agents]);

  // ── feed the engine the latest activity map ──────────────────────────────────
  useEffect(() => {
    engineRef.current?.setActivity(activity);
  }, [activity]);

  // ── live SSE stream ──────────────────────────────────────────────────────────
  useEffect(() => {
    setConnected(getStreamState() === "open");
    const offState = subscribeStreamState((s) => setConnected(s === "open"));
    const offStream = subscribeStream((frame) => {
      const evt = frame as StreamEvent;
      const d = evt.data ?? {};
      switch (evt.type) {
        case "connected":
          setConnected(true);
          break;

        case "agent_activity": {
          const name = String(d.agent ?? "");
          if (!name) break;
          const status = String(d.status ?? "idle") as ActivityStatus;
          // `peer` rides along on "talking" frames: the agent that initiated the
          // talk_to call, so the engine can walk this agent over beside it.
          const peer = status === "talking" && d.peer ? String(d.peer) : null;
          setActivity((prev) => {
            const now = Date.now();
            const was = prev[name];
            const working = status === "thinking" || status === "talking";
            return {
              ...prev,
              [name]: {
                status,
                updatedAt: now,
                lastWorkingAt: working ? now : was?.lastWorkingAt ?? 0,
                peer,
              },
            };
          });
          break;
        }

        case "agent_sprites_ready": {
          const name = String(d.name ?? "");
          const sprites = (d.sprites as Record<string, string> | undefined) ?? null;
          if (!name) break;
          // The same object feeds both the engine and React state, so setAgents
          // sees no spurious change.
          engineRef.current?.swapSprites(name, sprites);
          setAgents((prev) => (prev ?? []).map((a) => (a.name === name ? { ...a, sprites } : a)));
          const count = sprites ? Object.keys(sprites).length : 0;
          toast.push(t("toast.spritesReady", { agent: name, count: String(count) }));
          break;
        }

        case "agent_sprites_failed": {
          const name = String(d.name ?? "");
          const err = String(d.error ?? "unknown error");
          if (name) toast.push(t("toast.spritesFailed", { agent: name, error: err }), "error");
          break;
        }

        case "agent_avatar_updated":
          // Portrait changed — sprites are the office representation, so there
          // is nothing to redraw on the canvas. Nothing to do.
          break;

        case "agent_started":
        case "agent_stopped":
        case "agent_deleted": {
          const name = String(d.name ?? "");
          if (!name) break;
          setAgents((prev) => {
            if (evt.type === "agent_deleted") return (prev ?? []).filter((a) => a.name !== name);
            const running = evt.type === "agent_started";
            return (prev ?? []).map((a) =>
              a.name === name ? { ...a, running, status: running ? "running" : "configured" } : a,
            );
          });
          break;
        }

        default:
          break;
      }
    });

    return () => {
      offState();
      offStream();
    };
  }, [toast, t]);

  // ── canvas pointer handlers ──────────────────────────────────────────────────
  function localXY(e: MouseEvent<HTMLCanvasElement>): { x: number; y: number } {
    const rect = e.currentTarget.getBoundingClientRect();
    return { x: e.clientX - rect.left, y: e.clientY - rect.top };
  }
  function onCanvasClick(e: MouseEvent<HTMLCanvasElement>) {
    const { x, y } = localXY(e);
    engineRef.current?.click(x, y);
  }
  function onCanvasMove(e: MouseEvent<HTMLCanvasElement>) {
    const { x, y } = localXY(e);
    const info = engineRef.current?.hoverInfo(x, y) ?? null;
    setHovering(!!info);
    setHover(info);
  }
  function onCanvasLeave() {
    setHovering(false);
    setHover(null);
  }

  // ── derived header counts ────────────────────────────────────────────────────
  const agentCount = agents?.length ?? 0;
  const liveCount = useMemo(() => {
    const now = Date.now();
    return Object.values(activity).filter(
      (a) => a.status === "thinking" || a.status === "talking" || now - a.lastWorkingAt < WORKING_TAIL_MS,
    ).length;
  }, [activity]);

  return (
    <>
      <div className="topbar">
        <div style={{ display: "flex", flexDirection: "column", gap: 1, minWidth: 0 }}>
          <strong>{t("nav.playground")}</strong>
          <span className="muted" style={{ fontSize: 12 }}>
            {t("subtitle")}
          </span>
        </div>
        <div className="row" style={{ gap: 12 }}>
          <span className="muted" style={{ fontSize: 12 }}>
            {t("count.working", {
              live: String(liveCount),
              agents: String(agentCount),
              agentWord: agentCount === 1 ? t("word.agent") : t("word.agents"),
            })}
          </span>
          <span className="badge" title={t("stream.title")}>
            <span
              style={{
                display: "inline-block",
                width: 7,
                height: 7,
                borderRadius: "50%",
                marginRight: 6,
                background: connected ? "var(--success)" : "var(--text-4)",
                boxShadow: connected ? "0 0 6px var(--success)" : "none",
              }}
            />
            {connected ? t("stream.live") : t("stream.offline")}
          </span>
          <button
            className="btn ghost"
            style={{ padding: "4px 9px", fontSize: 12 }}
            onClick={() => setLegendOpen((o) => !o)}
            title={legendOpen ? t("guide.hideTitle") : t("guide.showTitle")}
          >
            {legendOpen ? t("guide.hide") : t("guide.show")}
          </button>
        </div>
      </div>

      <div style={{ height: "calc(100vh - var(--topbar-h))", display: "flex", flexDirection: "column" }}>
        <div
          ref={hostRef}
          style={{
            position: "relative",
            flex: 1,
            overflow: "hidden",
            background: "var(--bg)",
          }}
        >
          {agents === null && (
            <div
              style={{
                position: "absolute",
                inset: 0,
                display: "grid",
                placeItems: "center",
                zIndex: 2,
                gap: 10,
              }}
            >
              {loadError ? (
                <ErrorState message={t("toast.loadFailed")} detail={loadError} onRetry={loadRoster} />
              ) : (
                <div style={{ display: "grid", placeItems: "center", gap: 10 }}>
                  <div className="spinner" />
                  <span className="muted" style={{ fontSize: 12 }}>
                    {t("loading")}
                  </span>
                </div>
              )}
            </div>
          )}
          {agents !== null && agents.length === 0 && (
            <div
              style={{
                position: "absolute",
                inset: 0,
                display: "grid",
                placeItems: "center",
                zIndex: 2,
              }}
            >
              <div
                className="card"
                style={{
                  padding: "22px 26px",
                  textAlign: "center",
                  maxWidth: 320,
                  background: "color-mix(in srgb, var(--surface) 88%, transparent)",
                  backdropFilter: "blur(6px)",
                }}
              >
                <div style={{ marginBottom: 8, color: "var(--text-3)", display: "grid", placeItems: "center" }}>
                  <Bot size={34} strokeWidth={1.5} />
                </div>
                <p style={{ margin: "0 0 12px", fontWeight: 600 }}>{t("empty.title")}</p>
                <p className="muted" style={{ margin: "0 0 16px", fontSize: 13 }}>
                  {t("empty.body")}
                </p>
                <button
                  className="btn primary"
                  onClick={() => navigate("/agents")}
                  style={{ display: "inline-flex", alignItems: "center", gap: 6 }}
                >
                  <Plus size={15} strokeWidth={1.75} /> {t("empty.cta")}
                </button>
              </div>
            </div>
          )}

          {legendOpen && agents !== null && agents.length > 0 && <Legend onClose={() => setLegendOpen(false)} />}

          {hover && (
            <HoverTip host={hostRef.current} info={hover} />
          )}

          <canvas
            ref={canvasRef}
            onClick={onCanvasClick}
            onMouseMove={onCanvasMove}
            onMouseLeave={onCanvasLeave}
            style={{ display: "block", cursor: hovering ? "pointer" : "default" }}
          />
        </div>
      </div>
    </>
  );
}

// ── legend overlay ────────────────────────────────────────────────────────────
// Glassy card pinned top-left explaining the activity halos + interactions —
// the original Playground shipped this guide; restored here, themed to Aurora.
function Legend({ onClose }: { onClose: () => void }) {
  const t = useT(STR);
  return (
    <div
      className="card"
      style={{
        position: "absolute",
        top: 16,
        left: 16,
        zIndex: 3,
        maxWidth: 290,
        padding: "13px 15px",
        background: "color-mix(in srgb, var(--bg-elev) 88%, transparent)",
        backdropFilter: "blur(8px)",
        boxShadow: "var(--shadow)",
      }}
    >
      <div className="row" style={{ justifyContent: "space-between", marginBottom: 4 }}>
        <strong style={{ fontSize: 13, letterSpacing: "-0.01em" }}>{t("legend.title")}</strong>
        <button
          className="btn ghost"
          style={{
            padding: "2px 7px",
            fontSize: 12,
            color: "var(--text-3)",
            display: "inline-flex",
            alignItems: "center",
          }}
          onClick={onClose}
          title={t("legend.hide")}
          aria-label={t("legend.hideAria")}
        >
          <X size={14} strokeWidth={1.75} />
        </button>
      </div>
      <p className="muted" style={{ margin: "0 0 9px", fontSize: 12, lineHeight: 1.5 }}>
        {t("legend.intro")}
      </p>
      <div style={{ display: "flex", flexDirection: "column", gap: 6, fontSize: 12 }}>
        <LegendKey color="var(--info)" label={t("legend.talking")} />
        <LegendKey color="var(--accent)" label={t("legend.thinking")} />
        <LegendKey dim label={t("legend.asleep")} />
      </div>
      <div
        className="muted"
        style={{ marginTop: 10, paddingTop: 9, borderTop: "1px solid var(--border)", fontSize: 11.5, lineHeight: 1.5 }}
      >
        {t("legend.footer")}
      </div>
    </div>
  );
}

function LegendKey({ color, label, dim }: { color?: string; label: string; dim?: boolean }) {
  return (
    <div className="row" style={{ gap: 8 }}>
      <span
        style={{
          width: 11,
          height: 11,
          borderRadius: "50%",
          flexShrink: 0,
          background: dim ? "var(--text-5)" : color,
          boxShadow: dim ? "none" : `0 0 7px ${color}`,
        }}
      />
      <span style={{ color: "var(--text-2)" }}>{label}</span>
    </div>
  );
}

// ── hover tooltip ─────────────────────────────────────────────────────────────
// Follows the hovered character, anchored above its head. Positioned in host-
// local coordinates so it tracks correctly regardless of viewport scroll/size.
function HoverTip({ host, info }: { host: HTMLDivElement | null; info: HoverInfo }) {
  const t = useT(STR);
  if (!host) return null;
  const w = host.clientWidth;
  const left = Math.min(Math.max(info.x, 70), Math.max(70, w - 70));
  const top = Math.max(8, info.y - 6);
  const statusColor =
    info.status === "talking"
      ? "var(--info)"
      : info.status === "thinking" || info.status === "working"
        ? "var(--accent)"
        : "var(--text-4)";
  return (
    <div
      style={{
        position: "absolute",
        left,
        top,
        transform: "translate(-50%, -100%)",
        zIndex: 4,
        pointerEvents: "none",
        background: "color-mix(in srgb, var(--bg-elev) 92%, transparent)",
        border: "1px solid var(--border-strong)",
        borderRadius: "var(--r-sm)",
        boxShadow: "var(--shadow)",
        backdropFilter: "blur(6px)",
        padding: "6px 10px",
        whiteSpace: "nowrap",
      }}
    >
      <div style={{ fontWeight: 600, fontSize: 12.5 }}>{info.name}</div>
      <div className="row" style={{ gap: 6, fontSize: 11.5, marginTop: 2 }}>
        <span style={{ width: 7, height: 7, borderRadius: "50%", background: statusColor, flexShrink: 0 }} />
        <span style={{ color: "var(--text-3)" }}>{t(`status.${info.status}`)}</span>
      </div>
    </div>
  );
}
