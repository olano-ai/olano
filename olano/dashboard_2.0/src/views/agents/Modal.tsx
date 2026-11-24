// Local modal shell + form primitives for the Agents view. Mirrors the
// scheduler view's Modal/Field but kept self-contained so the agents view owns
// its files. Built from the Aurora tokens (no hardcoded colours). Closes on
// backdrop click or Esc.

import { useRef, useState, type ReactNode } from "react";
import { X, Plus, Sparkles } from "lucide-react";
import { api, ApiError } from "../../core/api";
import { useT } from "../../core/i18n";
import { useToast } from "../../ui/toast";
import { useDialogA11y } from "../../ui/dialogA11y";
import { STR } from "./strings";

export function Modal({
  title,
  subtitle,
  onClose,
  children,
  footer,
  width = 560,
}: {
  title: string;
  subtitle?: string;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
  width?: number;
}) {
  const t = useT(STR);
  const panelRef = useRef<HTMLDivElement>(null);
  // Escape, focus trap + restore — shared dialog behavior (ui/dialogA11y).
  useDialogA11y(panelRef, onClose);

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div
        className="card modal-card"
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onClick={(e) => e.stopPropagation()}
        style={{
          // Cap to the viewport (minus the overlay's 20px padding each side) so
          // wide modals like the MCP editor don't overflow + clip on phones —
          // "100%" alone doesn't shrink the card under the grid-centered overlay.
          // The vertical cap is .modal-card's job (it knows about iOS browser
          // chrome and the keyboard, which `90vh` did not).
          width,
          maxWidth: "calc(100vw - 40px)",
          display: "flex",
          flexDirection: "column",
        }}
      >
        <div className="modal-head">
          <div style={{ minWidth: 0 }}>
            <strong style={{ fontSize: 16 }}>{title}</strong>
            {subtitle ? (
              <div className="muted" style={{ fontSize: 12, marginTop: 2 }}>
                {subtitle}
              </div>
            ) : null}
          </div>
          <button
            className="btn"
            onClick={onClose}
            aria-label={t("card.close")}
            style={{ padding: "4px 9px", flexShrink: 0 }}
          >
            <X size={15} strokeWidth={1.75} />
          </button>
        </div>
        <div className="modal-body">{children}</div>
        {footer ? <div className="modal-foot">{footer}</div> : <div style={{ height: 20 }} />}
      </div>
    </div>
  );
}

export function Field({
  label,
  hint,
  action,
  children,
}: {
  label: string;
  // ReactNode, not string: a field's explanation is often a paragraph plus an
  // <OptionNotes> list saying what each choice in the control actually does.
  hint?: ReactNode;
  action?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="field" style={{ marginBottom: 14 }}>
      {action ? (
        <div className="row" style={{ justifyContent: "space-between", alignItems: "center", gap: 8 }}>
          <label>{label}</label>
          {action}
        </div>
      ) : (
        <label>{label}</label>
      )}
      {children}
      {hint ? (
        <div className="muted" style={{ fontSize: 11, lineHeight: 1.45 }}>
          {hint}
        </div>
      ) : null}
    </div>
  );
}

// "Generate with AI" affordance for system-prompt fields. Sends the in-progress
// name / description / tools to the runtime, which asks an LLM to draft a
// focused system prompt, then hands the result back via `onGenerated`. Self
// contained (owns its own busy state + toasts) so the create agent, edit agent,
// and subagent forms can drop it in identically.
export function GeneratePromptButton({
  name,
  description,
  tools,
  model,
  agent,
  kind = "agent",
  onGenerated,
}: {
  name: string;
  description: string;
  tools: string[];
  model?: string;
  agent?: string;
  kind?: "agent" | "subagent";
  onGenerated: (prompt: string) => void;
}) {
  const t = useT(STR);
  const toast = useToast();
  const [busy, setBusy] = useState(false);

  async function generate() {
    if (!name.trim() && !description.trim()) {
      toast.push(t("genPrompt.needContext"), "error");
      return;
    }
    setBusy(true);
    try {
      const res = await api.post<{ system_prompt: string }>("/api/agents/generate-prompt", {
        name: name.trim(),
        description: description.trim(),
        tools,
        model: model || undefined,
        agent: agent || undefined,
        kind,
      });
      const prompt = (res?.system_prompt || "").trim();
      if (!prompt) {
        toast.push(t("genPrompt.empty"), "error");
        return;
      }
      onGenerated(prompt);
      toast.push(t("genPrompt.done"));
    } catch (e) {
      const msg = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : t("genPrompt.failed");
      toast.push(msg, "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <button
      type="button"
      className="btn ghost row"
      onClick={() => void generate()}
      disabled={busy}
      title={t("genPrompt.hint")}
      style={{ padding: "3px 9px", fontSize: 12, gap: 5 }}
    >
      {busy ? <span className="spinner" style={{ width: 13, height: 13 }} /> : <Sparkles size={13} strokeWidth={1.75} />}
      {busy ? t("genPrompt.busy") : t("genPrompt.label")}
    </button>
  );
}

// Confirmation modal — replaces window.confirm with a themed dialog so
// destructive actions read native to Aurora (danger gradient button, etc).
export function ConfirmModal({
  title,
  message,
  confirmLabel,
  danger = false,
  busy = false,
  onConfirm,
  onClose,
}: {
  title: string;
  message: ReactNode;
  confirmLabel?: string;
  danger?: boolean;
  busy?: boolean;
  onConfirm: () => void;
  onClose: () => void;
}) {
  const t = useT(STR);
  return (
    <Modal
      title={title}
      onClose={onClose}
      width={440}
      footer={
        <>
          <button className="btn" onClick={onClose} disabled={busy}>
            {t("btn.cancel")}
          </button>
          <button
            className={danger ? "btn danger" : "btn primary"}
            onClick={onConfirm}
            disabled={busy}
            autoFocus
          >
            {busy ? t("modal.working") : (confirmLabel ?? t("modal.confirm"))}
          </button>
        </>
      }
    >
      <div className="muted" style={{ fontSize: 13, lineHeight: 1.55 }}>
        {message}
      </div>
    </Modal>
  );
}

// Compact multi-select for tool names: filterable list + selected chips.
// Dependency-free replacement for the original tagPicker + tool browser.
export function ToolPicker({
  all,
  selected,
  onChange,
}: {
  all: string[];
  selected: string[];
  onChange: (next: string[]) => void;
}) {
  const t = useT(STR);
  const [q, setQ] = useState("");
  const sel = new Set(selected);
  const matches = all
    .filter((item) => !sel.has(item) && item.toLowerCase().includes(q.toLowerCase()))
    .slice(0, 60);

  return (
    <div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 5, marginBottom: selected.length ? 8 : 0 }}>
        {selected.map((item) => (
          <span
            key={item}
            className="badge"
            style={{ paddingRight: 4, gap: 4 }}
          >
            {item}
            <button
              type="button"
              aria-label={t("ov.removeTool", { name: item })}
              onClick={() => onChange(selected.filter((x) => x !== item))}
              style={{
                border: "none",
                background: "none",
                color: "var(--text-3)",
                cursor: "pointer",
                padding: "0 2px",
                lineHeight: 0,
                display: "inline-flex",
              }}
            >
              <X size={13} strokeWidth={2} />
            </button>
          </span>
        ))}
      </div>
      <input
        className="input"
        placeholder={t("tool.searchPlaceholder")}
        value={q}
        onChange={(e) => setQ(e.target.value)}
      />
      {q && (
        <div
          className="card"
          style={{ marginTop: 6, maxHeight: 180, overflow: "auto", padding: 4 }}
        >
          {matches.length === 0 ? (
            <div className="muted" style={{ padding: "8px 10px", fontSize: 13 }}>
              {t("tool.noMatches")}
            </div>
          ) : (
            matches.map((item) => (
              <button
                key={item}
                type="button"
                className="menu-item"
                onClick={() => {
                  onChange([...selected, item]);
                  setQ("");
                }}
              >
                <Plus size={13} strokeWidth={2} style={{ color: "var(--accent)" }} /> {item}
              </button>
            ))
          )}
        </div>
      )}
    </div>
  );
}

// Accent-colour picker: preset swatches + a free-text hex field. Themes the
// agent card bar / avatar tint. Empty string clears the colour.
const COLOR_PRESETS = [
  "#ff6a3d",
  "#fb923c",
  "#fbbf24",
  "#34d399",
  "#22d3ee",
  "#60a5fa",
  "#818cf8",
  "#c084fc",
  "#f472b6",
  "#a3a3a3",
];

export function ColorPicker({ value, onChange }: { value: string; onChange: (c: string) => void }) {
  const t = useT(STR);
  return (
    <div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBottom: 8 }}>
        {COLOR_PRESETS.map((c) => (
          <button
            key={c}
            type="button"
            onClick={() => onChange(c)}
            aria-label={c}
            style={{
              width: 24,
              height: 24,
              borderRadius: "50%",
              background: c,
              cursor: "pointer",
              border: value.toLowerCase() === c.toLowerCase() ? "2px solid var(--text)" : "2px solid transparent",
              boxShadow: value.toLowerCase() === c.toLowerCase() ? "0 0 0 2px var(--bg)" : "none",
            }}
          />
        ))}
      </div>
      <div className="row" style={{ gap: 8 }}>
        <input
          className="input"
          placeholder="#ff6a3d"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          style={{ width: 130, fontFamily: "var(--font-mono)", fontSize: 12 }}
        />
        <button type="button" className="btn ghost" onClick={() => onChange("")}>
          {t("color.clear")}
        </button>
      </div>
    </div>
  );
}
