// The "Connect" button card rendered under an agent message whose tool result
// carried a sign-in link (an MCP OAuth authorization_url, or a minted magic
// link). Before this, the agent pasted the raw OAuth URL into its reply — a
// six-hundred-character wall of text that wasn't even clickable. The card
// shows the service (name + logo when the MCP directory knows it), a real
// button, and the PIN for magic links; the raw URL never needs to appear.
//
// Stateless by design: completion is reported by the backend watcher as its
// own chat message, so the card never has to poll or track the flow.
//
// One exception to "a link": a secret request (`secret-entry` /
// `provider-key`) on this dashboard's own origin is drawn as an inline form
// instead (`SecretFormCard`): the person is already signed in here, so a new
// tab and a PIN would only add friction, while the value still never becomes
// a chat message. The same-origin rule is load-bearing: detection is by tool
// name, and a tool merely named `magic_link_*` could point anywhere.

import { ExternalLink, Plug } from "lucide-react";
import { useState } from "react";
import { useMcpDirectory } from "../agents/mcpDirectory";
import { useT } from "../../core/i18n";
import { STR } from "./strings";
import { hostOf, inlineFormLink, serviceNameFromUrl, type ConnectLink } from "./connectLinks";
import { SecretFormCard } from "./SecretFormCard";

/** Logo with fallback: directory logos are third-party favicons and may 404. */
function ServiceLogo({ src, monogram }: { src?: string; monogram: string }) {
  const [broken, setBroken] = useState(false);
  if (src && !broken) {
    return <img className="connect-card__logo" src={src} alt="" onError={() => setBroken(true)} />;
  }
  if (monogram) {
    return (
      <span className="connect-card__logo connect-card__logo--mono" aria-hidden>
        {monogram.charAt(0).toUpperCase()}
      </span>
    );
  }
  return (
    <span className="connect-card__logo connect-card__logo--mono" aria-hidden>
      <Plug size={16} strokeWidth={1.75} />
    </span>
  );
}

export function ConnectCard({ link }: { link: ConnectLink }) {
  // Falls back to the link card when the inline routes are not there (an
  // older runtime, a link past retention) or the form could not be loaded.
  const [inline, setInline] = useState(
    () => typeof window !== "undefined" && inlineFormLink(link, window.location.origin),
  );
  if (inline) return <SecretFormCard link={link} onUnavailable={() => setInline(false)} />;
  return <LinkCard link={link} />;
}

function LinkCard({ link }: { link: ConnectLink }) {
  const t = useT(STR);
  const { presets, examples } = useMcpDirectory();

  let service = "";
  let logoUrl: string | undefined;
  if (link.kind === "mcp-oauth" && link.serverUrl) {
    const host = hostOf(link.serverUrl);
    const match =
      presets.find((p) => p.url && hostOf(p.url) === host) ||
      examples.find((e) => typeof e.config.url === "string" && hostOf(e.config.url) === host);
    if (match) {
      service = match.title;
      logoUrl = match.logoUrl;
    } else {
      service = serviceNameFromUrl(link.serverUrl);
    }
  }

  const title =
    link.kind === "magic-link"
      ? link.label || t("connect.magicTitle")
      : service
        ? t("connect.title", { service })
        : t("connect.magicTitle");
  const subtitle =
    link.kind === "mcp-oauth" && link.serverUrl ? hostOf(link.serverUrl) : hostOf(link.href);
  // Monogram letter for the logo fallback — a magic-link label usually reads
  // "Connect X", and the letter worth showing is X's, not the verb's.
  const monogram = service || (link.label || "").replace(/^connect\s+/i, "");

  return (
    <div className="connect-card">
      <ServiceLogo src={logoUrl} monogram={monogram} />
      <div className="connect-card__text">
        <strong>{title}</strong>
        <span className="connect-card__sub">
          {subtitle}
          {link.pin ? (
            <>
              {" · "}
              {t("connect.pin")} <code className="connect-card__pin">{link.pin}</code>
            </>
          ) : null}
        </span>
        <span className="connect-card__hint">{t("connect.hint")}</span>
      </div>
      <button
        type="button"
        className="btn primary connect-card__btn"
        onClick={() => window.open(link.href, "_blank", "noreferrer")}
      >
        <ExternalLink size={14} strokeWidth={1.75} /> {t("connect.button")}
      </button>
    </div>
  );
}
