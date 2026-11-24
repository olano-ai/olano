// Which model a conversation runs on. Pure (no store, no React) so node's
// --test can pin the rules.
//
// The model picker in the chat header is PER CONVERSATION, never per agent:
// picking a model in one thread changes that thread only, a new chat always
// opens on the agent's configured model, and reopening an old thread continues
// on the model it last answered with — the runner stamps every assistant row
// with it (`runner._model_stamp`), and `GET /api/agents/{name}/threads/{id}`
// reports the newest stamp as `model` / `reasoning_effort`.

/** The subset of a loaded thread's payload this module reads. */
export interface ThreadModelFields {
  model?: string | null;
  reasoning_effort?: string | null;
}

/** The per-conversation override pair a session carries ("" = agent default). */
export interface SessionModel {
  model: string;
  reasoningEffort: string;
}

/**
 * The override to seed a freshly opened thread with. A thread recorded before
 * the stamp existed (or one that has not answered yet) restores nothing and
 * opens on the agent's default, exactly like a new chat.
 */
export function restoreModelFromThread(res: ThreadModelFields): SessionModel {
  return {
    model: (res.model || "").trim(),
    reasoningEffort: (res.reasoning_effort || "").trim(),
  };
}

/**
 * The model the header shows and the next turn is sent with. The session's
 * own pick wins; otherwise the agent's configured model; `streamModel` (what
 * the last streamed turn reported) only covers the moment before the agent
 * roster has loaded.
 */
export function effectiveChatModel(args: {
  sessionModel: string | undefined;
  agentModel: string | undefined;
  streamModel?: string;
}): string {
  return args.sessionModel || args.agentModel || args.streamModel || "";
}

/**
 * Whether a restored model can still be offered: it has to be one the catalog
 * lists for this box, or the agent's own configured model (always sendable,
 * catalogued or not). A model that is neither is dropped back to the default
 * rather than sent on every turn to fail — the caller tells the person why.
 */
export function restoredModelUsable(args: {
  model: string;
  agentModel: string | undefined;
  catalogValues: readonly string[];
}): boolean {
  if (!args.model) return true;
  if (args.model === args.agentModel) return true;
  return args.catalogValues.includes(args.model);
}
