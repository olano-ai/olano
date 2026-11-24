/**
 * Every thread an agent has, newest first: the conversations people had with
 * it and everything else (scheduled runs, other agents, Cortex, outside
 * calls), filtered by where each came from with the dashboard's chips. Tapping
 * an agent opens its last conversation instead (chat/threadPick.ts); this list
 * is a conversation's **...** menu, Conversations. A compose button starts a
 * new one; swipe a row to rename it or (for agent admins) delete it.
 *
 * The list is read a page at a time (chat/threadPages.ts): the newest twenty,
 * and the page below as the person nears the bottom, since an agent used for
 * months has thousands. The chips count what has been read so far. Typing in
 * the search bar asks the deployment for the threads whose messages hold the
 * words, with a snippet of where; one search reads a bounded stretch of
 * history, and **Search older conversations** continues it.
 */

import {
  IonBackButton,
  IonButton,
  IonButtons,
  IonChip,
  IonContent,
  IonHeader,
  IonIcon,
  IonInfiniteScroll,
  IonInfiniteScrollContent,
  IonItem,
  IonItemOption,
  IonItemOptions,
  IonItemSliding,
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
  useIonAlert,
  useIonToast,
} from "@ionic/react";
import { createOutline, pencil, trash } from "ionicons/icons";

import { useEffect, useMemo, useState } from "react";

import { threadTitle } from "../../chat/model.ts";
import { filterChips } from "../../chat/threadFilters.ts";
import { mapThread, mergePages, PAGE_SIZE } from "../../chat/threadPages.ts";
import { sourceBadges } from "../../chat/sources.ts";
import { ApiError } from "../../net/api.ts";
import type { ThreadPage, ThreadSummary } from "../../net/types.ts";
import { FILTER_ALL, filterThreadRows, readThreadFilter, writeThreadFilter } from "../../shared/dashboard.ts";
import { messageOf } from "../../native/shell.ts";
import { patchResource, useResource } from "../../state/resources.ts";
import { currentApi, session } from "../../state/session.ts";
import { useStore } from "../../state/store.ts";
import { Avatar, displayName } from "../../ui/Avatar.tsx";
import { Empty, ErrorNote } from "../../ui/Empty.tsx";
import { listTime } from "../../ui/time.ts";
import { useRouteParams } from "../../routeParams.ts";
import { useAgents } from "./AgentsPage.tsx";

const SEARCH_MIN = 2;

/** The pages below the first, read as the person scrolls. */
interface OlderPages {
  threads: ThreadSummary[];
  /** Where the next page starts; null once the first page says there is nothing below it. */
  cursor: string | null;
  loading: boolean;
  error: string | null;
}

interface SearchState {
  term: string;
  pages: ThreadPage[];
  loading: boolean;
  error: string | null;
  /** The deployment has no search (a runtime from before it). */
  unsupported: boolean;
}

export function ThreadsPage() {
  const params = useRouteParams();
  const agentName = params.agent ?? "";
  const [agents] = useAgents();
  const agent = agents.data?.find((a) => a.name === agentName);
  const key = `threads:${agentName}`;
  const [first, reload] = useResource<ThreadPage>(key, () => currentApi().threadPage(agentName, { limit: PAGE_SIZE }), 15000);
  const [older, setOlder] = useState<OlderPages>({ threads: [], cursor: null, loading: false, error: null });
  useEffect(() => setOlder({ threads: [], cursor: null, loading: false, error: null }), [agentName]);
  const cursor = older.cursor ?? (older.threads.length ? null : (first.data?.next_before ?? null));
  const hasMore = older.threads.length ? older.cursor !== null : !!first.data?.has_more;
  const rows = useMemo(() => (first.data ? mergePages([first.data.threads, older.threads]) : undefined), [first.data, older.threads]);

  async function loadOlder(): Promise<void> {
    if (!cursor || older.loading) return;
    setOlder((o) => ({ ...o, loading: true, error: null }));
    try {
      const page = await currentApi().threadPage(agentName, { limit: PAGE_SIZE, before: cursor });
      setOlder((o) => ({
        threads: [...o.threads, ...page.threads],
        cursor: page.has_more ? page.next_before : null,
        loading: false,
        error: null,
      }));
    } catch (error) {
      setOlder((o) => ({ ...o, loading: false, error: messageOf(error, "Couldn't load earlier conversations.") }));
    }
  }

  const { me } = useStore(session);
  const canDelete = !!me && (me.user.is_admin || me.agents?.some((g) => g.agent_name === agentName && g.role === "admin"));
  const [alert] = useIonAlert();
  const [toast] = useIonToast();
  const base = `/chats/${encodeURIComponent(agentName)}`;
  // Where the threads came from: one chip per source, the choice remembered
  // per agent on this phone.
  const [filter, setFilter] = useState(() => readThreadFilter(agentName));
  useEffect(() => setFilter(readThreadFilter(agentName)), [agentName]);
  const chips = useMemo(() => filterChips(rows ?? []), [rows]);
  const active = chips.some((c) => c.id === filter) ? filter : FILTER_ALL;
  const shown = useMemo(() => {
    const list = (rows ?? []).map((t) => ({ ...t, origin: t.origin ?? "", channel: t.channel ?? "" }));
    return filterThreadRows(list, active);
  }, [rows, active]);
  const choose = (id: string) => {
    setFilter(id);
    writeThreadFilter(agentName, id);
  };

  // Searching waits for a pause in typing, and asks the deployment.
  const [query, setQuery] = useState("");
  const [search, setSearch] = useState<SearchState>({ term: "", pages: [], loading: false, error: null, unsupported: false });
  useEffect(() => {
    const term = query.trim();
    if (term.length < SEARCH_MIN) {
      setSearch({ term: "", pages: [], loading: false, error: null, unsupported: false });
      return;
    }
    const id = window.setTimeout(() => void runSearch(term, null), 350);
    return () => window.clearTimeout(id);
  }, [query, agentName]);

  async function runSearch(term: string, before: string | null): Promise<void> {
    setSearch((s) => ({ term, pages: s.term === term ? s.pages : [], loading: true, error: null, unsupported: false }));
    try {
      const page = await currentApi().searchThreads(agentName, term, { limit: PAGE_SIZE, before: before ?? undefined });
      setSearch((s) => (s.term === term ? { ...s, pages: before ? [...s.pages, page] : [page], loading: false } : s));
    } catch (error) {
      const unsupported = error instanceof ApiError && error.status === 404;
      setSearch((s) =>
        s.term === term
          ? { ...s, loading: false, unsupported, error: unsupported ? null : messageOf(error, "The search did not go through.") }
          : s,
      );
    }
  }
  const searching = search.term.length >= SEARCH_MIN;
  const hits = useMemo(() => mergePages(search.pages.map((p) => p.threads)), [search.pages]);
  const lastSearchPage = search.pages[search.pages.length - 1];
  const searchHasMore = !!lastSearchPage?.has_more && !!lastSearchPage.next_before;

  function patch(id: string, update: (row: ThreadSummary) => ThreadSummary | null) {
    patchResource<ThreadPage>(key, (page) => (page ? mapThread(page, id, update) : { threads: [], has_more: false, next_before: null }));
    setOlder((o) => ({ ...o, threads: mapThread({ threads: o.threads, has_more: false, next_before: null }, id, update).threads }));
    setSearch((s) => ({ ...s, pages: s.pages.map((p) => mapThread(p, id, update)) }));
  }

  function rename(thread: ThreadSummary) {
    void alert({
      header: "Rename conversation",
      inputs: [{ name: "name", value: thread.name || threadTitle(thread), attributes: { maxlength: 200 } }],
      buttons: [
        { text: "Cancel", role: "cancel" },
        {
          text: "Save",
          handler: (values: { name: string }) => {
            const name = String(values.name ?? "").trim();
            patch(thread.id, (t) => ({ ...t, name }));
            currentApi()
              .renameThread(agentName, thread.id, name)
              .catch((e) => {
                void toast({ message: messageOf(e), duration: 2500, color: "danger" });
                void reload();
              });
          },
        },
      ],
    });
  }

  function remove(thread: ThreadSummary) {
    void alert({
      header: "Delete this conversation?",
      message: "It is deleted for everyone, with the agent's memory of it.",
      buttons: [
        { text: "Cancel", role: "cancel" },
        {
          text: "Delete",
          role: "destructive",
          handler: () => {
            patch(thread.id, () => null);
            currentApi()
              .deleteThread(agentName, thread.id)
              .catch((e) => {
                void toast({ message: messageOf(e), duration: 2500, color: "danger" });
                void reload();
              });
          },
        },
      ],
    });
  }

  const title = agent ? displayName(agent) : agentName;

  const row = (thread: ThreadSummary) => (
    <IonItemSliding key={thread.id}>
      <IonItem button detail routerLink={`${base}/${encodeURIComponent(thread.id)}`}>
        <IonLabel>
          <h3 className="olano-thread-title">{threadTitle(thread)}</h3>
          {thread.match && (
            <p className="olano-thread-match">
              <span className="olano-thread-match-role">{thread.match.role === "assistant" ? title : thread.sender || "You"}:</span>{" "}
              {thread.match.snippet}
            </p>
          )}
          <p className="olano-thread-meta">
            {sourceBadges(thread).map((badge) => (
              <span key={badge.label} className={`olano-source olano-source-${badge.tone}`}>
                {badge.label}
              </span>
            ))}
            {thread.running ? (
              <>
                <span className="olano-live-dot" />
                Working…
              </>
            ) : (
              [thread.sender, thread.message_count ? `${thread.message_count} messages` : ""].filter(Boolean).join(" · ")
            )}
          </p>
        </IonLabel>
        <IonNote slot="end" className="olano-row-time">
          {listTime(thread.updated_at)}
        </IonNote>
      </IonItem>
      <IonItemOptions side="end">
        <IonItemOption color="medium" onClick={() => rename(thread)}>
          <IonIcon slot="icon-only" icon={pencil} />
        </IonItemOption>
        {canDelete && (
          <IonItemOption color="danger" expandable onClick={() => remove(thread)}>
            <IonIcon slot="icon-only" icon={trash} />
          </IonItemOption>
        )}
      </IonItemOptions>
    </IonItemSliding>
  );

  return (
    <IonPage>
      <IonHeader translucent>
        <IonToolbar>
          <IonButtons slot="start">
            <IonBackButton defaultHref="/chats" text="Chats" />
          </IonButtons>
          <IonTitle>Conversations</IonTitle>
          <IonButtons slot="end">
            <IonButton routerLink={`${base}/new`} aria-label="New conversation">
              <IonIcon slot="icon-only" icon={createOutline} />
            </IonButton>
          </IonButtons>
        </IonToolbar>
        <IonToolbar>
          <IonSearchbar
            value={query}
            placeholder={`Search ${title}'s conversations`}
            debounce={0}
            onIonInput={(e) => setQuery(String(e.detail.value ?? ""))}
          />
        </IonToolbar>
      </IonHeader>
      <IonContent fullscreen className="olano-grouped">
        <IonRefresher slot="fixed" onIonRefresh={(e) => void reload().finally(() => e.detail.complete())}>
          <IonRefresherContent />
        </IonRefresher>

        {searching ? (
          <>
            {search.unsupported && (
              <Empty title="Search needs a newer deployment" body="This deployment cannot search conversations yet. Update it to search." />
            )}
            {search.error && <ErrorNote message={search.error} onRetry={() => void runSearch(search.term, null)} />}
            {search.loading && !search.pages.length && (
              <div className="olano-center">
                <IonSpinner name="crescent" />
              </div>
            )}
            {!search.loading && !search.error && !search.unsupported && search.pages.length > 0 && hits.length === 0 && (
              <Empty title="Nothing found" body={`No conversation with ${title} mentions "${search.term}".`} />
            )}
            {hits.length > 0 && <IonList inset>{hits.map(row)}</IonList>}
            {search.pages.length > 0 && !search.unsupported && (
              <div className="olano-older">
                {search.loading ? (
                  <IonSpinner name="dots" aria-label="Searching" />
                ) : searchHasMore ? (
                  <button type="button" className="olano-older-retry" onClick={() => void runSearch(search.term, lastSearchPage.next_before)}>
                    Search older conversations
                  </button>
                ) : (
                  hits.length > 0 && "Every conversation searched"
                )}
              </div>
            )}
          </>
        ) : (
          <>
            {agent && (
              <div className="olano-agent-hero">
                <Avatar agent={agent} size={72} />
                <h2>{title}</h2>
                {(agent.title || agent.description) && <p>{agent.title || agent.description}</p>}
              </div>
            )}

            {chips.length > 2 && (
              <div className="olano-chips olano-thread-filters" role="group" aria-label="Show conversations from">
                {chips.map((chip) => (
                  <IonChip
                    key={chip.id}
                    outline={active !== chip.id}
                    color={active === chip.id ? "primary" : undefined}
                    aria-pressed={active === chip.id}
                    onClick={() => choose(chip.id)}
                  >
                    <IonLabel>
                      {chip.label} <span className="olano-chip-count">{chip.count}</span>
                    </IonLabel>
                  </IonChip>
                ))}
              </div>
            )}

            {first.error && !first.data && <ErrorNote message={first.error} onRetry={() => void reload()} />}

            {!first.data && !first.error && (
              <IonList inset>
                {[0, 1, 2].map((i) => (
                  <IonItem key={i}>
                    <IonLabel>
                      <IonSkeletonText animated style={{ width: "55%" }} />
                      <IonSkeletonText animated style={{ width: "30%" }} />
                    </IonLabel>
                  </IonItem>
                ))}
              </IonList>
            )}

            {rows && rows.length === 0 && (
              <Empty title="No conversations yet" body={`Start one with ${title}.`}>
                <IonButton routerLink={`${base}/new`}>New conversation</IonButton>
              </Empty>
            )}

            {rows && rows.length > 0 && <IonList inset>{shown.map(row)}</IonList>}

            {older.error && (
              <div className="olano-older">
                <button type="button" className="olano-older-retry" onClick={() => void loadOlder()}>
                  Couldn't load earlier conversations. Tap to try again.
                </button>
              </div>
            )}
            {rows && rows.length > 0 && (
              <IonInfiniteScroll
                threshold="40%"
                disabled={!hasMore || !!older.error}
                onIonInfinite={(e) => void loadOlder().finally(() => void e.target.complete())}
              >
                <IonInfiniteScrollContent loadingSpinner="dots" />
              </IonInfiniteScroll>
            )}
          </>
        )}
      </IonContent>
    </IonPage>
  );
}
