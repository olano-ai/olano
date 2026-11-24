/**
 * Signing in to the connected deployment with the same email and password
 * as its dashboard (`POST /api/auth/login`). The session cookie it sets is
 * kept by native code for 14 days, like the dashboard's.
 */

import {
  IonButton,
  IonContent,
  IonInput,
  IonInputPasswordToggle,
  IonItem,
  IonList,
  IonPage,
  IonSpinner,
  IonText,
} from "@ionic/react";
import { useState, type FormEvent } from "react";

import { ApiError } from "../../net/api.ts";
import { messageOf } from "../../native/shell.ts";
import { describeDemoFailure, hostOf, isDemo, renewDemo, session, signIn, switchDeployment } from "../../state/session.ts";
import { useStore } from "../../state/store.ts";
import { Logo } from "../../ui/Logo.tsx";

export function SignInScreen() {
  const { origin } = useStore(session);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const demo = isDemo(origin);

  /** The demo's session ran out: the demo box signs visitors in without a password. */
  async function reenterDemo() {
    setBusy(true);
    setError(null);
    try {
      await renewDemo();
    } catch (e) {
      setError(describeDemoFailure(e));
    } finally {
      setBusy(false);
    }
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!email.trim() || !password) return;
    setBusy(true);
    setError(null);
    try {
      await signIn(email, password);
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) setError("That email and password don't match an account here.");
      else if (e instanceof ApiError && e.status === 429) setError("Too many attempts. Wait a minute and try again.");
      else setError(messageOf(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <IonPage>
      <IonContent className="ion-padding olano-start" fullscreen>
        <div className="olano-start-inner">
          <Logo className="olano-start-logo" />
          <h1>Sign in</h1>
          <p className="olano-start-lead">
            to <strong>{origin ? hostOf(origin) : "your deployment"}</strong> with the account you use for its dashboard.
          </p>
          <form onSubmit={(e) => void submit(e)} noValidate>
            <IonList inset className="olano-start-form">
              <IonItem>
                <IonInput
                  label="Email"
                  labelPlacement="stacked"
                  type="email"
                  inputmode="email"
                  autocomplete="email"
                  autocapitalize="off"
                  value={email}
                  onIonInput={(e) => setEmail(String(e.detail.value ?? ""))}
                />
              </IonItem>
              <IonItem>
                <IonInput
                  label="Password"
                  labelPlacement="stacked"
                  type="password"
                  autocomplete="current-password"
                  enterkeyhint="go"
                  value={password}
                  onIonInput={(e) => setPassword(String(e.detail.value ?? ""))}
                >
                  <IonInputPasswordToggle slot="end" />
                </IonInput>
              </IonItem>
            </IonList>
            {error && (
              <IonText color="danger">
                <p className="olano-start-error" role="alert">
                  {error}
                </p>
              </IonText>
            )}
            <IonButton type="submit" expand="block" className="olano-start-button" disabled={busy || !email.trim() || !password}>
              {busy ? <IonSpinner name="crescent" /> : "Sign in"}
            </IonButton>
          </form>
          {demo && (
            <IonButton expand="block" fill="outline" className="olano-start-button" disabled={busy} onClick={() => void reenterDemo()}>
              Enter the demo again
            </IonButton>
          )}
          <IonButton fill="clear" expand="block" size="small" onClick={() => void switchDeployment()}>
            Use a different deployment
          </IonButton>
        </div>
      </IonContent>
    </IonPage>
  );
}
