/**
 * A menu as a grouped bottom sheet, the way iOS draws a screen's options:
 * who it is about at the top, sections with small grey headers, an icon and a
 * plain label per row, the current value on the right, and a chevron only on
 * rows that open another screen. Replaces a long flat action sheet, where
 * every option was the same orange line and nothing said which was which.
 *
 * A row that opens another sheet or screen runs once this sheet has closed:
 * two sheets at once is one too many, and a page transition under a closing
 * modal can leave a blank page. A row that must start inside the tap
 * (Hands-Free opens the microphone, which a phone allows only for a gesture)
 * runs in the click itself.
 */

import { IonContent, IonIcon, IonItem, IonLabel, IonList, IonModal, IonNote } from "@ionic/react";
import {
  alarmOutline,
  chatbubblesOutline,
  contractOutline,
  createOutline,
  folderOpenOutline,
  headsetOutline,
  informationCircleOutline,
  optionsOutline,
  pencilOutline,
  personAddOutline,
} from "ionicons/icons";
import { useRef, type ReactNode } from "react";

import type { MenuAction, MenuIcon, MenuSection } from "../chat/menu.ts";

const ICONS: Record<MenuIcon, string> = {
  new: createOutline,
  mode: optionsOutline,
  rename: pencilOutline,
  compact: contractOutline,
  conversations: chatbubblesOutline,
  files: folderOpenOutline,
  scheduled: alarmOutline,
  info: informationCircleOutline,
  handsFree: headsetOutline,
  addAgent: personAddOutline,
};

export function MenuSheet({
  open,
  onClose,
  header,
  sections,
  onAction,
}: {
  open: boolean;
  onClose: () => void;
  /** Who or what the menu is about, drawn above the sections. */
  header?: ReactNode;
  sections: MenuSection[];
  onAction: (action: MenuAction) => void;
}) {
  const modal = useRef<HTMLIonModalElement>(null);
  const after = useRef<MenuAction | null>(null);

  function pick(action: MenuAction, opens: "screen" | "sheet" | "now") {
    // Another sheet or another screen waits until this sheet has gone: a
    // page transition under a closing modal can leave the page underneath
    // blank. Only what must start inside the tap (`now`) runs here.
    if (opens === "now") onAction(action);
    else after.current = action;
    void modal.current?.dismiss();
  }

  return (
    <IonModal
      ref={modal}
      isOpen={open}
      onDidDismiss={() => {
        onClose();
        const next = after.current;
        after.current = null;
        if (next) window.setTimeout(() => onAction(next), 0);
      }}
      breakpoints={[0, 0.75, 1]}
      initialBreakpoint={0.75}
      // Scrolls at its first height too: by default a sheet scrolls only once
      // dragged to full height, which left the last rows out of reach.
      expandToScroll={false}
      handle
      className="olano-menu-sheet"
    >
      <IonContent className="olano-grouped">
        {header && <div className="olano-menu-header">{header}</div>}
        {sections.map((section) => (
          <div key={section.title}>
            <div className="olano-section">{section.title}</div>
            <IonList inset>
              {section.rows.map((row, i) => (
                <IonItem
                  key={row.action}
                  button
                  detail={row.opens === "screen"}
                  lines={i === section.rows.length - 1 ? "none" : undefined}
                  onClick={() => pick(row.action, row.opens)}
                >
                  <IonIcon slot="start" icon={ICONS[row.icon]} color="primary" aria-hidden="true" />
                  <IonLabel>
                    <span className="olano-menu-label">{row.label}</span>
                    {row.detail && <p>{row.detail}</p>}
                  </IonLabel>
                  {row.value && <IonNote slot="end">{row.value}</IonNote>}
                </IonItem>
              ))}
            </IonList>
          </div>
        ))}
        <div className="olano-menu-bottom" />
      </IonContent>
    </IonModal>
  );
}
