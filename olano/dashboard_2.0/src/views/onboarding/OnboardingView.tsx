import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { chatPath } from "../../core/routeUrls";
import { api } from "../../core/api";
import { useAuth } from "../../core/auth";
import { useT } from "../../core/i18n";
import { pokePersistentState } from "../../core/usePersistentState";
import { useToast } from "../../ui/toast";
import { BrandWordmark } from "../../ui/BrandMark";
import { stageChatHandoff } from "../chat/handoff";
import { AgentFatherStep } from "./AgentFatherStep";
import { isManagedDeployment, useDeployment } from "./cloudDeployment";
import { ArtDashboard } from "./illustrations";
import { ProfileStep } from "./ProfileStep";
import { clearProgress, loadProgress, saveProgress } from "./progress";
import { ProviderStep } from "./ProviderStep";
import { STR } from "./strings";
import type { Outcome, StepId } from "./types";

// Wizard order. "welcome" opens; the actionable steps' outcomes are reported
// to POST /api/onboarding/complete; "agentfather" is last and finishes the
// tour itself (its primary button posts the completion), so there is no
// separate terminal panel.
const BASE_ORDER: StepId[] = ["welcome", "profile", "provider", "agentfather"];

// Where the wizard hands over. The completion response names the agent and
// the thread the runtime greeted the owner on; a box with no Agent Father
// (or a plan that withholds it) answers null and lands on Overview instead.
interface Greeting {
  agent: string;
  thread_id: string;
}

/**
 * First-run setup wizard for a deployed instance's admin. Rendered by
 * GatedApp *instead of* the app tree (like Login/Bootstrap) while
 * /api/auth/me reports `onboarding.required`. Four panels — welcome, your
 * profile, AI provider, meet Agent Father — and the tour ends inside Agent
 * Father's chat, where it has already said hello. Completion is persisted
 * server-side (deployment-wide), so no admin sees the wizard twice.
 */
export function OnboardingView() {
  const { refresh, logout, multiUser, adminPasswordRotated, vaultPasswordRotated, uiMode } =
    useAuth();
  const t = useT(STR);
  const { push } = useToast();
  const navigate = useNavigate();
  // The profile step exists to walk the owner off the provisioned temporary
  // credentials (and, on a box with no accounts, to create the admin sign-in).
  // When both have already been replaced — the owner bootstrapped their own
  // account and rekeyed the vault before the tour ran — there is nothing left
  // to ask, and a form for work that already happened reads as though it
  // didn't take. Captured once, at mount: the step's own submit flips these
  // mid-tour, and the panel the owner is standing on must not vanish under
  // them.
  const [order] = useState<StepId[]>(() => {
    const drop = new Set<StepId>();
    if (multiUser && adminPasswordRotated && vaultPasswordRotated) drop.add("profile");
    return BASE_ORDER.filter((s) => !drop.has(s));
  });
  const setupSteps = useMemo(() => order.slice(1), [order]);
  // Resume where the admin left off. A first-run box can lose its runtime
  // mid-wizard (first boot is still busy behind the dashboard), and the reload
  // that follows must not re-ask for steps already done — see ./progress.
  const [resumed] = useState(() => loadProgress(order.length));
  const [idx, setIdx] = useState(resumed.idx);
  const [maxIdx, setMaxIdx] = useState(resumed.maxIdx);
  const [outcomes, setOutcomes] = useState<Partial<Record<StepId, Outcome>>>(resumed.outcomes);
  const [finishing, setFinishing] = useState(false);
  const [finishErr, setFinishErr] = useState(false);

  const step = order[idx];

  // Managed deployments are provisioned with TEMPORARY credentials (admin
  // password + vault passphrase) that the control plane generated and keeps
  // copyable on the deployment card — rotating both is not optional, so the
  // profile step loses its Skip button and the whole-tour skips are withheld
  // until it is done. While the lookup is still in flight (undefined) the box
  // is treated as managed so a fast click can't slip past the requirement
  // before the answer lands; a self-hosted box resolves to null a round-trip
  // later and the skips appear.
  const deployment = useDeployment();
  const credentialsMandatory = deployment === undefined || isManagedDeployment(deployment);
  // A profile step that isn't in the tour is already satisfied — the owner
  // set both credentials themselves; there is nothing temporary left.
  const profileSatisfied = !order.includes("profile") || outcomes.profile === "done";
  const skipAllAllowed = !credentialsMandatory || profileSatisfied;

  useEffect(() => {
    saveProgress({ idx, maxIdx, outcomes });
  }, [idx, maxIdx, outcomes]);

  function goto(i: number) {
    const next = Math.max(0, Math.min(order.length - 1, i));
    setIdx(next);
    setMaxIdx((m) => Math.max(m, next));
  }

  function advance(outcome: Outcome) {
    setOutcomes((prev) => ({
      ...prev,
      // A step that already succeeded stays "done" even if re-visited and
      // then skipped past — the work happened.
      [step]: prev[step] === "done" ? "done" : outcome,
    }));
    goto(idx + 1);
  }

  async function finish(skippedAll: boolean, override: Partial<Record<StepId, Outcome>> = {}) {
    if (finishing) return; // double-click before the disabled re-render lands
    setFinishing(true);
    setFinishErr(false);
    const merged = { ...outcomes, ...override };
    const steps: Record<string, string> = {};
    for (const s of setupSteps) steps[s] = merged[s] ?? (skippedAll ? "skipped" : "pending");
    try {
      const res = await api.post<{ greeting: Greeting | null }>("/api/onboarding/complete", {
        steps,
        skipped: skippedAll,
      });
      // Completion is deployment-wide server-side; drop the local resume point
      // so a future visit can never be restored into a finished tour.
      clearProgress();
      const greeting = res?.greeting ?? null;
      if (greeting) {
        // Land in Agent Father's chat, on the thread the runtime is greeting
        // the owner on. Three things make that land: the app-wide agent
        // selection is seeded before CurrentAgentProvider mounts (it reads
        // this cache on mount), the chat's landing effect is told the thread
        // may not exist yet (the greeting turn can still be running — the
        // rows arrive live over thread_updated), and the URL names the
        // thread so a reload keeps it. Navigate BEFORE refresh(): the wizard
        // is still mounted here, and the route table mounts on whatever URL
        // is current once onboarding.required flips.
        pokePersistentState("global.agent", greeting.agent);
        stageChatHandoff({ agent: greeting.agent, threadId: greeting.thread_id, awaiting: "greeting" });
        // The URL names the agent as well as the thread: thread ids are per
        // agent, and a reload must reopen this one under Agent Father.
        navigate(
          uiMode === "simple"
            ? `/?agent=${encodeURIComponent(greeting.agent)}&thread=${encodeURIComponent(greeting.thread_id)}`
            : chatPath(greeting.agent, greeting.thread_id),
          { replace: true },
        );
      } else {
        navigate("/overview", { replace: true });
      }
      await refresh(); // me() now reports required=false → GatedApp falls through
    } catch {
      // Reachable from the welcome/rail skip buttons too, where the last
      // panel's inline error never renders — the toast is the only feedback there.
      push(t("ob.finish.err"), "error");
      setFinishErr(true);
      setFinishing(false);
    }
  }

  // Steps remount when revisited (the panel is keyed on `step`), so their
  // completion must come from the wizard's record, not component state.
  const stepApi = {
    onDone: () => advance("done"),
    onSkip: () => advance("skipped"),
    onBack: () => goto(idx - 1),
    done: outcomes[step] === "done",
  };

  // Only states with a visual treatment; unvisited steps read from :disabled.
  const railState = (s: StepId): string => {
    if (s === step) return "current";
    const o = outcomes[s];
    return o === "done" ? "done" : o === "skipped" ? "skipped" : "";
  };

  return (
    <div className="ob-root">
      <style>{CSS}</style>

      <aside className="ob-rail" aria-label={t("ob.nav.welcome")}>
        <div className="ob-brand">
          <BrandWordmark />
        </div>
        <ol className="ob-steps">
          {order.map((s, i) => (
            <li key={s}>
              <button
                type="button"
                className={`ob-step ${railState(s)}`}
                disabled={i > maxIdx || finishing}
                onClick={() => goto(i)}
                aria-current={s === step ? "step" : undefined}
              >
                <span className="ob-step-dot" aria-hidden>
                  {outcomes[s] === "done" ? "✓" : outcomes[s] === "skipped" ? "–" : i === 0 ? "•" : i}
                </span>
                <span className="ob-step-label">{t(`ob.nav.${s}`)}</span>
              </button>
            </li>
          ))}
        </ol>
        <div className="ob-rail-foot">
          {step !== "agentfather" && skipAllAllowed && (
            <button
              type="button"
              className="ob-skip-all"
              onClick={() => finish(true)}
              disabled={finishing}
            >
              {t("ob.skipAll")}
            </button>
          )}
          {/* Escape hatch: the wizard replaces the whole app, so without this a
              failed completion POST (or the wrong account) would trap the admin. */}
          <button type="button" className="ob-skip-all" onClick={() => void logout()}>
            {t("ob.signout")}
          </button>
        </div>
      </aside>

      <button type="button" className="ob-signout-floating" onClick={() => void logout()}>
        {t("ob.signout")}
      </button>

      <main className="ob-main">
        {/* Mobile progress dots (rail is hidden on small screens) */}
        <div className="ob-dots" aria-hidden>
          {order.map((s, i) => (
            <span key={s} className={`ob-dot ${i === idx ? "on" : i < idx ? "past" : ""}`} />
          ))}
        </div>

        <div className="ob-panel" key={step}>
          {step === "welcome" && (
            <WelcomeStep
              onStart={() => goto(1)}
              onSkipAll={() => finish(true)}
              busy={finishing}
              showSkip={skipAllAllowed}
            />
          )}
          {step === "profile" && <ProfileStep {...stepApi} required={credentialsMandatory} />}
          {step === "provider" && <ProviderStep {...stepApi} />}
          {step === "agentfather" && (
            <AgentFatherStep
              {...stepApi}
              onFinish={(outcome) => finish(false, { agentfather: outcome })}
              finishing={finishing}
              finishErr={finishErr}
            />
          )}
        </div>
      </main>
    </div>
  );
}

// ── welcome ──────────────────────────────────────────────────────────────────

function WelcomeStep({
  onStart,
  onSkipAll,
  busy,
  showSkip,
}: {
  onStart(): void;
  onSkipAll(): void;
  busy: boolean;
  // False on managed deployments until the temporary admin password and vault
  // passphrase have been rotated — skipping the whole tour would skip those.
  showSkip: boolean;
}) {
  const t = useT(STR);
  const features: Array<[string, string]> = [
    [t("ob.welcome.f1.t"), t("ob.welcome.f1.d")],
    [t("ob.welcome.f2.t"), t("ob.welcome.f2.d")],
    [t("ob.welcome.f3.t"), t("ob.welcome.f3.d")],
    [t("ob.welcome.f4.t"), t("ob.welcome.f4.d")],
  ];
  return (
    <div className="card ob-card ob-welcome">
      <div className="ob-kicker">
        <span>{t("ob.welcome.kicker")}</span>
        <BrandWordmark />
      </div>
      <ArtDashboard />
      <h1>{t("ob.welcome.title")}</h1>
      <p className="ob-lead">{t("ob.welcome.mission")}</p>
      <div className="ob-feature-grid">
        {features.map(([title, desc]) => (
          <div key={title} className="ob-feature">
            <strong>{title}</strong>
            <span>{desc}</span>
          </div>
        ))}
      </div>
      <p className="ob-lead muted">{t("ob.welcome.lead")}</p>
      <div className="ob-actions">
        {showSkip ? (
          <button type="button" className="btn ghost" onClick={onSkipAll} disabled={busy}>
            {t("ob.welcome.skip")}
          </button>
        ) : (
          <span />
        )}
        <button type="button" className="btn primary" onClick={onStart} disabled={busy} autoFocus>
          {t("ob.welcome.start")}
        </button>
      </div>
    </div>
  );
}

// ── styles ───────────────────────────────────────────────────────────────────

const CSS = `
.ob-root {
  height: 100%;
  display: flex;
  background:
    radial-gradient(1100px 500px at 85% -10%, var(--accent-soft), transparent 60%),
    var(--bg);
  overflow: hidden;
}
.ob-rail {
  width: 236px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  gap: 18px;
  padding: 26px 18px;
  border-right: 1px solid var(--border);
  background: var(--bg-elev);
  overflow-y: auto;
}
.ob-brand { padding: 0 8px; }
.ob-steps {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.ob-step {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 8px;
  border: none;
  border-radius: var(--r-sm);
  background: none;
  color: var(--text-3);
  font: inherit;
  font-size: 13.5px;
  text-align: left;
  cursor: pointer;
}
.ob-step:disabled { cursor: default; opacity: 0.55; }
.ob-step:not(:disabled):hover { background: var(--surface-2); }
.ob-step.current { color: var(--text); background: var(--accent-soft); }
.ob-step.done { color: var(--text-2); }
.ob-step-dot {
  width: 22px;
  height: 22px;
  flex-shrink: 0;
  display: grid;
  place-items: center;
  border-radius: 50%;
  font-size: 11.5px;
  background: var(--surface-2);
  border: 1px solid var(--border);
  color: var(--text-3);
}
.ob-step.current .ob-step-dot { background: var(--accent); border-color: var(--accent); color: var(--bg); }
.ob-step.done .ob-step-dot { background: var(--success-soft); border-color: var(--success); color: var(--success); }
.ob-step.skipped { color: var(--text-4); }
.ob-rail-foot {
  margin-top: auto;
  display: flex;
  flex-direction: column;
}
.ob-skip-all {
  background: none;
  border: none;
  color: var(--text-4);
  font: inherit;
  font-size: 12.5px;
  cursor: pointer;
  padding: 8px;
  text-align: left;
}
.ob-skip-all:hover { color: var(--text-2); }
.ob-main {
  flex: 1;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 34px 20px;
}
.ob-dots { display: none; gap: 6px; margin-bottom: 16px; }
.ob-dot {
  width: 7px; height: 7px; border-radius: 50%;
  background: var(--surface-3, var(--surface-2));
  border: 1px solid var(--border);
}
.ob-dot.on { background: var(--accent); border-color: var(--accent); }
.ob-dot.past { background: var(--success); border-color: var(--success); }
.ob-panel {
  width: min(660px, 100%);
  margin: auto 0;
  animation: ob-in var(--t) var(--ease);
}
@keyframes ob-in {
  from { opacity: 0; transform: translateY(10px); }
  to { opacity: 1; transform: none; }
}
@media (prefers-reduced-motion: reduce) {
  .ob-panel { animation: none; }
}
.ob-card {
  display: flex;
  flex-direction: column;
  gap: 14px;
  padding: 30px;
  box-shadow: var(--shadow-lg);
}
.ob-card h1 {
  margin: 0;
  font-size: 21px;
  font-weight: 600;
  letter-spacing: -0.02em;
  display: flex;
  align-items: center;
  gap: 10px;
}
.ob-optional { background: var(--surface-2); color: var(--text-3); }
.ob-lead { margin: 0; color: var(--text-2); font-size: 13.5px; line-height: 1.55; }
.ob-lead.muted { color: var(--text-3); }
.ob-art { width: 210px; height: 114px; align-self: center; }
.ob-kicker {
  display: flex;
  align-items: baseline;
  gap: 8px;
  color: var(--text-3);
  font-size: 13px;
}
.ob-feature-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}
.ob-feature {
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding: 12px;
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  background: var(--surface-2);
  font-size: 12.5px;
}
.ob-feature strong { font-size: 13px; }
.ob-feature span { color: var(--text-3); line-height: 1.45; }
.ob-actions {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 10px;
  margin-top: 6px;
}
.ob-required-note {
  color: var(--text-3);
  font-size: 12.5px;
  text-align: right;
  line-height: 1.4;
}
.ob-form { display: flex; flex-direction: column; gap: 12px; }
.ob-extlink {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  align-self: flex-start;
  padding: 7px 12px;
  border: 1px solid var(--border);
  border-radius: var(--r-pill);
  background: var(--surface-2);
  color: var(--text-2);
  font-size: 12.5px;
  text-decoration: none;
}
.ob-extlink:hover { border-color: var(--border-strong); color: var(--text); }
.ob-extlink span { color: var(--accent); }
.ob-hint { margin: 0; color: var(--text-4); font-size: 12px; line-height: 1.5; }
.ob-hint a { color: var(--accent); }
.ob-ok {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 12px;
  border-radius: var(--r-sm);
  background: var(--success-soft);
  color: var(--success);
  font-size: 13px;
}
.ob-howto {
  margin: 0;
  padding-left: 20px;
  display: flex;
  flex-direction: column;
  gap: 5px;
  color: var(--text-2);
  font-size: 13px;
}
.ob-provider-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 8px;
}
.ob-provider-grid.two { grid-template-columns: 1fr 1fr; }
.ob-provider {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 4px;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  background: var(--surface-2);
  color: var(--text);
  font: inherit;
  font-size: 13px;
  cursor: pointer;
  text-align: left;
}
.ob-provider:hover { border-color: var(--border-strong); }
.ob-provider.on { border-color: var(--accent); background: var(--accent-soft); }
.ob-provider .ok { color: var(--success); font-size: 11.5px; }
/* Placeholder tile: same box as a real provider card so the grid doesn't
   resize when the catalog lands. Height is pinned to one line of label plus
   the card's padding — .ob-provider only gets its height from its text. */
.ob-provider.skeleton {
  height: 40px;
  cursor: default;
  border-style: dashed;
  background:
    linear-gradient(90deg, transparent 0%, var(--surface-3) 50%, transparent 100%)
    var(--surface-2);
  background-size: 220% 100%;
  animation: ob-shimmer 1.3s ease-in-out infinite;
}
@keyframes ob-shimmer {
  from { background-position: 120% 0; }
  to { background-position: -120% 0; }
}
.ob-retry {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  background: var(--surface-2);
  color: var(--text-2);
  font-size: 13px;
}
@media (prefers-reduced-motion: reduce) {
  .ob-provider.skeleton { animation: none; background: var(--surface-2); }
  .ob-first-dot { animation: none; }
}
/* "Included with your plan" checklist on the provider step. Two columns of
   equal-height cards, one column on a phone (see the media query below). */
.ob-included {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
}
.ob-included li {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 11px 12px;
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  background: var(--surface-2);
}
.ob-included .tick { color: var(--success); font-size: 12px; line-height: 1.5; }
.ob-included .ob-inc-body { display: flex; flex-direction: column; gap: 2px; }
.ob-included strong { font-size: 12.5px; }
.ob-included span:not(.tick) { color: var(--text-3); font-size: 12px; line-height: 1.45; }
/* Condensed: label-only rows, for the plan that shows this above a live key
   form rather than as the whole step. */
.ob-included.condensed li { padding: 8px 10px; align-items: center; }
.ob-credits-chip {
  align-self: flex-start;
  padding: 6px 12px;
  border-radius: var(--r-pill);
  background: var(--success-soft);
  color: var(--success);
  font-size: 12.5px;
}
.ob-byok-split {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding-top: 14px;
  border-top: 1px solid var(--border);
}
.ob-byok-head { display: flex; align-items: center; gap: 8px; }
.ob-byok-head strong { font-size: 13.5px; }
/* Shape C's "Run on Olano credits" card: its own bordered block above the
   BYOK picker, so the two routes read as two choices rather than one form. */
.ob-olano-key {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 12px;
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  background: var(--surface-2);
}
.ob-olano-key .ob-included li { background: var(--surface); }
.ob-olano-key .olano-key .field { display: flex; flex-direction: column; gap: 6px; }
.ob-olano-byok {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding-top: 14px;
  border-top: 1px solid var(--border);
}
/* The profile step's two halves (account, vault): a titled sub-block with
   the same top divider as .ob-byok-split, so one card reads as one form. */
.ob-section { display: flex; flex-direction: column; gap: 12px; }
.ob-section + .ob-section { padding-top: 14px; border-top: 1px solid var(--border); }
.ob-section-head { margin: 0; font-size: 13.5px; font-weight: 600; }
.ob-profile-submit { align-self: flex-start; }
/* Meet Agent Father: the capabilities list reuses the condensed included
   rows; the install status line sits under it. */
.ob-father-status { display: flex; align-items: center; gap: 8px; }
.ob-unlock {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 12px;
  border: 1px solid var(--warn, var(--border));
  border-radius: var(--r-md);
  background: var(--warn-soft, var(--surface-2));
}
.ob-unlock strong { font-size: 13px; }
.ob-signout-floating {
  display: none;
  position: fixed;
  top: 10px;
  right: 14px;
  z-index: 1;
  background: none;
  border: none;
  color: var(--text-4);
  font: inherit;
  font-size: 12.5px;
  cursor: pointer;
  padding: 6px;
}
/* "Meet Agent Father": what happens after the button, and what to say first.
   Replaces a one-line "next: it says hello" — a new owner used to land in
   the chat with nothing on screen while the greeting was still being written
   (Agent Father can be booting for a minute on a cold box). */
.ob-father-first { display: flex; flex-direction: column; gap: 10px; }
.ob-father-waking {
  margin: 0;
  display: flex;
  align-items: flex-start;
  gap: 8px;
  color: var(--text-2);
  font-size: 12.5px;
  line-height: 1.5;
}
.ob-first-dot {
  flex: none;
  width: 8px;
  height: 8px;
  margin-top: 5px;
  border-radius: 50%;
  background: var(--accent);
  animation: ob-first-pulse 1.4s ease-in-out infinite;
}
@keyframes ob-first-pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.35; } }
.ob-first-cols { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.ob-first-col {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 11px 12px;
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  background: var(--surface-2);
}
.ob-first-col strong { font-size: 12.5px; }
.ob-first-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 4px; }
.ob-first-list li { color: var(--text-3); font-size: 12px; line-height: 1.45; }
.ob-first-list li.ob-prompt { color: var(--text-2); }
.ob-first-list li.ob-prompt::before { content: "\\201C"; color: var(--text-4); }
.ob-first-list li.ob-prompt::after { content: "\\201D"; color: var(--text-4); }
@media (max-width: 760px) {
  .ob-rail { display: none; }
  .ob-signout-floating { display: block; }
  .ob-dots { display: flex; }
  .ob-main { padding: 22px 14px; }
  .ob-card { padding: 22px 18px; }
  .ob-feature-grid, .ob-provider-grid { grid-template-columns: 1fr 1fr; }
  /* One column: these rows carry a label *and* a description, so two of them
     side by side on a phone wrap into unreadable slivers. */
  .ob-included { grid-template-columns: 1fr; }
  .ob-first-cols { grid-template-columns: 1fr; }
  .ob-art { width: 180px; height: 98px; }
}
`;
