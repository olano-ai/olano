// Step ids mirror olano/users/onboarding.py KNOWN_STEPS. Four panels: welcome,
// your profile (claim the provisioned account + rekey the vault), AI provider
// (credits / BYOK) and meet Agent Father — whose primary button finishes the
// tour, so there is no separate terminal panel any more.
export type StepId = "welcome" | "profile" | "provider" | "agentfather";

// "pending" exists only on the wire (POST /api/onboarding/complete payload
// for never-visited steps); the wizard itself records just these two.
export type Outcome = "done" | "skipped";

// Props every actionable step receives from the wizard shell.
export interface StepProps {
  onDone(): void;
  onSkip(): void;
  onBack(): void;
  // True when the wizard already recorded this step as done — steps remount
  // on revisit, so success state must be rehydrated from the shell.
  done: boolean;
  // True when this step must be completed before the tour can move past it:
  // the profile step on a managed deployment, whose provisioned TEMPORARY
  // credentials must be rotated during setup — StepNav then offers no "Skip
  // for now". Unset/false everywhere else.
  required?: boolean;
}

// The last panel finishes the tour itself instead of advancing: its primary
// button posts the completion and lands the owner in Agent Father's chat.
export interface FinalStepProps extends StepProps {
  onFinish(outcome: Outcome): void;
  finishing: boolean;
  finishErr: boolean;
}
