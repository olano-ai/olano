/**
 * The conversation's model and reasoning level, picked from a sheet that
 * slides up from the header: reasoning levels at the top (when the model has
 * them), then every model this deployment can run. The choice is this
 * conversation's only (chat/models.ts).
 */

import {
  IonButton,
  IonButtons,
  IonContent,
  IonHeader,
  IonIcon,
  IonItem,
  IonLabel,
  IonList,
  IonModal,
  IonSearchbar,
  IonSegment,
  IonSegmentButton,
  IonSpinner,
  IonTitle,
  IonToolbar,
} from "@ionic/react";
import { checkmark } from "ionicons/icons";
import { useMemo, useState } from "react";

import { levelLabel, modelLabel, type ModelChoice, type ProvidersResponse, reasoningLevels } from "../../chat/models.ts";
import { haptic } from "../../native/haptics.ts";
import { ErrorNote } from "../../ui/Empty.tsx";

const GROUPS: { id: ModelChoice["group"]; title: string }[] = [
  { id: "default", title: "Deployment defaults" },
  { id: "olano", title: "Olano" },
  { id: "other", title: "Your providers" },
];

/** Searching shows once the list is long enough to need it. */
const SEARCH_FROM = 9;

export function ModelSheet({
  open,
  onClose,
  catalog,
  catalogError,
  onRetry,
  choices,
  agentModel,
  model,
  level,
  onPick,
}: {
  open: boolean;
  onClose: () => void;
  catalog: ProvidersResponse | undefined;
  catalogError: string | null;
  onRetry: () => void;
  choices: ModelChoice[];
  /** The agent's own model: what "Agent default" runs. */
  agentModel: string;
  /** This conversation's pick ("" = the agent's own). */
  model: string;
  level: string;
  onPick: (model: string, level: string) => void;
}) {
  const [query, setQuery] = useState("");
  const effective = model || agentModel;
  const levels = reasoningLevels(catalog, effective);
  const q = query.trim().toLowerCase();
  const shown = useMemo(
    () => (q ? choices.filter((c) => `${c.label} ${c.value}`.toLowerCase().includes(q)) : choices),
    [choices, q],
  );

  function pickModel(value: string) {
    void haptic("light");
    // A level the new model does not offer goes back to its default.
    const next = reasoningLevels(catalog, value || agentModel);
    onPick(value, next?.levels.includes(level) ? level : "");
  }

  return (
    <IonModal
      isOpen={open}
      onDidDismiss={() => {
        setQuery("");
        onClose();
      }}
      breakpoints={[0, 0.65, 1]}
      initialBreakpoint={0.65}
      className="olano-model-sheet"
    >
      <IonHeader>
        <IonToolbar>
          <IonTitle>Model</IonTitle>
          <IonButtons slot="end">
            <IonButton onClick={onClose}>Done</IonButton>
          </IonButtons>
        </IonToolbar>
      </IonHeader>
      <IonContent className="olano-grouped">
        <p className="olano-model-note">For this conversation only. New conversations start on the agent's model.</p>

        {levels && (
          <div className="olano-model-levels">
            <div className="olano-model-levels-title">Reasoning</div>
            <IonSegment
              value={level || "default"}
              scrollable
              aria-label="Reasoning"
              onIonChange={(e) => {
                const value = String(e.detail.value ?? "default");
                void haptic("light");
                onPick(model, value === "default" ? "" : value);
              }}
            >
              <IonSegmentButton value="default">
                <IonLabel>{levels.defaultLevel ? `Default (${levelLabel(levels.defaultLevel)})` : "Default"}</IonLabel>
              </IonSegmentButton>
              {levels.levels.map((l) => (
                <IonSegmentButton key={l} value={l}>
                  <IonLabel>{levelLabel(l)}</IonLabel>
                </IonSegmentButton>
              ))}
            </IonSegment>
          </div>
        )}

        {choices.length >= SEARCH_FROM && (
          <IonSearchbar
            value={query}
            placeholder="Search models"
            onIonInput={(e) => setQuery(String(e.detail.value ?? ""))}
            className="olano-model-search"
          />
        )}

        {catalogError && !catalog && <ErrorNote message={catalogError} onRetry={onRetry} />}
        {!catalog && !catalogError && (
          <div className="olano-model-loading">
            <IonSpinner name="crescent" />
          </div>
        )}

        {catalog && !q && (
          <IonList inset>
            <ModelRow
              label="Agent default"
              hint={agentModel ? modelLabel(agentModel) : undefined}
              selected={!model}
              onClick={() => pickModel("")}
            />
          </IonList>
        )}
        {catalog &&
          GROUPS.map((group) => {
            const rows = shown.filter((c) => c.group === group.id && c.value !== agentModel);
            if (!rows.length) return null;
            return (
              <section key={group.id} aria-label={group.title}>
                <div className="olano-model-group">{group.title}</div>
                <IonList inset>
                  {rows.map((c) => (
                    <ModelRow key={c.value} label={c.label} hint={c.hint} selected={model === c.value} onClick={() => pickModel(c.value)} />
                  ))}
                </IonList>
              </section>
            );
          })}
        {catalog && q && !shown.length && <p className="olano-model-note">No model matches “{query.trim()}”.</p>}
      </IonContent>
    </IonModal>
  );
}

function ModelRow({ label, hint, selected, onClick }: { label: string; hint?: string; selected: boolean; onClick: () => void }) {
  return (
    <IonItem button detail={false} onClick={onClick} aria-current={selected ? "true" : undefined}>
      <IonLabel>
        <h3>{label}</h3>
        {hint && <p>{hint}</p>}
      </IonLabel>
      {selected && <IonIcon slot="end" icon={checkmark} color="primary" aria-label="Selected" />}
    </IonItem>
  );
}
