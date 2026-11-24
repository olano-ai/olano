import { useState, type FormEvent } from "react";
import { api } from "../../core/api";
import { useT } from "../../core/i18n";
import { PasswordField } from "../vault/components";
import { STR } from "./strings";

// Vault writes shared by the steps that collect a credential (provider keys,
// Tavily). Extracted from the step panels so ProviderStep can live in its own module
// without either file importing the other.

/** Store a secret in the vault. Throws ApiError — 503 means vault locked. */
export async function saveSecret(
  name: string,
  value: string,
  description: string,
): Promise<void> {
  await api.post("/api/vault/secrets", { name, value, description, category: "api-keys" });
}

/** Inline unlock block shown when a vault write hit a 503 (locked). */
export function VaultUnlock({ onUnlocked }: { onUnlocked(): void }) {
  const t = useT(STR);
  const [passphrase, setPassphrase] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(false);

  async function unlock(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setErr(false);
    try {
      await api.post("/api/vault/unlock", { passphrase });
      onUnlocked();
    } catch {
      setErr(true);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="ob-unlock" onSubmit={unlock}>
      <strong>{t("ob.unlock.title")}</strong>
      <p className="ob-hint">{t("ob.unlock.lead")}</p>
      <div className="field">
        <label>{t("ob.unlock.field")}</label>
        <PasswordField value={passphrase} onChange={setPassphrase} autoFocus />
      </div>
      {err && <div className="error-text">{t("ob.unlock.err")}</div>}
      <button className="btn primary" disabled={busy || !passphrase}>
        {busy ? t("ob.saving") : t("ob.unlock.submit")}
      </button>
    </form>
  );
}
