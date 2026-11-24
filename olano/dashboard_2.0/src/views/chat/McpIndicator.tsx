// MCP status indicator for the chat header. Shows a green/yellow/red dot for the
// agent's MCP servers and, on click, a popover listing each server's live status
// with a deep-link to the agent's MCP setup tab. State is seeded from
// /api/agents/{name}/mcp/health and kept live via the shared /api/stream channel
// (the backend pushes an `mcp_status` frame on reload / authorize).
import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { agentDetailPath } from "../../core/routeUrls";
import { Plug } from "lucide-react";
import { api } from "../../core/api";
import { subscribeStream } from "../../core/agentStream";
import { useT } from "../../core/i18n";
import { AnchoredPopover } from "../../ui/AnchoredPopover";
import { STR } from "./strings";

interface McpHealthServer {
  url: string;
  transport: string;
  status: string;
  ok: boolean | null;
  tool_count: number;
  enabled: boolean;
  message?: string;
}

interface McpHealth {
  ok: boolean;
  running: boolean;
  overall: "green" | "yellow" | "red" | "none";
  servers: McpHealthServer[];
}

const OVERALL_COLOR: Record<string, string> = {
  green: "var(--success)",
  yellow: "var(--warn)",
  red: "var(--danger)",
};

function serverDot(s: McpHealthServer): string {
  if (!s.enabled) return "var(--text-3)";
  if (s.ok === true) return "var(--success)";
  if (s.ok === false) return "var(--danger)";
  return "var(--warn)";
}

export function McpIndicator({ agentName }: { agentName: string }) {
  const t = useT(STR);
  const [health, setHealth] = useState<McpHealth | null>(null);
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  const load = useCallback(async () => {
    if (!agentName) return;
    try {
      setHealth(await api.get<McpHealth>(`/api/agents/${encodeURIComponent(agentName)}/mcp/health`));
    } catch {
      /* indicator is best-effort — leave prior state on a transient failure */
    }
  }, [agentName]);

  useEffect(() => {
    void load();
  }, [load]);

  // Live updates: the backend broadcasts `mcp_status` on reload / authorize.
  // Agent lifecycle frames (start/restart/roster) can change the live toolset
  // too, so re-fetch health on those rather than relying on a dedicated emit.
  useEffect(() => {
    return subscribeStream((f) => {
      if (f.type === "mcp_status") {
        const d = f.data as (McpHealth & { name?: string }) | undefined;
        if (!d || d.name !== agentName) return;
        setHealth({ ok: true, running: d.running, overall: d.overall, servers: d.servers || [] });
      } else if (f.type === "agent_started" || f.type === "agents_update") {
        const d = f.data as { name?: string } | undefined;
        if (!d || !d.name || d.name === agentName) void load();
      }
    });
  }, [agentName, load]);

  const close = useCallback(() => setOpen(false), []);

  // No MCP servers configured → no chip (keep the header uncluttered).
  if (!health || health.overall === "none") return null;

  const dot = OVERALL_COLOR[health.overall] || "var(--warn)";
  const enabled = health.servers.filter((s) => s.enabled);
  const liveCount = enabled.filter((s) => s.ok === true).length;

  return (
    <div ref={ref} style={{ position: "relative" }}>
      <button
        className="chat-chip"
        onClick={() => setOpen((v) => !v)}
        title={t("mcp.statusTitle")}
      >
        <span className="chat-chip-dot" style={{ background: dot }} />
        <Plug size={12} strokeWidth={2} />{" "}
        {t("mcp.badge", { live: String(liveCount), total: String(enabled.length) })}
      </button>
      {/* Measured and clamped rather than pinned with `right: 0`: the chat
          header's chip row wraps on a phone, so this chip lands wherever the
          wrap puts it and a 330px panel hanging off its right edge went ~60px
          past the LEFT edge of the viewport. See ui/AnchoredPopover. */}
      <AnchoredPopover
        anchorRef={ref}
        open={open}
        onClose={close}
        minWidth={330}
        className="card mcp-pop"
        role="dialog"
        ariaLabel={t("mcp.heading")}
      >
        <div className="row" style={{ justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
          <strong style={{ fontSize: 13 }}>{t("mcp.heading")}</strong>
          <Link
            to={agentDetailPath(agentName, "mcp")}
            style={{ fontSize: 12 }}
            onClick={close}
          >
            {t("mcp.setup")}
          </Link>
        </div>
        {/* The rows scroll inside the clamped panel; the heading and the
            not-running note stay put. */}
        <div className="mcp-pop-list">
          {health.servers.length === 0 ? (
            <div style={{ fontSize: 12, color: "var(--text-3)" }}>{t("mcp.noServers")}</div>
          ) : (
            health.servers.map((s) => (
              <div key={s.url} className="row" style={{ gap: 8, alignItems: "center", padding: "4px 0" }}>
                <span className="chat-chip-dot" style={{ background: serverDot(s), flex: "0 0 auto" }} />
                <div style={{ minWidth: 0, flex: 1 }}>
                  <div style={{ fontSize: 12, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {s.url}
                  </div>
                  <div style={{ fontSize: 11, color: "var(--text-3)" }}>
                    {s.status}
                    {s.tool_count
                      ? ` · ${t(s.tool_count === 1 ? "slash.mcps.tool" : "slash.mcps.tools", {
                          n: String(s.tool_count),
                        })}`
                      : ""}
                  </div>
                </div>
              </div>
            ))
          )}
        </div>
        {!health.running && (
          <div style={{ fontSize: 11, color: "var(--text-3)", marginTop: 6 }}>
            {t("slash.mcps.notRunning")}
          </div>
        )}
      </AnchoredPopover>
    </div>
  );
}
