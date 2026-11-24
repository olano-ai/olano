import { useEffect, type ReactNode } from "react";
import { Navigate, useLocation } from "react-router-dom";
import { useCurrentAgent } from "./currentAgent";
import { canonicalTarget } from "./routeUrls";
import { useToast } from "../ui/toast";
import { useI18n } from "./i18n";

/**
 * Renders its children only on a canonical URL (core/routeUrls.ts); anything
 * else is replaced first, so no view ever runs its one-shot URL readers
 * against a legacy spelling or an agent that is not in the roster.
 *
 * The rewrite is a `replace`: the old URL was never a place of its own, so it
 * must not become a Back step either.
 */
export function RouteCanonical({ children }: { children: ReactNode }) {
  const location = useLocation();
  const { agent, agents, settled, fallback } = useCurrentAgent();
  const toast = useToast();
  const { t } = useI18n();
  const target = canonicalTarget(location.pathname, location.search, location.hash, {
    agent,
    known: settled ? (name) => agents.some((a) => a.name === name) : null,
    fallback,
  });
  const unknown = target?.unknownAgent;
  useEffect(() => {
    if (unknown) toast.push(t("toast.unknownAgent", { agent: unknown }), "error");
  }, [unknown, toast, t]);
  if (target) return <Navigate to={target.to} replace />;
  return <>{children}</>;
}
