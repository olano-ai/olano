/**
 * The dashboard modules the app shares, in one place.
 *
 * They are imported from the dashboard's source tree on purpose, not copied:
 * `renderProse` is the XSS-hardened Markdown renderer (escape first, then
 * format), and the turn helpers are the client half of the contract the
 * runner's segments follow. A second copy of either is how the app and the
 * dashboard would start rendering the same reply differently, or one of them
 * would miss a hardening fix. Every module listed here is pure (no React, no
 * stores), so both Vite and `node --test` can load it, and so is everything
 * it imports, types included: the app's CI job installs no dashboard
 * packages, so a module that reaches React (a type imported from the chat
 * store, say) fails the typecheck there while passing on a machine that has
 * the dashboard's node_modules.
 */

export type { ChatFrame, ErrorRemediation, MediaRef } from "../../../olano/dashboard_2.0/src/core/types.ts";
export { renderProse } from "../../../olano/dashboard_2.0/src/ui/markdownProse.ts";
export { escapeHtml, highlight, highlighterReady, loadHighlighter } from "../../../olano/dashboard_2.0/src/ui/highlight.ts";
export {
  appendText,
  applyToolResult,
  mergeMedia,
  subagentPath,
  upsertToolCall,
  type GrowingTurn,
  type Sub,
  type ToolCall,
  type TurnSegment,
} from "../../../olano/dashboard_2.0/src/views/chat/turnText.ts";
export {
  CHANNEL_NAMES,
  CHANNEL_TONES,
  isPlatformChannel,
} from "../../../olano/dashboard_2.0/src/views/chat/channelMeta.ts";
export {
  createGroup as createFolder,
  deleteGroup as deleteFolder,
  EMPTY_ARRANGEMENT,
  GROUP_NAME_MAX as FOLDER_NAME_MAX,
  homeOf,
  membersOf,
  moveTo,
  nudge,
  parseArrangement,
  renameGroup as renameFolder,
  toggleFold,
  togglePin,
  type Arrangement,
  type BotHome,
  type CustomGroup as Folder,
  type GroupNameError as FolderNameError,
} from "../../../olano/dashboard_2.0/src/views/nav/botArrangement.ts";
export {
  applyLiveFrame,
  liveTurn,
  type GroupLiveTurn,
  type GroupStreamFrame,
} from "../../../olano/dashboard_2.0/src/views/groups/liveTurn.ts";
export {
  extractConnectLinks,
  hostOf as connectHost,
  inlineFormLink,
  inlineFormSpent,
  serviceNameFromUrl,
  type ConnectLink,
  type InlineForm,
  type InlineFormField,
} from "../../../olano/dashboard_2.0/src/views/chat/connectLinks.ts";
export { cardText, resultSucceeded } from "../../../olano/dashboard_2.0/src/views/chat/connectResultText.ts";
export {
  fmtSize,
  ingestFile,
  MAX_ATTACHMENT_BYTES,
  toWire,
  type Attachment as FileAttachment,
  type WireAttachment,
} from "../../../olano/dashboard_2.0/src/views/chat/attachments.ts";
export {
  defaultRecurrence,
  describeRecurrence,
  guessTimeZone,
  isCadenceTooFrequent,
  isRecurrenceValid,
  scheduleToSpec,
  specToSchedule,
  weekdayName,
  type Freq,
  type Recurrence,
} from "../../../olano/dashboard_2.0/src/views/scheduler/recurrence.ts";
export type { HistoryEntry as TaskRun, Task as ScheduledTask } from "../../../olano/dashboard_2.0/src/views/scheduler/types.ts";
export { isHumanConversation, newestFirst } from "../../../olano/dashboard_2.0/src/views/chat/threadPick.ts";
export {
  buildFilterOptions,
  FILTER_ALL,
  filterThreadRows,
  readThreadFilter,
  threadBucket,
  writeThreadFilter,
  type ThreadFilterId,
  type ThreadFilterOption,
} from "../../../olano/dashboard_2.0/src/views/chat/threadFilters.ts";
export { cortexItemDraft, type DraftIntent } from "../../../olano/dashboard_2.0/src/views/cortex/handoffDraft.ts";
