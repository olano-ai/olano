// The recovery-key ceremony, step by step, against the sealer's API
// (POST /api/seal/recovery/{start,confirm,cancel}, proxied by Caddy to the
// sealer's unix socket after the runtime's owner gate).
//
//   intro   -> what is about to happen; "Generate the key" posts start
//   show    -> the key, ONCE: it lives in this component's state and nowhere
//              else on this side (the sealer has it in the volume's keyslot,
//              which is the point); a checkbox gates the next step
//   confirm -> the owner types it back; the sealer checks it against the
//              volume itself (400 mismatch keeps the step, 409 no_pending
//              means the sealer expired it -> the expired step)
//   done    -> acknowledged; for a replace, the old key is gone
//
// Closing while this dialog owns a pending key (started here, not yet
// confirmed) asks first and then cancels it: a key left pending blocks any
// new one for its TTL and keeps a live keyslot nobody has confirmed. A key
// the card sent us to confirm (mode "confirm") is the card's to cancel, so
// closing that one just closes.

import { useState, type FormEvent } from "react";
import { ShieldAlert } from "lucide-react";

import { api, ApiError } from "../../core/api";
import { useT } from "../../core/i18n";
import { useConfirm } from "../../ui/confirm";
import { Field, Modal } from "../agents/Modal";
import { CopyButton } from "../agents/CopyButton";
import { sealError, type SealState } from "./DiskEncryptionCard";
import { formatRecoveryKey, isValidRecoveryKey, normalizeRecoveryKey } from "./recoveryKey";
import { STR } from "./strings";

interface StartResponse {
  op_id: string;
  key: string;
  slot: number;
  expires_at: string;
  expires_in?: number;
  replaces_slot?: number | null;
}

interface ConfirmResponse {
  status: string;
  slot: number;
  at: string;
  replaced_slot?: number | null;
}

type Step = "intro" | "show" | "confirm" | "done" | "expired";

const when = (iso: string | null | undefined): string =>
  iso ? new Date(iso).toLocaleString() : "?";

export function RecoveryKeyDialog({
  mode,
  pending,
  onClose,
}: {
  mode: "create" | "replace" | "confirm";
  /** The sealer's pending record when the card sent us straight to confirm. */
  pending: SealState["recovery"] | null;
  onClose: () => void;
}) {
  const t = useT(STR);
  const confirmDialog = useConfirm();
  const [step, setStep] = useState<Step>(mode === "confirm" ? "confirm" : "intro");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [opId, setOpId] = useState<string | null>(mode === "confirm" ? (pending?.op_id ?? null) : null);
  const [expiresAt, setExpiresAt] = useState<string | null>(
    mode === "confirm" ? (pending?.expires_at ?? null) : null,
  );
  const [key, setKey] = useState<string | null>(null);
  const [stored, setStored] = useState(false);
  const [typed, setTyped] = useState("");
  const [replaced, setReplaced] = useState(false);

  // A key started here and not yet confirmed is ours to clean up on close.
  const ownsPending = mode !== "confirm" && opId !== null && (step === "show" || step === "confirm");

  const title =
    mode === "replace"
      ? t("diskEncryption.dialog.replaceTitle")
      : step === "confirm"
        ? t("diskEncryption.dialog.confirmTitle")
        : t("diskEncryption.dialog.createTitle");

  async function requestClose() {
    if (busy) return;
    if (!ownsPending) {
      onClose();
      return;
    }
    const abandon = await confirmDialog({
      title: t("diskEncryption.dialog.abandonTitle"),
      message: t("diskEncryption.dialog.abandonBody"),
      confirmLabel: t("diskEncryption.dialog.abandon"),
      cancelLabel: t("diskEncryption.dialog.keepGoing"),
      danger: true,
    });
    if (!abandon) return;
    setBusy(true);
    try {
      await api.post("/api/seal/recovery/cancel", { op_id: opId });
    } catch {
      // The sealer expires it on its own within the TTL; nothing to tell the
      // user that the card's pending row will not.
    } finally {
      setBusy(false);
      setKey(null);
      onClose();
    }
  }

  async function start() {
    setBusy(true);
    setError("");
    try {
      const res = await api.post<StartResponse>("/api/seal/recovery/start", {});
      setOpId(res.op_id);
      setExpiresAt(res.expires_at);
      setKey(res.key);
      setStored(false);
      setStep("show");
    } catch (err) {
      setError(sealError(err, t));
    } finally {
      setBusy(false);
    }
  }

  async function confirm(e: FormEvent) {
    e.preventDefault();
    if (!isValidRecoveryKey(typed)) {
      setError(t("diskEncryption.dialog.invalid"));
      return;
    }
    setBusy(true);
    setError("");
    try {
      const res = await api.post<ConfirmResponse>("/api/seal/recovery/confirm", {
        op_id: opId,
        key: normalizeRecoveryKey(typed),
      });
      setReplaced(res.replaced_slot !== null && res.replaced_slot !== undefined);
      setKey(null);
      setTyped("");
      setStep("done");
    } catch (err) {
      if (err instanceof ApiError && err.code === "mismatch") {
        setError(t("diskEncryption.dialog.mismatch", { when: when(expiresAt) }));
      } else if (err instanceof ApiError && err.code === "invalid_key") {
        setError(t("diskEncryption.dialog.invalid"));
      } else if (err instanceof ApiError && err.code === "no_pending") {
        setKey(null);
        setTyped("");
        setStep("expired");
      } else {
        setError(sealError(err, t));
      }
    } finally {
      setBusy(false);
    }
  }

  function startOver() {
    setOpId(null);
    setExpiresAt(null);
    setKey(null);
    setTyped("");
    setError("");
    setStep("intro");
  }

  const closeButton = (
    <button className="btn" type="button" onClick={() => void requestClose()} disabled={busy}>
      {step === "done" || step === "expired"
        ? t("diskEncryption.dialog.close")
        : t("diskEncryption.dialog.cancel")}
    </button>
  );

  if (step === "intro") {
    return (
      <Modal
        title={title}
        onClose={() => void requestClose()}
        footer={
          <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
            {closeButton}
            <button className="btn primary" type="button" onClick={() => void start()} disabled={busy}>
              {busy ? t("diskEncryption.dialog.starting") : t("diskEncryption.dialog.start")}
            </button>
          </div>
        }
      >
        <p style={{ fontSize: 13, lineHeight: 1.5, marginTop: 0 }}>{t("diskEncryption.dialog.intro")}</p>
        {mode === "replace" && (
          <p style={{ fontSize: 13, lineHeight: 1.5 }}>{t("diskEncryption.dialog.introReplace")}</p>
        )}
        {error && <p style={{ color: "var(--danger)", fontSize: 12.5 }}>{error}</p>}
      </Modal>
    );
  }

  if (step === "show" && key) {
    const dashed = formatRecoveryKey(key);
    return (
      <Modal
        title={t("diskEncryption.dialog.showTitle")}
        onClose={() => void requestClose()}
        footer={
          <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
            {closeButton}
            <button
              className="btn primary"
              type="button"
              disabled={!stored || busy}
              onClick={() => {
                setError("");
                setStep("confirm");
              }}
            >
              {t("diskEncryption.dialog.next")}
            </button>
          </div>
        }
      >
        <div
          role="alert"
          style={{
            display: "flex",
            gap: 10,
            alignItems: "flex-start",
            background: "var(--warn-soft)",
            color: "var(--warn)",
            borderRadius: 8,
            padding: "10px 12px",
            fontSize: 12.5,
            lineHeight: 1.5,
          }}
        >
          <ShieldAlert size={16} strokeWidth={1.75} style={{ flexShrink: 0, marginTop: 2 }} />
          <span>{t("diskEncryption.dialog.showWarning")}</span>
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 14 }}>
          <code
            data-testid="recovery-key-shown"
            style={{
              flex: 1,
              fontSize: 15,
              letterSpacing: "0.04em",
              wordBreak: "break-all",
              userSelect: "all",
              padding: "10px 12px",
              borderRadius: 8,
              border: "1px solid var(--border)",
            }}
          >
            {dashed}
          </code>
          <CopyButton value={dashed} label={t("diskEncryption.dialog.copy")} t={t} />
        </div>
        <p className="muted" style={{ fontSize: 12, marginTop: 10 }}>
          {t("diskEncryption.recovery.pendingUntil", { when: when(expiresAt) })}
        </p>
        <label style={{ display: "flex", gap: 8, alignItems: "center", fontSize: 13, marginTop: 10 }}>
          <input
            type="checkbox"
            checked={stored}
            onChange={(e) => setStored(e.target.checked)}
            data-testid="recovery-key-stored"
          />
          {t("diskEncryption.dialog.stored")}
        </label>
      </Modal>
    );
  }

  if (step === "confirm") {
    return (
      <Modal title={t("diskEncryption.dialog.confirmTitle")} onClose={() => void requestClose()}>
        <form onSubmit={(e) => void confirm(e)}>
          <p style={{ fontSize: 13, lineHeight: 1.5, marginTop: 0 }}>{t("diskEncryption.dialog.confirmBody")}</p>
          <Field label={t("diskEncryption.dialog.confirmLabel")}>
            <input
              className="input"
              name="recovery-key"
              data-testid="recovery-key-input"
              value={typed}
              onChange={(e) => setTyped(e.target.value)}
              placeholder="XXXX-XXXX-XXXX-XXXX-XXXX-XXXX-XXXX-XXXX"
              autoComplete="off"
              autoCapitalize="characters"
              spellCheck={false}
              autoFocus
              style={{ fontFamily: "var(--font-mono)", letterSpacing: "0.04em" }}
            />
          </Field>
          {error && <p style={{ color: "var(--danger)", fontSize: 12.5 }}>{error}</p>}
          <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
            {closeButton}
            <button className="btn primary" type="submit" disabled={busy || !typed.trim()}>
              {busy ? t("diskEncryption.dialog.confirming") : t("diskEncryption.dialog.confirm")}
            </button>
          </div>
        </form>
      </Modal>
    );
  }

  if (step === "expired") {
    return (
      <Modal
        title={t("diskEncryption.dialog.expiredTitle")}
        onClose={onClose}
        footer={
          <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
            {closeButton}
            {mode !== "confirm" && (
              <button className="btn primary" type="button" onClick={startOver}>
                {t("diskEncryption.dialog.startOver")}
              </button>
            )}
          </div>
        }
      >
        <p style={{ fontSize: 13, lineHeight: 1.5, marginTop: 0 }}>{t("diskEncryption.dialog.expiredBody")}</p>
      </Modal>
    );
  }

  // done (and the one unreachable shape, show without a key, closes cleanly)
  return (
    <Modal
      title={t("diskEncryption.dialog.doneTitle")}
      onClose={onClose}
      footer={
        <button className="btn primary" type="button" onClick={onClose}>
          {t("diskEncryption.dialog.close")}
        </button>
      }
    >
      <p style={{ fontSize: 13, lineHeight: 1.5, marginTop: 0 }}>{t("diskEncryption.dialog.doneBody")}</p>
      {replaced && <p style={{ fontSize: 13, lineHeight: 1.5 }}>{t("diskEncryption.dialog.doneReplaced")}</p>}
    </Modal>
  );
}
