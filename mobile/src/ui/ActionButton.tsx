/**
 * A button that shows it is working. While `busy` it is out of reach for a
 * second tap, keeps its colour, and carries a spinner with a present-tense
 * label ("Making it an idea…"), so a tap is answered on the button itself
 * before the deployment has. Every button that starts a request and waits for
 * the answer goes through this, so they all say it the same way.
 */

import { IonButton, IonIcon, IonSpinner } from "@ionic/react";
import type { ComponentProps, ReactNode } from "react";

type Props = ComponentProps<typeof IonButton> & {
  busy?: boolean;
  /** Shown while busy; the children stay when it is not given. */
  busyLabel?: ReactNode;
  /** An ionicon drawn before the label when not busy; the spinner takes its place. */
  icon?: string;
};

export function ActionButton({ busy = false, busyLabel, icon, children, disabled, className, ...rest }: Props) {
  const classes = [className, busy ? "olano-busy" : ""].filter(Boolean).join(" ") || undefined;
  return (
    <IonButton {...rest} className={classes} disabled={disabled || busy} aria-busy={busy || undefined}>
      {busy ? <IonSpinner slot="start" name="crescent" aria-hidden="true" /> : icon ? <IonIcon slot="start" icon={icon} aria-hidden="true" /> : null}
      {busy ? (busyLabel ?? children) : children}
    </IonButton>
  );
}
