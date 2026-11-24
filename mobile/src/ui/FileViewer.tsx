/**
 * A file from a reply, opened over the conversation (chat/files.ts says how
 * each kind opens): Markdown rendered, text and code highlighted, a PDF drawn
 * where the web view can, and for everything else the phone's "Open in..."
 * sheet. Share in the corner hands any of them to another app.
 */

import {
  IonButton,
  IonButtons,
  IonContent,
  IonHeader,
  IonIcon,
  IonModal,
  IonSpinner,
  IonTitle,
  IonToolbar,
} from "@ionic/react";
import { documentTextOutline, shareOutline } from "ionicons/icons";
import { useEffect, useState } from "react";

import {
  clipText,
  decodeText,
  fileView,
  formatSize,
  MAX_HIGHLIGHT_CHARS,
  MAX_OPEN_BYTES,
  readableText,
} from "../chat/files.ts";
import { openWithOtherApp } from "../native/files.ts";
import { isNative } from "../native/shell.ts";
import { Capacitor } from "@capacitor/core";
import type { MediaRef } from "../shared/dashboard.ts";
import { currentApi } from "../state/session.ts";
import { createStore, useStore } from "../state/store.ts";
import { CodeView, Markdown } from "./Markdown.tsx";

const opened = createStore<MediaRef | null>(null);

export function openFileViewer(item: MediaRef): void {
  opened.set(item);
}

interface Loaded {
  type: string;
  base64: string;
  text?: string;
  clipped?: boolean;
  pdfUrl?: string;
}

/** An iPhone's web view (and a desktop browser) draws a PDF itself; Android's does not. */
const DRAWS_PDF = !isNative() || Capacitor.getPlatform() === "ios";

export function FileViewer() {
  const item = useStore(opened);
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [sharing, setSharing] = useState(false);
  const how = item ? fileView(item.name, item.mime) : null;
  const tooBig = !!item?.size && item.size > MAX_OPEN_BYTES;

  useEffect(() => {
    setLoaded(null);
    setError(null);
    if (!item || tooBig) return;
    let alive = true;
    let pdfUrl: string | undefined;
    void currentApi()
      .file(item.url)
      .then((file) => {
        if (!alive) return;
        const view = fileView(item.name, item.mime || file.type);
        const out: Loaded = { type: file.type, base64: file.base64 };
        if (view.view === "markdown" || view.view === "text") {
          const clip = clipText(readableText(decodeText(file.base64), view.lang));
          out.text = clip.text;
          out.clipped = clip.clipped;
        } else if (view.view === "pdf" && DRAWS_PDF) {
          const bytes = Uint8Array.from(atob(file.base64), (c) => c.charCodeAt(0));
          pdfUrl = URL.createObjectURL(new Blob([bytes], { type: "application/pdf" }));
          out.pdfUrl = pdfUrl;
        }
        setLoaded(out);
      })
      .catch(() => {
        if (alive) setError("The file could not be opened. It may have been moved or deleted.");
      });
    return () => {
      alive = false;
      if (pdfUrl) URL.revokeObjectURL(pdfUrl);
    };
  }, [item, tooBig]);

  async function share() {
    if (!item || !loaded) return;
    setSharing(true);
    try {
      await openWithOtherApp(item.name, loaded.base64, loaded.type);
    } catch {
      setError("The file could not be handed to another app.");
    } finally {
      setSharing(false);
    }
  }

  const showsInline = !!loaded && (loaded.text !== undefined || !!loaded.pdfUrl);

  return (
    <IonModal isOpen={!!item} onDidDismiss={() => opened.set(null)} className="olano-file-viewer">
      <IonHeader>
        <IonToolbar>
          <IonButtons slot="start">
            <IonButton onClick={() => opened.set(null)}>Close</IonButton>
          </IonButtons>
          <IonTitle>{item?.name}</IonTitle>
          <IonButtons slot="end">
            <IonButton onClick={() => void share()} disabled={!loaded || sharing} aria-label="Share file">
              <IonIcon slot="icon-only" icon={shareOutline} />
            </IonButton>
          </IonButtons>
        </IonToolbar>
      </IonHeader>
      <IonContent className={loaded?.pdfUrl ? "olano-file-pdf" : undefined}>
        {item && !loaded && !error && !tooBig && (
          <div className="olano-file-loading">
            <IonSpinner name="crescent" />
          </div>
        )}

        {loaded?.text !== undefined && how?.view === "markdown" && (
          <div className="olano-file-body">
            <Markdown text={loaded.text} />
          </div>
        )}
        {loaded?.text !== undefined && how?.view !== "markdown" && (
          <div className="olano-file-body">
            <CodeView
              code={loaded.text}
              lang={how?.lang ?? ""}
              label={item?.name.split(".").pop()?.toUpperCase()}
              plain={loaded.text.length > MAX_HIGHLIGHT_CHARS}
            />
          </div>
        )}
        {loaded?.clipped && <p className="olano-file-note">This is the start of a long file. Share it to see all of it.</p>}
        {loaded?.pdfUrl && <iframe slot="fixed" className="olano-file-frame" src={loaded.pdfUrl} title={item?.name} />}

        {item && (tooBig || error || (loaded && !showsInline)) && (
          <div className="olano-file-card">
            <IonIcon icon={documentTextOutline} aria-hidden="true" />
            <h2>{item.name}</h2>
            {item.size ? <p>{formatSize(item.size)}</p> : null}
            {error && <p className="olano-file-error">{error}</p>}
            {tooBig && <p>Too large to open on the phone. Open it from the dashboard in a browser.</p>}
            {loaded && !showsInline && (
              <IonButton onClick={() => void share()} disabled={sharing}>
                <IonIcon slot="start" icon={shareOutline} />
                Open in another app
              </IonButton>
            )}
          </div>
        )}
      </IonContent>
    </IonModal>
  );
}
