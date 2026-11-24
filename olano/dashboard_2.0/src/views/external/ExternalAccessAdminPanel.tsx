import { useT } from "../../core/i18n";
import { ClientsCard, EndpointsCard, StatusBadge, TokensCard, useExternalStatus } from "./ExternalAccessPanel";
import { STR } from "./strings";

/** Fleet view of the inbound surfaces, embedded in Config → External access. */
export function ExternalAccessAdminPanel() {
  const t = useT(STR);
  const { status, forbidden, reload } = useExternalStatus();
  if (forbidden || !status) return null;
  return (
    <div data-testid="ext-admin-panel" style={{ marginTop: 14 }}>
      <div style={{ display: "flex", gap: 14, flexWrap: "wrap", fontSize: 12.5 }}>
        <span>
          MCP{" "}
          <StatusBadge
            serving={status.serving}
            enabled={status.enabled}
            https={status.https}
            requireHttps={status.require_https}
            originKnown={status.public_origin_known}
          />{" "}
          <span className="muted">{status.exposed_agents.join(", ") || t("ext.status.noAgents")}</span>
        </span>
        {status.a2a && (
          <span>
            A2A{" "}
            <StatusBadge
              serving={status.a2a.serving}
              enabled={status.a2a.enabled}
              https={status.https}
              requireHttps={status.require_https}
              originKnown={status.public_origin_known}
            />{" "}
            <span className="muted">{status.a2a.exposed_agents.join(", ") || t("ext.status.noAgents")}</span>
          </span>
        )}
      </div>
      <EndpointsCard status={status} />
      <TokensCard status={status} onChange={reload} />
      <ClientsCard onChange={reload} />
      <p className="muted" style={{ fontSize: 11.5, marginTop: 8 }}>{t("ext.restartNote.fleet")}</p>
    </div>
  );
}
