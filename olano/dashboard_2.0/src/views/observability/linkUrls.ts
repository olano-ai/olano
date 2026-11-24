// Pure "Open in Langfuse" URL builders (no React, no API), so node's --test
// can import them. The hook that fetches the targets lives in ./links.ts.

export interface LangfuseLinks {
  available: boolean;
  host?: string;
  project_id?: string;
}

export function traceUrl(links: LangfuseLinks | null | undefined, traceId: string | undefined): string {
  if (!links?.available || !links.host || !links.project_id || !traceId) return "";
  if (!/^[0-9a-f]{32}$/.test(traceId)) return "";
  return `${links.host.replace(/\/+$/, "")}/project/${encodeURIComponent(links.project_id)}/traces/${traceId}`;
}

/** ``sessionId`` is the checkpoint-scoped thread id (``<agent>::<thread>``,
 * or a Cortex run's ``cog::<worker>``): the id the runner names sessions by,
 * and the id Insights' drill-down already carries. */
export function sessionUrl(links: LangfuseLinks | null | undefined, sessionId: string | undefined): string {
  if (!links?.available || !links.host || !links.project_id || !sessionId) return "";
  return `${links.host.replace(/\/+$/, "")}/project/${encodeURIComponent(links.project_id)}/sessions/${encodeURIComponent(sessionId)}`;
}
