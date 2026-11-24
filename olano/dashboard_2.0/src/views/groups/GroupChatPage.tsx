// The dashboard's page for one group chat — `/groups/:id`.
//
// It is the simple shell's group transcript (GroupThreadView) and the group's
// details panel (GroupInfoPanel), unchanged, dressed by simple.css. That file
// is the transcript's own stylesheet — its messenger bubbles are written as
// `.simple-shell .simple-msg` so they outrank the dashboard's `.msg` — so the
// page wraps the two in a `.simple-shell` that is told it is EMBEDDED: no
// full-window height, no rail column, no rounded frame, just the conversation
// column (and the details column beside it on a wide viewport) filling the
// dashboard's content area. One transcript implementation for both shells;
// the alternative was a second GroupThreadView that would drift from the
// first the day either one changed.
//
// There is no sidebar tab for this page. The rows under GROUP CHATS in the
// sidebar (views/nav/BotNav.tsx) and the chat header's "Add a bot" lead here.

import "../../styles/simple.css";
import { useEffect, useMemo } from "react";
import { Navigate, useNavigate, useParams } from "react-router-dom";
import { useCurrentAgent } from "../../core/currentAgent";
import { useT } from "../../core/i18n";
import { MOBILE_QUERY, useMediaQuery } from "../../core/useMediaQuery";
import { usePersistentState } from "../../core/usePersistentState";
import { GroupInfoPanel } from "./GroupInfoPanel";
import { GroupThreadView } from "./GroupThreadView";
import { setOpenGroup, useGroups, useGroupsEnabled, type GroupMessage, type PendingMessage } from "./groupsStore";
import { GROUPS_STR as STR } from "./strings";

// Stable empty fallbacks: a group with nothing yet must not churn identity on
// every render.
const EMPTY_MESSAGES: GroupMessage[] = [];
const EMPTY_PENDING: PendingMessage[] = [];

export function GroupChatPage() {
  const t = useT(STR);
  const { id: raw } = useParams();
  const id = Number(raw);
  const navigate = useNavigate();
  const enabled = useGroupsEnabled();
  const groups = useGroups(enabled === true);
  const { agents } = useCurrentAgent();
  const isMobile = useMediaQuery(MOBILE_QUERY);
  // Whether the details column is open — remembered across groups and
  // navigations, the way the simple shell remembers its profile column.
  const [infoOpen, setInfoOpen] = usePersistentState<boolean>("groups.infoOpen", false);

  const agentsByName = useMemo(() => new Map(agents.map((a) => [a.name, a] as const)), [agents]);
  const group = useMemo(() => groups.groups.find((g) => g.id === id), [groups.groups, id]);

  // Tell the store which transcript is on screen, so its SSE pokes refetch
  // this group's messages in full rather than a one-line preview.
  useEffect(() => {
    if (!Number.isFinite(id)) return;
    setOpenGroup(id);
    return () => setOpenGroup(null);
  }, [id]);

  // The feature is off, or the id is junk: nothing to show. `undefined` (still
  // loading the capability) must not redirect — the group would flash away on
  // every reload.
  if (enabled === false || !Number.isFinite(id)) return <Navigate to="/overview" replace />;

  if (!group) {
    // Deleted, or somebody else's (the list filters server-side): once the
    // list has loaded and it is not there, it is not coming.
    if (groups.status === "ready" || groups.status === "error") {
      return <Navigate to="/overview" replace />;
    }
    return (
      <div className="page" style={{ display: "grid", placeItems: "center", height: "100%" }}>
        <div className="spinner" aria-label={t("groups.loading")} />
      </div>
    );
  }

  return (
    <div
      className={`simple-shell is-embedded${infoOpen && !isMobile ? " with-profile" : ""}`}
      data-testid="group-chat-page"
    >
      <GroupThreadView
        group={group}
        messages={groups.messages[id] ?? EMPTY_MESSAGES}
        pending={groups.pending[id] ?? EMPTY_PENDING}
        awaiting={groups.awaiting[id]}
        streaming={groups.streaming[id]}
        activity={groups.activity[id]}
        agentsByName={agentsByName}
        // The transcript's own "open the bot list" button is the simple
        // shell's drawer handle; the dashboard's topbar hamburger is that
        // here, so the header never draws a second one.
        isMobile={false}
        infoOpen={infoOpen}
        onOpenRail={() => {}}
        onToggleInfo={() => setInfoOpen((o) => !o)}
      />
      {infoOpen && (
        <GroupInfoPanel
          group={group}
          agents={agents}
          asOverlay={isMobile}
          onClose={() => setInfoOpen(false)}
          onDeleted={() => navigate("/overview")}
        />
      )}
    </div>
  );
}
