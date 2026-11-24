// The windowed, keyboard-driven tree the Files view paints. Rendering only:
// rows come in flat (see tree.ts `flattenTree`), the view owns every piece
// of state and every side effect. What lives here is what a file explorer
// has needed since the 1990s and a React tree of nested components does not
// give you:
//
// * **Windowing.** Only the rows inside the scroll viewport (± overscan) are
//   in the DOM. Twenty agents, a 2,000-entry directory, one open folder — the
//   DOM holds the same ~40 rows either way. Rows are a fixed height, so the
//   window is arithmetic on scrollTop, not measurement.
// * **The WAI-ARIA tree pattern.** `role="tree"` / `role="treeitem"` with
//   `aria-level`, `aria-expanded`, `aria-selected` and a roving tabindex, so
//   assistive tech reads it as a tree and one Tab lands in it.
// * **Keyboard.** ↑/↓ move, → expands or steps into, ← collapses or steps to
//   the parent, Home/End, Enter activates (open a file, toggle a folder),
//   F2 renames, Delete deletes, letters type-ahead by prefix.
//
// Rows are memoized: hover state is per row, and every callback the view
// passes in is read through a ref, so nothing above re-renders the window
// when the pointer moves.

import {
  memo,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type DragEvent,
  type KeyboardEvent,
  type MouseEvent,
  type ReactNode,
} from "react";
import { fmtSize, findByPrefix, splitKey, type NodeKey, type Row } from "./tree";

export const ROW_HEIGHT = 28;
const ROW_INDENT = 14;
const OVERSCAN = 8;

/** Custom drag MIME carrying `<agent>\t<path>` of the dragged entry. */
export const DND_MIME = "application/x-olano-path";

export function encodeDrag(agent: string, path: string): string {
  return `${agent}\t${path}`;
}
export function decodeDrag(data: string): { agent: string; path: string } | null {
  if (!data) return null;
  const i = data.indexOf("\t");
  return i === -1 ? null : { agent: data.slice(0, i), path: data.slice(i + 1) };
}

export interface TreeLabels {
  workspace: string;
  loading: string;
  retry: string;
  empty: string;
  truncated: (shown: string, total: string) => string;
  scanSize: (size: string, files: string) => string;
  /** Tooltip on a search hit — names where the match lives. */
  hit: (path: string) => string;
}

export interface TreeHandlers {
  /** Click / Enter on a navigable row. */
  onActivate: (row: Row) => void;
  /** Caret click, ← / → keys. */
  onToggle: (row: Row, open: boolean) => void;
  onContextMenu: (e: MouseEvent, row: Row) => void;
  onRename: (row: Row) => void;
  onDelete: (row: Row) => void;
  onMove: (src: { agent: string; path: string }, dest: { agent: string; path: string }) => void;
  onRetry: (agent: string, path: string) => void;
  onFocusChange: (key: NodeKey | null) => void;
}

export interface FileTreeProps {
  rows: Row[];
  selectedKey: NodeKey | null;
  focusedKey: NodeKey | null;
  /** One-shot: scroll this row into view once it exists (deep links). */
  revealKey: NodeKey | null;
  onRevealed: () => void;
  readOnly: boolean;
  labels: TreeLabels;
  handlers: TreeHandlers;
  icons: { folder: ReactNode; file: ReactNode; caret: ReactNode };
  statusColor: (status: string) => string;
  initialScrollTop: number;
  onScrollTop: (top: number) => void;
  /** Right-click on the empty space below the rows. */
  onBackgroundContextMenu?: (e: MouseEvent) => void;
  className?: string;
}

/** Rows a keyboard can land on. */
function navigable(row: Row): boolean {
  return row.kind === "agent" || row.kind === "workspace" || row.kind === "entry" || row.kind === "hit";
}
function expandable(row: Row): boolean {
  return row.kind === "agent" || row.kind === "workspace" || (row.kind === "entry" && row.entry.type === "directory");
}
function isOpen(row: Row): boolean {
  return (row.kind === "agent" || row.kind === "workspace" || row.kind === "entry") && row.expanded;
}
function labelOf(row: Row, workspaceLabel: string): string {
  if (row.kind === "agent") return row.info.label;
  if (row.kind === "workspace") return workspaceLabel;
  if (row.kind === "entry") return row.entry.name;
  if (row.kind === "hit") return row.hit.name;
  return "";
}

export function FileTree(props: FileTreeProps) {
  const {
    rows,
    selectedKey,
    focusedKey,
    revealKey,
    onRevealed,
    readOnly,
    labels,
    handlers,
    icons,
    statusColor,
    initialScrollTop,
    onScrollTop,
    onBackgroundContextMenu,
    className,
  } = props;

  const scrollerRef = useRef<HTMLDivElement>(null);
  const [scrollTop, setScrollTop] = useState(initialScrollTop);
  const [viewportH, setViewportH] = useState(600);
  const rafRef = useRef<number | null>(null);
  // Handlers through a ref so rows never re-render because a callback identity
  // changed in the parent.
  const handlersRef = useRef(handlers);
  handlersRef.current = handlers;
  const focusPendingRef = useRef<NodeKey | null>(null);
  const typeAhead = useRef({ buffer: "", at: 0 });

  // Viewport height tracks the element (window resize, split drag, phone
  // rotation) — the only measurement the window ever takes.
  useLayoutEffect(() => {
    const el = scrollerRef.current;
    if (!el) return;
    const measure = () => setViewportH(el.clientHeight || 600);
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // Restore the saved offset once (tab switch back); later scrolls are the
  // user's own.
  useLayoutEffect(() => {
    const el = scrollerRef.current;
    if (el && initialScrollTop > 0 && Math.abs(el.scrollTop - initialScrollTop) > 1) {
      el.scrollTop = initialScrollTop;
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const onScroll = useCallback(() => {
    const el = scrollerRef.current;
    if (!el) return;
    if (rafRef.current != null) return; // one window update per frame
    rafRef.current = window.requestAnimationFrame(() => {
      rafRef.current = null;
      const top = el.scrollTop;
      setScrollTop(top);
      onScrollTop(top);
    });
  }, [onScrollTop]);

  useEffect(
    () => () => {
      if (rafRef.current != null) window.cancelAnimationFrame(rafRef.current);
    },
    [],
  );

  const indexByKey = useMemo(() => {
    const m = new Map<NodeKey, number>();
    rows.forEach((r, i) => {
      if (navigable(r)) m.set(r.key, i);
    });
    return m;
  }, [rows]);

  const total = rows.length;
  const start = Math.max(0, Math.floor(scrollTop / ROW_HEIGHT) - OVERSCAN);
  const end = Math.min(total, Math.ceil((scrollTop + viewportH) / ROW_HEIGHT) + OVERSCAN);

  /** Make row `i` visible inside the scroller (no-op when it already is). */
  const scrollRowIntoView = useCallback((i: number) => {
    const el = scrollerRef.current;
    if (!el) return;
    const top = i * ROW_HEIGHT;
    const bottom = top + ROW_HEIGHT;
    if (top < el.scrollTop) el.scrollTop = top;
    else if (bottom > el.scrollTop + el.clientHeight) el.scrollTop = bottom - el.clientHeight;
  }, []);

  // Deep-link reveal: scroll the row into view once it exists in `rows`.
  useEffect(() => {
    if (!revealKey) return;
    const i = indexByKey.get(revealKey);
    if (i === undefined) return;
    scrollRowIntoView(i);
    onRevealed();
  }, [revealKey, indexByKey, scrollRowIntoView, onRevealed]);

  // Move DOM focus to the row the keyboard chose, after it has been painted.
  useEffect(() => {
    const key = focusPendingRef.current;
    if (!key) return;
    const el = scrollerRef.current?.querySelector<HTMLElement>(`[data-key="${cssEscape(key)}"]`);
    if (el) {
      focusPendingRef.current = null;
      el.focus({ preventScroll: true });
    }
  });

  const moveFocusTo = useCallback(
    (i: number) => {
      const row = rows[i];
      if (!row) return;
      scrollRowIntoView(i);
      focusPendingRef.current = row.key;
      handlersRef.current.onFocusChange(row.key);
    },
    [rows, scrollRowIntoView],
  );

  const onKeyDown = useCallback(
    (e: KeyboardEvent<HTMLDivElement>) => {
      const h = handlersRef.current;
      const cur = focusedKey ? (indexByKey.get(focusedKey) ?? -1) : -1;
      const navRows = (from: number, step: 1 | -1): number => {
        let i = from + step;
        while (i >= 0 && i < rows.length && !navigable(rows[i])) i += step;
        return i >= 0 && i < rows.length ? i : -1;
      };
      const firstNav = () => navRows(-1, 1);
      const lastNav = () => navRows(rows.length, -1);
      const row = cur >= 0 ? rows[cur] : undefined;

      switch (e.key) {
        case "ArrowDown": {
          e.preventDefault();
          const next = cur < 0 ? firstNav() : navRows(cur, 1);
          if (next >= 0) moveFocusTo(next);
          return;
        }
        case "ArrowUp": {
          e.preventDefault();
          const prev = cur < 0 ? lastNav() : navRows(cur, -1);
          if (prev >= 0) moveFocusTo(prev);
          return;
        }
        case "ArrowRight": {
          if (!row) return;
          e.preventDefault();
          if (expandable(row) && !isOpen(row)) h.onToggle(row, true);
          else if (isOpen(row)) {
            const next = navRows(cur, 1);
            if (next >= 0 && rows[next].depth > row.depth) moveFocusTo(next);
          }
          return;
        }
        case "ArrowLeft": {
          if (!row) return;
          e.preventDefault();
          if (isOpen(row)) h.onToggle(row, false);
          else {
            // Step to the nearest row above with a smaller depth: the parent.
            for (let i = cur - 1; i >= 0; i -= 1) {
              if (navigable(rows[i]) && rows[i].depth < row.depth) {
                moveFocusTo(i);
                break;
              }
            }
          }
          return;
        }
        case "Home": {
          e.preventDefault();
          const i = firstNav();
          if (i >= 0) moveFocusTo(i);
          return;
        }
        case "End": {
          e.preventDefault();
          const i = lastNav();
          if (i >= 0) moveFocusTo(i);
          return;
        }
        case "Enter":
        case " ": {
          if (!row) return;
          e.preventDefault();
          h.onActivate(row);
          return;
        }
        case "F2": {
          if (!row || readOnly || row.kind !== "entry") return;
          e.preventDefault();
          h.onRename(row);
          return;
        }
        case "Delete":
        case "Backspace": {
          if (!row || readOnly || row.kind !== "entry") return;
          if (e.key === "Backspace" && !(e.metaKey || e.ctrlKey)) return; // ⌘⌫ / Ctrl+⌫ only
          e.preventDefault();
          h.onDelete(row);
          return;
        }
        default:
          break;
      }
      // Type-ahead: printable characters typed within 700ms form a prefix.
      if (e.key.length === 1 && !e.metaKey && !e.ctrlKey && !e.altKey) {
        const now = Date.now();
        const ta = typeAhead.current;
        ta.buffer = now - ta.at < 700 ? ta.buffer + e.key : e.key;
        ta.at = now;
        const navIdx: number[] = [];
        const navLabels: string[] = [];
        rows.forEach((r, i) => {
          if (navigable(r)) {
            navIdx.push(i);
            navLabels.push(labelOf(r, labels.workspace));
          }
        });
        const from = cur >= 0 ? navIdx.indexOf(cur) : -1;
        const hit = findByPrefix(navLabels, from, ta.buffer);
        if (hit >= 0) {
          e.preventDefault();
          moveFocusTo(navIdx[hit]);
        }
      }
    },
    [rows, indexByKey, focusedKey, moveFocusTo, readOnly, labels.workspace],
  );

  const effectiveFocus = focusedKey && indexByKey.has(focusedKey) ? focusedKey : (rows.find(navigable)?.key ?? null);

  const visible: ReactNode[] = [];
  for (let i = start; i < end; i += 1) {
    const row = rows[i];
    visible.push(
      <TreeRow
        key={row.key + (row.kind === "entry" || navigable(row) ? "" : `#${row.kind}`)}
        row={row}
        top={i * ROW_HEIGHT}
        selected={row.key === selectedKey && row.kind === "entry"}
        tabbable={row.key === effectiveFocus}
        readOnly={readOnly}
        labels={labels}
        icons={icons}
        statusColor={statusColor}
        handlersRef={handlersRef}
      />,
    );
  }

  return (
    <div
      ref={scrollerRef}
      role="tree"
      aria-multiselectable={false}
      className={className}
      onScroll={onScroll}
      onKeyDown={onKeyDown}
      onContextMenu={(e) => {
        // Only the empty space: rows stop propagation of their own menus.
        if (onBackgroundContextMenu && e.target === e.currentTarget) onBackgroundContextMenu(e);
      }}
      style={{ overflow: "auto", flex: 1, minHeight: 0, position: "relative", outline: "none" }}
    >
      <div style={{ height: total * ROW_HEIGHT, position: "relative" }}>{visible}</div>
    </div>
  );
}

function cssEscape(s: string): string {
  return typeof CSS !== "undefined" && CSS.escape ? CSS.escape(s) : s.replace(/["\\]/g, "\\$&");
}

// ── one row ──────────────────────────────────────────────────────────────

interface TreeRowProps {
  row: Row;
  top: number;
  selected: boolean;
  tabbable: boolean;
  readOnly: boolean;
  labels: TreeLabels;
  icons: FileTreeProps["icons"];
  statusColor: (status: string) => string;
  handlersRef: { current: TreeHandlers };
}

const TreeRow = memo(function TreeRow({
  row,
  top,
  selected,
  tabbable,
  readOnly,
  labels,
  icons,
  statusColor,
  handlersRef,
}: TreeRowProps) {
  const [hover, setHover] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [dropping, setDropping] = useState(false);
  const base: CSSProperties = {
    position: "absolute",
    left: 0,
    right: 0,
    top,
    height: ROW_HEIGHT,
    display: "flex",
    alignItems: "center",
    gap: 6,
    padding: "0 8px",
    paddingLeft: 8 + row.depth * ROW_INDENT,
    boxSizing: "border-box",
    whiteSpace: "nowrap",
    fontSize: 13,
  };

  // Non-navigable rows: loading / error / truncated / empty.
  if (row.kind === "loading") {
    return (
      <div role="presentation" style={{ ...base, color: "var(--text-4)" }}>
        <span className="spinner" style={{ width: 12, height: 12, margin: 0 }} />
        <span style={{ fontSize: 12 }}>{labels.loading}</span>
      </div>
    );
  }
  if (row.kind === "error") {
    return (
      <div role="presentation" style={{ ...base, color: "var(--danger)", fontSize: 12, gap: 8 }}>
        <span style={{ minWidth: 0, overflow: "hidden", textOverflow: "ellipsis" }} title={row.error}>
          {row.error}
        </span>
        <button
          type="button"
          onClick={() => {
            const { agent, path } = splitKey(row.key);
            handlersRef.current.onRetry(agent, path);
          }}
          style={{
            flexShrink: 0,
            background: "none",
            border: "none",
            padding: 0,
            cursor: "pointer",
            font: "inherit",
            color: "var(--accent)",
            textDecoration: "underline",
          }}
        >
          {labels.retry}
        </button>
      </div>
    );
  }
  if (row.kind === "truncated") {
    return (
      <div role="presentation" className="muted" style={{ ...base, fontSize: 12 }} data-truncated-row>
        {labels.truncated(row.shown.toLocaleString(), row.total.toLocaleString())}
      </div>
    );
  }
  if (row.kind === "empty") {
    return (
      <div role="presentation" className="muted" style={{ ...base, fontSize: 12 }}>
        {labels.empty}
      </div>
    );
  }

  // A search hit: a match the server found in a directory the tree has not
  // listed. Reads like an entry row — same icon, same activation — with the
  // parent path beside the name, since its ancestors are not on screen.
  // Activating it opens the file (or folder) and reveals it in the tree. Not
  // draggable or renamable here: those need the real listing, one click away.
  if (row.kind === "hit") {
    const isDir = row.hit.type === "directory";
    const parent = row.hit.path.includes("/") ? row.hit.path.slice(0, row.hit.path.lastIndexOf("/")) : "";
    return (
      <div
        role="treeitem"
        data-key={row.key}
        data-kind="hit"
        data-agent={row.agent}
        aria-level={row.depth + 1}
        aria-selected={selected}
        tabIndex={tabbable ? 0 : -1}
        title={labels.hit(row.hit.path)}
        onFocus={() => handlersRef.current.onFocusChange(row.key)}
        onClick={(e) => {
          e.currentTarget.focus({ preventScroll: true });
          handlersRef.current.onActivate(row);
        }}
        onContextMenu={(e) => {
          e.stopPropagation();
          handlersRef.current.onContextMenu(e, row);
        }}
        onMouseEnter={() => setHover(true)}
        onMouseLeave={() => setHover(false)}
        style={{
          ...base,
          cursor: "pointer",
          userSelect: "none",
          border: "1px solid transparent",
          borderRadius: "var(--r-sm)",
          background: selected ? "var(--accent-soft)" : hover ? "var(--surface-2)" : "transparent",
          color: selected ? "var(--accent)" : "var(--text)",
          transition: "background var(--t-fast)",
        }}
      >
        <span style={{ flexShrink: 0, width: 14, height: 14 }} />
        <span style={{ flexShrink: 0, display: "flex", color: selected ? "var(--accent)" : "var(--text-3)" }}>
          {isDir ? icons.folder : icons.file}
        </span>
        <span style={{ flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis" }}>
          {row.hit.name}
          {parent && (
            <span style={{ color: "var(--text-4)", fontSize: 12, marginLeft: 8 }}>{parent}</span>
          )}
        </span>
      </div>
    );
  }

  const isAgent = row.kind === "agent";
  const isWorkspace = row.kind === "workspace";
  const isDir = isAgent || isWorkspace || row.entry.type === "directory";
  const open = row.expanded;
  const loading = !isAgent && row.loading;
  const { agent, path } = isAgent || isWorkspace ? { agent: row.agent, path: "" } : splitKey(row.key);
  // Drop target: a folder targets itself, a file its parent, agent/workspace the root.
  const dropDir = isAgent || isWorkspace ? "" : isDir ? path : path.slice(0, Math.max(0, path.lastIndexOf("/")));
  const draggable = !readOnly && row.kind === "entry";
  const bg = dropping || selected ? "var(--accent-soft)" : hover ? "var(--surface-2)" : "transparent";
  const label = labelOf(row, labels.workspace);

  return (
    <div
      role="treeitem"
      data-key={row.key}
      data-kind={row.kind}
      data-agent={agent}
      aria-level={row.depth + 1}
      aria-expanded={isDir ? open : undefined}
      aria-selected={selected}
      tabIndex={tabbable ? 0 : -1}
      title={isAgent ? `${label} (${row.info.name})` : label}
      draggable={draggable}
      onFocus={() => handlersRef.current.onFocusChange(row.key)}
      onDragStart={
        draggable
          ? (e: DragEvent) => {
              e.dataTransfer.setData(DND_MIME, encodeDrag(agent, path));
              e.dataTransfer.setData("text/plain", path);
              e.dataTransfer.effectAllowed = "move";
              setDragging(true);
            }
          : undefined
      }
      onDragEnd={() => setDragging(false)}
      onDragOver={
        readOnly
          ? undefined
          : (e: DragEvent) => {
              if (!e.dataTransfer.types.includes(DND_MIME)) return;
              e.preventDefault();
              e.dataTransfer.dropEffect = "move";
              setDropping(true);
            }
      }
      onDragLeave={() => setDropping(false)}
      onDrop={
        readOnly
          ? undefined
          : (e: DragEvent) => {
              e.preventDefault();
              e.stopPropagation();
              setDropping(false);
              const src = decodeDrag(e.dataTransfer.getData(DND_MIME));
              if (src) handlersRef.current.onMove(src, { agent, path: dropDir });
            }
      }
      onClick={(e) => {
        e.currentTarget.focus({ preventScroll: true });
        handlersRef.current.onActivate(row);
      }}
      onContextMenu={(e) => {
        e.stopPropagation();
        handlersRef.current.onContextMenu(e, row);
      }}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      style={{
        ...base,
        cursor: "pointer",
        userSelect: "none",
        border: dropping ? "1px solid var(--accent)" : "1px solid transparent",
        borderRadius: "var(--r-sm)",
        opacity: dragging ? 0.45 : 1,
        background: bg,
        color: selected ? "var(--accent)" : "var(--text)",
        fontWeight: isAgent ? 600 : 400,
        transition: "background var(--t-fast), opacity var(--t-fast)",
      }}
    >
      {/* disclosure caret (expandable) or alignment spacer */}
      <span
        onClick={
          isDir
            ? (e) => {
                e.stopPropagation();
                handlersRef.current.onToggle(row, !open);
              }
            : undefined
        }
        style={{
          flexShrink: 0,
          width: 14,
          height: 14,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          color: "var(--text-4)",
          transform: open ? "rotate(90deg)" : "none",
          transition: "transform var(--t-fast)",
        }}
      >
        {isDir ? icons.caret : null}
      </span>
      {isAgent ? (
        <>
          <span className="agent-status-dot" style={{ background: statusColor(row.info.status) }} />
          {row.info.emoji && <span className="agent-item-emoji">{row.info.emoji}</span>}
        </>
      ) : (
        <span style={{ flexShrink: 0, display: "flex", color: selected ? "var(--accent)" : "var(--text-3)" }}>
          {isDir ? icons.folder : icons.file}
        </span>
      )}
      <span style={{ flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis" }}>{label}</span>
      {loading && <span className="spinner" style={{ width: 11, height: 11, margin: 0, flexShrink: 0 }} />}
      {row.kind === "entry" && (
        <span
          title={
            isDir && row.entry.scan_bytes != null
              ? labels.scanSize(fmtSize(row.entry.scan_bytes), (row.entry.scan_files ?? 0).toLocaleString())
              : undefined
          }
          style={{ fontSize: 11, flexShrink: 0, fontFamily: "var(--font-mono)", color: "var(--text-4)" }}
        >
          {isDir ? (row.entry.scan_bytes != null ? fmtSize(row.entry.scan_bytes) : "") : fmtSize(row.entry.size)}
        </span>
      )}
    </div>
  );
});
