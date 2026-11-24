/**
 * The two dead ends before a session, each with a way out: a deployment
 * that has no accounts yet (setup happens in a browser), and one that did
 * not answer.
 */

import { IonButton, IonContent, IonIcon, IonPage } from "@ionic/react";
import { cloudOfflineOutline, constructOutline } from "ionicons/icons";
import { useState } from "react";

import { hostOf, resolveSession, session, switchDeployment } from "../../state/session.ts";
import { useStore } from "../../state/store.ts";
import { openLink } from "../../ui/Markdown.tsx";

export function SetupScreen() {
  const { origin } = useStore(session);
  return (
    <IonPage>
      <IonContent className="ion-padding olano-start" fullscreen>
        <div className="olano-start-inner olano-centered">
          <IonIcon icon={constructOutline} className="olano-start-icon" color="primary" />
          <h1>Finish setting up</h1>
          <p className="olano-start-lead">
            {origin ? hostOf(origin) : "This deployment"} has no accounts yet. Create the first one in its dashboard, then
            come back and sign in.
          </p>
          <IonButton expand="block" className="olano-start-button" onClick={() => origin && void openLink(origin)}>
            Open the dashboard
          </IonButton>
          <IonButton expand="block" fill="outline" onClick={() => origin && void resolveSession(origin)}>
            I've done it
          </IonButton>
          <IonButton fill="clear" expand="block" size="small" onClick={() => void switchDeployment()}>
            Use a different deployment
          </IonButton>
        </div>
      </IonContent>
    </IonPage>
  );
}

export function OfflineScreen() {
  const { origin, error } = useStore(session);
  const [busy, setBusy] = useState(false);
  return (
    <IonPage>
      <IonContent className="ion-padding olano-start" fullscreen>
        <div className="olano-start-inner olano-centered">
          <IonIcon icon={cloudOfflineOutline} className="olano-start-icon" color="medium" />
          <h1>Can't connect</h1>
          <p className="olano-start-lead">{error ?? "The deployment did not answer."}</p>
          <IonButton
            expand="block"
            className="olano-start-button"
            disabled={busy || !origin}
            onClick={() => {
              if (!origin) return;
              setBusy(true);
              void resolveSession(origin).finally(() => setBusy(false));
            }}
          >
            Try again
          </IonButton>
          <IonButton fill="clear" expand="block" size="small" onClick={() => void switchDeployment()}>
            Use a different deployment
          </IonButton>
        </div>
      </IonContent>
    </IonPage>
  );
}
