import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { api } from "../../core/api";
import { useT } from "../../core/i18n";
import { useToast } from "../../ui/toast";
import { UnlockModal } from "./modals";
import { STR } from "./strings";
import type { VaultStatus } from "./types";

// App-level vault-lock monitor. The vault holds the API keys for every LLM
// provider and connector, so when it's locked nothing works — agents, Cortex,
// and the dashboard all silently fail. The vault is *daemon session state*: a
// stop/restart of the olano server (or the dashboard) re-locks it unless a
// keyring or OLANO_VAULT_KEY is set.
//
// We therefore watch its status app-wide (not just on the Vault page) and pop
// the unlock prompt the moment it's locked — at first load AND in real time if
// it locks mid-session. Mirrors the original dashboard's global
// `startVaultMonitor()` / `checkVaultStatus()` pair.

const VAULT_POLL_MS = 15000;

interface VaultMonitorApi {
  // Latest known vault status, or null before the first poll completes.
  status: VaultStatus | null;
  // Force-refresh the status now (e.g. after a vault mutation) and return it.
  refresh: () => Promise<VaultStatus | null>;
  // Open the shared unlock prompt. `onSuccess` is replayed after a successful
  // unlock — the gating callback pattern from the original dashboard.
  requestUnlock: (onSuccess?: () => void) => void;
}

const Ctx = createContext<VaultMonitorApi | null>(null);

export function VaultMonitorProvider({ children }: { children: ReactNode }) {
  const t = useT(STR);
  const { push } = useToast();
  const [status, setStatus] = useState<VaultStatus | null>(null);
  const [unlockOpen, setUnlockOpen] = useState(false);

  // Latched once we've auto-prompted for the current lock episode, so the 15s
  // poller never stacks a second modal. Re-armed on a successful unlock or
  // whenever the vault is unlocked / not configured. Mirrors the original
  // dashboard's `_vaultLockedPrompted`.
  const lockedPrompted = useRef(false);
  // Mirror of `unlockOpen` for the polling closure, which would otherwise
  // capture a stale value and could open a second modal over a manual one.
  const openRef = useRef(false);
  // Callback to replay after a successful unlock (set by requestUnlock).
  const successCb = useRef<(() => void) | null>(null);
  // Latest toast/translator, reached via refs so the mount-once poll effect
  // below doesn't list them as deps. `useToast()` returns a fresh object on
  // every ToastProvider render (any toast app-wide), and `t` changes on a
  // language switch — depending on either would tear down and re-create the
  // 15s interval, regressing the original's single stable poller.
  const pushRef = useRef(push);
  pushRef.current = push;
  const tRef = useRef(t);
  tRef.current = t;

  const fetchStatus = useCallback(async (): Promise<VaultStatus | null> => {
    try {
      return await api.get<VaultStatus>("/api/vault/status");
    } catch {
      return null;
    }
  }, []);

  // Commit a fetched status, skipping the state write (and the consumer
  // re-render it triggers) when nothing material changed — otherwise every 15s
  // poll re-renders the app-level provider with a byte-identical status.
  const applyStatus = useCallback((s: VaultStatus) => {
    setStatus((prev) =>
      prev &&
      prev.unlocked === s.unlocked &&
      prev.has_vault === s.has_vault &&
      prev.env_key_set === s.env_key_set &&
      prev.keyring_available === s.keyring_available
        ? prev
        : s,
    );
    if (s.unlocked || !s.has_vault) lockedPrompted.current = false;
  }, []);

  const refresh = useCallback(async (): Promise<VaultStatus | null> => {
    const s = await fetchStatus();
    if (s) applyStatus(s);
    return s;
  }, [fetchStatus, applyStatus]);

  const openModal = useCallback((onSuccess?: () => void) => {
    successCb.current = onSuccess ?? null;
    openRef.current = true;
    setUnlockOpen(true);
  }, []);

  const requestUnlock = useCallback(
    (onSuccess?: () => void) => openModal(onSuccess),
    [openModal],
  );

  // ── boot check + live monitor (15s poll, plus a recheck on tab focus) ──────
  useEffect(() => {
    let cancelled = false;
    const check = async () => {
      const s = await fetchStatus();
      if (cancelled || !s) return;
      applyStatus(s); // also re-arms the latch when unlocked / no vault
      // Unlocked, or no vault configured → nothing to nag about.
      if (s.unlocked || !s.has_vault) return;
      // Vault exists and is LOCKED.
      if (lockedPrompted.current || openRef.current) return; // already handling
      lockedPrompted.current = true;
      pushRef.current(tRef.current("toast.locked"), "error");
      openModal();
    };
    void check(); // boot check
    // Skip the poll while the tab is hidden — the visibility handler below
    // rechecks immediately on return, so a lock is still surfaced on arrival.
    const id = window.setInterval(() => {
      if (!document.hidden) void check();
    }, VAULT_POLL_MS);
    // A daemon restart while the tab is backgrounded won't be caught until the
    // next poll; rechecking on focus surfaces the lock the moment the user
    // returns. (The original re-checked on SSE reconnect for the same reason.)
    const onVisible = () => {
      if (document.visibilityState === "visible") void check();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      cancelled = true;
      window.clearInterval(id);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [fetchStatus, applyStatus, openModal]);

  const closeModal = useCallback(() => {
    openRef.current = false;
    successCb.current = null;
    setUnlockOpen(false);
    // Keep `lockedPrompted` latched: dismissing the prompt shouldn't make it
    // immediately re-pop on the next poll. It re-arms once the vault is
    // actually unlocked or no longer present.
  }, []);

  const onUnlockSuccess = useCallback(() => {
    openRef.current = false;
    setUnlockOpen(false);
    lockedPrompted.current = false; // re-arm for a future re-lock
    const cb = successCb.current;
    successCb.current = null;
    void refresh();
    cb?.();
  }, [refresh]);

  // Stable identity except when `status` actually changes (applyStatus dedupes),
  // so consumers don't re-render on every poll.
  const value = useMemo<VaultMonitorApi>(
    () => ({ status, refresh, requestUnlock }),
    [status, refresh, requestUnlock],
  );

  return (
    <Ctx.Provider value={value}>
      {children}
      {unlockOpen && <UnlockModal onClose={closeModal} onSuccess={onUnlockSuccess} />}
    </Ctx.Provider>
  );
}

export function useVaultMonitor(): VaultMonitorApi {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useVaultMonitor must be used within <VaultMonitorProvider>");
  return ctx;
}
