// A small "Langfuse" link beside a reply's Copy button, or on a thread header.
// Renders nothing when there is no URL (not an admin, not traced, no project).

import { ExternalLink } from "lucide-react";

import { useT } from "../../core/i18n";
import { STR } from "./strings";

export function LangfuseLink({ href, kind }: { href: string; kind: "trace" | "session" }) {
  const t = useT(STR);
  if (!href) return null;
  const label = kind === "trace" ? t("ob.link.trace") : t("ob.link.session");
  const title = kind === "trace" ? t("ob.link.traceTitle") : t("ob.link.sessionTitle");
  return (
    <a
      className="msg-edit-btn"
      data-testid={kind === "trace" ? "langfuse-trace-link" : "langfuse-session-link"}
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      title={title}
      aria-label={title}
      style={{ textDecoration: "none", display: "inline-flex", alignItems: "center", gap: 4 }}
    >
      <ExternalLink size={13} /> {label}
    </a>
  );
}
