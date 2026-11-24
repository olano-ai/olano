import { Check, Loader2 } from "lucide-react";
import { useT } from "../../core/i18n";
import type { TodoItem } from "./todos";
import { STR } from "./strings";

// Checklist rendered from the agent's `write_todos` tool. Each call replaces the
// list, so items animate pending → in_progress → completed within a message.
// `live` is true only while this message is actively streaming — a settled
// in_progress item shows a static dot rather than spinning forever (the agent's
// last reported state, not active work).
export function TodoList({ items, live = false }: { items: TodoItem[]; live?: boolean }) {
  const t = useT(STR);
  if (!items.length) return null;
  const done = items.filter((i) => i.status === "completed").length;
  return (
    <div className="todo-block">
      <div className="todo-block-head">
        {t("todo.title")} · {t("todo.progress", { done: String(done), total: String(items.length) })}
      </div>
      <div className="todo-list">
        {items.map((it, i) => {
          const paused = it.status === "in_progress" && !live;
          return (
            <div className={`todo-item is-${it.status}${paused ? " is-paused" : ""}`} key={i}>
              <span className="todo-icon">
                {it.status === "completed" ? (
                  <Check size={11} strokeWidth={3} />
                ) : it.status === "in_progress" && live ? (
                  <Loader2 size={11} strokeWidth={2.5} className="todo-spin" />
                ) : null}
              </span>
              <span className="todo-text">{it.content}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
