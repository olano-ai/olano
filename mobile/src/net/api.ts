/**
 * The runtime API, typed, over whichever transport the platform has.
 *
 * Every call goes to the one deployment the app is connected to. A 401 with
 * `auth_required` means the session is gone (expired, signed out elsewhere,
 * the account removed): `onSignedOut` hears about it once per response and
 * the session store takes the app back to the sign-in screen.
 */

import { SseParser, jsonFrames } from "./sse.ts";
import { getTransport, NetworkError, type OpenStream } from "./transport.ts";
import type {
  CreditsBalance,
  CreditsDisplay,
  FileListing,
  FileSearch,
  ServiceUsage,
  Issue,
  IssueEvent,
  Agent,
  CascadeState,
  CortexItemDetail,
  GroupMessage,
  GroupSummary,
  CortexItems,
  HitlPending,
  Me,
  NotificationList,
  PulseItem,
  PushStatus,
  Status,
  ThreadDetail,
  ThreadPage,
  ThreadSummary,
} from "./types.ts";
import type { BrowserStatus, LiveRecording, SavedBrowserFile } from "../browser/model.ts";
import type { ProvidersResponse } from "../chat/models.ts";
import { normalizeThreadPage, type ThreadPageRequest } from "../chat/threadPages.ts";
import type { ChatFrame, GroupStreamFrame, InlineForm, ScheduledTask, TaskRun } from "../shared/dashboard.ts";

/** A response the runtime answered with an error status. */
export class ApiError extends Error {
  readonly status: number;
  /** `detail` when it is a string, or `detail.code` when it is an object. */
  readonly code: string;
  readonly detail: unknown;
  constructor(status: number, detail: unknown) {
    super(messageFor(status, detail));
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
    this.code =
      typeof detail === "string"
        ? detail
        : detail && typeof detail === "object" && "code" in detail
          ? String((detail as { code: unknown }).code)
          : "";
  }
}

function messageFor(status: number, detail: unknown): string {
  if (detail && typeof detail === "object" && "message" in detail) {
    const message = String((detail as { message: unknown }).message ?? "");
    if (message) return message;
  }
  if (typeof detail === "string" && detail && !/^[a-z_]+$/.test(detail)) return detail;
  switch (status) {
    case 401:
      return "Your session has ended. Sign in again.";
    case 403:
      return "Your account is not allowed to do that.";
    case 404:
      return "That is no longer there.";
    case 429:
      return "Too many requests. Wait a moment and try again.";
    case 502:
    case 503:
    case 504:
      return "The deployment is not responding right now. It may be restarting.";
    default:
      return `The deployment answered with an error (${status}).`;
  }
}

type Listener = () => void;
const signedOutListeners = new Set<Listener>();

export function onSignedOut(listener: Listener): () => void {
  signedOutListeners.add(listener);
  return () => signedOutListeners.delete(listener);
}

function parseJson(text: string): unknown {
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

const JSON_HEADERS = { Accept: "application/json", "Content-Type": "application/json" };

export class Api {
  readonly origin: string;

  constructor(origin: string) {
    this.origin = origin.replace(/\/+$/, "");
  }

  url(path: string): string {
    return `${this.origin}${path}`;
  }

  async call<T>(method: string, path: string, body?: unknown, timeoutMs?: number): Promise<T> {
    const response = await getTransport().request(this.url(path), {
      method,
      headers: body === undefined ? { Accept: "application/json" } : JSON_HEADERS,
      body: body === undefined ? undefined : JSON.stringify(body),
      timeoutMs,
    });
    const parsed = parseJson(response.body);
    if (response.status >= 200 && response.status < 300) return parsed as T;
    const detail = parsed && typeof parsed === "object" && "detail" in parsed ? (parsed as { detail: unknown }).detail : parsed;
    const error = new ApiError(response.status, detail);
    if (response.status === 401 && (error.code === "auth_required" || error.code === "")) {
      for (const listener of signedOutListeners) listener();
    }
    throw error;
  }

  get = <T>(path: string) => this.call<T>("GET", path);
  post = <T>(path: string, body: unknown = {}) => this.call<T>("POST", path, body);
  patch = <T>(path: string, body: unknown) => this.call<T>("PATCH", path, body);
  del = <T>(path: string) => this.call<T>("DELETE", path);

  // ── auth ──────────────────────────────────────────────────────────────────

  status = () => this.get<Status>("/api/status");
  me = () => this.get<Me>("/api/auth/me");
  login = (email: string, password: string) => this.post<{ user: Me["user"] }>("/api/auth/login", { email, password });
  /**
   * Reports a reply (a thumbs-down with the reason), which the deployment's
   * administrators see in Insights: the in-app way to flag what an agent
   * wrote (App Review 1.2, Google Play's AI-generated content policy).
   */
  reportReply = (agent: string, threadId: string, turnId: string, reason: string) =>
    this.post<unknown>("/api/chat/feedback", { agent, thread_id: threadId, turn_id: turnId, value: -1, comment: `Reported in the app: ${reason}` });
  /** The demo deployment's passwordless sign-in (olano/runtime/demo_access.py); 404 everywhere else. */
  demoSignIn = () => this.post<{ user: Me["user"] }>("/api/auth/demo");
  logout = () => this.post<{ ok: boolean }>("/api/auth/logout");

  /** The models this deployment offers, for a conversation's model picker. */
  providers = () => this.get<ProvidersResponse>("/api/catalog/providers");

  /** This person's stored preferences (the dashboard's per-user blob). */
  prefs = () => this.get<{ prefs: Record<string, unknown> }>("/api/auth/me/prefs");
  /** Merges `prefs` into the stored blob, one whole value per key. */
  savePrefs = (prefs: Record<string, unknown>) => this.call<{ prefs: Record<string, unknown> }>("PUT", "/api/auth/me/prefs", { prefs });

  // ── agents and threads ────────────────────────────────────────────────────

  agents = () => this.get<Agent[]>("/api/agents");
  /** The agent's browser: whether one is open and whether it can be watched live. */
  browserStatus = (agent: string) => this.get<BrowserStatus>(`/api/agents/${encodeURIComponent(agent)}/browser`);
  /** Record the agent's browser to a video in its workspace (`recordings/`), or stop and get the file. */
  browserRecord = (agent: string, action: "start" | "stop") =>
    this.post<{ recording: LiveRecording | null; file: SavedBrowserFile | null }>(`/api/agents/${encodeURIComponent(agent)}/browser/recording`, { action });
  /** Save the page the agent's browser shows as a PNG under its `screenshots/`. */
  browserPicture = (agent: string) => this.post<{ file: SavedBrowserFile }>(`/api/agents/${encodeURIComponent(agent)}/browser/picture`);
  restartAgent = (agent: string) => this.post<{ ok?: boolean }>(`/api/agents/${encodeURIComponent(agent)}/restart`);
  /**
   * The form behind an inline secret card (a `secret-entry` / `provider-key`
   * magic link drawn in the conversation): title, fields, live status. A
   * runtime from before the card answers 404, and the card shows the link.
   */
  magicLinkForm = (id: string) => this.get<InlineForm>(`/api/magic-links/links/${encodeURIComponent(id)}/form`);
  /** Submit that card: the value goes to the vault through the link, never as a message. */
  magicLinkSubmit = (id: string, pin: string, fields: Record<string, string>) =>
    this.post<InlineForm>(`/api/magic-links/links/${encodeURIComponent(id)}/submit`, { pin, fields });
  /** Boots a stopped agent (admins); 4xx with the boot error when it cannot start. */
  startAgent = (agent: string) => this.post<{ ok?: boolean }>(`/api/agents/${encodeURIComponent(agent)}/start`);
  /** Stops an agent and keeps it stopped across restarts until it is started again (admins). */
  stopAgent = (agent: string) => this.post<{ ok?: boolean }>(`/api/agents/${encodeURIComponent(agent)}/stop`);
  /** Every thread of the agent, as a list (the dashboard's read; the app pages instead). */
  threads = (agent: string) => this.get<ThreadSummary[]>(`/api/agents/${encodeURIComponent(agent)}/threads`);
  /**
   * One page of the agent's threads, newest first: `limit` of them, those
   * below `before` (the cursor the previous page returned), and with
   * `kind: "conversation"` only the conversations a person had. A runtime from
   * before paging answers with every thread; `normalizeThreadPage` makes that
   * read as one page holding them all.
   */
  threadPage = async (agent: string, request: ThreadPageRequest): Promise<ThreadPage> => {
    const query = new URLSearchParams({ limit: String(request.limit) });
    if (request.before) query.set("before", request.before);
    if (request.kind) query.set("kind", request.kind);
    const answer = await this.get<ThreadPage | ThreadSummary[]>(`/api/agents/${encodeURIComponent(agent)}/threads?${query.toString()}`);
    return normalizeThreadPage(answer, request);
  };
  /**
   * The agent's threads whose messages hold `q`, newest first, each with a
   * `match` snippet. One request reads a bounded stretch of history; with
   * `has_more` the next one continues from `before`. 404 on a runtime from
   * before search.
   */
  searchThreads = (agent: string, q: string, request: { limit?: number; before?: string } = {}) => {
    const query = new URLSearchParams({ q });
    if (request.limit) query.set("limit", String(request.limit));
    if (request.before) query.set("before", request.before);
    return this.get<ThreadPage>(`/api/agents/${encodeURIComponent(agent)}/threads/search?${query.toString()}`);
  };
  /**
   * A conversation. With `limit`, the newest page of it (or the one before
   * `before`), rounded out to whole turns; `toolOutput: false` leaves out what
   * each tool step returned, which the app never draws. A runtime that
   * predates either ignores it and sends the whole transcript.
   */
  thread = (agent: string, id: string, page?: { limit: number; before?: number; toolOutput?: boolean }) => {
    const query = new URLSearchParams();
    if (page) {
      query.set("limit", String(page.limit));
      if (page.before !== undefined) query.set("before", String(page.before));
      if (page.toolOutput === false) query.set("tool_results", "status");
    }
    const qs = query.toString();
    return this.get<ThreadDetail>(`/api/agents/${encodeURIComponent(agent)}/threads/${encodeURIComponent(id)}${qs ? `?${qs}` : ""}`);
  };
  renameThread = (agent: string, id: string, name: string) =>
    this.patch<{ ok: boolean }>(`/api/agents/${encodeURIComponent(agent)}/threads/${encodeURIComponent(id)}`, { name });
  /** Summarises a conversation's history so it takes less of the agent's memory. */
  compactThread = (agent: string, id: string) =>
    this.post<{ ok: boolean; compacted: boolean; context_tokens?: number }>(
      `/api/agents/${encodeURIComponent(agent)}/threads/${encodeURIComponent(id)}/compact`,
      {},
    );
  deleteThread = (agent: string, id: string) =>
    this.del<{ ok: boolean }>(`/api/agents/${encodeURIComponent(agent)}/threads/${encodeURIComponent(id)}`);

  /** The avatar as a data: URL (images need the session cookie, so <img src> cannot fetch them). */
  async image(path: string): Promise<string | null> {
    const response = await getTransport().request(this.url(path), { responseType: "base64", timeoutMs: 20000 });
    if (response.status !== 200 || !response.body) return null;
    const type = (response.headers["content-type"] || "image/png").split(";")[0].trim();
    if (!/^(image|audio|video)\//.test(type)) return null;
    return `data:${type};base64,${response.body}`;
  }

  /** Any file from the deployment (a path like /api/files-raw/...), as base64. */
  async file(path: string): Promise<{ type: string; base64: string }> {
    const response = await getTransport().request(this.url(path), { responseType: "base64", timeoutMs: 60000 });
    if (response.status !== 200) throw new ApiError(response.status, "The file could not be opened.");
    const type = (response.headers["content-type"] || "application/octet-stream").split(";")[0].trim();
    return { type, base64: response.body || "" };
  }

  /**
   * Words read aloud in the deployment's voice (`POST /api/speech/speak`,
   * text-to-speech set up in Config > Speech), as base64 audio. Throws
   * ApiError 409 `tts_disabled` / `tts_not_configured` when it has no voice,
   * and 404 on a deployment from before spoken replies.
   */
  async speak(agent: string, text: string): Promise<{ type: string; base64: string }> {
    const response = await getTransport().request(this.url("/api/speech/speak"), {
      method: "POST",
      headers: { Accept: "audio/*, application/json", "Content-Type": "application/json" },
      body: JSON.stringify({ agent, text }),
      responseType: "base64",
      timeoutMs: 90000,
    });
    const type = (response.headers["content-type"] || "").split(";")[0].trim();
    if (response.status === 200 && type.startsWith("audio/") && response.body) return { type, base64: response.body };
    let detail: unknown = null;
    try {
      const parsed = parseJson(atob(response.body || ""));
      detail = parsed && typeof parsed === "object" && "detail" in parsed ? (parsed as { detail: unknown }).detail : parsed;
    } catch {
      // Not JSON: the status says enough.
    }
    throw new ApiError(response.status === 200 ? 502 : response.status, detail);
  }

  // ── chat ──────────────────────────────────────────────────────────────────

  /** A message for the turn running on a thread, read at the agent's next step. */
  steer = (agent: string, threadId: string, message: string, turnId?: string, steerId?: string) =>
    this.post<{ accepted: boolean }>("/api/chat/steer", {
      agent,
      thread_id: threadId,
      message,
      ...(turnId ? { turn_id: turnId } : {}),
      ...(steerId ? { steer_id: steerId } : {}),
    });
  /** Withdraws a steered message the agent has not read yet. */
  cancelSteer = (agent: string, threadId: string, steerId: string) =>
    this.post<{ cancelled: boolean }>("/api/chat/steer/cancel", { agent, thread_id: threadId, steer_id: steerId });
  stopRun = (agent: string, threadId: string, runId?: string) =>
    this.post<{ ok: boolean; stopped: boolean }>("/api/chat/stop", { agent, thread_id: threadId, run_id: runId });
  answerApproval = (threadId: string, decisions: { type: "approve" | "reject" }[]) =>
    this.post<{ ok: boolean }>("/api/chat/approval", { thread_id: threadId, decisions });

  /**
   * Opens a chat stream: `POST /api/chat/stream` for a new message, or
   * `GET /api/chat/attach` to rejoin a turn that is still running. Frames
   * reach `onFrame` in order. Rejects with ApiError when the runtime refuses
   * before streaming (409 run_in_progress, 404, 422...).
   */
  async chatStream(
    request: { kind: "send"; body: Record<string, unknown> } | { kind: "attach"; agent: string; threadId: string; cursor: number; runId?: string },
    onFrame: (frame: ChatFrame) => void,
  ): Promise<OpenStream> {
    let path: string;
    let method: string;
    let body: string | undefined;
    if (request.kind === "send") {
      path = "/api/chat/stream";
      method = "POST";
      body = JSON.stringify(request.body);
    } else {
      const query = new URLSearchParams({
        agent: request.agent,
        thread_id: request.threadId,
        cursor: String(request.cursor),
      });
      if (request.runId) query.set("run_id", request.runId);
      path = `/api/chat/attach?${query.toString()}`;
      method = "GET";
    }
    return this.openFrames(path, method, body, onFrame);
  }

  /** Opens a stream of JSON server-sent events; an error status rejects with its ApiError. */
  private async openFrames<F>(path: string, method: string, body: string | undefined, onFrame: (frame: F) => void): Promise<OpenStream> {
    const parser = new SseParser();
    const deliver = (text: string) => {
      for (const frame of jsonFrames<F>(parser.push(text))) onFrame(frame);
    };
    // Text the stream delivers before we know the status is held back: an
    // error status's body is JSON, not frames.
    let pending = "";
    let open = false;
    const stream = await getTransport().stream(
      this.url(path),
      { method, headers: { ...JSON_HEADERS, Accept: "text/event-stream" }, body },
      (text) => {
        if (open) deliver(text);
        else pending += text;
      },
    );
    if (stream.status >= 200 && stream.status < 300) {
      open = true;
      if (pending) deliver(pending);
      pending = "";
      stream.done.then(() => {
        for (const frame of jsonFrames<F>(parser.flush())) onFrame(frame);
      });
      return stream;
    }
    await stream.done;
    const parsed = parseJson(pending);
    const detail = parsed && typeof parsed === "object" && "detail" in parsed ? (parsed as { detail: unknown }).detail : parsed;
    const error = new ApiError(stream.status, detail);
    if (stream.status === 401) for (const listener of signedOutListeners) listener();
    throw error;
  }

  /**
   * The runtime's event stream (`GET /api/stream`): `{type, data, ts}` frames
   * for thread updates, notifications and approvals. Resolves once it opens.
   */
  async events(onEvent: (event: { type: string; data?: Record<string, unknown> }) => void): Promise<OpenStream> {
    const parser = new SseParser();
    const stream = await getTransport().stream(this.url("/api/stream"), { headers: { Accept: "text/event-stream" } }, (text) => {
      for (const frame of jsonFrames<{ type: string; data?: Record<string, unknown> }>(parser.push(text))) onEvent(frame);
    });
    if (stream.status !== 200) {
      stream.cancel();
      throw new ApiError(stream.status, null);
    }
    return stream;
  }

  // ── group chats ───────────────────────────────────────────────────────────

  groups = () => this.get<{ groups: GroupSummary[] }>("/api/groups").then((r) => r.groups ?? []);
  /**
   * Turns a conversation into a group: `agent` leads it, `members` join, and
   * the conversation so far is copied in so nobody has to repeat it.
   */
  groupFromThread = (agent: string, threadId: string, members: string[]) =>
    this.post<{ group: GroupSummary }>("/api/groups/from-thread", { agent, thread_id: threadId, members, carry_over: true }).then(
      (r) => r.group,
    );
  createGroup = (name: string, members: string[]) =>
    this.post<{ group: GroupSummary }>("/api/groups", { name, members }).then((r) => r.group);
  renameGroup = (id: number, name: string) => this.patch<{ group: GroupSummary }>(`/api/groups/${id}`, { name }).then((r) => r.group);
  deleteGroup = (id: number) => this.del<{ ok: boolean }>(`/api/groups/${id}`);
  addGroupMember = (id: number, agent: string) =>
    this.post<{ group: GroupSummary }>(`/api/groups/${id}/members`, { agent }).then((r) => r.group);
  removeGroupMember = (id: number, agent: string) =>
    this.del<{ group: GroupSummary }>(`/api/groups/${id}/members/${encodeURIComponent(agent)}`).then((r) => r.group);
  groupMessages = (id: number, limit = 200) =>
    this.get<{ group: GroupSummary; messages: GroupMessage[] }>(`/api/groups/${id}/messages?limit=${limit}`);
  groupCascade = (id: number) => this.get<{ cascade: CascadeState }>(`/api/groups/${id}/cascade`).then((r) => r.cascade);
  stopGroupCascade = (id: number) => this.post<{ ok: boolean; stopped: boolean }>(`/api/groups/${id}/stop`);

  /**
   * Posts to a group and follows the replies (`POST .../messages/stream`), or
   * rejoins a turn still running (`GET .../messages/attach?cursor=N`). Rejects
   * with ApiError when the runtime refuses before streaming.
   */
  async groupStream(
    request: { kind: "send"; id: number; text: string; to?: string } | { kind: "attach"; id: number; cursor: number },
    onFrame: (frame: GroupStreamFrame<GroupMessage> & { seq?: number }) => void,
  ): Promise<OpenStream> {
    const send = request.kind === "send";
    return this.openFrames(
      send ? `/api/groups/${request.id}/messages/stream` : `/api/groups/${request.id}/messages/attach?cursor=${request.cursor}`,
      send ? "POST" : "GET",
      send ? JSON.stringify({ text: request.text, ...(request.to ? { to: request.to } : {}) }) : undefined,
      onFrame,
    );
  }

  // ── cortex, pulse, approvals, notifications ───────────────────────────────

  cortexItems = (agent: string) =>
    this.get<CortexItems>(`/api/cortex/items?agent=${encodeURIComponent(agent)}`);
  cortexItem = (agent: string, id: string) =>
    this.get<CortexItemDetail>(`/api/cortex/items/${encodeURIComponent(id)}?agent=${encodeURIComponent(agent)}`);
  promoteItem = (agent: string, id: string, target = "next") =>
    this.post<{ ok: boolean }>(`/api/cortex/items/${encodeURIComponent(id)}/promote`, { agent, target, background: true });
  discardItem = (agent: string, id: string) =>
    this.post<{ ok: boolean }>(`/api/cortex/items/${encodeURIComponent(id)}/discard`, { agent });

  pulse = (lane: "pulse" | "approval") => this.get<{ items: PulseItem[] }>(`/api/pulse?lane=${lane}&status=pending`);
  /** `undo` reverts work an agent already applied (a card whose action is `restore_backup`). */
  pulseAct = (action: "approve" | "reject" | "defer" | "undo", item: PulseItem) =>
    this.post<{ ok: boolean; message?: string; job_id?: string }>(`/api/pulse/${action}`, { item_id: item.id, agent: item.agent });

  hitlPending = () => this.get<{ pending: HitlPending[] }>("/api/hitl/pending");
  hitlResolve = (id: string, approved: boolean) => this.post<{ ok: boolean }>("/api/hitl/resolve", { id, approved });

  notifications = () => this.get<NotificationList>("/api/notifications?include_read=true&limit=50");
  markRead = (ids?: string[]) => this.post<{ ok: boolean }>("/api/notifications/read", ids ? { ids } : {});
  dismissNotification = (id: string) => this.del<{ ok: boolean }>(`/api/notifications/${encodeURIComponent(id)}`);

  // ── issues ────────────────────────────────────────────────────────────────

  /** Issues on the deployment's tracker, most recently updated first. Every signed-in person sees all of them. */
  issues = (state: "open" | "closed" | "all", assignee = "") =>
    this.get<{ issues: Issue[] }>(
      `/api/issues?state=${state}&sort=updated&limit=200${assignee ? `&assignee=${encodeURIComponent(assignee)}` : ""}`,
    );
  issue = (id: number) => this.get<{ issue: Issue; events: IssueEvent[] }>(`/api/issues/${id}`);
  commentIssue = (id: number, body: string) => this.post<{ issue: Issue }>(`/api/issues/${id}/comment`, { body });
  closeIssue = (id: number) => this.post<{ issue: Issue }>(`/api/issues/${id}/close`, {});
  reopenIssue = (id: number) => this.post<{ issue: Issue }>(`/api/issues/${id}/reopen`, {});
  /** `assignee`: `ai-<agent>` hands it to an agent (its issue poll picks it up), `human-<user>` to a person. */
  assignIssue = (id: number, assignee: string) => this.post<{ issue: Issue }>(`/api/issues/${id}/assign`, { assignee });

  /** Renames skill folders to match their skills' names (admins; `apply: false` only reports). */
  repairSkillNames = (agent: string | null, apply: boolean) =>
    this.post<{ count: number; skipped: { name: string; skipped: string }[] }>("/api/skills/repair-names", { agent, apply });

  // ── scheduled tasks ───────────────────────────────────────────────────────

  scheduledTasks = () => this.get<ScheduledTask[]>("/api/scheduler/tasks");
  createTask = (body: {
    name: string;
    prompt: string;
    agent_name: string;
    schedule: string;
    run_at: string;
    timezone: string;
  }) => this.post<{ result: string; id: string }>("/api/scheduler/tasks", body);
  updateTask = (
    id: string,
    body: Partial<{ name: string; prompt: string; schedule: string; run_at: string; timezone: string; enabled: boolean }>,
  ) => this.call<{ ok: boolean }>("PUT", `/api/scheduler/tasks/${encodeURIComponent(id)}`, body);
  deleteTask = (id: string) => this.del<{ ok: boolean }>(`/api/scheduler/tasks/${encodeURIComponent(id)}`);
  /** Runs the task now, whatever its schedule; 409 when the deployment's scheduler is not running. */
  runTask = (id: string) => this.post<{ ok: boolean; message?: string }>(`/api/scheduler/tasks/${encodeURIComponent(id)}/run`);
  taskHistory = (id: string, limit = 20) =>
    this.get<TaskRun[]>(`/api/scheduler/history/${encodeURIComponent(id)}?limit=${limit}`);

  // ── credits (deployments with an Olano account; `available: false` otherwise) ──

  creditsDisplay = () => this.get<CreditsDisplay>("/api/credits");
  creditBalance = () => this.get<CreditsBalance>("/api/olano/credits");
  serviceUsage = () => this.get<ServiceUsage>("/api/olano/services/usage");

  // ── an agent's files ──────────────────────────────────────────────────────

  /** One folder of the agent's workspace ("" is its top), folders and files by name. */
  listFiles = (agent: string, path = "") =>
    this.get<FileListing>(`/api/files?agent=${encodeURIComponent(agent)}&path=${encodeURIComponent(path)}&limit=500`);
  /** Files and folders whose name or path holds `q` (at least two characters) in the agent's workspace. */
  searchFiles = (agent: string, q: string) =>
    this.get<FileSearch>(`/api/files/search?agent=${encodeURIComponent(agent)}&q=${encodeURIComponent(q)}&limit=200`);
  /**
   * Puts a file into the agent's workspace folder `dir`. 409 with code
   * `file_exists` when one by that name is there, unless `overwrite`.
   */
  async uploadFile(agent: string, dir: string, name: string, base64: string, overwrite = false): Promise<{ path: string; size: number }> {
    const query = `agent=${encodeURIComponent(agent)}&dir=${encodeURIComponent(dir)}&name=${encodeURIComponent(name)}${overwrite ? "&overwrite=true" : ""}`;
    const response = await getTransport().request(this.url(`/api/files/upload?${query}`), {
      method: "POST",
      headers: { Accept: "application/json", "Content-Type": "application/octet-stream" },
      body: base64,
      bodyEncoding: "base64",
      timeoutMs: 300000,
    });
    const parsed = parseJson(response.body);
    if (response.status >= 200 && response.status < 300) return parsed as { path: string; size: number };
    const detail = parsed && typeof parsed === "object" && "detail" in parsed ? (parsed as { detail: unknown }).detail : parsed;
    throw new ApiError(response.status, detail);
  }

  // ── push (runtimes with the mobile push backend; older ones answer 404) ───

  pushStatus = () => this.get<PushStatus>("/api/push/status");
  /** The app layout an admin set (dashboard Config > Mobile app); 404 on a deployment that predates it. */
  mobileConfig = () => this.get<unknown>("/api/mobile/config");
  registerDevice = (body: { token: string; platform: string; name?: string; app_version?: string; events?: string[] }) =>
    this.post<{ device: { id: string; events: string[] } }>("/api/push/devices", body);
  updateDevice = (id: string, events: string[]) => this.patch<{ device: { id: string; events: string[] } }>(`/api/push/devices/${encodeURIComponent(id)}`, { events });
  removeDevice = (id: string) => this.del<{ ok: boolean }>(`/api/push/devices/${encodeURIComponent(id)}`);
  testPush = () => this.post<{ ok: boolean }>("/api/push/test");
}

export function isNetworkError(error: unknown): error is NetworkError {
  return error instanceof NetworkError;
}
