// Config → Disk encryption: the sealed data volume of a managed box and the
// one action it asks of the owner, the recovery key.
//
// Two sources, deliberately. `/api/status` (the runtime, cached 5 s server
// side) says whether this box HAS a sealed volume at all: custody mode "none"
// is every legacy and self-hosted install, and then this component renders
// nothing. Everything live comes from `GET /api/seal/state`, which Caddy
// proxies to the sealer's own unix socket after asking the runtime's owner
// gate (deploy/Caddyfile.allinone); the runtime itself never answers it, so
// on a box without Caddy in front (the dev harness) it is a 404 and on a
// sealed box whose serve unit is down it is a 502. Both read as "the
// encryption service is unavailable" here: the card still shows what
// /api/status knows and pauses only the actions.
//
// The recovery key itself never passes through this file: RecoveryKeyDialog
// holds it in component state for the one screen that shows it.

import { useCallback, useEffect, useState } from "react";
import { ShieldCheck } from "lucide-react";

import { api, ApiError } from "../../core/api";
import { useT } from "../../core/i18n";
import { useConfirm } from "../../ui/confirm";
import { useToast } from "../../ui/toast";
import { KvRow } from "../agents/Toggle";
import { RecoveryKeyDialog } from "./RecoveryKeyDialog";
import { Section } from "./Section";
import { STR } from "./strings";

// The custody half of /api/status (runtime/seal_guard.py custody_status).
interface Custody {
  mode: string;
  sealed?: boolean;
  luks_uuid?: string | null;
  sealer_version?: string | null;
  recovery?: { status: string; at: string | null };
  header_generation?: number | null;
  ceremony_api?: boolean;
}

// GET /api/seal/state, answered by the sealer (olano-sealer serve).
export interface SealState {
  mode: string;
  luks_uuid: string | null;
  sealer_version: string;
  recovery: {
    status: "none" | "pending" | "acknowledged" | string;
    slot?: number;
    at?: string | null;
    expires_at?: string | null;
    op_id?: string | null;
    replaces_slot?: number | null;
  };
  header: { generation: number | null; uploaded_at: string | null; pending_upload?: boolean };
}

type ServiceState = { kind: "loading" } | { kind: "ok"; state: SealState } | { kind: "unavailable" };

const when = (iso: string | null | undefined): string =>
  iso ? new Date(iso).toLocaleString() : "";

export function DiskEncryptionCard() {
  const t = useT(STR);
  const toast = useToast();
  const confirm = useConfirm();
  const [custody, setCustody] = useState<Custody | null | undefined>(undefined);
  const [service, setService] = useState<ServiceState>({ kind: "loading" });
  const [dialog, setDialog] = useState<"create" | "replace" | "confirm" | null>(null);
  const [cancelling, setCancelling] = useState(false);

  useEffect(() => {
    let alive = true;
    api
      .cachedGet<{ custody?: Custody }>("/api/status")
      .then((s) => alive && setCustody(s.custody ?? null))
      .catch(() => alive && setCustody(null));
    return () => {
      alive = false;
    };
  }, []);

  const sealed = !!custody && custody.mode !== "none";

  const loadService = useCallback(() => {
    api
      .get<SealState>("/api/seal/state")
      .then((state) => setService({ kind: "ok", state }))
      .catch(() => setService({ kind: "unavailable" }));
  }, []);

  useEffect(() => {
    if (sealed) loadService();
  }, [sealed, loadService]);

  if (!sealed || !custody) return null;

  // The live document when the sealer answers, else what the runtime knows.
  const live = service.kind === "ok" ? service.state : null;
  const recovery = live?.recovery ?? custody.recovery ?? { status: "none", at: null };
  const generation = live ? live.header.generation : (custody.header_generation ?? null);
  const uploadedAt = live?.header.uploaded_at ?? null;
  const version = live?.sealer_version ?? custody.sealer_version ?? null;
  const actionsReady = service.kind === "ok";

  async function cancelPending() {
    const ok = await confirm({
      title: t("diskEncryption.recovery.cancelConfirm.title"),
      message: t("diskEncryption.recovery.cancelConfirm.body"),
      confirmLabel: t("diskEncryption.recovery.cancelPending"),
      danger: true,
    });
    if (!ok) return;
    setCancelling(true);
    try {
      await api.post("/api/seal/recovery/cancel", { op_id: live?.recovery.op_id ?? null });
      toast.push(t("diskEncryption.recovery.cancelled"), "success");
    } catch (err) {
      toast.push(sealError(err, t), "error");
    } finally {
      setCancelling(false);
      loadService();
    }
  }

  async function replaceKey() {
    const ok = await confirm({
      title: t("diskEncryption.recovery.replaceConfirm.title"),
      message: t("diskEncryption.recovery.replaceConfirm.body"),
      confirmLabel: t("diskEncryption.recovery.replace"),
    });
    if (ok) setDialog("replace");
  }

  // KvRow keeps its right column at its natural width, so everything there
  // must stay short (a badge): on a phone a button row or a date beside the
  // label squeezes the hint to a word per line. Actions, dates and the UUID
  // go in the row's full-width note instead.
  let recoveryValue: string;
  let recoveryNote: string | null = null;
  let recoveryNoteTone: string = "var(--muted)";
  if (recovery.status === "pending") {
    recoveryValue = t("diskEncryption.recovery.pending");
    recoveryNote = t("diskEncryption.recovery.pendingUntil", {
      when: when((recovery as SealState["recovery"]).expires_at) || "?",
    });
    recoveryNoteTone = "var(--warn)";
  } else if (recovery.status === "acknowledged") {
    recoveryValue = t("diskEncryption.recovery.acknowledged");
    recoveryNote = t("diskEncryption.recovery.activeSince", { when: when(recovery.at) || "?" });
  } else {
    recoveryValue = t("diskEncryption.recovery.none");
  }

  const recoveryActions =
    recovery.status === "pending" ? (
      <>
        <button
          className="btn"
          type="button"
          disabled={!actionsReady}
          onClick={() => setDialog("confirm")}
        >
          {t("diskEncryption.recovery.confirmPending")}
        </button>
        <button
          className="btn danger"
          type="button"
          disabled={!actionsReady || cancelling}
          onClick={() => void cancelPending()}
        >
          {t("diskEncryption.recovery.cancelPending")}
        </button>
      </>
    ) : recovery.status === "acknowledged" ? (
      <button className="btn" type="button" disabled={!actionsReady} onClick={() => void replaceKey()}>
        {t("diskEncryption.recovery.replace")}
      </button>
    ) : (
      <button
        className="btn primary"
        type="button"
        disabled={!actionsReady}
        onClick={() => setDialog("create")}
      >
        {t("diskEncryption.recovery.create")}
      </button>
    );

  return (
    <Section
      settingId="config.diskEncryption"
      title={t("section.diskEncryption")}
      hint={t("section.diskEncryption.hint")}
      right={
        <button
          className="btn"
          type="button"
          style={{ whiteSpace: "nowrap" }}
          onClick={() => {
            setService({ kind: "loading" });
            loadService();
          }}
          disabled={service.kind === "loading"}
        >
          {t("diskEncryption.refresh")}
        </button>
      }
    >
      <KvRow
        settingId="config.diskEncryption.status"
        label={t("diskEncryption.status")}
        hint={t("diskEncryption.status.hint")}
      >
        <span
          className="badge"
          style={{
            background: "var(--success-soft)",
            color: "var(--success)",
            display: "inline-flex",
            alignItems: "center",
            gap: 6,
          }}
        >
          <ShieldCheck size={13} strokeWidth={1.75} />
          {custody.mode === "standard"
            ? t("diskEncryption.status.standard")
            : t("diskEncryption.status.other", { mode: custody.mode })}
        </span>
      </KvRow>

      <KvRow
        settingId="config.diskEncryption.volume"
        label={t("diskEncryption.volume")}
        hint={t("diskEncryption.volume.hint")}
        note={
          <code
            data-testid="disk-encryption-uuid"
            style={{ display: "block", fontSize: 12, wordBreak: "break-all", marginTop: 8 }}
          >
            {custody.luks_uuid ?? "?"}
          </code>
        }
      >
        <span />
      </KvRow>

      <KvRow
        settingId="config.diskEncryption.recovery"
        label={t("diskEncryption.recovery")}
        hint={t("diskEncryption.recovery.hint")}
        note={
          <div style={{ marginTop: 8 }}>
            {recoveryNote ? (
              <p style={{ fontSize: 12.5, margin: "0 0 8px", color: recoveryNoteTone }}>{recoveryNote}</p>
            ) : null}
            <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center" }}>
              {recoveryActions}
            </div>
          </div>
        }
      >
        <span
          className="badge"
          data-testid="disk-encryption-recovery"
          style={
            recovery.status === "acknowledged"
              ? { background: "var(--success-soft)", color: "var(--success)" }
              : { background: "var(--warn-soft)", color: "var(--warn)" }
          }
        >
          {recoveryValue}
        </span>
      </KvRow>

      <KvRow
        settingId="config.diskEncryption.header"
        label={t("diskEncryption.header")}
        hint={t("diskEncryption.header.hint")}
        note={
          generation && uploadedAt ? (
            <p className="muted" style={{ fontSize: 12.5, margin: "8px 0 0" }}>
              {t("diskEncryption.header.uploadedAt", { when: when(uploadedAt) })}
            </p>
          ) : null
        }
      >
        <span
          className="badge"
          style={
            generation
              ? { background: "var(--success-soft)", color: "var(--success)" }
              : { background: "var(--warn-soft)", color: "var(--warn)" }
          }
        >
          {generation
            ? t("diskEncryption.header.generation", { n: String(generation) })
            : t("diskEncryption.header.none")}
        </span>
      </KvRow>

      <KvRow
        settingId="config.diskEncryption.service"
        label={t("diskEncryption.service")}
        hint={t("diskEncryption.service.hint")}
        note={
          service.kind === "unavailable" ? (
            <p style={{ fontSize: 12.5, margin: "8px 0 0", color: "var(--warn)" }}>
              {t("diskEncryption.service.unavailable.hint")}
            </p>
          ) : null
        }
      >
        {service.kind === "loading" ? (
          <div className="spinner" />
        ) : service.kind === "ok" ? (
          <span className="badge" style={{ background: "var(--success-soft)", color: "var(--success)" }}>
            {t("diskEncryption.service.ok", { version: version ?? "?" })}
          </span>
        ) : (
          <span className="badge" style={{ background: "var(--warn-soft)", color: "var(--warn)" }}>
            {t("diskEncryption.service.unavailable")}
          </span>
        )}
      </KvRow>

      {dialog && (
        <RecoveryKeyDialog
          mode={dialog}
          pending={dialog === "confirm" && live ? live.recovery : null}
          onClose={() => {
            setDialog(null);
            loadService();
          }}
        />
      )}
    </Section>
  );
}

/** A sealer refusal or a transport failure as one readable line. */
export function sealError(err: unknown, t: (key: string, vars?: Record<string, string>) => string): string {
  if (err instanceof ApiError) {
    if (err.status === 502 || err.status === 503 || err.status === 504 || err.status === 404) {
      return t("diskEncryption.dialog.unavailable");
    }
    if (err.code === "busy") return t("diskEncryption.dialog.busy");
    if (err.code === "pending") return t("diskEncryption.dialog.pendingExists");
    return t("diskEncryption.dialog.failed", { detail: err.detail || String(err.status) });
  }
  return t("diskEncryption.dialog.failed", { detail: String(err) });
}
