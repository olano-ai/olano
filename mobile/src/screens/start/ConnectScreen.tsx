/**
 * First launch: which Olano deployment to talk to. The address is checked
 * (`GET /api/status`, public) before it is saved, so a typo is caught here
 * with a precise message rather than as a failed sign-in.
 */

import {
  IonButton,
  IonContent,
  IonIcon,
  IonInput,
  IonItem,
  IonLabel,
  IonList,
  IonNote,
  IonPage,
  IonSpinner,
  IonText,
} from "@ionic/react";
import { playCircleOutline, timeOutline } from "ionicons/icons";
import { useEffect, useState, type FormEvent } from "react";

import { parseAddress } from "../../address.ts";
import { demoOrigin, needsUpdate } from "../../appConfig.ts";
import { describeFailure, probeDeployment } from "../../probe.ts";
import { describeWhen, loadRecents, rememberDeployment } from "../../recents.ts";
import { getTransport } from "../../net/transport.ts";
import { appInfo, isDebugBuild, messageOf } from "../../native/shell.ts";
import { appConfig } from "../../state/appConfig.ts";
import { connectTo, describeDemoFailure, enterDemo } from "../../state/session.ts";
import { useStore } from "../../state/store.ts";
import { Logo } from "../../ui/Logo.tsx";

/** fetch() for the probe, over the app's transport (native code in the app). */
const probeFetch: typeof fetch = async (input, init) => {
  const response = await getTransport().request(String(input), {
    method: init?.method,
    headers: { Accept: "application/json" },
    timeoutMs: 10000,
  });
  const empty = [101, 204, 205, 304].includes(response.status);
  return new Response(empty ? null : response.body, { status: response.status, headers: response.headers });
};

export function ConnectScreen() {
  const [address, setAddress] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const recents = loadRecents();
  const config = useStore(appConfig);
  const demoUrl = config.demo.enabled ? demoOrigin(config.demo.url) : null;
  const [outdated, setOutdated] = useState(false);

  useEffect(() => {
    void appInfo().then((info) => setOutdated(needsUpdate(info.version, config)));
  }, [config]);

  /**
   * "Try the demo": the Olano-run demo deployment, checked like any address,
   * then entered with the demo box's passwordless sign-in. Shown to everyone
   * while the remote config has it on (mobile/store/DEMO.md).
   */
  async function tryDemo(origin: string) {
    setError(null);
    setBusy(true);
    try {
      const probe = await probeDeployment(origin, { fetchImpl: probeFetch });
      if (!probe.ok) {
        setError(
          probe.reason === "offline"
            ? "Your phone is offline. Connect to the internet and try again."
            : "The demo is not available right now. Try again later, or connect to your own deployment.",
        );
        return;
      }
      await enterDemo(probe.origin);
    } catch (e) {
      setError(describeDemoFailure(e));
    } finally {
      setBusy(false);
    }
  }

  async function connect(raw: string) {
    setError(null);
    const parsed = parseAddress(raw, { allowHttp: isDebugBuild() });
    if (!parsed.ok) {
      setError(parsed.error);
      return;
    }
    setBusy(true);
    try {
      const probe = await probeDeployment(parsed.origin, { fetchImpl: probeFetch });
      if (!probe.ok) {
        setError(describeFailure(parsed.host, probe));
        return;
      }
      rememberDeployment(probe.origin);
      await connectTo(probe.origin);
    } catch (e) {
      setError(messageOf(e));
    } finally {
      setBusy(false);
    }
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    void connect(address);
  }

  return (
    <IonPage>
      <IonContent className="ion-padding olano-start" fullscreen>
        <div className="olano-start-inner">
          <Logo className="olano-start-logo" />
          <h1>Connect to your Olano</h1>
          <p className="olano-start-lead">
            Enter the address you open your Olano dashboard with. A bare name like <strong>acme</strong> means{" "}
            <strong>acme.olano.run</strong>.
          </p>
          <form onSubmit={submit} noValidate>
            <IonList inset className="olano-start-form">
              <IonItem>
                <IonInput
                  aria-label="Olano address"
                  value={address}
                  placeholder="yourname.olano.run"
                  inputmode="url"
                  autocapitalize="off"
                  autocorrect={false}
                  spellcheck={false}
                  enterkeyhint="go"
                  clearInput
                  onIonInput={(e) => setAddress(String(e.detail.value ?? ""))}
                />
              </IonItem>
            </IonList>
            {error && (
              <IonText color="danger">
                <p className="olano-start-error" role="alert">
                  {error}
                </p>
              </IonText>
            )}
            <IonButton type="submit" expand="block" className="olano-start-button" disabled={busy || !address.trim()}>
              {busy ? <IonSpinner name="crescent" /> : "Continue"}
            </IonButton>
          </form>

          {demoUrl && (
            <div className="olano-start-demo">
              <p className="olano-start-note olano-start-or">No deployment of your own yet?</p>
              <IonButton expand="block" fill="outline" className="olano-start-button" disabled={busy} onClick={() => void tryDemo(demoUrl)}>
                <IonIcon slot="start" icon={playCircleOutline} />
                {config.demo.label}
              </IonButton>
              <p className="olano-start-note">{config.demo.accountHint}</p>
            </div>
          )}

          {outdated && (
            <p className="olano-start-note" role="status">
              A newer version of Olano is available. Update the app from the App Store or Google Play to keep it working
              with every deployment.
            </p>
          )}

          {recents.length > 0 && (
            <>
            <div className="olano-section">Recent</div>
            <IonList inset className="olano-start-form">
              {recents.map((recent) => (
                <IonItem key={recent.origin} button detail disabled={busy} onClick={() => void connect(recent.origin)}>
                  <IonIcon icon={timeOutline} slot="start" color="medium" />
                  <IonLabel>{recent.host}</IonLabel>
                  <IonNote slot="end">{describeWhen(recent.lastOpened)}</IonNote>
                </IonItem>
              ))}
            </IonList>
            </>
          )}
        </div>
      </IonContent>
    </IonPage>
  );
}
