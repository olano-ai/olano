/**
 * An agent's files: its workspace a folder at a time, a search across it, and
 * uploads from the phone. A file opens in the photo viewer or the file viewer,
 * the same ones a reply's files open in.
 *
 * Reached from Settings > Files (the agents, then one agent's files) and from
 * a conversation's menu (that agent's files). The folder is in the route
 * (`.../_files/reports%2F2026`), so the back button climbs folder by folder.
 */

import {
  IonBackButton,
  IonButton,
  IonButtons,
  IonContent,
  IonHeader,
  IonIcon,
  IonItem,
  IonLabel,
  IonList,
  IonNote,
  IonPage,
  IonRefresher,
  IonRefresherContent,
  IonSearchbar,
  IonSkeletonText,
  IonSpinner,
  IonTitle,
  IonToolbar,
  useIonActionSheet,
  useIonToast,
} from "@ionic/react";
import { cloudUploadOutline, documentOutline, folderOutline, imageOutline, searchOutline } from "ionicons/icons";
import { useEffect, useMemo, useRef, useState } from "react";

import { formatSize } from "../../chat/files.ts";
import {
  baseName,
  fileKind,
  folderRoute,
  freeName,
  parentOf,
  rawUrl,
  SEARCH_MIN,
  sortEntries,
} from "../../files/workspace.ts";
import { haptic } from "../../native/haptics.ts";
import { messageOf } from "../../native/shell.ts";
import { ApiError } from "../../net/api.ts";
import type { FileListing, FileSearch, WorkspaceEntry } from "../../net/types.ts";
import { useRouteParams } from "../../routeParams.ts";
import { invalidate, useResource } from "../../state/resources.ts";
import { currentApi, session } from "../../state/session.ts";
import { useStore } from "../../state/store.ts";
import { Avatar, displayName } from "../../ui/Avatar.tsx";
import { Empty, ErrorNote } from "../../ui/Empty.tsx";
import { openFileViewer } from "../../ui/FileViewer.tsx";
import { openPhotoViewer } from "../../ui/PhotoViewer.tsx";
import { ago } from "../../ui/time.ts";
import { useAgents } from "../chats/AgentsPage.tsx";

/** Settings > Files: every agent you can reach, then its files. */
export function FilesHomePage() {
  const [agents, reload] = useAgents();
  return (
    <IonPage>
      <IonHeader translucent>
        <IonToolbar>
          <IonButtons slot="start">
            <IonBackButton defaultHref="/settings" text="" />
          </IonButtons>
          <IonTitle>Files</IonTitle>
        </IonToolbar>
      </IonHeader>
      <IonContent fullscreen className="olano-grouped">
        {agents.error && !agents.data && <ErrorNote message={agents.error} onRetry={() => void reload()} />}
        {!agents.data && !agents.error && (
          <div className="olano-empty">
            <IonSpinner name="crescent" />
          </div>
        )}
        {agents.data && (
          <IonList inset>
            {agents.data.map((agent) => (
              <IonItem key={agent.name} button detail routerLink={`/settings/files/${encodeURIComponent(agent.name)}`}>
                <div slot="start">
                  <Avatar agent={agent} size={30} />
                </div>
                <IonLabel>{displayName(agent)}</IonLabel>
              </IonItem>
            ))}
          </IonList>
        )}
        <IonNote className="olano-list-note">
          Each agent works in its own folder: what it writes and keeps, and what you give it to work on.
        </IonNote>
      </IonContent>
    </IonPage>
  );
}

function useFolder(agent: string, path: string) {
  return useResource<FileListing>(`files:${agent}:${path}`, () => currentApi().listFiles(agent, path), 15000);
}

/**
 * Where this screen lives: under Settings or under the agent's conversations.
 * Told by its route (`under`), not read off the current URL: a page kept in
 * the stack re-renders while another tab is on screen, and the URL then
 * gave it the wrong back link and title.
 */
function filesBase(agent: string, under: "settings" | "chats"): { base: string; top: string } {
  const enc = encodeURIComponent(agent);
  return under === "settings"
    ? { base: `/settings/files/${enc}`, top: "/settings/files" }
    : { base: `/chats/${enc}/_files`, top: `/chats/${enc}` };
}

function readBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const url = String(reader.result || "");
      resolve(url.slice(url.indexOf(",") + 1));
    };
    reader.onerror = () => reject(reader.error ?? new Error("The file could not be read."));
    reader.readAsDataURL(file);
  });
}

function EntryRow({ entry, agent, base, showFolder }: { entry: WorkspaceEntry; agent: string; base: string; showFolder?: boolean }) {
  const folder = entry.type === "directory";
  const kind = folder ? null : fileKind(entry.name);
  const where = showFolder ? parentOf(entry.path) : "";

  function open() {
    if (kind === "image") {
      openPhotoViewer([{ key: entry.path, name: entry.name, url: rawUrl(agent, entry.path) }], 0);
      return;
    }
    openFileViewer({
      kind: kind ?? "file",
      name: entry.name,
      path: entry.path,
      mime: "",
      url: rawUrl(agent, entry.path),
      size: entry.size,
    });
  }

  const label = (
    <IonLabel>
      <h3>{entry.name}</h3>
      <p>
        {[where, folder ? "" : formatSize(entry.size), entry.modified ? ago(entry.modified) : ""].filter(Boolean).join(" · ")}
      </p>
    </IonLabel>
  );
  const icon = <IonIcon slot="start" icon={folder ? folderOutline : kind === "image" ? imageOutline : documentOutline} color={folder ? "primary" : "medium"} />;
  return folder ? (
    <IonItem button detail routerLink={folderRoute(base, entry.path)} className="olano-file-row">
      {icon}
      {label}
    </IonItem>
  ) : (
    <IonItem button detail={false} onClick={open} className="olano-file-row">
      {icon}
      {label}
    </IonItem>
  );
}

/** One folder of one agent's files, or what a search found in them. */
export function FilesPage({ under = "chats" }: { under?: "settings" | "chats" }) {
  const params = useRouteParams();
  const agent = params.agent ?? "";
  const path = params.folder ?? "";
  const { base, top } = filesBase(agent, under);
  const [agents] = useAgents();
  const owner = agents.data?.find((a) => a.name === agent);
  const [listing, reload] = useFolder(agent, path);
  const [query, setQuery] = useState("");
  const [term, setTerm] = useState("");
  const [uploading, setUploading] = useState<string | null>(null);
  // Members look at an agent's files; only admins add to them.
  const me = useStore(session).me;
  const canUpload = !!me?.user?.is_admin || me?.multi_user === false;
  const input = useRef<HTMLInputElement>(null);
  const [toast] = useIonToast();
  const [sheet] = useIonActionSheet();

  // Searching waits for a pause in typing.
  useEffect(() => {
    const id = window.setTimeout(() => setTerm(query.trim()), 300);
    return () => window.clearTimeout(id);
  }, [query]);
  const searching = term.length >= SEARCH_MIN;
  const [found] = useResource<FileSearch>(
    searching ? `files:${agent}:?${term}` : null,
    () => currentApi().searchFiles(agent, term),
    15000,
  );

  const entries = useMemo(() => sortEntries(listing.data?.entries ?? []), [listing.data]);
  const title = path ? baseName(path) : owner ? `${displayName(owner)}'s Files` : "Files";
  const back = path ? folderRoute(base, parentOf(path)) : top;

  async function upload(file: File, overwrite = false, name = file.name) {
    setUploading(name);
    try {
      const data = await readBase64(file);
      await currentApi().uploadFile(agent, path, name, data, overwrite);
      void haptic("success");
      invalidate(`files:${agent}:`);
      void toast({ message: `${name} is in ${path ? baseName(path) : "the agent's folder"}.`, duration: 2500 });
    } catch (error) {
      if (error instanceof ApiError && error.status === 409 && error.code === "file_exists") {
        const taken = entries.map((e) => e.name);
        void sheet({
          header: `${name} is already here`,
          buttons: [
            { text: "Replace It", role: "destructive", handler: () => void upload(file, true, name) },
            { text: "Keep Both", handler: () => void upload(file, false, freeName(name, taken)) },
            { text: "Cancel", role: "cancel" },
          ],
        });
      } else if (error instanceof ApiError && error.status === 403) {
        void toast({ message: "Your account can look at these files but not add to them.", duration: 3500, color: "danger" });
      } else {
        void toast({ message: messageOf(error, "The file could not be uploaded."), duration: 3500, color: "danger" });
      }
    } finally {
      setUploading(null);
    }
  }

  return (
    <IonPage>
      <IonHeader translucent>
        <IonToolbar>
          <IonButtons slot="start">
            <IonBackButton defaultHref={back} text="" />
          </IonButtons>
          <IonTitle>{title}</IonTitle>
          {canUpload && (
            <IonButtons slot="end">
              <IonButton aria-label="Upload a file" disabled={!!uploading} onClick={() => input.current?.click()}>
                {uploading ? <IonSpinner name="crescent" /> : <IonIcon slot="icon-only" icon={cloudUploadOutline} />}
              </IonButton>
            </IonButtons>
          )}
        </IonToolbar>
        <IonToolbar>
          <IonSearchbar
            value={query}
            placeholder={path ? `Search all of ${owner ? displayName(owner) : agent}'s files` : "Search files"}
            debounce={0}
            onIonInput={(e) => setQuery(String(e.detail.value ?? ""))}
          />
        </IonToolbar>
      </IonHeader>
      <IonContent fullscreen className="olano-grouped">
        <IonRefresher slot="fixed" onIonRefresh={(e) => void reload().finally(() => e.detail.complete())}>
          <IonRefresherContent />
        </IonRefresher>
        <input
          ref={input}
          type="file"
          hidden
          onChange={(e) => {
            const file = e.target.files?.[0];
            e.target.value = "";
            if (file) void upload(file);
          }}
        />
        {uploading && <IonNote className="olano-list-note">Uploading {uploading}…</IonNote>}

        {searching ? (
          <>
            {!found.data && !found.error && (
              <div className="olano-empty">
                <IonSpinner name="crescent" />
              </div>
            )}
            {found.error && !found.data && <ErrorNote message={found.error} />}
            {found.data && found.data.results.length === 0 && (
              <Empty icon={searchOutline} title="Nothing found" body={`No file or folder name holds "${term}".`} />
            )}
            {found.data && found.data.results.length > 0 && (
              <IonList inset>
                {found.data.results.map((entry) => (
                  <EntryRow key={entry.path} entry={entry} agent={agent} base={base} showFolder />
                ))}
              </IonList>
            )}
            {found.data?.truncated && <IonNote className="olano-list-note">Showing the first matches. Type more to narrow it down.</IonNote>}
          </>
        ) : (
          <>
            {listing.error && !listing.data && <ErrorNote message={listing.error} onRetry={() => void reload()} />}
            {!listing.data && !listing.error && (
              <IonList inset>
                {[0, 1, 2, 3].map((i) => (
                  <IonItem key={i}>
                    <IonLabel>
                      <IonSkeletonText animated style={{ width: "50%" }} />
                      <IonSkeletonText animated style={{ width: "30%" }} />
                    </IonLabel>
                  </IonItem>
                ))}
              </IonList>
            )}
            {listing.data && entries.length === 0 && (
              <Empty
                icon={folderOutline}
                title="This folder is empty"
                body={canUpload ? "Upload a file with the button at the top, or ask the agent to write one here." : "Ask the agent to write a file here."}
              />
            )}
            {entries.length > 0 && (
              <IonList inset>
                {entries.map((entry) => (
                  <EntryRow key={entry.path} entry={entry} agent={agent} base={base} />
                ))}
              </IonList>
            )}
            {listing.data?.truncated && (
              <IonNote className="olano-list-note">
                Showing {entries.length} of {listing.data.total}. Search to find the rest.
              </IonNote>
            )}
          </>
        )}
      </IonContent>
    </IonPage>
  );
}
