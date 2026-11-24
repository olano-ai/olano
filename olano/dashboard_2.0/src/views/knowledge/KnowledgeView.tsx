import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { subscribeStream } from "../../core/agentStream";
import { usePersistentState } from "../../core/usePersistentState";
import { usePathSection } from "../../core/usePathSection";
import { viewPath } from "../../core/routeUrls";
import { useT } from "../../core/i18n";
import { BrandWordmark } from "../../ui/BrandMark";
import { Markdown } from "../../ui/Markdown";
import { useToast } from "../../ui/toast";
import { useCurrentAgent } from "../../core/currentAgent";
import { AgentPicker } from "../nav/AgentPicker";
import { STR } from "./strings";
import {
  BUILTIN_STORES,
  fetchKBAvailability,
  hasKBTool,
  isUrlSource,
  parseKBList,
  parseSearchResults,
  pickKBAgent,
  quote,
  runKBTool,
  sourceLabel,
  type KBAvailability,
  type KBListing,
  type KBSource,
  type SearchHit,
  type SearchMode,
  type StreamEvent,
} from "./kb";

type Tab = "search" | "sources" | "add";
const TABS: readonly Tab[] = ["search", "sources", "add"];

// Result of a completed search, kept so we can render either parsed hit cards
// or fall back to the raw markdown reply.
interface SearchState {
  agent: string;
  query: string;
  store: string;
  mode: SearchMode;
  raw: string;
  hits: SearchHit[];
}

// A pending destructive delete awaiting confirmation in the modal.
interface DeleteTarget {
  source: KBSource;
}

export function KnowledgeView() {
  const toast = useToast();
  const t = useT(STR);

  // ── roster + agent selection ──────────────────────────────────────────
  // The app-wide selection (sidebar picker + the inline copy in this view's
  // topbar). Knowledge used to keep its own roster and its own remembered
  // agent, which meant the sidebar could say one agent while this page
  // searched another; one store makes that impossible.
  const { agents, agent } = useCurrentAgent();

  // ── tabs ──────────────────────────────────────────────────────────────
  // /knowledge/<agent>/<tab>; the search tab is the bare page.
  const [tab, setTab] = usePathSection<Tab>({
    allowed: TABS,
    fallback: "search",
    rememberKey: "knowledge.tab",
    pathFor: (section) => (agent ? viewPath("knowledge", { agent, section }) : null),
  });

  // ── search ────────────────────────────────────────────────────────────
  const [query, setQuery] = usePersistentState("knowledge.query", "");
  const [store, setStore] = usePersistentState<string>("knowledge.store", "all");
  const [k, setK] = usePersistentState("knowledge.k", 5);
  const [mode, setMode] = usePersistentState<SearchMode>("knowledge.mode", "similarity");
  const [searching, setSearching] = useState(false);
  const [result, setResult] = useState<SearchState | null>(null);
  const [searchError, setSearchError] = useState<string | null>(null);

  // ── sources (kb_list) ─────────────────────────────────────────────────
  const [listing, setListing] = useState<KBListing | null>(null);
  const [listingLoading, setListingLoading] = useState(false);
  const [listingError, setListingError] = useState<string | null>(null);
  const [listStore, setListStore] = usePersistentState<string>("knowledge.listStore", "all");
  const [deleteTarget, setDeleteTarget] = useState<DeleteTarget | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);

  // ── add tab inputs ────────────────────────────────────────────────────
  const [addUrl, setAddUrl] = usePersistentState("knowledge.addUrl", "");
  const [addFile, setAddFile] = usePersistentState("knowledge.addFile", "");
  const [addCollection, setAddCollection] = usePersistentState(
    "knowledge.addCollection",
    "knowledgebase",
  );
  const [ingesting, setIngesting] = useState(false);

  const [storeName, setStoreName] = usePersistentState("knowledge.storeName", "");
  const [storeDir, setStoreDir] = usePersistentState("knowledge.storeDir", "");
  const [storeGlob, setStoreGlob] = usePersistentState("knowledge.storeGlob", "**/*");
  const [creatingStore, setCreatingStore] = useState(false);

  const [syncing, setSyncing] = useState(false);

  // Track the last loaded store so the SSE listener can refresh the right one.
  const listStoreRef = useRef(listStore);
  listStoreRef.current = listStore;
  const tabRef = useRef(tab);
  tabRef.current = tab;

  const runningCount = useMemo(() => agents.filter((a) => a.running).length, [agents]);

  // ── the agent every KB call is dispatched to ──────────────────────────
  // Hoisted out of the handlers: the render needs it too, to gate the buttons
  // whose tool this agent does not have.
  const target = useMemo(() => pickKBAgent(agents, agent), [agents, agent]);

  // Which kb_* tools that agent can actually run. Null while unknown (probe in
  // flight, or it failed) — hasKBTool() then treats everything as available so
  // a probe we could not complete never locks the page. `probed` is separate
  // because null is both of those states, and the Sources tab must not dispatch
  // a kb_list before we know whether the agent can serve one.
  const [kbTools, setKbTools] = useState<KBAvailability | null>(null);
  const [probed, setProbed] = useState(false);

  useEffect(() => {
    const name = target?.name;
    setKbTools(null);
    setProbed(false);
    if (!name) return;
    let cancelled = false;
    fetchKBAvailability(name)
      .then((av) => {
        if (!cancelled) setKbTools(av);
      })
      .catch(() => {
        // Best effort: a failed probe leaves the page ungated rather than
        // blocking work over a diagnostic we could not run.
      })
      .finally(() => {
        if (!cancelled) setProbed(true);
      });
    return () => {
      cancelled = true;
    };
  }, [target?.name]);

  // Why a dispatch cannot happen, or null when it can. Both reasons are the
  // user's to fix, so neither deserves a generic "failed" message: without this
  // the call still goes out as a chat message and the agent answers it in prose
  // ("I don't have a kb_search tool connected in this chat"), which the parsers
  // below then render as if it were a result.
  const dispatchBlock = useCallback(
    (tool: string, noAgent: string): string | null => {
      if (!target) return noAgent;
      if (!hasKBTool(kbTools, tool)) return t("err.missingTool", { agent: target.name, tool });
      return null;
    },
    [target, kbTools, t],
  );

  const can = useMemo(
    () => ({
      search: hasKBTool(kbTools, "kb_search"),
      list: hasKBTool(kbTools, "kb_list"),
      addFile: hasKBTool(kbTools, "kb_add_file"),
      addUrl: hasKBTool(kbTools, "kb_add_url"),
      createStore: hasKBTool(kbTools, "kb_create_store"),
      sync: hasKBTool(kbTools, "kb_sync"),
      del: hasKBTool(kbTools, "kb_delete"),
      removeFile: hasKBTool(kbTools, "kb_remove_file"),
    }),
    [kbTools],
  );

  // Custom stores discovered from the current listing, merged with built-ins.
  const knownStores = useMemo(() => {
    const set = new Set<string>(BUILTIN_STORES);
    for (const s of listing?.stats ?? []) set.add(s.store);
    for (const s of listing?.sources ?? []) set.add(s.store);
    return Array.from(set);
  }, [listing]);

  // ── kb_list loader (used by the Sources tab + after mutations) ────────
  const loadSources = useCallback(
    async (which: string) => {
      const blocked = dispatchBlock("kb_list", t("err.noAgent.list"));
      if (blocked || !target) {
        setListing(null);
        setListingError(blocked);
        return;
      }
      setListingLoading(true);
      setListingError(null);
      try {
        const res = await runKBTool(target.name, `kb_list("${quote(which)}")`);
        setListing(parseKBList(res.response ?? ""));
      } catch (e) {
        setListingError(e instanceof Error ? e.message : t("err.list"));
        setListing(null);
      } finally {
        setListingLoading(false);
      }
    },
    [target, dispatchBlock, t],
  );

  // Lazy-load sources the first time the Sources tab is opened (and whenever
  // the agent or selected store changes while it is open). Held until the tool
  // probe has settled: `loadSources` changes identity when the probe lands, so
  // firing before then costs a second kb_list — a whole extra agent turn — and
  // the first one would have been dispatched blind anyway.
  useEffect(() => {
    if (tab !== "sources" || !agent || !probed) return;
    void loadSources(listStore);
  }, [tab, agent, listStore, probed, loadSources]);

  // ── live refresh: another tab / agent may mutate the KB. We can't see the
  // tool name over the global stream, but a chat_response is a strong signal a
  // tool just ran, so re-pull the source list when the Sources tab is open.
  useEffect(() => {
    return subscribeStream((frame) => {
      const ev = frame as StreamEvent;
      if (ev.type !== "chat_response") return;
      if (tabRef.current === "sources") {
        void loadSources(listStoreRef.current);
      }
    });
  }, [loadSources]);

  // ── search ────────────────────────────────────────────────────────────
  async function searchKB() {
    const q = query.trim();
    if (!q || searching) return;
    const blocked = dispatchBlock("kb_search", t("err.noAgent.search"));
    if (blocked || !target) {
      setSearchError(blocked);
      setResult(null);
      if (blocked) toast.push(blocked, "error");
      return;
    }
    setSearching(true);
    setSearchError(null);
    setResult(null);
    try {
      const call = `kb_search("${quote(q)}", k=${k}, collection="${quote(store)}", mode="${mode}")`;
      const res = await runKBTool(target.name, call);
      const raw = res.response ?? "";
      setResult({
        agent: res.agent || target.name,
        query: q,
        store,
        mode,
        raw,
        hits: parseSearchResults(raw),
      });
    } catch (e) {
      const msg = e instanceof Error ? e.message : t("err.search");
      setSearchError(msg);
      toast.push(msg, "error");
    } finally {
      setSearching(false);
    }
  }

  function copyResult() {
    if (!result) return;
    navigator.clipboard
      ?.writeText(result.raw)
      .then(() => toast.push(t("toast.copied")))
      .catch(() => toast.push(t("toast.copyFailed"), "error"));
  }

  function downloadResult() {
    if (!result) return;
    const blob = new Blob([result.raw], { type: "text/markdown" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `kb-search-${result.query.slice(0, 24).replace(/\W+/g, "_") || "result"}.md`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  }

  // ── add sources ───────────────────────────────────────────────────────
  async function ingestUrl() {
    const url = addUrl.trim();
    if (!url || ingesting) return;
    const blocked = dispatchBlock("kb_add_url", t("err.noAgent.url"));
    if (blocked || !target) {
      if (blocked) toast.push(blocked, "error");
      return;
    }
    setIngesting(true);
    try {
      const res = await runKBTool(
        target.name,
        `kb_add_url("${quote(url)}", collection="${quote(addCollection)}")`,
      );
      setAddUrl("");
      toast.push(res.response?.trim() || t("toast.urlIngested", { agent: target.name }));
      if (tab === "sources") void loadSources(listStore);
    } catch (e) {
      toast.push(e instanceof Error ? e.message : t("err.ingest"), "error");
    } finally {
      setIngesting(false);
    }
  }

  async function ingestFile() {
    const path = addFile.trim();
    if (!path || ingesting) return;
    const blocked = dispatchBlock("kb_add_file", t("err.noAgent.file"));
    if (blocked || !target) {
      if (blocked) toast.push(blocked, "error");
      return;
    }
    setIngesting(true);
    try {
      const res = await runKBTool(
        target.name,
        `kb_add_file("${quote(path)}", collection="${quote(addCollection)}")`,
      );
      setAddFile("");
      toast.push(res.response?.trim() || t("toast.fileIngested", { agent: target.name }));
      if (tab === "sources") void loadSources(listStore);
    } catch (e) {
      toast.push(e instanceof Error ? e.message : t("err.ingest"), "error");
    } finally {
      setIngesting(false);
    }
  }

  async function createStore() {
    const name = storeName.trim();
    const dir = storeDir.trim();
    if (!name || !dir || creatingStore) return;
    const blocked = dispatchBlock("kb_create_store", t("err.noAgent.store"));
    if (blocked || !target) {
      if (blocked) toast.push(blocked, "error");
      return;
    }
    setCreatingStore(true);
    try {
      const glob = storeGlob.trim() || "**/*";
      const res = await runKBTool(
        target.name,
        `kb_create_store("${quote(name)}", "${quote(dir)}", glob="${quote(glob)}")`,
      );
      setStoreName("");
      setStoreDir("");
      setStoreGlob("**/*");
      toast.push(res.response?.trim() || t("toast.storeCreated", { name }));
      if (tab === "sources") void loadSources(listStore);
    } catch (e) {
      toast.push(e instanceof Error ? e.message : t("err.createStore"), "error");
    } finally {
      setCreatingStore(false);
    }
  }

  async function syncStore() {
    if (syncing) return;
    const blocked = dispatchBlock("kb_sync", t("err.noAgent.sync"));
    if (blocked || !target) {
      if (blocked) toast.push(blocked, "error");
      return;
    }
    setSyncing(true);
    try {
      const res = await runKBTool(
        target.name,
        `kb_sync(collection="${quote(addCollection)}")`,
      );
      toast.push(res.response?.trim() || t("toast.synced"));
      if (tab === "sources") void loadSources(listStore);
    } catch (e) {
      toast.push(e instanceof Error ? e.message : t("err.sync"), "error");
    } finally {
      setSyncing(false);
    }
  }

  // ── delete a source (modal-confirmed) ─────────────────────────────────
  async function confirmDelete(removeFromDisk: boolean) {
    const dt = deleteTarget;
    if (!dt) return;
    const blocked = dispatchBlock(
      removeFromDisk ? "kb_remove_file" : "kb_delete",
      t("err.noAgent.delete"),
    );
    if (blocked || !target) {
      if (blocked) toast.push(blocked, "error");
      setDeleteTarget(null);
      return;
    }
    const src = dt.source;
    setDeletingId(src.id);
    setDeleteTarget(null);
    try {
      const call = removeFromDisk
        ? `kb_remove_file("${quote(src.id)}", collection="${quote(src.store)}", delete_from_disk=True)`
        : `kb_delete("${quote(src.id)}", collection="${quote(src.store)}")`;
      const res = await runKBTool(target.name, call);
      toast.push(res.response?.trim() || t("toast.sourceRemoved"));
      await loadSources(listStore);
    } catch (e) {
      toast.push(e instanceof Error ? e.message : t("err.delete"), "error");
    } finally {
      setDeletingId(null);
    }
  }

  // ── render ────────────────────────────────────────────────────────────
  return (
    <>
      <div className="topbar">
        <BrandWordmark />
        <div className="row">
          <span className="muted" style={{ fontSize: 12 }}>
            {t("running", { n: String(runningCount) })}
          </span>
          <AgentPicker variant="inline" emptyLabel={t("agents.none")} />
        </div>
      </div>

      <div className="page">
        <h1>{t("page.title")}</h1>
        <p className="muted">{t("page.subtitle")}</p>

        {/* kb_* calls go to a RUNNING agent (pickKBAgent), so a stopped
            selection is answered by another agent's knowledge base. Say so
            rather than letting the results look like they came from the one
            named in the picker. */}
        {target && agent && target.name !== agent && (
          <p className="muted" data-testid="kb-reroute-notice" style={{ fontSize: 13 }}>
            {t("reroute.notice", { selected: agent, target: target.name })}
          </p>
        )}

        {/* The page dispatches kb_* tool calls into the selected agent's chat.
            An agent without those tools answers the call in prose instead of
            running it, so say so here rather than rendering the prose as a
            result. Only shown once the probe has actually answered. */}
        {kbTools && kbTools.missing.length > 0 && (
          <MissingToolsNotice agent={kbTools.agent} missing={kbTools.missing} />
        )}

        {/* ── tab bar ─────────────────────────────────────────────────── */}
        <div className="row" style={{ marginTop: 16, gap: 6 }}>
          {(["search", "sources", "add"] as Tab[]).map((tk) => {
            const sourceCount =
              tk === "sources" && listing && !listing.empty ? listing.sources.length : null;
            return (
              <button
                key={tk}
                className={`btn${tab === tk ? " primary" : ""}`}
                onClick={() => setTab(tk)}
              >
                {t(`tab.${tk}`)}
                {sourceCount != null && (
                  <span
                    className="badge"
                    style={{
                      marginLeft: 2,
                      background: tab === tk ? "color-mix(in srgb, var(--accent-fg) 18%, transparent)" : "var(--surface-3)",
                      borderColor: "transparent",
                      color: tab === tk ? "var(--accent-fg)" : "var(--text-2)",
                    }}
                  >
                    {sourceCount}
                  </span>
                )}
              </button>
            );
          })}
        </div>

        {tab === "search" && (
          <SearchTab
            query={query}
            setQuery={setQuery}
            store={store}
            setStore={setStore}
            knownStores={knownStores}
            k={k}
            setK={setK}
            mode={mode}
            setMode={setMode}
            searching={searching}
            result={result}
            searchError={searchError}
            onSearch={() => void searchKB()}
            onCopy={copyResult}
            onDownload={downloadResult}
            canSearch={can.search}
          />
        )}

        {tab === "sources" && (
          <SourcesTab
            knownStores={knownStores}
            listStore={listStore}
            setListStore={setListStore}
            listing={listing}
            loading={listingLoading}
            error={listingError}
            deletingId={deletingId}
            onRefresh={() => void loadSources(listStore)}
            onDelete={(source) => setDeleteTarget({ source })}
            canList={can.list}
            canDelete={can.del || can.removeFile}
          />
        )}

        {tab === "add" && (
          <AddTab
            knownStores={knownStores}
            addUrl={addUrl}
            setAddUrl={setAddUrl}
            addFile={addFile}
            setAddFile={setAddFile}
            addCollection={addCollection}
            setAddCollection={setAddCollection}
            ingesting={ingesting}
            onIngestUrl={() => void ingestUrl()}
            onIngestFile={() => void ingestFile()}
            storeName={storeName}
            setStoreName={setStoreName}
            storeDir={storeDir}
            setStoreDir={setStoreDir}
            storeGlob={storeGlob}
            setStoreGlob={setStoreGlob}
            creatingStore={creatingStore}
            onCreateStore={() => void createStore()}
            syncing={syncing}
            onSync={() => void syncStore()}
            canUrl={can.addUrl}
            canFile={can.addFile}
            canStore={can.createStore}
            canSync={can.sync}
          />
        )}
      </div>

      {deleteTarget && (
        <DeleteModal
          source={deleteTarget.source}
          onCancel={() => setDeleteTarget(null)}
          onConfirm={confirmDelete}
          canDeindex={can.del}
          canRemoveFile={can.removeFile}
        />
      )}
    </>
  );
}

// ─────────────────────────────────────────────────────────────────────────
//  Missing kb_* tools notice
// ─────────────────────────────────────────────────────────────────────────
// The page has no KB backend: it sends `kb_search("…")` to the selected agent
// as a chat message and renders the reply. An agent without the tool answers in
// prose, and the reply parsers happily render that prose under a "Results"
// header — a wrong answer dressed as a right one. This names the actual cause
// and the three places it can be fixed.
function MissingToolsNotice({ agent, missing }: { agent: string; missing: string[] }) {
  const t = useT(STR);
  return (
    <section
      className="card"
      data-testid="kb-missing-tools"
      style={{ padding: 16, marginTop: 14, borderLeft: "3px solid var(--warn)" }}
    >
      <strong style={{ fontSize: 14, color: "var(--warn)" }}>{t("missing.title", { agent })}</strong>
      <p className="muted" style={{ margin: "8px 0 0", fontSize: 12.5, lineHeight: 1.6 }}>
        {t("missing.why")}
      </p>
      <div className="row" style={{ gap: 6, flexWrap: "wrap", marginTop: 10 }}>
        <span className="muted" style={{ fontSize: 12 }}>
          {t("missing.list")}
        </span>
        {missing.map((name) => (
          <span key={name} className="badge" style={{ fontFamily: "var(--font-mono)" }}>
            {name}
          </span>
        ))}
      </div>
      <ol
        style={{
          margin: "12px 0 0",
          paddingLeft: 18,
          fontSize: 12.5,
          lineHeight: 1.65,
          color: "var(--text-2)",
        }}
      >
        <li>{t("missing.fix.add", { agent })}</li>
        <li>{t("missing.fix.global")}</li>
        <li>{t("missing.fix.other")}</li>
      </ol>
    </section>
  );
}

// ─────────────────────────────────────────────────────────────────────────
//  Search tab
// ─────────────────────────────────────────────────────────────────────────
function SearchTab(props: {
  query: string;
  setQuery: (v: string) => void;
  store: string;
  setStore: (v: string) => void;
  knownStores: string[];
  k: number;
  setK: (v: number) => void;
  mode: SearchMode;
  setMode: (v: SearchMode) => void;
  searching: boolean;
  result: SearchState | null;
  searchError: string | null;
  onSearch: () => void;
  onCopy: () => void;
  onDownload: () => void;
  // False when the routed agent has no kb_search tool: the dispatch would go
  // out as an ordinary chat message and come back as prose.
  canSearch: boolean;
}) {
  const {
    query,
    setQuery,
    store,
    setStore,
    knownStores,
    k,
    setK,
    mode,
    setMode,
    searching,
    result,
    searchError,
    onSearch,
    onCopy,
    onDownload,
    canSearch,
  } = props;
  const t = useT(STR);

  return (
    <>
      <section className="card" style={{ padding: 18, marginTop: 14 }}>
        <h2 style={{ marginTop: 0, fontSize: 15 }}>{t("search.heading")}</h2>
        <p className="muted" style={{ marginTop: -4, fontSize: 12 }}>
          {t("search.subtitle")}
        </p>
        <div className="row" style={{ marginTop: 10 }}>
          <input
            className="input"
            placeholder={t("search.placeholder")}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                onSearch();
              }
            }}
          />
          <button
            className="btn primary"
            data-testid="kb-search-submit"
            disabled={searching || !query.trim() || !canSearch}
            title={canSearch ? undefined : t("disabled.tool", { tool: "kb_search" })}
            onClick={onSearch}
          >
            {searching ? "…" : t("search.btn")}
          </button>
        </div>

        <div className="row" style={{ marginTop: 12, gap: 16, flexWrap: "wrap" }}>
          <label className="row" style={{ gap: 6 }}>
            <span className="muted" style={{ fontSize: 12 }}>
              {t("search.field.store")}
            </span>
            <select
              className="input"
              style={{ width: 180 }}
              value={store}
              onChange={(e) => setStore(e.target.value)}
            >
              {knownStores.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </label>

          <label className="row" style={{ gap: 6 }}>
            <span className="muted" style={{ fontSize: 12 }}>
              {t("search.field.k")}
            </span>
            <input
              type="range"
              min={1}
              max={20}
              value={k}
              onChange={(e) => setK(Number(e.target.value))}
              style={{ width: 120 }}
            />
            <span style={{ fontFamily: "var(--font-mono)", fontSize: 12, width: 18 }}>{k}</span>
          </label>

          <label className="row" style={{ gap: 6 }}>
            <span className="muted" style={{ fontSize: 12 }}>
              {t("search.field.mode")}
            </span>
            <select
              className="input"
              style={{ width: 150 }}
              value={mode}
              onChange={(e) => setMode(e.target.value as SearchMode)}
            >
              <option value="similarity">{t("search.mode.similarity")}</option>
              <option value="mmr">{t("search.mode.mmr")}</option>
            </select>
          </label>
        </div>
      </section>

      <div style={{ marginTop: 14 }}>
        {searching ? (
          <section className="card" style={{ padding: 18 }}>
            <div className="row">
              <div className="spinner" />
              <span className="muted">{t("search.searching")}</span>
            </div>
          </section>
        ) : searchError ? (
          <section className="card" style={{ padding: 18 }}>
            <p className="error-text" style={{ margin: 0 }}>
              {searchError}
            </p>
          </section>
        ) : result ? (
          <SearchResults result={result} onCopy={onCopy} onDownload={onDownload} />
        ) : (
          <section className="card" style={{ padding: 24, textAlign: "center" }}>
            <p className="muted" style={{ margin: 0 }}>
              {canSearch ? t("search.prompt") : t("search.prompt.blocked")}
            </p>
          </section>
        )}
      </div>
    </>
  );
}

function SearchResults({
  result,
  onCopy,
  onDownload,
}: {
  result: SearchState;
  onCopy: () => void;
  onDownload: () => void;
}) {
  const t = useT(STR);
  const noResults = /^no results found/i.test(result.raw.trim());
  return (
    <section className="card" style={{ padding: 18 }}>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
          <strong>{t("results.title")}</strong>
          {result.hits.length > 0 && !noResults && (
            <span className="badge">
              {result.hits.length} {result.hits.length === 1 ? t("results.hit") : t("results.hits")}
            </span>
          )}
          <span className="badge">{result.store}</span>
          <span className="badge">{result.mode}</span>
          <span className="badge admin">{t("results.via", { agent: result.agent })}</span>
        </div>
        <div className="row" style={{ gap: 6 }}>
          <button className="btn" onClick={onCopy} disabled={!result.raw.trim()}>
            {t("results.copy")}
          </button>
          <button className="btn" onClick={onDownload} disabled={!result.raw.trim()}>
            {t("results.download")}
          </button>
        </div>
      </div>

      {noResults ? (
        <p className="muted" style={{ marginTop: 14 }}>
          {t("results.none", { query: result.query })}
        </p>
      ) : result.hits.length > 0 ? (
        <div style={{ marginTop: 12, display: "flex", flexDirection: "column", gap: 10 }}>
          {result.hits.map((hit) => (
            <HitCard key={hit.index} hit={hit} />
          ))}
        </div>
      ) : (
        <div style={{ marginTop: 12 }}>
          <Markdown text={result.raw} />
        </div>
      )}
    </section>
  );
}

function HitCard({ hit }: { hit: SearchHit }) {
  const top = hit.index === 1;
  return (
    <div
      style={{
        borderRadius: "var(--r-sm)",
        padding: 12,
        background: top ? "var(--accent-gradient-soft)" : "var(--surface-2)",
      }}
    >
      <div className="row" style={{ justifyContent: "space-between", gap: 8 }}>
        <div className="row" style={{ gap: 6 }}>
          <span className={top ? "badge admin" : "badge"}>#{hit.index}</span>
          <span className="badge">{hit.store}</span>
          <span
            className="badge"
            style={{ fontFamily: "var(--font-mono)" }}
          >
            {hit.score}
          </span>
        </div>
        <span
          className="muted"
          style={{
            fontSize: 11,
            fontFamily: "var(--font-mono)",
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
            maxWidth: 360,
          }}
          title={hit.source}
        >
          {/* The file's name, as the Sources tab shows it: the full path was cut
              off mid-way and is still in the tooltip. */}
          {sourceLabel(hit.source)}
        </span>
      </div>
      <div
        style={{
          marginTop: 8,
          fontSize: 13,
          lineHeight: 1.55,
          whiteSpace: "pre-wrap",
          wordWrap: "break-word",
        }}
      >
        {hit.content}
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────
//  Sources tab (kb_list)
// ─────────────────────────────────────────────────────────────────────────
function SourcesTab(props: {
  knownStores: string[];
  listStore: string;
  setListStore: (v: string) => void;
  listing: KBListing | null;
  loading: boolean;
  error: string | null;
  deletingId: string | null;
  onRefresh: () => void;
  onDelete: (source: KBSource) => void;
  // Whether the routed agent can run kb_list / the removal tools at all.
  canList: boolean;
  canDelete: boolean;
}) {
  const {
    knownStores,
    listStore,
    setListStore,
    listing,
    loading,
    error,
    deletingId,
    onRefresh,
    onDelete,
    canList,
    canDelete,
  } = props;
  const t = useT(STR);

  return (
    <section className="card" style={{ padding: 18, marginTop: 14 }}>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h2 style={{ margin: 0, fontSize: 15 }}>{t("sources.heading")}</h2>
        <div className="row" style={{ gap: 8 }}>
          <select
            className="input"
            style={{ width: 180 }}
            value={listStore}
            onChange={(e) => setListStore(e.target.value)}
          >
            {knownStores.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
          <button
            className="btn"
            onClick={onRefresh}
            disabled={loading || !canList}
            title={canList ? undefined : t("disabled.tool", { tool: "kb_list" })}
          >
            {loading ? "…" : t("sources.refresh")}
          </button>
        </div>
      </div>

      {/* per-store stats */}
      {listing && listing.stats.length > 0 && (
        <div className="row" style={{ marginTop: 12, gap: 8, flexWrap: "wrap" }}>
          {listing.stats.map((s) => (
            <span key={s.store} className="badge">
              {t("sources.stat", {
                store: s.store,
                sources: String(s.sources),
                chunks: String(s.chunks),
              })}
            </span>
          ))}
        </div>
      )}

      <div style={{ marginTop: 14 }}>
        {loading ? (
          <div className="row">
            <div className="spinner" />
            <span className="muted">{t("sources.loading")}</span>
          </div>
        ) : error ? (
          <p className="error-text" style={{ margin: 0 }}>
            {error}
          </p>
        ) : !listing || listing.empty || listing.sources.length === 0 ? (
          <div style={{ textAlign: "center", color: "var(--text-3)", padding: "20px 0" }}>
            <div style={{ fontWeight: 600, color: "var(--text-2)" }}>{t("sources.empty.title")}</div>
            <div style={{ fontSize: 13, marginTop: 6 }}>{t("sources.empty.body")}</div>
          </div>
        ) : (
          <table className="table" style={{ marginTop: 4 }}>
            <thead>
              <tr>
                <th>{t("col.source")}</th>
                <th>{t("col.store")}</th>
                <th>{t("col.type")}</th>
                <th style={{ width: 90, textAlign: "right" }}>{t("col.actions")}</th>
              </tr>
            </thead>
            <tbody>
              {listing.sources.map((src) => {
                const busy = deletingId === src.id;
                return (
                  <tr key={`${src.store}::${src.id}`}>
                    <td title={src.id} style={{ maxWidth: 420 }}>
                      <div
                        style={{
                          overflow: "hidden",
                          textOverflow: "ellipsis",
                          whiteSpace: "nowrap",
                        }}
                      >
                        {sourceLabel(src.id)}
                      </div>
                      {sourceLabel(src.id) !== src.id && (
                        <div
                          className="muted"
                          style={{
                            fontSize: 11,
                            fontFamily: "var(--font-mono)",
                            overflow: "hidden",
                            textOverflow: "ellipsis",
                            whiteSpace: "nowrap",
                          }}
                        >
                          {src.id}
                        </div>
                      )}
                    </td>
                    <td>
                      <span className="badge">{src.store}</span>
                    </td>
                    <td>
                      <span className="badge">{isUrlSource(src.id) ? t("type.url") : t("type.file")}</span>
                    </td>
                    <td style={{ textAlign: "right" }}>
                      <button
                        className="btn danger"
                        disabled={busy || !canDelete}
                        title={canDelete ? undefined : t("disabled.tool", { tool: "kb_delete" })}
                        onClick={() => onDelete(src)}
                      >
                        {busy ? "…" : t("sources.remove")}
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </section>
  );
}

// ─────────────────────────────────────────────────────────────────────────
//  Add tab
// ─────────────────────────────────────────────────────────────────────────
function AddTab(props: {
  knownStores: string[];
  addUrl: string;
  setAddUrl: (v: string) => void;
  addFile: string;
  setAddFile: (v: string) => void;
  addCollection: string;
  setAddCollection: (v: string) => void;
  ingesting: boolean;
  onIngestUrl: () => void;
  onIngestFile: () => void;
  storeName: string;
  setStoreName: (v: string) => void;
  storeDir: string;
  setStoreDir: (v: string) => void;
  storeGlob: string;
  setStoreGlob: (v: string) => void;
  creatingStore: boolean;
  onCreateStore: () => void;
  syncing: boolean;
  onSync: () => void;
  // One flag per ingest tool — an agent can have kb_add_file without kb_sync.
  canUrl: boolean;
  canFile: boolean;
  canStore: boolean;
  canSync: boolean;
}) {
  const {
    knownStores,
    addUrl,
    setAddUrl,
    addFile,
    setAddFile,
    addCollection,
    setAddCollection,
    ingesting,
    onIngestUrl,
    onIngestFile,
    storeName,
    setStoreName,
    storeDir,
    setStoreDir,
    storeGlob,
    setStoreGlob,
    creatingStore,
    onCreateStore,
    syncing,
    onSync,
    canUrl,
    canFile,
    canStore,
    canSync,
  } = props;
  const t = useT(STR);

  // Built-in stores minus "all" (you can't add to the union pseudo-store).
  const targetStores = knownStores.filter((s) => s !== "all");

  return (
    <div
      style={{
        display: "grid",
        gap: 14,
        marginTop: 14,
        gridTemplateColumns: "repeat(auto-fit,minmax(340px,1fr))",
      }}
    >
      {/* target store selector applies to URL / file / sync */}
      <section className="card" style={{ padding: 18 }}>
        <h2 style={{ marginTop: 0, fontSize: 15 }}>{t("add.target.heading")}</h2>
        <p className="muted" style={{ marginTop: -4, fontSize: 12 }}>
          {t("add.target.subtitle")}
        </p>
        <select
          className="input"
          style={{ marginTop: 10 }}
          value={addCollection}
          onChange={(e) => setAddCollection(e.target.value)}
        >
          {targetStores.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
        <div className="row" style={{ marginTop: 12 }}>
          <button
            className="btn"
            onClick={onSync}
            disabled={syncing || !canSync}
            title={canSync ? undefined : t("disabled.tool", { tool: "kb_sync" })}
          >
            {syncing ? t("add.sync.busy") : t("add.sync.btn")}
          </button>
          <span className="muted" style={{ fontSize: 12 }}>
            {t("add.sync.hint")}
          </span>
        </div>
      </section>

      {/* ingest URL */}
      <section className="card" style={{ padding: 18 }}>
        <h2 style={{ marginTop: 0, fontSize: 15 }}>{t("add.url.heading")}</h2>
        <div className="row" style={{ marginTop: 10 }}>
          <input
            className="input"
            placeholder={t("add.url.placeholder")}
            value={addUrl}
            onChange={(e) => setAddUrl(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                onIngestUrl();
              }
            }}
          />
          <button
            className="btn"
            disabled={ingesting || !addUrl.trim() || !canUrl}
            title={canUrl ? undefined : t("disabled.tool", { tool: "kb_add_url" })}
            onClick={onIngestUrl}
          >
            {ingesting ? "…" : t("add.ingest")}
          </button>
        </div>
        <p className="muted" style={{ marginTop: 10, fontSize: 12 }}>
          {t("add.url.hint")}
        </p>
      </section>

      {/* ingest file */}
      <section className="card" style={{ padding: 18 }}>
        <h2 style={{ marginTop: 0, fontSize: 15 }}>{t("add.file.heading")}</h2>
        <div className="row" style={{ marginTop: 10 }}>
          <input
            className="input"
            placeholder={t("add.file.placeholder")}
            value={addFile}
            onChange={(e) => setAddFile(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                onIngestFile();
              }
            }}
          />
          <button
            className="btn"
            disabled={ingesting || !addFile.trim() || !canFile}
            title={canFile ? undefined : t("disabled.tool", { tool: "kb_add_file" })}
            onClick={onIngestFile}
          >
            {ingesting ? "…" : t("add.ingest")}
          </button>
        </div>
        <p className="muted" style={{ marginTop: 10, fontSize: 12 }}>
          {t("add.file.hint")}
        </p>
      </section>

      {/* create store */}
      <section className="card" style={{ padding: 18 }}>
        <h2 style={{ marginTop: 0, fontSize: 15 }}>{t("add.store.heading")}</h2>
        <div style={{ display: "flex", flexDirection: "column", gap: 8, marginTop: 10 }}>
          <input
            className="input"
            placeholder={t("add.store.name")}
            value={storeName}
            onChange={(e) => setStoreName(e.target.value)}
          />
          <input
            className="input"
            placeholder={t("add.store.dir")}
            value={storeDir}
            onChange={(e) => setStoreDir(e.target.value)}
          />
          <input
            className="input"
            placeholder={t("add.store.glob")}
            value={storeGlob}
            onChange={(e) => setStoreGlob(e.target.value)}
          />
          <button
            className="btn primary"
            disabled={creatingStore || !storeName.trim() || !storeDir.trim() || !canStore}
            title={canStore ? undefined : t("disabled.tool", { tool: "kb_create_store" })}
            onClick={onCreateStore}
          >
            {creatingStore ? t("add.store.creating") : t("add.store.create")}
          </button>
        </div>
      </section>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────
//  Delete confirmation modal (file sources can also be removed from disk)
// ─────────────────────────────────────────────────────────────────────────
function DeleteModal({
  source,
  onCancel,
  onConfirm,
  canDeindex,
  canRemoveFile,
}: {
  source: KBSource;
  onCancel: () => void;
  onConfirm: (removeFromDisk: boolean) => void;
  // De-indexing and deleting from disk are two different tools, so an agent
  // can hold one without the other.
  canDeindex: boolean;
  canRemoveFile: boolean;
}) {
  const t = useT(STR);
  const isFile = !isUrlSource(source.id);
  const [removeFromDisk, setRemoveFromDisk] = useState(false);

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onCancel();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onCancel]);

  return (
    <div className="modal-overlay" onClick={onCancel}>
      <div
        className="card"
        onClick={(e) => e.stopPropagation()}
        style={{ width: 460, maxWidth: "100%", padding: 20, boxShadow: "var(--shadow-lg)" }}
      >
        <strong style={{ fontSize: 16 }}>{t("modal.title")}</strong>
        <p className="muted" style={{ marginTop: 8, fontSize: 13 }}>
          {t("modal.deindex")} <span className="badge">{source.store}</span>:
        </p>
        <div
          style={{
            fontFamily: "var(--font-mono)",
            fontSize: 12,
            background: "var(--code-bg)",
            border: "1px solid var(--code-border)",
            borderRadius: "var(--r-sm)",
            padding: 10,
            wordBreak: "break-all",
          }}
        >
          {source.id}
        </div>

        {isFile && (
          <label className="row" style={{ marginTop: 14, gap: 8, cursor: "pointer" }}>
            <input
              type="checkbox"
              checked={removeFromDisk}
              disabled={!canRemoveFile}
              onChange={(e) => setRemoveFromDisk(e.target.checked)}
            />
            <span style={{ fontSize: 13 }}>{t("modal.disk")}</span>
          </label>
        )}
        {!isFile && (
          <p className="muted" style={{ marginTop: 14, fontSize: 12 }}>
            {t("modal.urlOnly")}
          </p>
        )}

        <div className="row" style={{ justifyContent: "flex-end", marginTop: 18, gap: 8 }}>
          <button className="btn" onClick={onCancel}>
            {t("modal.cancel")}
          </button>
          <button
            className="btn danger"
            disabled={isFile && removeFromDisk ? !canRemoveFile : !canDeindex}
            onClick={() => onConfirm(isFile && removeFromDisk)}
          >
            {isFile && removeFromDisk ? t("modal.removeDelete") : t("modal.removeIndex")}
          </button>
        </div>
      </div>
    </div>
  );
}
