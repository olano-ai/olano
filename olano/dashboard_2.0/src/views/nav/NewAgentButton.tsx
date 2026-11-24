// The sidebar's "New agent" button — the one door to creating an agent from
// the dashboard shell, sitting directly under the search box so the two
// things a person does most with the sidebar (find something, make an agent)
// share the top of it, the way the simple shell's rail lays them out.
//
// It used to live inside the bot list (BotNav) between the tabs and the
// PINNED heading. Pulling it out lets Layout place it above the list while
// the list keeps owning everything about arranging agents; the dialog, the
// ⌘N shortcut and the after-create hand-off (select the new agent, open a
// fresh chat) all travel with the button because they are the button.
//
// Gated exactly as before, and as the simple shell gates its own: admins
// always, members only when the deployment opened self-serve creation. The
// server re-checks, so this is a courtesy, never the guard.

import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../../core/auth";
import { useCapability } from "../../core/capabilities";
import { useCurrentAgent } from "../../core/currentAgent";
import { chatPath } from "../../core/routeUrls";
import { useT } from "../../core/i18n";
import { PlusIcon } from "../../ui/icons";
import { NewBotDialog } from "../agents/NewBotDialog";
import { chatStore } from "../chat/sessionStore";
import { STR } from "./strings";

export function NewAgentButton() {
  const t = useT(STR);
  const navigate = useNavigate();
  const { user } = useAuth();
  const { setAgent, reload } = useCurrentAgent();
  const selfServe = useCapability("self_serve_agents") === true;
  const canCreate = user?.is_admin === true || selfServe;
  const [open, setOpen] = useState(false);

  // ⌘N / Ctrl+N — new agent, exactly as the simple shell binds it. Only when
  // creation is allowed: a shortcut whose only outcome is a 403 is worse than
  // none. A dialog owns the keyboard while it is up.
  useEffect(() => {
    if (!canCreate) return;
    function onKey(e: KeyboardEvent) {
      if (!(e.metaKey || e.ctrlKey) || e.shiftKey || e.altKey) return;
      if (e.key.toLowerCase() !== "n" || e.repeat) return;
      if (document.querySelector('[role="dialog"], dialog[open]')) return;
      e.preventDefault();
      setOpen(true);
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [canCreate]);

  if (!canCreate) return null;

  function onCreated(name: string) {
    setOpen(false);
    reload();
    chatStore.newChat(name);
    setAgent(name, { navigate: false });
    navigate(chatPath(name));
  }

  return (
    <>
      <button
        type="button"
        className="btn primary sidebar-new-agent"
        onClick={() => setOpen(true)}
        title={t("bots.newAgentHelp")}
        aria-label={t("bots.newAgent")}
        data-testid="nav-new-agent"
      >
        <PlusIcon size={15} />
        <span className="nav-label">{t("bots.newAgent")}</span>
      </button>
      {open && <NewBotDialog onClose={() => setOpen(false)} onCreated={onCreated} />}
    </>
  );
}
