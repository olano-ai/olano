// One row of toggle chips over a chat list — the control half of
// threadFilters.ts, rendered identically wherever a list of one agent's chats
// appears (the simple rail, its all-chats panel, the dashboard sidebar).

import type { T } from "./channelMeta";
import { filterHelp, filterLabel, type ThreadFilterId, type ThreadFilterOption } from "./threadFilters";

/**
 * Renders nothing while the list only holds one kind of chat — a filter with a
 * single answer is noise. `t` is the chat-strings translator (the `filter.*`
 * keys and the channel product names both live in views/chat/strings.ts).
 */
export function ThreadFilterChips({
  options,
  value,
  t,
  onChange,
}: {
  options: ThreadFilterOption[];
  value: ThreadFilterId;
  t: T;
  onChange: (id: ThreadFilterId) => void;
}) {
  // options[0] is "All"; two real buckets is where a filter starts to help.
  if (options.length < 3) return null;
  return (
    <div className="thread-filter-row" role="group" aria-label={t("filter.label")}>
      {options.map((opt) => {
        const on = value === opt.id;
        return (
          <button
            key={opt.id}
            type="button"
            className={`thread-filter-chip${on ? " is-on" : ""}`}
            aria-pressed={on}
            title={filterHelp(opt.id, t)}
            onClick={() => onChange(opt.id)}
          >
            {filterLabel(opt.id, t)}
            <span className="thread-filter-count">{opt.count}</span>
          </button>
        );
      })}
    </div>
  );
}
