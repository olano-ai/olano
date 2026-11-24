/**
 * The one control that moves a person between the two shells.
 *
 * It lives in **both** — the dashboard's profile menu and the simple view's
 * account menu — and it is the same component in each, because the moment it
 * becomes two components is the moment one of them gets a feature the other
 * does not and an admin discovers the way back is missing.
 *
 * Admins only, by the server's rule (`PUT /api/auth/me/ui-mode` returns 403
 * `ui_mode_admin_only`). For a member the mode is their operator's decision
 * about what their day looks like, so this renders nothing at all for them —
 * not a disabled row, which only advertises a door that will not open.
 *
 * It is a real switch (`role="switch"`), in the same label-left / control-right
 * row shape as the theme and language rows above it. A switch is labelled with
 * the STATE it controls ("Simple view", on or off), which is the one labelling
 * a toggle can carry without ambiguity: the knob's position says where you
 * are, and flipping it says where you go. (The previous text button named the
 * destination, which is the right rule for a button and the wrong one for a
 * switch.)
 *
 * It deliberately says *view* and never *mode*, *role* or *access*: switching
 * changes what is on screen and nothing about what this person may do. Every
 * permission is enforced server-side either way, so an admin in the simple
 * view is still an admin, and a member moved to the dashboard gains nothing.
 */
import { useState } from "react";
import { MessageCircle } from "lucide-react";
import { useAuth } from "../core/auth";
import { useT } from "../core/i18n";
import { Toggle } from "./Toggle";
import { STR } from "./uiModeSwitchStrings";

export function UiModeSwitch({ onDone }: { onDone?: () => void }) {
  const { user, uiMode, switchUiMode } = useAuth();
  const t = useT(STR);
  const [busy, setBusy] = useState(false);

  // Members never see it. See the note above on why not a disabled row.
  if (!user?.is_admin) return null;

  const simple = uiMode === "simple";

  return (
    <div className="menu-select-row uimode-row" title={t("switch.help")} data-testid="uimode-switch">
      <span className="menu-select-label">
        <MessageCircle size={15} strokeWidth={1.75} aria-hidden /> {t("switch.simpleView")}
      </span>
      <Toggle
        variant="minimal"
        checked={simple}
        busy={busy}
        ariaLabel={t("switch.simpleView")}
        onChange={async (next) => {
          setBusy(true);
          const ok = await switchUiMode(next ? "simple" : "advanced");
          setBusy(false);
          // Only dismiss the menu on success. A failed switch that also closed
          // the menu would look exactly like a switch that worked and then
          // silently reverted.
          if (ok) onDone?.();
        }}
      />
    </div>
  );
}

/**
 * The cover painted over the swap.
 *
 * The two shells share no layout, so an uncovered switch is a hard cut from a
 * 25-route console to a chat app — which reads as the page having crashed
 * rather than as something the person just asked for. Naming the destination
 * while it happens turns the wait into a confirmation.
 */
export function UiModeSwitchCover({ to }: { to: "simple" | "advanced" }) {
  const t = useT(STR);
  return (
    <div className="uimode-cover" role="status" aria-live="polite">
      <div className="uimode-cover-inner">
        <div className="spinner" />
        <p>{to === "simple" ? t("cover.toSimple") : t("cover.toAdvanced")}</p>
      </div>
    </div>
  );
}
