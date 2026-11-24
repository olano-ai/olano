/**
 * Credits and usage, read-only (settings/credits.ts says why there is nothing
 * to buy here): the balance with this month's included credits, then what each
 * Olano service spent over the last weeks.
 */

import {
  IonBackButton,
  IonButtons,
  IonContent,
  IonHeader,
  IonItem,
  IonLabel,
  IonList,
  IonNote,
  IonPage,
  IonRefresher,
  IonRefresherContent,
  IonSpinner,
  IonTitle,
  IonToolbar,
} from "@ionic/react";
import { walletOutline } from "ionicons/icons";

import type { CreditsBalance, CreditsDisplay, ServiceUsage } from "../../net/types.ts";
import { creditUnit, formatCredits, isLow, renewsOn, spendingServices, usedPercent } from "../../settings/credits.ts";
import { useResource } from "../../state/resources.ts";
import { currentApi } from "../../state/session.ts";
import { Empty, ErrorNote } from "../../ui/Empty.tsx";

export function useCreditsDisplay() {
  return useResource<CreditsDisplay>("credits:display", () => currentApi().creditsDisplay(), 300000);
}

export function UsagePage() {
  const [display] = useCreditsDisplay();
  const [balance, reloadBalance] = useResource<CreditsBalance>("credits:balance", () => currentApi().creditBalance(), 60000);
  const [usage, reloadUsage] = useResource<ServiceUsage>("credits:usage", () => currentApi().serviceUsage(), 60000);
  const unit = creditUnit(display.data);
  const reload = () => Promise.all([reloadBalance(), reloadUsage()]);

  const b = balance.data;
  const used = b ? usedPercent(b) : null;
  const renews = b ? renewsOn(b) : null;
  const services = spendingServices(usage.data);
  const days = usage.data?.window?.days ?? 30;

  return (
    <IonPage>
      <IonHeader translucent>
        <IonToolbar>
          <IonButtons slot="start">
            <IonBackButton defaultHref="/settings" text="" />
          </IonButtons>
          <IonTitle>Credits and Usage</IonTitle>
        </IonToolbar>
      </IonHeader>
      <IonContent fullscreen className="olano-grouped">
        <IonRefresher slot="fixed" onIonRefresh={(e) => void reload().finally(() => e.detail.complete())}>
          <IonRefresherContent />
        </IonRefresher>

        {balance.error && !b && <ErrorNote message={balance.error} onRetry={() => void reload()} />}
        {!b && !balance.error && (
          <div className="olano-empty">
            <IonSpinner name="crescent" />
          </div>
        )}
        {b && !b.available && (
          <Empty
            icon={walletOutline}
            title="No credits on this deployment"
            body="It runs on its own provider keys, so there is no Olano credit balance to show."
          />
        )}

        {b?.available && (
          <>
            <div className="olano-credits" aria-live="polite">
              <span className={`olano-credits-balance${isLow(b) ? " low" : ""}`}>{formatCredits(b.balance ?? 0, unit)}</span>
              <span className="olano-credits-caption">left{b.stale ? " · last known, Olano Cloud did not answer" : ""}</span>
              {used !== null && (
                <div className="olano-credits-month">
                  <div className="olano-credits-month-head">
                    <span>Included this month{renews ? `, renews ${renews}` : ""}</span>
                    <span>
                      {Math.floor(b.monthly_used ?? 0).toLocaleString("en-US")} of {Math.floor(b.monthly_included ?? 0).toLocaleString("en-US")}
                    </span>
                  </div>
                  <div
                    className={`olano-credits-bar${used > 90 ? " full" : ""}`}
                    role="progressbar"
                    aria-label="Included credits used"
                    aria-valuemin={0}
                    aria-valuemax={100}
                    aria-valuenow={Math.round(used)}
                  >
                    <div style={{ width: `${used}%` }} />
                  </div>
                </div>
              )}
            </div>
            <IonList inset>
              <IonItem>
                <IonLabel>Included credits left</IonLabel>
                <IonNote slot="end">{formatCredits(b.included_remaining ?? 0, unit)}</IonNote>
              </IonItem>
              <IonItem lines="none">
                <IonLabel>Top-ups left</IonLabel>
                <IonNote slot="end">{formatCredits(b.purchased_remaining ?? 0, unit)}</IonNote>
              </IonItem>
            </IonList>

            <div className="olano-section">By service, last {days} days</div>
            {usage.error && !usage.data && <IonNote className="olano-list-note">Usage could not be loaded. Pull down to try again.</IonNote>}
            {usage.data && services.length === 0 && <IonNote className="olano-list-note">Nothing spent in this period.</IonNote>}
            {services.length > 0 && (
              <IonList inset>
                {services.map((s) => (
                  <IonItem key={s.service}>
                    <IonLabel>
                      {s.label}
                      <p>
                        {s.calls.toLocaleString("en-US")} {s.calls === 1 ? "call" : "calls"}
                      </p>
                    </IonLabel>
                    <IonNote slot="end">{formatCredits(s.credits, unit)}</IonNote>
                  </IonItem>
                ))}
                <IonItem lines="none" className="olano-credits-total">
                  <IonLabel>Total</IonLabel>
                  <IonNote slot="end">{formatCredits(usage.data?.total_credits ?? 0, unit)}</IonNote>
                </IonItem>
              </IonList>
            )}
            <IonNote className="olano-list-note">
              Credits pay for the AI Olano runs for this deployment. Admins manage them from the dashboard.
            </IonNote>
          </>
        )}
      </IonContent>
    </IonPage>
  );
}
