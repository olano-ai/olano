// Shapes returned by /api/revisions* (olano/runtime/routes/revisions.py).

export interface LogEntry {
  commit: string;
  author: string;
  ts: string;
  message: string;
  parents: string[];
}

export type FileStatus = "added" | "modified" | "deleted";

export interface CommitFile {
  path: string;
  status: FileStatus;
  additions: number;
  deletions: number;
  binary: boolean;
}

export interface CommitDetails {
  commit: string;
  parents: string[];
  author: string;
  email: string;
  ts: string;
  committed_ts: string;
  message: string;
  files: CommitFile[];
  stats: {
    files: number;
    added: number;
    modified: number;
    deleted: number;
    additions: number;
    deletions: number;
  };
}

export interface FileDiffResponse {
  commit: string;
  base: string;
  path: string;
  patch: string;
  binary: boolean;
  truncated: boolean;
}

export type SyncMode = "manual" | "daily" | "on_commit";

/** Where a running remote operation is (olano/revisions/remote.py report_progress). */
export interface OperationProgress {
  /** The operation's steps in order: check, snapshot, upload, download, apply, save. */
  steps: string[];
  /** The step under way, or null before the first one starts. */
  step: string | null;
  /** That step's own progress, 0-100, or null while it cannot be measured. */
  percent: number | null;
  /** git's phase inside an upload or download: counting, compressing, writing, receiving, resolving. */
  phase?: string;
  done?: number;
  total?: number;
  /** git's transfer figure, e.g. "12.00 MiB | 3.00 MiB/s". */
  transfer?: string;
}

export interface RemoteOperation {
  op: string;
  by: string;
  since: string;
  progress?: OperationProgress;
}
export type AuthMode = "token" | "connection";

export interface MergeSummary {
  ours: string;
  theirs: string;
  branch: string;
  created_at: string;
  files: { path: string; ours_deleted: boolean; theirs_deleted: boolean }[];
}

export interface RemoteStatus {
  enabled: boolean;
  connected: boolean;
  url: string;
  repo: string;
  web_url: string;
  branch: string;
  auth: AuthMode;
  sync: SyncMode;
  connection: { available: boolean; account?: string };
  merge: MergeSummary | null;
  ahead: number;
  behind: number;
  unpushed: string[];
  /** The remote operation running right now (any admin, or the automatic push). */
  busy: RemoteOperation | null;
  state: {
    connected_at?: string;
    last_push_at?: string;
    last_push_commit?: string;
    last_pull_at?: string;
    last_fetch_at?: string;
    last_error?: string;
    last_error_code?: string;
    last_error_op?: string;
    last_error_at?: string;
    needs_pull?: boolean;
  };
}

export interface PullResult {
  status: "pulled" | "up_to_date" | "conflicts";
  kind?: "fast_forward" | "merged";
  commit?: string;
  written?: string[];
  deleted?: string[];
  kept?: string[];
  ignored?: { path: string; reason: string }[];
  warnings?: string[];
  agents_changed?: string[];
  config_changed?: boolean;
  merge?: MergeSummary;
  reason?: string;
}

export interface MergeFileDetail {
  path: string;
  /** A document, image or recording: resolved by picking a side, not edited. */
  binary: boolean;
  sizes: { base: number | null; ours: number | null; theirs: number | null };
  base: string | null;
  ours: string | null;
  theirs: string | null;
  merged: string;
  patch: string;
}

export interface LogPage {
  entries: LogEntry[];
  /** Every commit the pager can reach (the whole history, or one file's). */
  total?: number;
  skip?: number;
  repo_bytes: number;
  held_back?: HeldBack[];
  max_file_kb?: number;
}

export interface HeldBack {
  path: string;
  /** too_large (over revisions.max_file_kb), secret (looks like it holds a
   *  credential), unparseable (an agent.json5 that does not parse). */
  reason: "too_large" | "secret" | "unparseable";
}

export interface RevisionFile {
  path: string;
  rev: string;
  binary: boolean;
  size: number;
  content: string;
}
