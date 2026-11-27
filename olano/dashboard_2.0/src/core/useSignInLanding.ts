import { useLayoutEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { browserStore, landingFor, recordSignIn } from "./signInLanding";
import { clearPersistentState } from "./usePersistentState";
import { forgetLastAgent } from "./currentAgent";
import { clearToasts, setSignedOutToastSuppression } from "../ui/toast";

// Applies core/signInLanding.ts's rule before the signed-in app renders: the
// caller shows a spinner while this returns true, so no view of the previous
// person's URL ever mounts (and fires its fetches and toasts) for the new one.
export function useSignInLanding(ready: boolean, userId: string | null): boolean {
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const [settled, setSettled] = useState<string | null>(null);
  // Signed out (or session lapsed): the next sign-in is judged afresh, even
  // when it is the same account. Setting state during render is React's
  // pattern for "derive from the previous render".
  if (!ready && settled !== null) setSettled(null);
  const unsettled = ready && !!userId && settled !== userId;
  // Hold the app only when the route really has to change. Holding on every
  // new session unmounted whatever was on screen for a frame, which threw away
  // in-page state that belongs to the same person (first-run setup's wizard
  // creating the admin mid-flow). Reading the rule is a localStorage lookup.
  const reset = unsettled && !!userId && landingFor(browserStore(), userId).reset;

  useLayoutEffect(() => {
    if (!unsettled || !userId) return;
    const store = browserStore();
    if (reset) {
      // The previous person's in-session UI state (selections, drafts, the
      // remembered agent) is theirs too.
      clearPersistentState();
      forgetLastAgent();
      // So are the toasts still on screen from the end of their session.
      clearToasts();
      if (pathname !== "/") navigate("/", { replace: true });
    }
    recordSignIn(store, userId);
    setSignedOutToastSuppression(false);
    setSettled(userId);
  }, [unsettled, reset, userId, pathname, navigate]);

  return reset;
}
