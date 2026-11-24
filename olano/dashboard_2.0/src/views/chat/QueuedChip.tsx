// One chip in the "waiting" strip above the composer — a message typed while
// the agent was still replying. Shared by the full chat and the simple chat so
// the two strips cannot drift. Every chip carries three actions:
//
//   Steer  — hand it to the running turn now; the agent reads it at its next
//            step without stopping (queued, text-only chips while a turn runs).
//   Edit   — take it back into the message box (a steer chip is withdrawn
//            first; one the agent already read just leaves the strip).
//   Cancel — drop it (a steer chip is withdrawn from the turn too).
//
// The kind icon says what will happen to the chip: a clock for "sends after
// this reply", a fast-forward for "the agent reads this at its next step".
import { Clock, FastForward, Pencil, X } from "lucide-react";
import type { QueuedMsg } from "./sessionStore";
import { canSteerChip } from "./steering";

type T = (key: string, vars?: Record<string, string>) => string;

export function QueuedChip({
  q,
  streaming,
  t,
  onSteer,
  onEdit,
  onCancel,
}: {
  q: QueuedMsg;
  streaming: boolean;
  t: T;
  onSteer: (id: number) => void;
  onEdit: (id: number) => void;
  onCancel: (id: number) => void;
}) {
  const isSteer = q.kind === "steer";
  const steerable = canSteerChip(q, streaming);
  const steerHint = isSteer
    ? t("queue.steered")
    : q.attachments.length > 0
      ? t("queue.steerAttachments")
      : !streaming
        ? t("queue.steerIdle")
        : t("queue.steer");
  const kindLabel = isSteer ? t("queue.kind.steer") : t("queue.kind.queue");
  return (
    <span className={`queue-item${isSteer ? " is-steer" : ""}`} title={`${q.text}\n${kindLabel}`}>
      <span className="queue-item-kind" aria-label={kindLabel} title={kindLabel}>
        {isSteer ? <FastForward size={11} /> : <Clock size={11} />}
      </span>
      <span className="queue-item-text">{q.text || `📎 ${q.attachments.length}`}</span>
      <span className="queue-item-actions">
        {!isSteer && (
          <button
            type="button"
            className="queue-item-btn"
            disabled={!steerable}
            aria-label={t("queue.steer")}
            title={steerHint}
            onClick={() => onSteer(q.id)}
          >
            <FastForward size={12} />
          </button>
        )}
        <button
          type="button"
          className="queue-item-btn"
          aria-label={t("queue.edit")}
          title={t("queue.edit")}
          onClick={() => onEdit(q.id)}
        >
          <Pencil size={12} />
        </button>
        <button
          type="button"
          className="queue-item-btn danger"
          aria-label={t("queue.cancel")}
          title={t("queue.cancel")}
          onClick={() => onCancel(q.id)}
        >
          <X size={12} />
        </button>
      </span>
    </span>
  );
}
