import { useCallback } from "react";
import { useNavigate } from "react-router-dom";
import { chatPath } from "../../core/routeUrls";
import { useCurrentAgent } from "../../core/currentAgent";
import { stageChatHandoff } from "../chat/handoff";

// "Chat about this" / "Explore more" — every Cortex surface's route into the
// real chat view.
//
// Cortex used to answer the Chat button by rendering its own miniature
// transcript (a Pulse card grew one inline; the item drawer had a Chat tab),
// and answered Explore more by spawning a background worker whose result came
// back as a toast. Those threads were real all along — both streamed to
// /api/chat/stream on a deterministic id — so the conversation was already
// sitting in the chat rail, just unreachable from a 280px box with no
// attachments, no model picker, no approvals and no error remediation. Every
// action that asks the agent to do work now hands the same thread id to /chat
// instead, which is why an old Cortex discussion continues rather than
// starting over.
//
// The thread ids are load-bearing and must stay exactly as they were:
//   `pulse:<item id>`   — a Pulse card / self-check finding
//   `cortex:<item id>`  — an idea, brainstorm, prototype or project
// The runtime badges them as the `cortex` origin from these prefixes alone
// (core/thread_labels.py), which is what puts the violet CORTEX pill on the
// row; renaming one here silently un-labels every conversation it started.

/** Deterministic thread id for a Pulse card's discussion. */
export function pulseThreadId(itemId: string): string {
  return `pulse:${itemId}`;
}

/** Deterministic thread id for a Cortex item's discussion. */
export function cortexThreadId(itemId: string): string {
  return `cortex:${itemId}`;
}

export type OpenChat = (agent: string, threadId: string, draft?: string) => void;

/**
 * Navigate to the chat view on `threadId`, switching the global agent picker to
 * `agent` first — a Pulse card can belong to an agent the picker isn't on, and
 * thread ids are per-agent.
 *
 * `draft` is preloaded into the composer — never sent — and only if that
 * thread turns out to have no history, so returning to a discussion continues
 * it with the composer left alone.
 */
export function useOpenAgentChat(): OpenChat {
  const navigate = useNavigate();
  const { setAgent } = useCurrentAgent();
  return useCallback(
    (agent: string, threadId: string, draft?: string) => {
      stageChatHandoff({ agent, threadId, draft });
      // The URL names the agent, so the chat lands on it with no wait for the
      // picker; the selection only keeps the sidebar in step.
      setAgent(agent, { navigate: false });
      navigate(chatPath(agent, threadId));
    },
    [navigate, setAgent],
  );
}

// The draft builders live in handoffDraft.ts (pure, unit-tested under Node);
// re-exported so every Cortex surface imports its hand-off from one module.
export { cortexItemDraft, pulseDraft, type DraftIntent } from "./handoffDraft";
