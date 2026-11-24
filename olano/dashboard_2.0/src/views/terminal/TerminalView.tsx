import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { ArrowDown, ArrowUp, ClipboardPaste, CornerDownLeft, Copy } from "lucide-react";
import { useCurrentAgent } from "../../core/currentAgent";
import { useCapability } from "../../core/capabilities";
import { useT } from "../../core/i18n";
import { useMediaQuery, MOBILE_BREAKPOINT_PX } from "../../core/useMediaQuery";
import { useToast } from "../../ui/toast";
import { ContextMenu, type MenuState } from "../../ui/ContextMenu";
import { TerminalIcon, PlusIcon, CloseIcon } from "../../ui/icons";
import { AgentPicker } from "../nav/AgentPicker";
import { terminalStore } from "./terminalStore";
import { STR } from "./strings";
import "@xterm/xterm/css/xterm.css";
import "./terminal.css";

// The key bar is for input that a physical keyboard already covers: a phone
// keyboard has no Tab, no arrows and no Ctrl, and a browser terminal has no
// right-click menu. Shown on small viewports and on any coarse pointer (a
// tablet is the same problem at 1024px).
const TOUCH_KEYS_QUERY = `(max-width: ${MOBILE_BREAKPOINT_PX}px), (pointer: coarse)`;

// Rendered height of the suggestion strip (one row of chips), used to decide
// whether it fits under the caret's line or has to go above it.
const STRIP_H = 34;

/**
 * Place the strip immediately under the line being typed — or above it when
 * that line is near the bottom, which is where a shell prompt normally lives.
 * Falls back to the pane's bottom edge (the CSS default) with no metrics.
 */
function suggestStyle(
  caret: { lineBottom: number; rowHeight: number } | null,
  bodyHeight: number | undefined,
): React.CSSProperties | undefined {
  if (!caret || !bodyHeight) return undefined;
  const below = caret.lineBottom + 4;
  const top =
    below + STRIP_H <= bodyHeight
      ? below
      : Math.max(4, caret.lineBottom - caret.rowHeight - STRIP_H - 4);
  return { top, bottom: "auto" };
}

export function TerminalView() {
  const t = useT(STR);
  const toast = useToast();
  // The "workspace agent": the sidebar's selection, repeated in this view's own
  // header because a terminal is per agent — its shell runs in that agent's
  // workspace or sandbox — and the strip below shows only that agent's tabs.
  const { agent } = useCurrentAgent();
  const enabled = useCapability("terminal_enabled");
  // Where the shell runs (managed posture: inside the agent's sandbox; open
  // posture: a host shell in the agent's workspace). Undefined while the
  // status probe is still in flight — then the header says nothing.
  const sandbox = useCapability("terminal_sandbox");
  const version = useSyncExternalStore(terminalStore.subscribe, terminalStore.getSnapshot);
  // Typing state rides its own channel so a keystroke re-renders this chrome
  // without re-running the attach/fit effect below.
  const inputTick = useSyncExternalStore(terminalStore.subscribeInput, terminalStore.getInputSnapshot);
  const showKeys = useMediaQuery(TOUCH_KEYS_QUERY);
  const containerRef = useRef<HTMLDivElement>(null);
  const dragId = useRef<string | null>(null);
  const resizeFrame = useRef<number | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [draggingId, setDraggingId] = useState<string | null>(null);
  const [dropId, setDropId] = useState<string | null>(null);
  // The shared right-click menu, which clamps itself into the viewport — the
  // hand-rolled copy this replaced hung its 220px panel off the right edge from
  // a tab right-clicked (or long-pressed on a phone) near it, and owned its own
  // outside-click/Escape handling on top.
  const [menu, setMenu] = useState<MenuState | null>(null);

  const tabs = terminalStore.tabs(agent);
  const activeId = terminalStore.activeId(agent);
  const activeStatus = terminalStore.activeStatus(agent);
  const unavailableReason = terminalStore.activeUnavailableReason(agent);
  const counts = terminalStore.tabCounts();

  function startRename(id: string) {
    const tab = terminalStore.tabs().find((x) => x.id === id);
    setEditingId(id);
    setDraft(tab?.title ?? "");
  }

  // Show this agent's tabs, and open its first terminal when it has none — on
  // arrival and on every switch of the workspace agent. The store is told the
  // agent first so the attach effect below finds the right active session.
  useEffect(() => {
    terminalStore.setAgent(agent);
    if (enabled) terminalStore.ensureOne(agent);
  }, [enabled, agent]);

  // (Re)attach the active session's persistent host whenever the active tab
  // changes or sessions mutate — this is also what restores terminals after
  // navigating away and back (the view remounts; the shells never died), and
  // what swaps the visible shell when the workspace agent changes.
  useEffect(() => {
    if (enabled && containerRef.current) terminalStore.attachActive(containerRef.current);
  }, [enabled, agent, activeId, version]);

  // Keep the terminal sized to its container.
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => {
      if (resizeFrame.current !== null) return;
      resizeFrame.current = window.requestAnimationFrame(() => {
        resizeFrame.current = null;
        terminalStore.fitActive();
      });
    });
    ro.observe(el);
    return () => {
      ro.disconnect();
      if (resizeFrame.current !== null) {
        window.cancelAnimationFrame(resizeFrame.current);
        resizeFrame.current = null;
      }
    };
  }, [enabled]);

  function commitRename() {
    if (editingId) terminalStore.rename(editingId, draft);
    setEditingId(null);
  }

  // `inputTick` is the dependency that matters: the suggestions live in the
  // store, and the tick is how the store says they may have changed.
  const suggestions = useMemo(
    () => (activeStatus === "open" ? terminalStore.suggestions() : []),
    [inputTick, activeId, activeStatus],
  );

  const copy = useCallback(async () => {
    const result = await terminalStore.copy();
    if (result === "failed") toast.push(t("keys.copyFailed"), "error");
    else if (result === "empty") toast.push(t("keys.copyEmpty"), "info");
    else toast.push(t(result === "selection" ? "keys.copiedSelection" : "keys.copiedScreen"), "success");
  }, [t, toast]);

  const paste = useCallback(async () => {
    // Clipboard *reads* need permission and a gesture, and Safari can still
    // refuse. A prompt is the universal fallback — the text still has to get
    // in somehow, and typing it is worse.
    if (await terminalStore.paste()) return;
    const text = window.prompt(t("keys.pastePrompt"));
    if (text) await terminalStore.paste(text);
  }, [t]);

  if (enabled === undefined) {
    return (
      <div className="page">
        <div className="spinner" style={{ marginTop: 40 }} />
      </div>
    );
  }
  if (!enabled) {
    return (
      <div className="page">
        <h1 style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <TerminalIcon size={20} /> {t("title")}
        </h1>
        <div className="card" style={{ marginTop: 16, maxWidth: 560, padding: 16 }}>
          <strong>{t("disabled.title")}</strong>
          <p className="muted" style={{ marginTop: 8 }}>
            {t("disabled.hint")}
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="term-view">
      {/* Which agent's shells these are. The same app-wide picker the sidebar
          holds, so switching here switches everywhere; the count on each row
          says where terminals are already open. */}
      <div className="term-header" data-testid="term-header">
        <span className="term-header-label">{t("header.workspaceAgent")}</span>
        <AgentPicker
          variant="inline"
          rowExtra={(a) =>
            counts[a.name] ? (
              <span className="term-agent-count" title={t("header.count", { n: String(counts[a.name]) })}>
                · {counts[a.name]}
              </span>
            ) : null
          }
        />
        {sandbox !== undefined && (
          <span className="term-header-hint muted" data-testid="term-posture">
            {t(sandbox ? "header.sandbox" : "header.host", { agent })}
          </span>
        )}
      </div>
      <div className="term-tabs" role="tablist" aria-label={t("tabs.aria", { agent })}>
        {tabs.map((tab) => (
          <div
            key={tab.id}
            role="tab"
            aria-selected={tab.id === activeId}
            draggable={editingId !== tab.id}
            className={`term-tab ${tab.id === activeId ? "active" : ""} ${draggingId === tab.id ? "dragging" : ""} ${dropId === tab.id ? "drop-target" : ""}`}
            onClick={() => terminalStore.setActive(tab.id)}
            onDoubleClick={() => startRename(tab.id)}
            onContextMenu={(e) => {
              e.preventDefault();
              setMenu({
                x: e.clientX,
                y: e.clientY,
                label: t("tabs.menuAria", { tab: tab.title || tab.id }),
                items: [
                  { label: t("tab.rename"), onClick: () => startRename(tab.id) },
                  { label: t("tab.close"), danger: true, onClick: () => terminalStore.kill(tab.id) },
                ],
              });
            }}
            onDragStart={(e) => {
              dragId.current = tab.id;
              setDraggingId(tab.id);
              e.dataTransfer.effectAllowed = "move";
            }}
            onDragEnd={() => {
              dragId.current = null;
              setDraggingId(null);
              setDropId(null);
            }}
            onDragOver={(e) => {
              if (dragId.current && dragId.current !== tab.id) {
                e.preventDefault();
                setDropId(tab.id);
              }
            }}
            onDragLeave={() => setDropId((d) => (d === tab.id ? null : d))}
            onDrop={(e) => {
              e.preventDefault();
              if (dragId.current) terminalStore.reorder(dragId.current, tab.id);
              setDropId(null);
            }}
            title={t("tab.renameHint")}
          >
            <span className={`term-dot ${tab.status}`} />
            {editingId === tab.id ? (
              <input
                className="term-tab-edit"
                value={draft}
                autoFocus
                onClick={(e) => e.stopPropagation()}
                onChange={(e) => setDraft(e.target.value)}
                onBlur={commitRename}
                onKeyDown={(e) => {
                  if (e.key === "Enter") commitRename();
                  else if (e.key === "Escape") setEditingId(null);
                }}
              />
            ) : (
              <span className="term-tab-label">{tab.title}</span>
            )}
            <button
              className="term-tab-x"
              onClick={(e) => {
                e.stopPropagation();
                terminalStore.kill(tab.id);
              }}
              title={t("btn.kill")}
              aria-label={t("btn.kill")}
            >
              <CloseIcon size={12} />
            </button>
          </div>
        ))}
        <button className="term-add" onClick={() => terminalStore.create(agent)} title={t("btn.new")} aria-label={t("btn.new")}>
          <PlusIcon size={15} />
        </button>
      </div>

      <div className="term-body">
        <div className="term-mount" ref={containerRef} />

        {/* Command recall. Overlaid rather than stacked so showing it never
            resizes the PTY mid-command (a re-fit while output is streaming
            reflows the whole screen). File paths are NOT completed here — Tab
            does that in the shell itself. */}
        {suggestions.length > 0 && (
          <div
            className="term-suggest"
            role="listbox"
            aria-label={t("suggest.aria")}
            style={suggestStyle(terminalStore.caretMetrics(), containerRef.current?.clientHeight)}
          >
            {suggestions.map((cmd, i) => (
              <button
                key={cmd}
                role="option"
                aria-selected={i === 0}
                className={`term-suggest-item${i === 0 ? " is-first" : ""}`}
                // Keep focus in the terminal: a blurred xterm drops the caret
                // and, on touch, dismisses the on-screen keyboard.
                onMouseDown={(e) => e.preventDefault()}
                onClick={() => terminalStore.accept(cmd)}
                title={cmd}
              >
                <span className="term-suggest-text">{cmd}</span>
                {i === 0 && <span className="term-suggest-key">→</span>}
              </button>
            ))}
          </div>
        )}

        {/* No tab for this agent (every one was closed by hand): offer a shell
            rather than opening one unasked. */}
        {tabs.length === 0 && (
          <div className="term-overlay term-empty" data-testid="term-empty">
            <span className="muted">{t("empty.none", { agent })}</span>
            <button className="btn primary" onClick={() => terminalStore.create(agent)}>
              {t("empty.open")}
            </button>
          </div>
        )}

        {activeStatus && activeStatus !== "open" && (
          <div className="term-overlay">
            {activeStatus === "connecting" && <span className="muted">{t("conn.connecting")}</span>}
            {activeStatus === "reconnecting" && <span className="muted">{t("conn.reconnecting")}</span>}
            {activeStatus === "disabled" && <span className="muted">{t("disabled.hint")}</span>}
            {activeStatus === "unavailable" && (
              <>
                <span className="muted">{t("conn.unavailable", { agent })}</span>
                {unavailableReason && (
                  <span className="muted term-overlay-reason">
                    {t("conn.unavailableReason", { reason: unavailableReason })}
                  </span>
                )}
                <button className="btn" onClick={() => terminalStore.reconnectActive()}>
                  {t("btn.reconnect")}
                </button>
              </>
            )}
            {activeStatus === "closed" && (
              <>
                <span className="muted">{t("conn.closed")}</span>
                <button className="btn" onClick={() => terminalStore.reconnectActive()}>
                  {t("btn.reconnect")}
                </button>
              </>
            )}
          </div>
        )}
      </div>

      {/* Touch key bar: the keys a soft keyboard doesn't have, plus the
          clipboard actions a browser terminal can't get from a right-click. */}
      {showKeys && (
        <div className="term-keys" role="toolbar" aria-label={t("keys.aria")}>
          <KeyBtn label={t("keys.copy")} onPress={() => void copy()}>
            <Copy size={15} strokeWidth={1.9} />
          </KeyBtn>
          <KeyBtn label={t("keys.paste")} onPress={() => void paste()}>
            <ClipboardPaste size={15} strokeWidth={1.9} />
          </KeyBtn>
          <KeyBtn label={t("keys.tab")} onPress={() => terminalStore.sendKey("\t")} wide>
            tab
          </KeyBtn>
          <KeyBtn label={t("keys.up")} onPress={() => terminalStore.sendKey("\x1b[A")}>
            <ArrowUp size={15} strokeWidth={2} />
          </KeyBtn>
          <KeyBtn label={t("keys.down")} onPress={() => terminalStore.sendKey("\x1b[B")}>
            <ArrowDown size={15} strokeWidth={2} />
          </KeyBtn>
          <KeyBtn label={t("keys.esc")} onPress={() => terminalStore.sendKey("\x1b")} wide>
            esc
          </KeyBtn>
          <KeyBtn label={t("keys.ctrlc")} onPress={() => terminalStore.sendKey("\x03")} wide>
            ^C
          </KeyBtn>
          <KeyBtn label={t("keys.enter")} onPress={() => terminalStore.sendKey("\r")}>
            <CornerDownLeft size={15} strokeWidth={1.9} />
          </KeyBtn>
        </div>
      )}

      {menu && <ContextMenu menu={menu} onClose={() => setMenu(null)} />}
    </div>
  );
}

// One key on the touch bar. Presses must not move focus: the terminal's hidden
// textarea is what holds the caret and, on a phone, what keeps the on-screen
// keyboard up — blurring it between every key would make the bar unusable
// exactly where it matters. `onMouseDown` preventDefault is the whole trick
// (touch delivers a compatibility mousedown before the click).
function KeyBtn({
  label,
  onPress,
  wide,
  children,
}: {
  label: string;
  onPress: () => void;
  wide?: boolean;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      className={`term-key${wide ? " is-wide" : ""}`}
      onMouseDown={(e) => e.preventDefault()}
      onClick={onPress}
      aria-label={label}
      title={label}
    >
      {children}
    </button>
  );
}
