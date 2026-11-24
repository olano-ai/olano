// A group conversation: several bots and one person in one transcript.
//
// The one thing a group must do that a 1:1 chat never has to is ATTRIBUTION.
// With six voices in a column, "who said this, and who were they saying it to"
// is the whole readability problem — so every bot message wears its own mark
// and name, and a message aimed at a particular member wears a "→ name" chip.
// Drop either and the transcript becomes a wall of alternating bubbles that
// nobody can follow.
//
// Turn-taking is the backend's rule, surfaced here rather than reimplemented:
// exactly ONE member answers each human message — the one named with `@`, the
// one picked in the composer, else the group's first member. The others read it
// and can be pulled in by name. The composer says so in plain words.
//
// A turn streams, and it can involve more than one bot: a message that names
// several members ("@a @b look at this") gives each of them a turn, in order,
// and each sees what the ones before it said. So the view shows the human's
// turn immediately (an optimistic echo from the store), a typing line under
// whichever member owes the next reply, and the words as they arrive.
//
// It also shows the WORK, at the reader's chosen level — and defaults that
// level to `normal` rather than the 1:1 chat's `quiet`. A group is opened to
// watch bots collaborate: the `talk_to_agent` call one member makes to a peer,
// the subagent another one delegates the research to, the handoff by @name.
// Hide all of that and six voices in a column read like strangers monologuing.

import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Info, Menu } from "lucide-react";
import { useI18n, useT } from "../../core/i18n";
import type { Agent, MediaRef } from "../../core/types";
import { Markdown } from "../../ui/Markdown";
import { AgentMedia } from "../chat/AttachmentViews";
import { TurnBlocks } from "../chat/TurnBlocksView";
import type { Sub, ToolCall } from "../chat/sessionStore";
import type { TurnSegment } from "../chat/turnText";
import { useToast } from "../../ui/toast";
import { BotMark, GroupMark } from "../agents/BotMark";
import { DetailButton, GROUP_DETAIL_KEY, PeerThinking, ToolTrail, useDetail } from "../simple/detail";
import { absoluteTime, authorLabel, daySeparator, parseTs, relativeTime } from "../simple/format";
import {
  GroupReplyError,
  groupErrorText,
  isShared,
  mentionedMembers,
  sendGroupMessage,
  type GroupActivity,
  type GroupLiveTurn,
  type GroupMessage,
  type GroupSummary,
  type PendingMessage,
} from "./groupsStore";
import { SimpleComposer } from "../simple/SimpleComposer";
import { GROUPS_STR as STR } from "./strings";

// Same block size the 1:1 transcript uses, so a person who switches between the
// two reads the same rhythm of time separators.
const TIME_BLOCK_MS = 30 * 60 * 1000;

// A group has no slash commands: /new and /compact are thread operations on a
// 1:1 conversation, and a group is renamed from its own row in the rail (or its
// info panel), so offering them here would mean commands that answer "not in a
// group".
const NO_COMMANDS: never[] = [];

// Stable empty fallbacks: a message with no recorded activity must not churn
// object identity on every render.
const EMPTY_TOOLS: ToolCall[] = [];
const EMPTY_SUBS: Sub[] = [];

export function GroupThreadView({
  group,
  messages,
  pending,
  awaiting,
  streaming,
  activity,
  agentsByName,
  isMobile,
  infoOpen,
  onOpenRail,
  onToggleInfo,
}: {
  group: GroupSummary;
  messages: GroupMessage[];
  /** Human turns not yet echoed back by the server. */
  pending: PendingMessage[];
  /** The member currently composing a reply, if a send is in flight. */
  awaiting: string | undefined;
  /** A reply arriving word by word — rendered as a live bubble, replacing the
      typing line the moment the first words exist. Carries the member's tool
      calls and subagent output as they happen. */
  streaming: GroupLiveTurn | undefined;
  /** Settled tool/subagent work, keyed by the message it produced. Client-side
      only: the transcript endpoint returns prose, this is the live stream's
      record of how that prose came about. */
  activity: Record<number, GroupActivity | undefined> | undefined;
  agentsByName: Map<string, Agent>;
  isMobile: boolean;
  infoOpen: boolean;
  onOpenRail: () => void;
  onToggleInfo: () => void;
}) {
  const t = useT(STR);
  const { lang } = useI18n();
  const toast = useToast();
  // A file a member named in its reply becomes a link into the Files tab;
  // client-side, the way the media cards under the same reply already go.
  const navigate = useNavigate();

  // Normal by default: the collaboration is what a group is opened to watch.
  // A reading preference, so it is per browser and shared across every group.
  const [detail, , cycleDetail] = useDetail(GROUP_DETAIL_KEY, "normal");

  // The draft and the scroll pin are per group: switching groups must not carry
  // a half-written message into someone else's thread.
  const [input, setInput] = useState("");
  const logRef = useRef<HTMLDivElement>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);
  const [nowTick, setNowTick] = useState(() => Date.now());

  useEffect(() => {
    setInput("");
  }, [group.id]);

  useEffect(() => {
    const id = window.setInterval(() => setNowTick(Date.now()), 60_000);
    return () => window.clearInterval(id);
  }, []);

  // New messages arrive by SSE rather than by keystroke, so the log follows the
  // bottom whenever the count changes or a reply is being waited on.
  useEffect(() => {
    const el = logRef.current;
    if (el) el.scrollTo({ top: el.scrollHeight });
  }, [
    group.id,
    messages.length,
    pending.length,
    awaiting,
    streaming?.body.length,
    streaming?.tools.length,
    streaming?.subs.length,
    streaming?.media?.length,
  ]);

  // The live turn earns its own bubble only when it has something to show at
  // THIS detail level. On Quiet a member that is only running tools has
  // nothing visible yet, so it keeps the typing line instead of drawing an
  // empty bubble — the tools it is running are exactly what Quiet hides.
  const live =
    streaming &&
    (streaming.body || (detail !== "quiet" && (streaming.tools.length || streaming.subs.length)))
      ? streaming
      : undefined;

  const first = group.members[0] ?? "";
  const history = useMemo(
    () => messages.filter((m) => m.author_kind === "human").map((m) => m.body),
    [messages],
  );

  async function submit() {
    const text = input.trim();
    if (!text) return;
    setInput("");
    try {
      // No explicit addressee: who answers is decided by the @mentions in the
      // text (several, in order) or, failing that, by the group's own
      // first-member rule. The composer's line under the box says which of
      // those is about to happen — see `answersLine`.
      await sendGroupMessage(group.id, text);
    } catch (e) {
      toast.push(groupErrorText(e, t("groups.sendFailed")), "error");
      // Hand the words back rather than making the reader retype them — but
      // only if they have not started a new message in the meantime, and only
      // when the message never landed. A GroupReplyError means it DID land and
      // a bot's reply is what failed; refilling the composer there invites
      // sending the same thing twice.
      if (!(e instanceof GroupReplyError)) setInput((cur) => cur || text);
    }
  }

  // Who the backend will hand this draft to, read out of the draft itself with
  // the same rule the store's optimistic echo uses — so the line under the box
  // and the "…is replying" line above it can never disagree.
  const addressed = mentionedMembers(input, group.members);

  // The one line under the box, in place of the "Who answers" picker that used
  // to sit above it.
  //
  // That control asked the reader to make a decision the message itself
  // already makes — and then needed a five-sentence paragraph to explain a
  // rule ("@ overrides this, and you can name several, and unnamed bots still
  // read everything…") that only existed because the control was there. The
  // rule without the control is one sentence, and it does not need reading
  // twice: type @ or don't.
  //
  // So this states an OUTCOME rather than offering a choice, and it recomputes
  // as the draft is typed: the moment "@maria" appears the line says maria.
  //
  // A shared room has one more outcome: with more than one person in it, a
  // message that names nobody is for the other people and wakes no agent , 
  // the server's `require_mention` rule, said here before the send.
  const shared = isShared(group);
  const answersLine =
    addressed.length > 1
      ? t("groups.answersEach", { names: addressed.join(", ") })
      : addressed.length === 1
        ? t("groups.answersNamed", { name: addressed[0] })
        : shared
          ? t("groups.answersNobody")
          : t("groups.answersFallback", { name: first });

  // A person's turn: their own text exactly as typed, another human's as
  // markdown. A member's reply does NOT come through here — it is a turn with
  // an order to it (words, the tools it ran, more words) and is drawn by
  // `TurnBlocks` below.
  function renderHumanBody(m: GroupMessage, mine: boolean) {
    return mine ? (
      <div className="bubble user">{m.body}</div>
    ) : (
      <div className="bubble assistant">
        <Markdown text={m.body} />
      </div>
    );
  }

  // One member's turn, in the order it happened, with the files it named. The
  // block builder and the file index are the 1:1 chat's, so a reader switching
  // between a group and a private thread reads the same layout; only the tool
  // markup differs, and that is this shell's own (`ToolTrail`).
  //
  // Subagent transcripts are per-TURN, not per-span — a subagent belongs to the
  // reply as a whole — so they are drawn once after the blocks (the 1:1 chat
  // places them the same way) rather than inside a tool run.
  function renderMemberTurn(args: {
    tools: ToolCall[];
    subs: Sub[];
    segments: TurnSegment[] | undefined;
    body: string;
    media: MediaRef[] | undefined;
  }) {
    return (
      <>
        <TurnBlocks
          tools={args.tools}
          segments={args.segments}
          body={args.body}
          media={args.media}
          renderTools={(tools) => <ToolTrail tools={tools} subs={EMPTY_SUBS} detail={detail} />}
          onNavigate={navigate}
        />
        <ToolTrail tools={EMPTY_TOOLS} subs={args.subs} detail={detail} />
        {!!args.media?.length && <AgentMedia items={args.media} />}
      </>
    );
  }

  let prevTs = 0;
  let prevAuthor = "";

  return (
    <main
      className="simple-chat"
      aria-label={t("groups.region", { name: group.name })}
      data-simple-group-thread
      data-testid="simple-group-thread"
    >
      <header className="simple-head">
        {isMobile && (
          <button
            type="button"
            className="simple-head-btn"
            onClick={onOpenRail}
            aria-label={t("groups.openBots")}
            title={t("groups.openBots")}
          >
            <Menu size={18} strokeWidth={1.75} />
          </button>
        )}
        <GroupMark members={group.members} agentsByName={agentsByName} size={36} />
        <div className="simple-head-text">
          <strong className="simple-head-name">{group.name}</strong>
          <span className="simple-head-sub muted">
            {group.members.length === 1
              ? t("groups.membersOne")
              : t("groups.members", { n: String(group.members.length) })}
            {" · "}
            {group.members.join(", ")}
            {(group.people?.length ?? 0) > 1 && (
              <>
                {" · "}
                {t("groups.peopleCount", { n: String(group.people?.length ?? 0) })}
              </>
            )}
          </span>
        </div>
        <DetailButton detail={detail} onCycle={cycleDetail} />
        <button
          type="button"
          className="simple-head-btn"
          onClick={onToggleInfo}
          aria-expanded={infoOpen}
          aria-label={infoOpen ? t("groups.infoClose") : t("groups.info")}
          title={infoOpen ? t("groups.infoClose") : t("groups.info")}
        >
          <Info size={17} strokeWidth={1.75} />
        </button>
      </header>

      <div className="chat-log simple-log" ref={logRef}>
        {messages.length === 0 && pending.length === 0 && (
          <div className="simple-blank">
            <p className="simple-blank-title">{t("groups.emptyThread", { name: group.name })}</p>
            <p className="simple-blank-sub muted">
              {shared
                ? t("groups.emptyThreadHintShared")
                : t("groups.emptyThreadHint", { first: first || group.name })}
            </p>
          </div>
        )}

        {messages.map((m) => {
          const ts = parseTs(m.ts);
          const gap = !prevTs || ts - prevTs > TIME_BLOCK_MS;
          const separator = ts && gap ? daySeparator(ts, nowTick, lang, t) : "";
          // Server-stamped and viewer-relative. The old rule — "any human
          // turn is mine" — put a member's messages on an admin's own side of
          // the column, in the view whose whole purpose is attribution. Older
          // backends omit the field; falling back to the old rule there only
          // affects the transition window.
          const mine = m.mine ?? m.author_kind === "human";
          // Grouping is by AUTHOR here, not by side: two different bots on the
          // same side of the column are two speakers, and collapsing their
          // marks together is exactly the attribution loss this view exists to
          // prevent.
          const grouped = !gap && prevAuthor === m.author;
          prevTs = ts || prevTs;
          prevAuthor = m.author;
          if (m.author_kind === "system") {
            return (
              <Fragment key={m.id}>
                {!!separator && (
                  <div className="simple-daysep">
                    <span>{separator}</span>
                  </div>
                )}
                <div className="chat-system simple-system">
                  <div className="md">
                    <Markdown text={m.body} />
                  </div>
                </div>
              </Fragment>
            );
          }
          const agent = agentsByName.get(m.author);
          return (
            <Fragment key={m.id}>
              {!!separator && (
                <div className="simple-daysep">
                  <span>{separator}</span>
                </div>
              )}
              <div
                className={`msg simple-msg ${mine ? "is-me" : "is-bot"}${grouped ? " is-grouped" : ""}`}
              >
                {!mine && (
                  <span className="simple-msg-face">
                    {!grouped && (
                      <BotMark
                        name={m.author}
                        avatarUrl={agent?.avatar_url}
                        emoji={agent?.emoji}
                        size={30}
                      />
                    )}
                  </span>
                )}
                <div className="msg-body simple-msg-body">
                  {/* In a group the speaker's name is VISIBLE, not screen-reader
                      text: six voices in one column is precisely the case where
                      the reader needs to be told who is talking. */}
                  {!grouped && (
                    <div className="msg-name simple-msg-author">
                      {authorLabel(m.author, m.author_kind, t("groups.you"), mine)}
                      {!!m.addressee && (
                        <span
                          className="simple-to-chip"
                          title={t("groups.toChipHelp", { name: m.addressee })}
                        >
                          {t("groups.toChip", { name: m.addressee })}
                        </span>
                      )}
                      {/* A bot-to-bot turn says who it is answering. Kept apart
                          from the "→" chip: a reply is not a hand-off, and the
                          reader should never have to infer either from the text. */}
                      {!!m.reply_to && (
                        <span
                          className="simple-to-chip simple-reply-chip"
                          title={t("groups.replyChipHelp", { name: m.reply_to })}
                        >
                          {t("groups.replyChip", { name: m.reply_to })}
                        </span>
                      )}
                    </div>
                  )}
                  {/* What this member SAID and what it DID, interleaved: the
                      peer it called, the subagent it delegated to, each sitting
                      between the paragraphs it sits between at the reader's
                      level. `segments` is persisted with the row, so a reload
                      redraws the same order; the tool trail is held per message
                      id by the store, because the transcript endpoint carries
                      prose and nothing else. */}
                  {m.author_kind === "agent"
                    ? renderMemberTurn({
                        tools: activity?.[m.id]?.tools ?? EMPTY_TOOLS,
                        subs: activity?.[m.id]?.subs ?? EMPTY_SUBS,
                        segments: m.segments,
                        body: m.body,
                        media: m.media,
                      })
                    : renderHumanBody(m, mine)}
                  {!!ts && (
                    <div className="msg-actions">
                      <time
                        className="msg-time"
                        dateTime={new Date(ts).toISOString()}
                        title={absoluteTime(ts, lang)}
                      >
                        {relativeTime(ts, nowTick, lang, t)}
                      </time>
                    </div>
                  )}
                </div>
              </div>
            </Fragment>
          );
        })}

        {/* The human's own turn, shown the instant it is sent. The POST does not
            return until a bot has answered, and a message that vanishes for a
            minute reads as a failure. */}
        {pending.map((p) => (
          <div className="msg simple-msg is-me is-pending" key={p.id}>
            <div className="msg-body simple-msg-body">
              <div className="msg-name simple-msg-author">
                {t("groups.you")}
                {!!p.addressee && (
                  <span className="simple-to-chip" title={t("groups.toChipHelp", { name: p.addressee })}>
                    {t("groups.toChip", { name: p.addressee })}
                  </span>
                )}
              </div>
              <div className="bubble user">{p.body}</div>
            </div>
          </div>
        ))}

        {!!live && (
          <div className="msg simple-msg is-bot" role="status">
            <span className="simple-msg-face">
              <BotMark
                name={live.author}
                avatarUrl={agentsByName.get(live.author)?.avatar_url}
                emoji={agentsByName.get(live.author)?.emoji}
                size={30}
              />
            </span>
            <div className="msg-body simple-msg-body">
              <span className="simple-msg-author">{live.author}</span>
              {/* The same blocks the settled row gets, growing: the store folds
                  each `text` frame into the current span and each `tool_call`
                  into a break, so the words arrive already in their places
                  instead of jumping there when the turn lands. */}
              {renderMemberTurn({
                tools: live.tools,
                subs: live.subs,
                segments: live.segments,
                body: live.body,
                media: live.media,
              })}
              {/* One bot has handed the question to another and is blocked on
                  the answer. Without this the transcript goes silent for as
                  long as the peer takes. */}
              <PeerThinking tools={live.tools} asker={live.author} agentsByName={agentsByName} />
              {live.body ? (
                // Words are arriving. This replaced an inline caret: a caret can
                // only sit at the end of the text when the text is a raw string,
                // and a live turn is markdown now (same renderer as the settled
                // row, so nothing reflows when it lands) whose last block is a
                // paragraph. The 1:1 shell already marks streaming this way.
                <div className="typing" aria-label={t("groups.replying", { name: live.author })}>
                  <span />
                  <span />
                  <span />
                </div>
              ) : (
                // Tools are running and nothing has been said yet — keep the
                // typing line under the trail rather than drawing an empty
                // bubble.
                <div className="simple-working">
                  <span className="typing" aria-hidden>
                    <span />
                    <span />
                    <span />
                  </span>
                  <span>{t("groups.replying", { name: live.author })}</span>
                </div>
              )}
            </div>
          </div>
        )}

        {!!awaiting && !live && (
          <div className="msg simple-msg is-bot" role="status">
            <span className="simple-msg-face">
              <BotMark
                name={awaiting}
                avatarUrl={agentsByName.get(awaiting)?.avatar_url}
                emoji={agentsByName.get(awaiting)?.emoji}
                size={30}
              />
            </span>
            <div className="msg-body simple-msg-body">
              <div className="simple-working">
                <span className="typing" aria-hidden>
                  <span />
                  <span />
                  <span />
                </span>
                <span>{t("groups.replying", { name: awaiting })}</span>
              </div>
            </div>
          </div>
        )}
      </div>

      <SimpleComposer
        botName={group.name}
        placeholder={t("groups.placeholder", { name: group.name })}
        hint={answersLine}
        canAttach={false}
        input={input}
        staged={[]}
        queued={[]}
        streaming={false}
        commands={NO_COMMANDS}
        mentions={group.members}
        history={history}
        textareaRef={taRef}
        onInput={setInput}
        onAddFiles={() => {}}
        onRemoveStaged={() => {}}
        onSteerQueued={() => {}}
        onEditQueued={() => {}}
        onCancelQueued={() => {}}
        onClearQueue={() => {}}
        onSubmit={() => void submit()}
        onSteer={() => void submit()}
        onStop={() => {}}
        onSlash={() => {}}
      />
    </main>
  );
}
