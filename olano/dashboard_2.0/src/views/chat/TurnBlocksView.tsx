// A turn drawn in the order it happened: text, the tools that ran, more text.
//
// Every shell had the same wrong layout — every tool card stacked above one
// glued reply — and fixing it in three places would have meant three copies of
// the ordering rule. What is shared here is that ordering (`buildTurnBlocks`)
// and the file-link index, NOT the markup: the dashboard chat draws a tool as
// `.tool-activity`, the simple and group shells draw it through `ToolTrail`
// with `.simple-tool-card`, and unifying those would be a visual change at two
// breakpoints riding along with a behavioural fix. So each shell passes its own
// `renderTools`.

import { Fragment, type ReactNode } from "react";

import type { MediaRef } from "../../core/types";
import { Markdown } from "../../ui/Markdown";
import { fileIndexFor } from "./fileLinks";
import { buildTurnBlocks } from "./buildTurnBlocks";
import type { ToolCall, TurnSegment } from "./turnText";

export function TurnBlocks({
  tools,
  segments,
  body,
  media,
  renderTools,
  onNavigate,
  onRunCommand,
  bubbleClass = "bubble assistant",
}: {
  tools: ToolCall[];
  segments: TurnSegment[] | undefined;
  body: string;
  media?: MediaRef[];
  /** How this shell draws a run of tool calls. `null` hides them (Quiet). */
  renderTools: (tools: ToolCall[]) => ReactNode;
  onNavigate?: (href: string) => void;
  onRunCommand?: (command: string) => void;
  bubbleClass?: string;
}) {
  // Memoized on the media array's identity — the store hands out a new array on
  // every merge, so the index is rebuilt exactly when the files change.
  const files = fileIndexFor(media);
  return (
    <>
      {buildTurnBlocks(tools, segments, body).map((block, i) =>
        block.kind === "text" ? (
          <div className={bubbleClass} key={`text-${i}`}>
            <Markdown
              text={block.text}
              files={files}
              onNavigate={onNavigate}
              onRunCommand={onRunCommand}
            />
          </div>
        ) : (
          <Fragment key={`tools-${i}`}>{renderTools(block.tools)}</Fragment>
        ),
      )}
    </>
  );
}
