import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "../../core/api";
import { useT } from "../../core/i18n";
import { StepShell } from "./chrome";
import { ArtTeam } from "./illustrations";
import { STR } from "./strings";
import type { FinalStepProps } from "./types";

const ARCHETYPE = "agentfather";

type Install = "installing" | "ready" | "unavailable" | "error";

const CAPABILITIES = ["create", "reconfigure", "connect", "people", "diagnose", "approve"] as const;

/**
 * "Meet Agent Father" — the last panel. Nobody creates a first agent, wires a
 * messenger or pastes a search key here any more: Agent Father does all of
 * that in conversation, so the tour introduces it and hands over.
 *
 * Installing runs on mount, not on the button: on a managed box the cloud
 * provisioned Agent Father already (409 → ready, instantly), and on a
 * self-hosted one the install + boot takes a few seconds that should overlap
 * the owner reading the panel rather than follow their click. The primary
 * button then FINISHES the tour — the shell posts the completion and lands in
 * Agent Father's chat, where the greeting the runtime ran is waiting.
 */
export function AgentFatherStep(p: FinalStepProps) {
  const t = useT(STR);
  const [install, setInstall] = useState<Install>("installing");
  const [detail, setDetail] = useState("");
  const [attempt, setAttempt] = useState(0);
  // One POST per attempt, whatever React does with the effect: StrictMode
  // runs mount effects twice in dev (run → cleanup → run), so a plain effect
  // would fire two installs, and an effect that cancels itself on cleanup
  // would throw away the answer to the only request that was made. The ref
  // dedupes the run; nothing is cancelled, because a state update landing on
  // this still-mounted panel is exactly what we want.
  const startedRef = useRef(-1);

  useEffect(() => {
    if (startedRef.current === attempt) return;
    startedRef.current = attempt;
    setInstall("installing");
    setDetail("");
    api
      .post("/api/archetypes/install", { archetype: ARCHETYPE })
      .then(() => {
        api.invalidate("/api/agents");
        setInstall("ready");
      })
      .catch((e: unknown) => {
        const status = e instanceof ApiError ? e.status : 0;
        const served = e instanceof ApiError ? e.detail : "";
        if (status === 409) {
          setInstall("ready"); // already installed — the managed case
        } else if (status === 403) {
          // A deployment refusal explains its own limit; replacing that with
          // generic copy is how a plan limit reads as a bug.
          setInstall("unavailable");
          setDetail(served);
        } else {
          setInstall("error");
          setDetail(served);
        }
      });
  }, [attempt]);

  const ready = install === "ready";
  const busy = p.finishing || install === "installing";

  return (
    <StepShell art={<ArtTeam />} title={t("ob.father.title")} lead={t("ob.father.lead")}>
      <ul className="ob-included condensed ob-father-caps" aria-label={t("ob.father.title")}>
        {CAPABILITIES.map((id) => (
          <li key={id}>
            <span className="tick" aria-hidden>
              ✓
            </span>
            <div className="ob-inc-body">
              <strong>{t(`ob.father.cap.${id}.t`)}</strong>
              <span>{t(`ob.father.cap.${id}.d`)}</span>
            </div>
          </li>
        ))}
      </ul>

      {install === "installing" && (
        <div className="ob-hint ob-father-status" role="status">
          <span className="spinner" style={{ width: 12, height: 12 }} /> {t("ob.father.installing")}
        </div>
      )}
      {ready && <div className="ob-ok">✓ {t("ob.father.ready")}</div>}
      {install === "unavailable" && (
        <div className="ob-retry" role="status">
          <span>
            {t("ob.father.unavailable")}
            {detail ? ` ${detail}` : ""}
          </span>
        </div>
      )}
      {install === "error" && (
        <div className="ob-retry" role="alert">
          <span>
            {t("ob.father.err")}
            {detail ? ` ${detail}` : ""}
          </span>
          <button type="button" className="btn" onClick={() => setAttempt((n) => n + 1)}>
            {t("ob.father.retry")}
          </button>
        </div>
      )}
      {ready && (
        <div className="ob-father-first">
          {/* What happens on the other side of the button. A new owner used
              to land in the chat with no sign the greeting was being written
              — Agent Father may still be booting (a minute on a cold box)
              before its first line appears. Say so here, and give them
              something to do with that minute: what to ask, what to share. */}
          <p className="ob-father-waking" role="status">
            <span className="ob-first-dot" aria-hidden />
            {t("ob.father.waking")}
          </p>
          <div className="ob-first-cols">
            <div className="ob-first-col">
              <strong>{t("ob.father.try.h")}</strong>
              <ul className="ob-first-list">
                {(["1", "2", "3"] as const).map((n) => (
                  <li key={n} className="ob-prompt">
                    {t(`ob.father.try.${n}`)}
                  </li>
                ))}
              </ul>
            </div>
            <div className="ob-first-col">
              <strong>{t("ob.father.tell.h")}</strong>
              <ul className="ob-first-list">
                {(["1", "2", "3", "4"] as const).map((n) => (
                  <li key={n}>{t(`ob.father.tell.${n}`)}</li>
                ))}
              </ul>
            </div>
          </div>
        </div>
      )}
      {p.finishErr && <div className="error-text">{t("ob.finish.err")}</div>}

      <div className="ob-actions">
        <button type="button" className="btn ghost" onClick={p.onBack} disabled={p.finishing}>
          {t("ob.back")}
        </button>
        <button
          type="button"
          className="btn primary"
          disabled={busy}
          onClick={() => p.onFinish(ready ? "done" : "skipped")}
          autoFocus={ready}
        >
          {p.finishing
            ? t("ob.saving")
            : ready
              ? t("ob.father.meet")
              : install === "installing"
                ? t("ob.father.installing")
                : t("ob.father.goDashboard")}
        </button>
      </div>
    </StepShell>
  );
}
