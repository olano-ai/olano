// The Issues list's filters as URL state — pure, React-free, unit-tested.
//
// The address bar is the source of truth for what the list shows, so a URL can
// be shared and reopens the same list. Only values that differ from the
// defaults are written, so the everyday URL stays `/issues`, and every writer
// copies the params it was given so `?issue=N` (the open issue) and `?focus=`
// (the settings palette, written on every route) survive a filter change.
// `state` and `sort` are part of the shareable address but are not "active
// filters": Clear filters and Copy link key off q/type/assignee/label, the
// four that narrow the list, exactly as the view always has.

export type IssueState = "open" | "closed";
export type IssueSort = "relevance" | "updated" | "created" | "oldest" | "id";

export interface IssueFilters {
  q: string;
  state: IssueState;
  type: string;
  assignee: string;
  label: string;
  sort: IssueSort;
}

export const SORTS: readonly IssueSort[] = ["relevance", "updated", "created", "oldest", "id"];

export const DEFAULT_FILTERS: IssueFilters = {
  q: "",
  state: "open",
  type: "",
  assignee: "",
  label: "",
  sort: "updated",
};

/** The URL keys this module owns — the same names the backend's list endpoint
 *  takes, so nothing is translated on the way to `/api/issues`. */
export const FILTER_KEYS = ["q", "state", "type", "assignee", "label", "sort"] as const;

/** Read the filters out of a URL. Unknown values fall back to the default —
 *  `state=all` (an old spelling) reads as open, an unknown sort as updated. */
export function parseFilters(params: URLSearchParams): IssueFilters {
  const sort = params.get("sort") ?? "";
  return {
    q: (params.get("q") ?? "").trim(),
    state: params.get("state") === "closed" ? "closed" : "open",
    type: params.get("type") ?? "",
    assignee: params.get("assignee") ?? "",
    label: params.get("label") ?? "",
    sort: (SORTS as readonly string[]).includes(sort) ? (sort as IssueSort) : "updated",
  };
}

/** True when the URL carries any filter key at all — `?issue=3` alone does not,
 *  which is what lets a notification's deep link land on the remembered list. */
export function hasFilterParams(params: URLSearchParams): boolean {
  return FILTER_KEYS.some((k) => params.has(k));
}

/**
 * Write `f` into a COPY of `params`: every filter key is cleared first, then
 * only non-default values are set back. Anything else in `params` is left
 * exactly as it was.
 */
export function writeFilters(params: URLSearchParams, f: IssueFilters): URLSearchParams {
  const next = new URLSearchParams(params);
  for (const k of FILTER_KEYS) next.delete(k);
  if (f.q) next.set("q", f.q);
  if (f.state !== DEFAULT_FILTERS.state) next.set("state", f.state);
  if (f.type) next.set("type", f.type);
  if (f.assignee) next.set("assignee", f.assignee);
  if (f.label) next.set("label", f.label);
  if (f.sort !== DEFAULT_FILTERS.sort) next.set("sort", f.sort);
  return next;
}

/** The four narrowing filters — what Clear filters resets and Copy link offers. */
export function filtersActive(f: IssueFilters): boolean {
  return f.q !== "" || f.type !== "" || f.assignee !== "" || f.label !== "";
}

export function isDefault(f: IssueFilters): boolean {
  return FILTER_KEYS.every((k) => f[k] === DEFAULT_FILTERS[k]);
}

/** A stable identity for effect dependencies — one string, not six values. */
export function filtersKey(f: IssueFilters): string {
  return writeFilters(new URLSearchParams(), f).toString();
}

/**
 * The query string for `GET /api/issues`. A search with the default sort is
 * ranked by relevance (the backend's hybrid ranking), which is a display-time
 * coercion and deliberately never written into the shared URL.
 */
export function apiQuery(f: IssueFilters): string {
  const sort = f.q && f.sort === "updated" ? "relevance" : f.sort;
  const params = new URLSearchParams({ state: f.state, sort, limit: "200" });
  if (f.q) params.set("q", f.q);
  if (f.type) params.set("type", f.type);
  if (f.assignee) params.set("assignee", f.assignee);
  if (f.label) params.set("label", f.label);
  return params.toString();
}

/** The address Copy link puts on the clipboard: `/issues` plus the non-default
 *  filters, without any `?issue=` or `?focus=` the current page may carry. */
export function shareUrl(origin: string, f: IssueFilters): string {
  const qs = writeFilters(new URLSearchParams(), f).toString();
  return `${origin}/issues${qs ? `?${qs}` : ""}`;
}
