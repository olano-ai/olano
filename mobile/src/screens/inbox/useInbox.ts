/**
 * The Inbox's lists and the count on its tab: approvals waiting on a person
 * (an agent's paused action, or a Pulse card in the approval lane) and the
 * dashboard's notifications. Issues have their own hook (useIssues.ts); Pulse
 * updates live in Cortex (screens/cortex/usePulse.ts).
 */

import type { HitlPending, NotificationList, PulseItem } from "../../net/types.ts";
import { inboxSections, type AppLayout } from "../../state/appLayout.ts";
import { useResource } from "../../state/resources.ts";
import { currentApi } from "../../state/session.ts";

const hitlLoader = async () => (await currentApi().hitlPending()).pending ?? [];
const pulseLoader = async () => (await currentApi().pulse("approval")).items ?? [];
const notesLoader = () => currentApi().notifications();

export function useHitl() {
  return useResource<HitlPending[]>("approvals:hitl", hitlLoader, 30000);
}

export function usePulseApprovals() {
  return useResource<PulseItem[]>("approvals:pulse", pulseLoader, 30000);
}

export function useNotifications() {
  return useResource<NotificationList>("notifications", notesLoader, 60000);
}

/** What the Inbox tab's badge counts: things waiting on this person, in the sections this app shows. */
export function useInboxCount(layout: AppLayout): number {
  const sections = inboxSections(layout);
  const approvals = sections.includes("approvals");
  const alerts = sections.includes("alerts");
  // A hidden section is not polled either: `null` keys load nothing.
  const [hitl] = useResource<HitlPending[]>(approvals ? "approvals:hitl" : null, hitlLoader, 30000);
  const [pulse] = useResource<PulseItem[]>(approvals ? "approvals:pulse" : null, pulseLoader, 30000);
  const [notes] = useResource<NotificationList>(alerts ? "notifications" : null, notesLoader, 60000);
  return (approvals ? (hitl.data?.length ?? 0) + (pulse.data?.length ?? 0) : 0) + (alerts ? (notes.data?.unread ?? 0) : 0);
}
