// A fix-it call-to-action rendered under a chat message that ended in an
// actionable error (e.g. an expired provider sign-in). It turns the structured
// `remediation.action` from the backend into a one-click path:
//   - reconnect_provider (OAuth/device, e.g. OpenAI Codex) → open the existing
//     CodexConnectDialog inline, right where the error happened;
//   - open_url → open it, except a mailto: which navigates in place (window.open
//     on a mailto: hands the mail client the request and leaves a blank tab);
//   - new_thread → start a fresh conversation with this agent, in place;
//   - set_api_key / anything else → jump to the Providers page.
//
// `new_thread` backs `message_format_unsupported`: a thread whose checkpoint
// holds a block the current model cannot read is stuck for good, so the only
// fix is a new conversation. It is the one action with no credential behind it,
// which makes it the one action a member can take for themselves — so it must
// skip the "ask your admin" branch below, and skip the simple-shell relabel
// (nothing here opens the dashboard; `chatStore` is the same singleton in both
// shells, so the same click works in either).
//
// This component is shared by BOTH shells, and the last branch is the one that
// differs between them. The simple shell mounts above the router — there is no
// route table — so a bare navigate("/providers") there changes the URL and
// draws nothing: the button silently does nothing, which for the person who
// clicked it is indistinguishable from the product being broken. So in simple
// mode the same intent is expressed with the tools that exist there:
//   - an admin is offered the full dashboard (switch shells + land on the
//     right page — the router already holds the URL when the dashboard
//     mounts, so it opens directly on Providers);
//   - a member gets a sentence instead of a button. They cannot set keys from
//     anywhere — that is their operator's job — and a button that goes
//     somewhere they will be refused is worse than saying so plainly.

import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { KeyRound, Mail, MessageSquarePlus, Plug } from "lucide-react";
import { CodexConnectDialog } from "../providers/CodexConnectDialog";
import type { Provider } from "../providers/types";
import type { ErrorRemediation as Remediation } from "../../core/types";
import { useAuth } from "../../core/auth";
import { useT } from "../../core/i18n";
import { STR } from "./strings";

const CODEX_KEY = "openai-codex";

export function ErrorRemediation({
  remediation,
  onNewThread,
}: {
  remediation: Remediation;
  /**
   * Start a fresh conversation with the agent this error belongs to. Supplied
   * by the two chat shells; deliberately absent on the Cortex surfaces
   * (AutonomyPanel, ThinkDrawer), which render the same errors for background
   * runs that have no conversation to restart. When it is absent the component
   * says what to do in words rather than drawing a button that cannot work —
   * the same rule the member/credential branch below follows.
   */
  onNewThread?: () => void;
}) {
  const navigate = useNavigate();
  const t = useT(STR);
  const { user, uiMode, switchUiMode } = useAuth();
  const [codexOpen, setCodexOpen] = useState(false);
  const simple = uiMode === "simple";

  const action = remediation.action;
  if (!action) return null;

  const isNewThread = action.type === "new_thread";
  if (isNewThread && !onNewThread) {
    return (
      <p className="muted" style={{ marginTop: 8, fontSize: 12.5 }}>
        {t("error.newChatHint")}
      </p>
    );
  }

  let label = action.label || remediation.summary || t("error.fix");
  if (isNewThread) {
    // Always the localized short label: `summary` is a full sentence naming
    // two different surfaces, which is prose, not a button.
    label = t("error.newChat");
  } else if (simple && action.type !== "open_url" && !(action.type === "reconnect_provider" && action.provider === CODEX_KEY)) {
    // The destination changed (full dashboard, not an in-place fix), so the
    // label must too — a button that says "Set API key" but swaps the whole
    // screen would violate the never-surprise rule the shell switch follows.
    label = t("error.openDashboard");
  }
  const isCodexReconnect = action.type === "reconnect_provider" && action.provider === CODEX_KEY;
  const isMail = action.type === "open_url" && !!action.url && action.url.startsWith("mailto:");
  const isExternal = action.type === "open_url" && !!action.url;

  // A minimal Provider for the dialog — it only reads `.name` for display.
  const codexProvider: Provider = {
    key: CODEX_KEY,
    name: t("error.codexName"),
    package: "",
    env_var: null,
    available: true,
    env_set: false,
    models: [],
    notes: "",
    auth: null,
  };

  function onClick() {
    if (isNewThread) {
      // Guarded above — this branch is only reachable with a handler.
      onNewThread?.();
    } else if (isCodexReconnect) {
      setCodexOpen(true);
    } else if (isExternal && action?.url) {
      if (action.url.startsWith("mailto:")) {
        window.location.href = action.url;
      } else {
        window.open(action.url, "_blank", "noreferrer");
      }
    } else if (simple) {
      // Same intent, expressed through the shell switch: set the URL first so
      // the dashboard mounts already showing Providers, then swap shells.
      navigate("/providers");
      void switchUiMode("advanced");
    } else {
      // set_api_key / open_providers / unknown → the Providers page is where
      // every credential is managed.
      navigate("/providers");
    }
  }

  // The credential branch, seen by a member in the simple view: no button.
  // See the header note — they cannot act on it from anywhere, so the honest
  // rendering is the explanation, not a control. Starting a new chat is not a
  // credential and needs no admin, so it is excluded: a member who hit a stuck
  // thread can fix it themselves, and telling them to ask an admin would be
  // both wrong and a dead end.
  const isCredential = !isCodexReconnect && !isExternal && !isNewThread;
  if (simple && isCredential && !user?.is_admin) {
    return (
      <p className="muted" style={{ marginTop: 8, fontSize: 12.5 }}>
        {t("error.askAdmin")}
      </p>
    );
  }

  return (
    <div style={{ display: "flex", gap: 8, marginTop: 8, flexWrap: "wrap", alignItems: "center" }}>
      <button className="btn primary" style={{ padding: "4px 11px", fontSize: 12, gap: 6 }} onClick={onClick}>
        {isNewThread ? (
          <MessageSquarePlus size={13} strokeWidth={1.75} />
        ) : isCodexReconnect ? (
          <Plug size={13} strokeWidth={1.75} />
        ) : isMail ? (
          <Mail size={13} strokeWidth={1.75} />
        ) : (
          <KeyRound size={13} strokeWidth={1.75} />
        )}
        {label}
      </button>
      {codexOpen && (
        <CodexConnectDialog
          provider={codexProvider}
          onClose={() => setCodexOpen(false)}
          onConnected={() => setCodexOpen(false)}
        />
      )}
    </div>
  );
}
