import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type ReactNode,
} from "react";
import { useNavigate } from "react-router-dom";
import { agentDetailPath } from "../../core/routeUrls";
import {
  AlertTriangle,
  Check,
  ExternalLink,
  Play,
  RotateCw,
  Wrench,
  X,
} from "lucide-react";
import { api } from "../../core/api";
import { useT } from "../../core/i18n";
import { usePersistentState } from "../../core/usePersistentState";
import { Markdown } from "../../ui/Markdown";
import { useToast } from "../../ui/toast";
import { Toggle } from "../../ui/Toggle";
import { STR } from "./strings";

// Translator function type — passed into non-component string-builders.
type T = (key: string, vars?: Record<string, string>) => string;

// ── API shapes ─────────────────────────────────────────────────────────
interface EnvVarSpec {
  name: string;
  label?: string;
  doc_url?: string;
  guidance?: string;
  optional?: boolean;
  in_env?: boolean;
  in_vault?: boolean;
  /** Supplied live by a connected OAuth connector (resolves above vault/env). */
  in_connector?: boolean;
}

interface Requirements {
  env_vars?: EnvVarSpec[];
  packages?: string[];
  npm_packages?: string[];
  setup_commands?: string[];
  cli?: string[];
  packages_status?: Record<string, boolean>;
  npm_packages_status?: Record<string, boolean>;
  cli_status?: Record<string, boolean>;
}

interface Tool {
  name: string;
  description: string;
  category: string;
  bundles: string[];
  tags?: string[];
  disabled: boolean;
  requirements: Requirements;
}

interface Bundle {
  name: string;
  tools: string[];
  requirements: Requirements;
}

interface EnvStatusEntry {
  in_env: boolean;
  in_vault: boolean;
  in_connector?: boolean;
}

interface InstallStatus {
  python: Record<string, boolean>;
  npm: Record<string, boolean>;
  cli: Record<string, boolean>;
}

interface CatalogResponse {
  tools: Tool[];
  bundles: Bundle[];
  env_status: Record<string, EnvStatusEntry>;
  install_status: InstallStatus;
  disabled: string[];
}

interface ToolUsageEntry {
  agent: string;
  subagent: string | null;
}

interface UsageResponse {
  usage: Record<string, ToolUsageEntry[]>;
}

interface DisabledResponse {
  disabled: string[];
}

interface BulkDisabledResponse {
  disabled: string[];
  applied: string[];
}

// ── scope (sidebar selection) ──────────────────────────────────────────
type Scope =
  | { kind: "all" }
  | { kind: "status"; value: "in_use" | "not_in_use" | "missing_keys" | "package_missing" | "disabled" }
  | { kind: "bundle"; value: string }
  | { kind: "category"; value: string };

type SortKey = "name" | "category" | "usage" | "status";

const USAGE_LIMIT = 8;
const MAX_BUNDLE_TAGS = 3;

// ── install dialog kinds ───────────────────────────────────────────────
type InstallKind = "python" | "npm" | "setup" | "cli";

interface InstallOption {
  label: string;
  cmd: string;
  runnable: boolean;
  note?: string;
}

// ── secret dialog spec (richest env spec for a name) ───────────────────
interface SecretSpec {
  name: string;
  doc_url?: string;
  guidance?: string;
  in_env?: boolean;
  in_vault?: boolean;
  in_connector?: boolean;
}

// ── per-tool derived facts ─────────────────────────────────────────────
interface ToolFacts {
  inUse: boolean;
  usageCount: number;
  missingKeys: boolean;
  packageMissing: boolean;
  disabled: boolean;
}

function envVarSatisfied(spec: EnvVarSpec): boolean {
  if (spec.optional) return true;
  // A connected OAuth connector supplies the value live through get_credential,
  // above both the vault and the environment — so it counts as satisfied even
  // though the name appears in neither store.
  return Boolean(spec.in_env) || Boolean(spec.in_vault) || Boolean(spec.in_connector);
}

function requirementsMissingKeys(req: Requirements): boolean {
  return (req.env_vars ?? []).some((spec) => !envVarSatisfied(spec));
}

function requirementsPackageMissing(req: Requirements): boolean {
  const checkStatus = (
    items: string[] | undefined,
    status: Record<string, boolean> | undefined,
  ): boolean => {
    if (!items || !status) return false;
    return items.some((name) => status[name] === false);
  };
  return (
    checkStatus(req.packages, req.packages_status) ||
    checkStatus(req.npm_packages, req.npm_packages_status) ||
    checkStatus(req.cli, req.cli_status)
  );
}

function computeFacts(tool: Tool, usage: ToolUsageEntry[]): ToolFacts {
  const usageCount = usage.length;
  return {
    inUse: usageCount > 0,
    usageCount,
    missingKeys: requirementsMissingKeys(tool.requirements),
    packageMissing: requirementsPackageMissing(tool.requirements),
    disabled: tool.disabled,
  };
}

function statusRank(facts: ToolFacts): number {
  // For "status" sort: disabled first, then missing keys, then package missing,
  // then in-use, then ready/unused last.
  if (facts.disabled) return 0;
  if (facts.missingKeys) return 1;
  if (facts.packageMissing) return 2;
  if (facts.inUse) return 3;
  return 4;
}

interface StatusLabel {
  text: string;
  color: string;
}

function statusLabel(facts: ToolFacts, t: T): StatusLabel {
  if (facts.disabled) return { text: t("status.disabled"), color: "var(--warn)" };
  if (facts.missingKeys) return { text: t("status.missingKeys"), color: "var(--warn)" };
  if (facts.packageMissing) return { text: t("status.installNeeded"), color: "var(--warn)" };
  return { text: t("status.ready"), color: "var(--success)" };
}

// ── install option builder (mirrors the original _installOptions) ──────
function detectOS(): "macos" | "windows" | "linux" {
  const nav = typeof navigator !== "undefined" ? navigator : undefined;
  const p = (nav?.platform ?? "").toLowerCase();
  const ua = (nav?.userAgent ?? "").toLowerCase();
  if (p.startsWith("mac") || ua.includes("mac os")) return "macos";
  if (p.startsWith("win") || ua.includes("windows")) return "windows";
  return "linux";
}

function installOptions(kind: InstallKind, value: string, t: T): InstallOption[] {
  const v = value;
  if (kind === "python") {
    return [
      {
        label: t("opt.uv"),
        cmd: `uv add ${v}`,
        runnable: true,
        note: t("opt.uv.note"),
      },
      {
        label: t("opt.pip"),
        cmd: `pip install ${v}`,
        runnable: true,
        note: t("opt.pip.note"),
      },
      {
        label: t("opt.pythonPip"),
        cmd: `python -m pip install ${v}`,
        runnable: true,
        note: t("opt.pythonPip.note"),
      },
    ];
  }
  if (kind === "npm") {
    return [
      { label: t("opt.npm"), cmd: `npm install ${v}`, runnable: true },
      { label: t("opt.pnpm"), cmd: `pnpm add ${v}`, runnable: true },
      { label: t("opt.yarn"), cmd: `yarn add ${v}`, runnable: true },
    ];
  }
  if (kind === "setup") {
    return [
      {
        label: t("opt.runAsIs"),
        cmd: v,
        runnable: true,
        note: t("opt.runAsIs.note"),
      },
    ];
  }
  // cli — platform-specific. Only macOS brew is runnable from the daemon.
  const os = detectOS();
  const opts: InstallOption[] = [];
  if (os === "macos") {
    opts.push({
      label: t("opt.brew"),
      cmd: `brew install ${v}`,
      runnable: true,
      note: t("opt.brew.note"),
    });
  } else {
    opts.push({ label: t("opt.brew"), cmd: `brew install ${v}`, runnable: false });
  }
  opts.push(
    {
      label: t("opt.apt"),
      cmd: `sudo apt-get install -y ${v}`,
      runnable: false,
      note: t("opt.apt.note"),
    },
    { label: t("opt.dnf"), cmd: `sudo dnf install -y ${v}`, runnable: false },
    { label: t("opt.pacman"), cmd: `sudo pacman -S --noconfirm ${v}`, runnable: false },
    { label: t("opt.winget"), cmd: `winget install ${v}`, runnable: false },
  );
  return opts;
}

const INSTALL_TITLE_KEYS: Record<InstallKind, string> = {
  python: "install.title.python",
  npm: "install.title.npm",
  setup: "install.title.setup",
  cli: "install.title.cli",
};

const INSTALL_SUB_KEYS: Record<InstallKind, string> = {
  python: "install.sub.python",
  npm: "install.sub.npm",
  setup: "install.sub.setup",
  cli: "install.sub.cli",
};

// ── shell-stream event shape (from /api/shell/stream) ──────────────────
interface ShellEvent {
  type: "started" | "stdout" | "stderr" | "error" | "done";
  data?: string;
  message?: string;
  exit_code?: number;
  log_path?: string | null;
  command?: string;
}

interface OutputLine {
  text: string;
  kind: "stdout" | "stderr" | "error";
}

// ════════════════════════════════════════════════════════════════════════
//  component
// ════════════════════════════════════════════════════════════════════════
export function ToolsView() {
  const toast = useToast();
  const navigate = useNavigate();
  const t = useT(STR);

  const [catalog, setCatalog] = useState<CatalogResponse | null>(null);
  const [usage, setUsage] = useState<Record<string, ToolUsageEntry[]>>({});
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  const [search, setSearch] = usePersistentState("tools.search", "");
  const [sort, setSort] = usePersistentState<SortKey>("tools.sort", "name");
  const [scope, setScope] = usePersistentState<Scope>("tools.scope", { kind: "all" });
  const [busy, setBusy] = useState<Set<string>>(new Set());

  // overlays
  const [secretSpec, setSecretSpec] = useState<SecretSpec | null>(null);
  const [installDialog, setInstallDialog] = useState<{ kind: InstallKind; value: string } | null>(
    null,
  );

  const ensureCatalog = useCallback(
    async (force: boolean) => {
      if (force) setRefreshing(true);
      try {
        const [cat, use] = await Promise.all([
          api.get<CatalogResponse>("/api/catalog/tools"),
          api.get<UsageResponse>("/api/catalog/tools/usage"),
        ]);
        setCatalog(cat);
        setUsage(use.usage ?? {});
        setError(null);
      } catch (e) {
        const msg = e instanceof Error ? e.message : t("load.error");
        setError(msg);
        if (force) toast.push(msg, "error");
      } finally {
        if (force) setRefreshing(false);
      }
    },
    [toast, t],
  );

  useEffect(() => {
    void ensureCatalog(false);
  }, [ensureCatalog]);

  // ── derived facts per tool ───────────────────────────────────────────
  const factsByName = useMemo(() => {
    const map = new Map<string, ToolFacts>();
    if (!catalog) return map;
    for (const tool of catalog.tools) {
      map.set(tool.name, computeFacts(tool, usage[tool.name] ?? []));
    }
    return map;
  }, [catalog, usage]);

  // ── aggregate counts for the sidebar / stats bar ─────────────────────
  const counts = useMemo(() => {
    const c = {
      all: 0,
      inUse: 0,
      notInUse: 0,
      missingKeys: 0,
      packageMissing: 0,
      disabled: 0,
    };
    if (!catalog) return c;
    for (const tool of catalog.tools) {
      const f = factsByName.get(tool.name);
      if (!f) continue;
      c.all += 1;
      if (f.inUse) c.inUse += 1;
      else c.notInUse += 1;
      if (f.missingKeys) c.missingKeys += 1;
      if (f.packageMissing) c.packageMissing += 1;
      if (f.disabled) c.disabled += 1;
    }
    return c;
  }, [catalog, factsByName]);

  const categories = useMemo(() => {
    if (!catalog) return [] as { name: string; count: number }[];
    const byCat = new Map<string, number>();
    for (const tool of catalog.tools) {
      const key = tool.category || "uncategorized";
      byCat.set(key, (byCat.get(key) ?? 0) + 1);
    }
    return [...byCat.entries()]
      .map(([name, count]) => ({ name, count }))
      .sort((a, b) => b.count - a.count || a.name.localeCompare(b.name));
  }, [catalog]);

  const bundleDisabledInfo = useMemo(() => {
    const info = new Map<string, { total: number; disabled: number }>();
    if (!catalog) return info;
    for (const bundle of catalog.bundles) {
      let disabled = 0;
      for (const name of bundle.tools) {
        if (factsByName.get(name)?.disabled) disabled += 1;
      }
      info.set(bundle.name, { total: bundle.tools.length, disabled });
    }
    return info;
  }, [catalog, factsByName]);

  // Sorted toolkits, mirroring the original sidebar ordering.
  const sortedBundles = useMemo(() => {
    if (!catalog) return [] as Bundle[];
    return [...catalog.bundles].sort((a, b) => a.name.localeCompare(b.name));
  }, [catalog]);

  // ── scope filter ─────────────────────────────────────────────────────
  const inScope = useCallback(
    (tool: Tool): boolean => {
      const f = factsByName.get(tool.name);
      if (!f) return false;
      switch (scope.kind) {
        case "all":
          return true;
        case "status":
          switch (scope.value) {
            case "in_use":
              return f.inUse;
            case "not_in_use":
              return !f.inUse;
            case "missing_keys":
              return f.missingKeys;
            case "package_missing":
              return f.packageMissing;
            case "disabled":
              return f.disabled;
            default:
              return true;
          }
        case "bundle":
          return tool.bundles.includes(scope.value);
        case "category":
          return (tool.category || "uncategorized") === scope.value;
        default:
          return true;
      }
    },
    [scope, factsByName],
  );

  // ── search filter ────────────────────────────────────────────────────
  const matchesSearch = useCallback(
    (tool: Tool): boolean => {
      const q = search.trim().toLowerCase();
      if (!q) return true;
      const haystack: string[] = [
        tool.name,
        tool.description,
        tool.category,
        ...tool.bundles,
        ...(tool.tags ?? []),
        ...(tool.requirements.packages ?? []),
        ...(tool.requirements.npm_packages ?? []),
        ...(tool.requirements.cli ?? []),
      ];
      return haystack.some((s) => s.toLowerCase().includes(q));
    },
    [search],
  );

  const visibleTools = useMemo(() => {
    if (!catalog) return [] as Tool[];
    const filtered = catalog.tools.filter((t) => inScope(t) && matchesSearch(t));
    const cmp = (a: Tool, b: Tool): number => {
      switch (sort) {
        case "category":
          return (
            (a.category || "").localeCompare(b.category || "") ||
            a.name.localeCompare(b.name)
          );
        case "usage": {
          const ua = factsByName.get(a.name)?.usageCount ?? 0;
          const ub = factsByName.get(b.name)?.usageCount ?? 0;
          return ub - ua || a.name.localeCompare(b.name);
        }
        case "status": {
          const fa = factsByName.get(a.name);
          const fb = factsByName.get(b.name);
          const ra = fa ? statusRank(fa) : 99;
          const rb = fb ? statusRank(fb) : 99;
          return ra - rb || a.name.localeCompare(b.name);
        }
        case "name":
        default:
          return a.name.localeCompare(b.name);
      }
    };
    return [...filtered].sort(cmp);
  }, [catalog, inScope, matchesSearch, sort, factsByName]);

  // ── toggle a single tool ─────────────────────────────────────────────
  async function toggleTool(tool: Tool, disabled: boolean) {
    if (busy.has(tool.name)) return;
    setBusy((prev) => new Set(prev).add(tool.name));
    try {
      await api.post<DisabledResponse>("/api/catalog/tools/disabled", {
        name: tool.name,
        disabled,
      });
      toast.push(
        disabled
          ? t("toast.disabledOne", { name: tool.name })
          : t("toast.enabledOne", { name: tool.name }),
      );
      await ensureCatalog(true);
    } catch (e) {
      toast.push(e instanceof Error ? e.message : t("toast.toggleFailed"), "error");
    } finally {
      setBusy((prev) => {
        const next = new Set(prev);
        next.delete(tool.name);
        return next;
      });
    }
  }

  // ── bulk toggle a toolkit (bundle) ───────────────────────────────────
  async function toggleBundle(names: string[], disabled: boolean) {
    if (!names.length) return;
    const key = `bundle:${names.join(",")}`;
    if (busy.has(key)) return;
    setBusy((prev) => new Set(prev).add(key));
    try {
      const res = await api.post<BulkDisabledResponse>("/api/catalog/tools/disabled/bulk", {
        names,
        disabled,
      });
      const n = res.applied?.length ?? names.length;
      const ns = String(n);
      toast.push(
        disabled
          ? n === 1
            ? t("toast.disabledManyOne", { count: ns })
            : t("toast.disabledMany", { count: ns })
          : n === 1
            ? t("toast.enabledManyOne", { count: ns })
            : t("toast.enabledMany", { count: ns }),
      );
      await ensureCatalog(true);
    } catch (e) {
      toast.push(e instanceof Error ? e.message : t("toast.bulkFailed"), "error");
    } finally {
      setBusy((prev) => {
        const next = new Set(prev);
        next.delete(key);
        return next;
      });
    }
  }

  function goToAgent(agent: string) {
    navigate(agentDetailPath(agent));
  }

  // ── open the secret dialog for an env-var name, picking the richest
  //    spec across every tool (so doc_url + guidance get filled in) ─────
  const openSecretByName = useCallback(
    (name: string) => {
      let best: SecretSpec = { name };
      if (catalog) {
        for (const t of catalog.tools) {
          for (const ev of t.requirements.env_vars ?? []) {
            if (ev.name !== name) continue;
            if ((ev.doc_url && !best.doc_url) || (ev.guidance && !best.guidance)) {
              best = { ...best, ...ev };
            } else if (!best.in_env && !best.in_vault) {
              best = { ...best, ...ev };
            }
          }
        }
        const live = catalog.env_status[name];
        if (live) best = { ...best, ...live };
      }
      setSecretSpec(best);
    },
    [catalog],
  );

  const activeBundle =
    scope.kind === "bundle" && catalog
      ? catalog.bundles.find((b) => b.name === scope.value) ?? null
      : null;

  // ── render ───────────────────────────────────────────────────────────
  return (
    <>
      <div className="topbar">
        <div style={{ display: "flex", flexDirection: "column", gap: 1, minWidth: 0 }}>
          <strong>{t("title")}</strong>
          <span
            className="muted"
            style={{
              fontSize: 12,
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
          >
            {t("subtitle")}
          </span>
        </div>
        <button
          className="btn"
          disabled={refreshing}
          onClick={() => void ensureCatalog(true)}
        >
          {refreshing ? (
            <>
              <span className="spinner" /> {t("btn.refreshing")}
            </>
          ) : (
            <>
              <RotateCw size={15} strokeWidth={1.75} /> {t("btn.refresh")}
            </>
          )}
        </button>
      </div>

      <div className="page" style={{ maxWidth: "none" }}>
        <div
          className="tools-split"
          style={{
            display: "grid",
            gridTemplateColumns: "minmax(200px, 240px) 1fr",
            gap: 20,
            alignItems: "start",
          }}
        >
          {/* ── sidebar ── */}
          <aside style={{ display: "flex", flexDirection: "column", gap: 18 }}>
            <SidebarSection title={t("side.status")}>
              <SidebarItem
                label={t("side.allTools")}
                count={counts.all}
                active={scope.kind === "all"}
                onClick={() => setScope({ kind: "all" })}
              />
              <SidebarItem
                label={t("side.inUse")}
                count={counts.inUse}
                color="var(--accent)"
                active={scope.kind === "status" && scope.value === "in_use"}
                onClick={() => setScope({ kind: "status", value: "in_use" })}
              />
              <SidebarItem
                label={t("side.notInUse")}
                count={counts.notInUse}
                active={scope.kind === "status" && scope.value === "not_in_use"}
                onClick={() => setScope({ kind: "status", value: "not_in_use" })}
              />
              <SidebarItem
                label={t("side.missingKeys")}
                count={counts.missingKeys}
                color={counts.missingKeys > 0 ? "var(--warn)" : undefined}
                active={scope.kind === "status" && scope.value === "missing_keys"}
                onClick={() => setScope({ kind: "status", value: "missing_keys" })}
              />
              <SidebarItem
                label={t("side.packageMissing")}
                count={counts.packageMissing}
                color={counts.packageMissing > 0 ? "var(--warn)" : undefined}
                active={scope.kind === "status" && scope.value === "package_missing"}
                onClick={() => setScope({ kind: "status", value: "package_missing" })}
              />
              <SidebarItem
                label={t("side.disabled")}
                count={counts.disabled}
                color={counts.disabled > 0 ? "var(--warn)" : undefined}
                active={scope.kind === "status" && scope.value === "disabled"}
                onClick={() => setScope({ kind: "status", value: "disabled" })}
              />
            </SidebarSection>

            {sortedBundles.length > 0 && (
              <SidebarSection title={t("side.toolkits")} hint={t("side.toolkitsHint")}>
                {sortedBundles.map((b) => {
                  const info = bundleDisabledInfo.get(b.name);
                  let suffix = "";
                  let allOff = false;
                  if (info) {
                    if (info.disabled === 0) suffix = "";
                    else if (info.disabled >= info.total && info.total > 0) {
                      suffix = t("badge.off");
                      allOff = true;
                    } else suffix = `${info.disabled}/${info.total}`;
                  }
                  return (
                    <SidebarItem
                      key={b.name}
                      label={b.name}
                      badge={suffix || undefined}
                      badgeColor={
                        info && info.disabled > 0 ? "var(--warn)" : undefined
                      }
                      count={suffix ? undefined : b.tools.length}
                      title={
                        allOff
                          ? t("toolkit.allOff")
                          : info && info.disabled > 0
                            ? t("toolkit.someOff", { count: String(info.disabled) })
                            : undefined
                      }
                      active={scope.kind === "bundle" && scope.value === b.name}
                      onClick={() => setScope({ kind: "bundle", value: b.name })}
                    />
                  );
                })}
              </SidebarSection>
            )}

            {categories.length > 0 && (
              <SidebarSection title={t("side.categories")}>
                {categories.map((c) => (
                  <SidebarItem
                    key={c.name}
                    label={c.name}
                    count={c.count}
                    active={scope.kind === "category" && scope.value === c.name}
                    onClick={() => setScope({ kind: "category", value: c.name })}
                  />
                ))}
              </SidebarSection>
            )}
          </aside>

          {/* ── main content ── */}
          <div style={{ minWidth: 0 }}>
            {/* top bar: search + sort */}
            <div className="row" style={{ flexWrap: "wrap", gap: 10 }}>
              <input
                className="input"
                style={{ flex: 1, minWidth: 200 }}
                placeholder={t("search.placeholder")}
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
              <select
                className="input"
                style={{ width: 170 }}
                value={sort}
                onChange={(e) => setSort(e.target.value as SortKey)}
              >
                <option value="name">{t("sort.name")}</option>
                <option value="category">{t("sort.category")}</option>
                <option value="usage">{t("sort.usage")}</option>
                <option value="status">{t("sort.status")}</option>
              </select>
            </div>

            {/* stats bar */}
            <div
              className="row"
              style={{ flexWrap: "wrap", gap: 16, marginTop: 12, fontSize: 13 }}
            >
              <Stat label={t("stat.tools")} value={counts.all} />
              <Stat label={t("stat.inUse")} value={counts.inUse} color="var(--success)" />
              <Stat
                label={t("stat.missingKeys")}
                value={counts.missingKeys}
                color={counts.missingKeys > 0 ? "var(--warn)" : undefined}
              />
              <Stat
                label={t("stat.disabled")}
                value={counts.disabled}
                color={counts.disabled > 0 ? "var(--danger)" : undefined}
              />
              <span className="muted">
                {t("stat.showing")}{" "}
                <strong style={{ color: "var(--text-2)" }}>{visibleTools.length}</strong>
              </span>
            </div>

            {/* toolkit banner */}
            {activeBundle &&
              (() => {
                const info = bundleDisabledInfo.get(activeBundle.name);
                const total = info?.total ?? activeBundle.tools.length;
                const disabledCount = info?.disabled ?? 0;
                const enabledCount = total - disabledCount;
                const bundleKey = `bundle:${activeBundle.tools.join(",")}`;
                const bundleBusy = busy.has(bundleKey);
                return (
                  <div
                    className="card"
                    style={{
                      padding: 14,
                      marginTop: 14,
                      display: "flex",
                      flexWrap: "wrap",
                      gap: 12,
                      alignItems: "center",
                      justifyContent: "space-between",
                      background: "var(--accent-gradient-soft)",
                    }}
                  >
                    <div>
                      <strong>{t("banner.toolkit", { name: activeBundle.name })}</strong>
                      <div className="muted" style={{ fontSize: 12, marginTop: 2 }}>
                        {disabledCount
                          ? t("banner.countDisabled", {
                              total: String(total),
                              enabled: String(enabledCount),
                              disabled: String(disabledCount),
                            })
                          : t("banner.count", {
                              total: String(total),
                              enabled: String(enabledCount),
                            })}
                      </div>
                    </div>
                    <div className="row">
                      <button
                        className="btn"
                        disabled={bundleBusy || enabledCount === 0}
                        title={t("banner.disableAllTip")}
                        onClick={() => void toggleBundle(activeBundle.tools, true)}
                      >
                        {t("banner.disableAll")}
                      </button>
                      <button
                        className="btn primary"
                        disabled={bundleBusy || disabledCount === 0}
                        title={t("banner.enableAllTip")}
                        onClick={() => void toggleBundle(activeBundle.tools, false)}
                      >
                        {t("banner.enableAll")}
                      </button>
                    </div>
                  </div>
                );
              })()}

            {/* list */}
            <div style={{ marginTop: 14, display: "flex", flexDirection: "column", gap: 12 }}>
              {catalog === null && error === null ? (
                <div className="row" style={{ gap: 10 }}>
                  <div className="spinner" />
                  <span className="muted">{t("list.loading")}</span>
                </div>
              ) : error !== null ? (
                <div className="card" style={{ padding: 24, textAlign: "center" }}>
                  <div style={{ marginBottom: 6, color: "var(--warn)" }}>
                    <AlertTriangle size={28} strokeWidth={1.75} />
                  </div>
                  <div style={{ fontWeight: 600 }}>{t("list.failedTitle")}</div>
                  <p className="muted" style={{ marginTop: 6 }}>
                    {error}
                  </p>
                  <button
                    className="btn primary"
                    style={{ marginTop: 12 }}
                    disabled={refreshing}
                    onClick={() => void ensureCatalog(true)}
                  >
                    {t("list.tryAgain")}
                  </button>
                </div>
              ) : visibleTools.length === 0 ? (
                <div className="card" style={{ padding: 24, textAlign: "center" }}>
                  <div style={{ marginBottom: 6, color: "var(--text-3)" }}>
                    <Wrench size={28} strokeWidth={1.75} />
                  </div>
                  <div style={{ fontWeight: 600 }}>{t("list.noneTitle")}</div>
                  <p className="muted" style={{ marginTop: 6 }}>
                    {t("list.noneBody")}
                  </p>
                  {(scope.kind !== "all" || search.trim() !== "") && (
                    <button
                      className="btn"
                      style={{ marginTop: 12 }}
                      onClick={() => {
                        setScope({ kind: "all" });
                        setSearch("");
                      }}
                    >
                      {t("list.clearFilters")}
                    </button>
                  )}
                </div>
              ) : (
                visibleTools.map((tool) => (
                  <ToolRow
                    key={tool.name}
                    tool={tool}
                    facts={factsByName.get(tool.name)}
                    usage={usage[tool.name] ?? []}
                    busy={busy.has(tool.name)}
                    onToggle={(disabled) => void toggleTool(tool, disabled)}
                    onAgentClick={goToAgent}
                    onEnvClick={openSecretByName}
                    onInstallClick={(kind, value) => setInstallDialog({ kind, value })}
                  />
                ))
              )}
            </div>
          </div>
        </div>
      </div>

      {/* ── overlays ── */}
      {secretSpec && (
        <SecretDialog
          spec={secretSpec}
          onClose={() => setSecretSpec(null)}
          onSaved={() => void ensureCatalog(true)}
        />
      )}
      {installDialog && (
        <InstallDialog
          kind={installDialog.kind}
          value={installDialog.value}
          onClose={() => setInstallDialog(null)}
          onRanSuccess={() => void ensureCatalog(true)}
        />
      )}
    </>
  );
}

// ════════════════════════════════════════════════════════════════════════
//  sidebar pieces
// ════════════════════════════════════════════════════════════════════════
function SidebarSection({
  title,
  hint,
  children,
}: {
  title: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <div className="card" style={{ padding: 10 }}>
      <div
        className="muted"
        style={{
          fontSize: 11,
          fontWeight: 700,
          textTransform: "uppercase",
          padding: "2px 6px 6px",
          display: "flex",
          gap: 6,
          alignItems: "baseline",
        }}
      >
        {title}
        {hint && (
          <span style={{ fontWeight: 400, textTransform: "none", color: "var(--text-4)" }}>
            {hint}
          </span>
        )}
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 1 }}>{children}</div>
    </div>
  );
}

interface SidebarItemProps {
  label: string;
  count?: number;
  badge?: string;
  badgeColor?: string;
  color?: string;
  title?: string;
  active: boolean;
  onClick: () => void;
}

function SidebarItem({
  label,
  count,
  badge,
  badgeColor,
  color,
  title,
  active,
  onClick,
}: SidebarItemProps) {
  return (
    <button
      onClick={onClick}
      title={title}
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        gap: 8,
        width: "100%",
        textAlign: "left",
        padding: "6px 8px",
        borderRadius: "var(--r-sm)",
        border: "none",
        cursor: "pointer",
        background: active ? "var(--accent-soft)" : "transparent",
        color: active ? "var(--accent)" : color ?? "var(--text-2)",
        fontWeight: active ? 600 : 400,
        fontSize: 13,
      }}
    >
      <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
        {label}
      </span>
      {badge ? (
        <span className="badge" style={badgeColor ? { color: badgeColor } : undefined}>
          {badge}
        </span>
      ) : count !== undefined ? (
        <span
          className="muted"
          style={{ fontSize: 12, color: active ? "var(--accent)" : undefined }}
        >
          {count}
        </span>
      ) : null}
    </button>
  );
}

function Stat({ label, value, color }: { label: string; value: number; color?: string }) {
  return (
    <span>
      <strong style={color ? { color } : undefined}>{value}</strong>{" "}
      <span className="muted">{label}</span>
    </span>
  );
}

// ════════════════════════════════════════════════════════════════════════
//  tool row
// ════════════════════════════════════════════════════════════════════════
interface ToolRowProps {
  tool: Tool;
  facts: ToolFacts | undefined;
  usage: ToolUsageEntry[];
  busy: boolean;
  onToggle: (disabled: boolean) => void;
  onAgentClick: (agent: string) => void;
  onEnvClick: (name: string) => void;
  onInstallClick: (kind: InstallKind, value: string) => void;
}

function ToolRow({
  tool,
  facts,
  usage,
  busy,
  onToggle,
  onAgentClick,
  onEnvClick,
  onInstallClick,
}: ToolRowProps) {
  const t = useT(STR);
  const label = facts
    ? statusLabel(facts, t)
    : ({ text: t("status.ready"), color: "var(--success)" } as StatusLabel);
  const disabled = tool.disabled;

  return (
    <div className="card" style={{ padding: 14, opacity: disabled ? 0.7 : 1 }}>
      {/* head */}
      <div
        className="row"
        style={{ justifyContent: "space-between", alignItems: "flex-start", gap: 12 }}
      >
        <div className="row" style={{ flexWrap: "wrap", gap: 8, minWidth: 0 }}>
          <strong style={{ fontFamily: "var(--font-mono)", fontSize: 13 }}>{tool.name}</strong>
          {tool.category && <span className="badge">{tool.category}</span>}
          {tool.bundles.slice(0, MAX_BUNDLE_TAGS).map((b) => (
            <span key={b} className="badge admin" title={t("row.toolkitTip", { name: b })}>
              {b}
            </span>
          ))}
          <span
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 5,
              fontSize: 12,
              color: label.color,
              fontWeight: 600,
            }}
          >
            <span
              style={{
                width: 7,
                height: 7,
                borderRadius: "50%",
                background: label.color,
                display: "inline-block",
              }}
            />
            {label.text}
          </span>
        </div>
        <ToggleSwitch
          checked={!disabled}
          busy={busy}
          onChange={(next) => onToggle(!next)}
          title={disabled ? t("row.enableTip") : t("row.disableTip")}
        />
      </div>

      {/* description */}
      {tool.description && (
        <div className="muted" style={{ marginTop: 6, fontSize: 13 }}>
          <Markdown text={tool.description} />
        </div>
      )}

      {/* requirements */}
      <RequirementChips req={tool.requirements} onEnvClick={onEnvClick} onInstallClick={onInstallClick} />

      {/* usage */}
      <UsageSection usage={usage} onAgentClick={onAgentClick} />
    </div>
  );
}

// ── toggle switch ──────────────────────────────────────────────────────
// Delegates to the shared app-wide switch (ui/Toggle) so tools toggles match
// every other pill switch; keeps this view's default enabled/disabled tooltip.
function ToggleSwitch({
  checked,
  busy,
  onChange,
  title,
}: {
  checked: boolean;
  busy: boolean;
  onChange: (next: boolean) => void;
  title?: string;
}) {
  const t = useT(STR);
  return (
    <Toggle
      checked={checked}
      busy={busy}
      onChange={onChange}
      title={title ?? (checked ? t("toggle.enabledTip") : t("toggle.disabledTip"))}
    />
  );
}

// ════════════════════════════════════════════════════════════════════════
//  requirement chips
// ════════════════════════════════════════════════════════════════════════
function chipStyle(border: string, color?: string): CSSProperties {
  return {
    display: "inline-flex",
    alignItems: "center",
    gap: 5,
    padding: "2px 8px",
    borderRadius: "var(--r-pill)",
    fontSize: 11,
    fontFamily: "var(--font-mono)",
    border: `1px solid ${border}`,
    color: color ?? "var(--text-2)",
    background: "var(--surface-2)",
    cursor: "pointer",
  };
}

function RequirementChips({
  req,
  onEnvClick,
  onInstallClick,
}: {
  req: Requirements;
  onEnvClick: (name: string) => void;
  onInstallClick: (kind: InstallKind, value: string) => void;
}) {
  const t = useT(STR);
  const envVars = req.env_vars ?? [];
  const packages = req.packages ?? [];
  const npmPackages = req.npm_packages ?? [];
  const cli = req.cli ?? [];
  const setupCommands = req.setup_commands ?? [];

  const nothing =
    envVars.length === 0 &&
    packages.length === 0 &&
    npmPackages.length === 0 &&
    cli.length === 0 &&
    setupCommands.length === 0;
  if (nothing) return null;

  const pkgColor = (name: string, status?: Record<string, boolean>): string => {
    if (status && status[name] === true) return "var(--success)";
    if (status && status[name] === false) return "var(--warn)";
    return "var(--text-2)";
  };
  const pkgBorder = (name: string, status?: Record<string, boolean>): string => {
    if (status && status[name] === true) return "var(--success)";
    if (status && status[name] === false) return "var(--warn)";
    return "var(--border)";
  };
  const isInstalled = (name: string, status?: Record<string, boolean>): boolean =>
    Boolean(status && status[name] === true);

  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginTop: 10 }}>
      {envVars.map((spec) => {
        const ok = envVarSatisfied(spec);
        const set = Boolean(spec.in_env) || Boolean(spec.in_vault);
        const dot = spec.in_env ? "var(--success)" : spec.in_vault ? "var(--info)" : "var(--warn)";
        const titleParts: string[] = [];
        if (spec.in_env) titleParts.push(t("env.setInEnv"));
        if (spec.in_vault) titleParts.push(t("env.inVault"));
        if (!set && spec.optional) titleParts.push(t("env.optionalClick"));
        if (!set && !spec.optional) titleParts.push(t("env.requiredClick"));
        return (
          <button
            key={spec.name}
            type="button"
            onClick={() => onEnvClick(spec.name)}
            title={titleParts.join(" · ") || spec.guidance || spec.label || spec.name}
            style={chipStyle(
              ok ? "var(--border)" : "var(--warn)",
              ok ? "var(--text-2)" : "var(--warn)",
            )}
          >
            <span
              style={{
                width: 7,
                height: 7,
                borderRadius: "50%",
                background: dot,
                display: "inline-block",
              }}
            />
            {spec.name}
            {spec.optional && (
              <span className="muted" style={{ fontSize: 10 }}>
                {t("env.opt")}
              </span>
            )}
          </button>
        );
      })}

      {packages.map((p) => {
        const installed = isInstalled(p, req.packages_status);
        return (
          <button
            key={`py:${p}`}
            type="button"
            onClick={() => onInstallClick("python", p)}
            title={
              installed ? t("chip.installed", { name: p }) : t("chip.installPy", { name: p })
            }
            style={chipStyle(pkgBorder(p, req.packages_status), pkgColor(p, req.packages_status))}
          >
            pip:{p}
            {installed ? <Check size={12} strokeWidth={2} /> : <Play size={11} strokeWidth={2} />}
          </button>
        );
      })}
      {npmPackages.map((p) => {
        const installed = isInstalled(p, req.npm_packages_status);
        return (
          <button
            key={`npm:${p}`}
            type="button"
            onClick={() => onInstallClick("npm", p)}
            title={
              installed ? t("chip.installed", { name: p }) : t("chip.installNpm", { name: p })
            }
            style={chipStyle(
              pkgBorder(p, req.npm_packages_status),
              pkgColor(p, req.npm_packages_status),
            )}
          >
            npm:{p}
            {installed ? <Check size={12} strokeWidth={2} /> : <Play size={11} strokeWidth={2} />}
          </button>
        );
      })}
      {cli.map((c) => {
        const installed = isInstalled(c, req.cli_status);
        return (
          <button
            key={`cli:${c}`}
            type="button"
            onClick={() => onInstallClick("cli", c)}
            title={
              installed ? t("chip.installed", { name: c }) : t("chip.installCli", { name: c })
            }
            style={chipStyle(pkgBorder(c, req.cli_status), pkgColor(c, req.cli_status))}
          >
            cli:{c}
            {installed ? <Check size={12} strokeWidth={2} /> : <Play size={11} strokeWidth={2} />}
          </button>
        );
      })}
      {setupCommands.map((cmd, i) => (
        <button
          key={`setup:${i}`}
          type="button"
          onClick={() => onInstallClick("setup", cmd)}
          title={t("chip.runSetup", { cmd })}
          style={chipStyle("var(--border)")}
        >
          $ {cmd.length > 40 ? `${cmd.slice(0, 40)}…` : cmd}
          <Play size={11} strokeWidth={2} />
        </button>
      ))}
    </div>
  );
}

// ════════════════════════════════════════════════════════════════════════
//  usage section
// ════════════════════════════════════════════════════════════════════════
function UsageSection({
  usage,
  onAgentClick,
}: {
  usage: ToolUsageEntry[];
  onAgentClick: (agent: string) => void;
}) {
  const t = useT(STR);
  if (usage.length === 0) {
    return (
      <div className="muted" style={{ marginTop: 10, fontSize: 12 }}>
        {t("usage.none")}
      </div>
    );
  }
  const shown = usage.slice(0, USAGE_LIMIT);
  const more = usage.length - shown.length;
  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginTop: 10, alignItems: "center" }}>
      <span className="muted" style={{ fontSize: 12, marginRight: 2 }}>
        {t("usage.usedBy")}
      </span>
      {shown.map((entry, i) => (
        <button
          key={`${entry.agent}:${entry.subagent ?? ""}:${i}`}
          className="badge"
          onClick={() => onAgentClick(entry.agent)}
          title={
            entry.subagent
              ? `${entry.agent} · ${entry.subagent}`
              : t("usage.openAgent", { agent: entry.agent })
          }
          style={{
            cursor: "pointer",
            border: "1px solid var(--border)",
            background: "var(--surface-2)",
          }}
        >
          {entry.agent}
          {entry.subagent ? ` · ${entry.subagent}` : ""}
        </button>
      ))}
      {more > 0 && (
        <span className="muted" style={{ fontSize: 12 }}>
          {t("usage.more", { count: String(more) })}
        </span>
      )}
    </div>
  );
}

// ════════════════════════════════════════════════════════════════════════
//  modal shell
// ════════════════════════════════════════════════════════════════════════
function ModalOverlay({ children, onClose }: { children: ReactNode; onClose: () => void }) {
  // Close on Escape for a native feel (matches the app's other overlays).
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="modal-overlay" onClick={onClose} style={{ zIndex: 60 }}>
      <div
        onClick={(e) => e.stopPropagation()}
        className="card modal-card"
        style={{
          width: "min(640px, 100%)",
          overflow: "auto",
          overscrollBehavior: "contain",
          padding: 20,
        }}
      >
        {children}
      </div>
    </div>
  );
}

// ════════════════════════════════════════════════════════════════════════
//  secret dialog — saves a value into the vault (POST /api/vault/secrets)
// ════════════════════════════════════════════════════════════════════════
function SecretDialog({
  spec,
  onClose,
  onSaved,
}: {
  spec: SecretSpec;
  onClose: () => void;
  onSaved: () => void;
}) {
  const toast = useToast();
  const t = useT(STR);
  const [value, setValue] = useState("");
  const [reveal, setReveal] = useState(false);
  const [saving, setSaving] = useState(false);
  const [status, setStatus] = useState("");
  const [statusError, setStatusError] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const id = window.setTimeout(() => inputRef.current?.focus(), 60);
    return () => window.clearTimeout(id);
  }, []);

  const alreadySet = Boolean(spec.in_env) || Boolean(spec.in_vault);
  const title = alreadySet
    ? t("secret.update", { name: spec.name })
    : t("secret.set", { name: spec.name });
  const sub = spec.in_env
    ? t("secret.subEnv")
    : spec.in_vault
      ? t("secret.subVault")
      : t("secret.subNew");

  async function save() {
    if (!value) {
      setStatus(t("secret.empty"));
      setStatusError(true);
      return;
    }
    setSaving(true);
    setStatusError(false);
    setStatus(t("secret.saving"));
    try {
      await api.post<{ ok: boolean; name: string }>("/api/vault/secrets", {
        name: spec.name,
        value,
        category: "api_keys",
      });
      setStatus(t("secret.saved"));
      toast.push(t("secret.savedToast", { name: spec.name }));
      onSaved();
      window.setTimeout(onClose, 400);
    } catch (e) {
      const msg = e instanceof Error ? e.message : t("secret.failed");
      setStatus(t("secret.failedStatus", { msg }));
      setStatusError(true);
      setSaving(false);
    }
  }

  return (
    <ModalOverlay onClose={onClose}>
      <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-start" }}>
        <div>
          <div style={{ fontWeight: 700, fontSize: 16 }}>{title}</div>
          <div className="muted" style={{ fontSize: 13, marginTop: 4 }}>
            {sub}
          </div>
        </div>
        <button className="btn" onClick={onClose} aria-label={t("secret.close")}>
          <X size={15} strokeWidth={1.75} />
        </button>
      </div>

      {spec.guidance && (
        <div
          style={{
            marginTop: 14,
            padding: "8px 12px",
            borderRadius: "var(--r-sm)",
            background: "var(--info-soft)",
            border: "1px solid color-mix(in srgb, var(--info) 28%, transparent)",
            color: "var(--text-2)",
            fontSize: 13,
          }}
        >
          <Markdown text={spec.guidance} />
        </div>
      )}

      <div style={{ marginTop: 14, display: "flex", flexDirection: "column", gap: 10 }}>
        <div>
          <label className="muted" style={{ fontSize: 12 }}>
            {t("secret.name")}
          </label>
          <input className="input" value={spec.name} readOnly style={{ marginTop: 4 }} />
        </div>
        <div>
          <label className="muted" style={{ fontSize: 12 }}>
            {t("secret.value")}
          </label>
          <div className="row" style={{ marginTop: 4, gap: 6 }}>
            <input
              ref={inputRef}
              className="input"
              type={reveal ? "text" : "password"}
              value={value}
              autoComplete="off"
              onChange={(e) => setValue(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") void save();
              }}
              placeholder={t("secret.valuePlaceholder")}
              style={{ flex: 1 }}
            />
            <button
              className="btn"
              type="button"
              onClick={() => setReveal((r) => !r)}
              title={reveal ? t("secret.hide") : t("secret.show")}
            >
              {reveal ? t("secret.hide") : t("secret.show")}
            </button>
          </div>
        </div>
      </div>

      <div
        className="row"
        style={{ marginTop: 16, justifyContent: "space-between", flexWrap: "wrap", gap: 10 }}
      >
        <div className="row" style={{ gap: 10 }}>
          {spec.doc_url && (
            <a
              href={spec.doc_url}
              target="_blank"
              rel="noreferrer"
              style={{ fontSize: 13, display: "inline-flex", alignItems: "center", gap: 4 }}
            >
              {t("secret.whereToGet")}
              <ExternalLink size={13} strokeWidth={1.75} />
            </a>
          )}
          {status && (
            <span
              className="muted"
              style={{
                fontSize: 13,
                color: statusError ? "var(--danger)" : "var(--text-3)",
              }}
            >
              {status}
            </span>
          )}
        </div>
        <div className="row">
          <button className="btn" onClick={onClose}>
            {t("secret.cancel")}
          </button>
          <button className="btn primary" disabled={saving} onClick={() => void save()}>
            {saving ? t("secret.saving") : t("secret.save")}
          </button>
        </div>
      </div>
    </ModalOverlay>
  );
}

// ════════════════════════════════════════════════════════════════════════
//  install dialog — platform-aware install options with Copy + Run
// ════════════════════════════════════════════════════════════════════════
function InstallDialog({
  kind,
  value,
  onClose,
  onRanSuccess,
}: {
  kind: InstallKind;
  value: string;
  onClose: () => void;
  onRanSuccess: () => void;
}) {
  const t = useT(STR);
  const options = useMemo(() => installOptions(kind, value, t), [kind, value, t]);

  // run-with-confirm state
  const [pending, setPending] = useState<InstallOption | null>(null);
  // active streaming run
  const [run, setRun] = useState<{ cmd: string } | null>(null);

  return (
    <ModalOverlay onClose={onClose}>
      <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-start" }}>
        <div>
          <div style={{ fontWeight: 700, fontSize: 16 }}>
            {t(INSTALL_TITLE_KEYS[kind])}: <code>{value}</code>
          </div>
          <div className="muted" style={{ fontSize: 13, marginTop: 4 }}>
            {t(INSTALL_SUB_KEYS[kind])}
          </div>
        </div>
        <button className="btn" onClick={onClose} aria-label={t("secret.close")}>
          <X size={15} strokeWidth={1.75} />
        </button>
      </div>

      <div style={{ marginTop: 14, display: "flex", flexDirection: "column", gap: 10 }}>
        {options.map((o, i) => (
          <InstallOptionCard
            key={i}
            option={o}
            onRun={() => setPending(o)}
          />
        ))}
      </div>

      {run && (
        <RunOutput cmd={run.cmd} onSuccess={onRanSuccess} />
      )}

      {pending && (
        <RunConfirm
          cmd={pending.cmd}
          note={pending.note}
          onCancel={() => setPending(null)}
          onConfirm={() => {
            const cmd = pending.cmd;
            setPending(null);
            setRun({ cmd });
          }}
        />
      )}
    </ModalOverlay>
  );
}

function InstallOptionCard({
  option,
  onRun,
}: {
  option: InstallOption;
  onRun: () => void;
}) {
  const t = useT(STR);
  const [copied, setCopied] = useState(false);

  function copy() {
    void navigator.clipboard?.writeText(option.cmd);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1200);
  }

  return (
    <div
      style={{
        borderRadius: "var(--r-sm)",
        padding: 10,
        background: option.runnable ? "var(--surface)" : "var(--surface-2)",
      }}
    >
      <div className="row" style={{ justifyContent: "space-between", gap: 10 }}>
        <div style={{ fontWeight: 600, fontSize: 13 }}>
          {option.label}
          {!option.runnable && (
            <span className="badge" style={{ marginLeft: 8 }}>
              {t("opt.copyOnly")}
            </span>
          )}
        </div>
        <div className="row" style={{ gap: 6 }}>
          <button className="btn" type="button" onClick={copy} style={{ padding: "4px 10px" }}>
            {copied ? t("opt.copied") : t("opt.copy")}
          </button>
          {option.runnable && (
            <button
              className="btn primary"
              type="button"
              onClick={onRun}
              style={{ padding: "4px 10px" }}
            >
              {t("opt.run")}
            </button>
          )}
        </div>
      </div>
      <pre
        style={{
          margin: "8px 0 0",
          padding: "8px 10px",
          background: "var(--code-bg)",
          border: "1px solid var(--code-border)",
          borderRadius: "var(--r-sm)",
          fontFamily: "var(--font-mono)",
          fontSize: 12,
          whiteSpace: "pre-wrap",
          wordBreak: "break-word",
        }}
      >
        {option.cmd}
      </pre>
      {option.note && (
        <div className="muted" style={{ fontSize: 12, marginTop: 6 }}>
          {option.note}
        </div>
      )}
    </div>
  );
}

// ── confirmation overlay before a Run ──────────────────────────────────
function RunConfirm({
  cmd,
  note,
  onCancel,
  onConfirm,
}: {
  cmd: string;
  note?: string;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const t = useT(STR);
  return (
    <div className="modal-overlay" onClick={onCancel} style={{ zIndex: 70 }}>
      <div
        onClick={(e) => e.stopPropagation()}
        className="card"
        style={{
          width: "min(460px, 100%)",
          padding: 20,
          boxShadow: "var(--shadow-lg)",
          borderColor: "var(--border-strong)",
        }}
      >
        <div style={{ fontWeight: 700, fontSize: 15 }}>{t("confirm.title")}</div>
        <pre
          style={{
            margin: "10px 0 0",
            padding: "8px 10px",
            background: "var(--code-bg)",
            border: "1px solid var(--code-border)",
            borderRadius: "var(--r-sm)",
            fontFamily: "var(--font-mono)",
            fontSize: 12,
            whiteSpace: "pre-wrap",
            wordBreak: "break-word",
          }}
        >
          {cmd}
        </pre>
        {note && (
          <div className="muted" style={{ fontSize: 12, marginTop: 8 }}>
            {note}
          </div>
        )}
        <div className="row" style={{ marginTop: 16, justifyContent: "flex-end" }}>
          <button className="btn" onClick={onCancel}>
            {t("confirm.cancel")}
          </button>
          <button className="btn primary" onClick={onConfirm}>
            {t("confirm.run")}
          </button>
        </div>
      </div>
    </div>
  );
}

// ── live streamed command output via /api/shell/stream (SSE) ───────────
function RunOutput({ cmd, onSuccess }: { cmd: string; onSuccess: () => void }) {
  const [lines, setLines] = useState<OutputLine[]>([]);
  const [phase, setPhase] = useState<"streaming" | "running" | "done">("streaming");
  const [exitCode, setExitCode] = useState<number | null>(null);
  const [logPath, setLogPath] = useState<string | null>(null);
  const bodyRef = useRef<HTMLPreElement>(null);
  const toast = useToast();
  const t = useT(STR);
  const onSuccessRef = useRef(onSuccess);
  onSuccessRef.current = onSuccess;

  useEffect(() => {
    setLines([]);
    setPhase("streaming");
    setExitCode(null);
    setLogPath(null);

    const es = new EventSource(`/api/shell/stream?command=${encodeURIComponent(cmd)}`);
    let finished = false;

    es.onmessage = (ev) => {
      let payload: ShellEvent;
      try {
        payload = JSON.parse(ev.data) as ShellEvent;
      } catch {
        return;
      }
      if (payload.type === "started") {
        setPhase("running");
      } else if (payload.type === "stdout") {
        setLines((prev) => [...prev, { text: payload.data ?? "", kind: "stdout" }]);
      } else if (payload.type === "stderr") {
        setLines((prev) => [...prev, { text: payload.data ?? "", kind: "stderr" }]);
      } else if (payload.type === "error") {
        setLines((prev) => [...prev, { text: payload.message ?? "", kind: "error" }]);
      } else if (payload.type === "done") {
        finished = true;
        const code = payload.exit_code ?? -1;
        setExitCode(code);
        setLogPath(payload.log_path ?? null);
        setPhase("done");
        es.close();
        if (code === 0) {
          toast.push(t("run.finished"));
          onSuccessRef.current();
        } else {
          toast.push(t("run.failed", { code: String(code) }), "error");
        }
      }
    };
    es.onerror = () => {
      if (!finished) {
        setLines((prev) => [...prev, { text: t("run.connectionLost"), kind: "error" }]);
        setPhase("done");
        setExitCode((c) => c ?? -1);
        es.close();
      }
    };

    return () => es.close();
  }, [cmd, toast, t]);

  // auto-scroll to newest line
  useEffect(() => {
    const el = bodyRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [lines]);

  const ok = exitCode === 0;
  const statusText =
    phase === "done"
      ? logPath
        ? t("run.exitLog", { code: String(exitCode) })
        : t("run.exit", { code: String(exitCode) })
      : phase === "running"
        ? t("run.running")
        : t("run.streaming");

  return (
    <div
      style={{
        marginTop: 14,
        border: `1px solid ${phase === "done" && !ok ? "var(--danger)" : "var(--code-border)"}`,
        borderRadius: "var(--r-sm)",
        overflow: "hidden",
      }}
    >
      <div
        className="row"
        style={{
          justifyContent: "space-between",
          gap: 10,
          padding: "6px 10px",
          background: "var(--surface-2)",
          borderBottom: "1px solid var(--code-border)",
        }}
      >
        <code style={{ fontFamily: "var(--font-mono)", fontSize: 12 }}>{cmd}</code>
        <span
          className="muted"
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 4,
            fontSize: 11,
            color: phase === "done" ? (ok ? "var(--success)" : "var(--danger)") : undefined,
          }}
        >
          {phase === "done" &&
            (ok ? (
              <Check size={12} strokeWidth={2} />
            ) : (
              <X size={12} strokeWidth={2} />
            ))}
          {statusText}
        </span>
      </div>
      <pre
        ref={bodyRef}
        style={{
          margin: 0,
          padding: "8px 10px",
          maxHeight: 280,
          overflow: "auto",
          background: "var(--code-bg)",
          fontFamily: "var(--font-mono)",
          fontSize: 12,
          whiteSpace: "pre-wrap",
          wordBreak: "break-word",
        }}
      >
        {lines.length === 0 ? (
          <span className="muted">{t("run.waiting")}</span>
        ) : (
          lines.map((l, i) => (
            <span
              key={i}
              style={{
                display: "block",
                color:
                  l.kind === "error"
                    ? "var(--danger)"
                    : l.kind === "stderr"
                      ? "var(--text-3)"
                      : "var(--text)",
              }}
            >
              {l.text}
            </span>
          ))
        )}
      </pre>
      {logPath && (
        <div className="muted" style={{ fontSize: 11, padding: "6px 10px" }}>
          {t("run.log")} <code>{logPath}</code>
        </div>
      )}
    </div>
  );
}
