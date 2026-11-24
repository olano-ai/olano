// The completion half of ConnectCard: what happened after the user tapped
// Connect and went off to sign in on their phone.
//
// Before this, nothing happened. The Connect card promised "the connection
// completes here on its own after you sign in", the backend built a ✅ notice —
// and dropped it, because the dashboard chat is not a channel it could send on.
// The user's only way to find out was to ask the agent, which is exactly the
// question this card exists to answer before it is asked.
//
// The restart affordance is deliberately conditional. A flow that already
// applied itself (channel-connect restarts the agent, MCP OAuth hot-reloads its
// tools) reports that and offers no button — asking someone to authorize a
// restart that already ran is noise. Only `restart_needed` gets a button: a
// flow that writes config and cannot apply it, or one that tried and failed.

import { useState } from "react";
import { CheckCircle2, AlertTriangle, RotateCw } from "lucide-react";
import { api } from "../../core/api";
import { useAuth } from "../../core/auth";
import { useT } from "../../core/i18n";
import { STR } from "./strings";
import { cardText, resultSucceeded } from "./connectResultText";
import type { MagicLinkResult } from "./sessionStore";

type RestartState = "idle" | "running" | "done" | "failed";

export function ConnectResultCard({ result }: { result: MagicLinkResult }) {
  const t = useT(STR);
  const { user } = useAuth();
  const [state, setState] = useState<RestartState>("idle");

  const ok = resultSucceeded(result.status);
  const agent = result.targetAgent;
  // `is_admin`, NOT canAdminAgent: the auth middleware refuses every non-admin
  // on POST /api/agents/* (auth._member_forbidden), a per-agent owner included,
  // so offering the button to an owner would offer them a 403. That narrower
  // grant is a pre-existing gap in the restart route, not this card's to close.
  const canRestart = !!user?.is_admin;

  // A failed link applied nothing, so it never offers a restart whatever the
  // flow declares — the backend already forces `apply` to "none" there, and
  // this mirrors it rather than trusting one side alone.
  const needsRestart = ok && result.apply === "restart_needed" && !!agent;

  let note = "";
  if (ok && result.apply === "restarted") note = t("result.restarted", { agent });
  else if (ok && result.apply === "reloaded") note = t("result.reloaded", { agent });
  else if (needsRestart) {
    note = canRestart ? t("result.restartNeeded", { agent }) : t("result.restartAsk", { agent });
  }
  if (state === "done") note = t("result.restartDone", { agent });
  if (state === "failed") note = t("result.restartFailed", { agent });

  async function restart() {
    setState("running");
    try {
      await api.post(`/api/agents/${encodeURIComponent(agent)}/restart`);
      setState("done");
    } catch {
      setState("failed");
    }
  }

  return (
    <div className={`connect-result${ok ? "" : " connect-result--warn"}`}>
      <span className="connect-result__icon" aria-hidden>
        {ok ? <CheckCircle2 size={16} strokeWidth={2} /> : <AlertTriangle size={16} strokeWidth={2} />}
      </span>
      <div className="connect-result__text">
        <strong>{ok ? t("result.doneTitle") : t("result.failedTitle")}</strong>
        <span className="connect-result__body">{cardText(result.text)}</span>
        {!!note && <span className="connect-result__note">{note}</span>}
      </div>
      {needsRestart && canRestart && state !== "done" && (
        <button
          type="button"
          className="btn connect-result__btn"
          onClick={() => void restart()}
          disabled={state === "running"}
        >
          <RotateCw size={14} strokeWidth={1.75} />{" "}
          {state === "running" ? t("result.restarting") : t("result.restartBtn", { agent })}
        </button>
      )}
    </div>
  );
}
