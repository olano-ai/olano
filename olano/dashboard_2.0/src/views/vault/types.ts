// View-local types for the Vault slice (not in core/types yet).

export interface VaultStatus {
  unlocked: boolean;
  keyring_available: boolean;
  env_key_set: boolean;
  has_vault: boolean;
}

// Shape returned by VaultStore.list_names() (api.py /vault/secrets).
// `agent` is null for the common value, or the agent name for a per-agent override.
export interface SecretMetadata {
  name: string;
  description: string;
  category?: string;
  agent?: string | null;
  created_at: string | null;
  updated_at: string | null;
  expires_at: string | null;
  last_used: string | null;
}

// Shape returned by /vault/secrets/{name}/reveal.
export interface SecretValue {
  name: string;
  value: string;
  agent?: string | null;
}

// POST /vault/secrets request body.
export interface SecretInput {
  name: string;
  value: string;
  description: string;
  category: string;
  expires_in_days: number | null;
  rotation_reminder_days: number;
  // null/omitted → common value; otherwise a per-agent override.
  agent?: string | null;
}

export interface UnlockResponse {
  ok: true;
  mode: string;
}

export interface RekeyResponse {
  ok: true;
  rekeyed: number;
}
