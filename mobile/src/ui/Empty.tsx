import { IonButton, IonIcon, IonText } from "@ionic/react";
import { alertCircleOutline } from "ionicons/icons";
import type { ReactNode } from "react";

/** The centred message a list shows when it has nothing to list. */
export function Empty({ title, body, icon, children }: { title: string; body?: string; icon?: string; children?: ReactNode }) {
  return (
    <div className="olano-empty">
      {icon && <IonIcon icon={icon} className="olano-empty-icon" />}
      <h3>{title}</h3>
      {body && <p>{body}</p>}
      {children}
    </div>
  );
}

/** A load that failed, with the way to try again. */
export function ErrorNote({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="olano-empty">
      <IonIcon icon={alertCircleOutline} className="olano-empty-icon" color="medium" />
      <IonText color="medium">
        <p>{message}</p>
      </IonText>
      {onRetry && (
        <IonButton fill="outline" size="small" onClick={onRetry}>
          Try again
        </IonButton>
      )}
    </div>
  );
}
