/**
 * The Inbox's Issues: every issue on the deployment's tracker, not only the
 * ones an alert pointed at. Open ones first by default, the ones assigned to
 * you, or all of them including closed; tap one to read it, comment, hand it
 * to an agent or close it (IssuePage).
 */

import { IonChip, IonItem, IonLabel, IonList, IonNote, IonSkeletonText } from "@ionic/react";
import { checkmarkCircleOutline } from "ionicons/icons";
import { useState } from "react";

import { bareName, issueQuery, myPrincipal, principalKind, type IssueFilter } from "../../inbox/issues.ts";
import type { Issue } from "../../net/types.ts";
import { useResource } from "../../state/resources.ts";
import { currentApi, session } from "../../state/session.ts";
import { useStore } from "../../state/store.ts";
import { displayName } from "../../ui/Avatar.tsx";
import { Empty, ErrorNote } from "../../ui/Empty.tsx";
import { ago } from "../../ui/time.ts";
import { useAgents } from "../chats/AgentsPage.tsx";

const FILTERS: { id: IssueFilter; label: string }[] = [
  { id: "open", label: "Open" },
  { id: "mine", label: "Assigned to me" },
  { id: "all", label: "All" },
];

const EMPTY: Record<IssueFilter, { title: string; body: string }> = {
  open: { title: "No open issues", body: "When an agent or a person files an issue, it shows up here." },
  mine: { title: "Nothing assigned to you", body: "Issues assigned to you show up here, and you get an alert when one is." },
  all: { title: "No issues yet", body: "Agents and people file issues to track work; they show up here." },
};

export function useIssues(filter: IssueFilter) {
  const me = myPrincipal(useStore(session).me);
  const query = issueQuery(filter, me);
  return useResource<Issue[]>(
    `issues:list:${filter}`,
    async () => (await currentApi().issues(query.state, query.assignee)).issues ?? [],
    30000,
  );
}

export function IssuesList() {
  const [filter, setFilter] = useState<IssueFilter>("open");
  const [issues, reload] = useIssues(filter);
  const [agents] = useAgents();
  const me = myPrincipal(useStore(session).me);

  const name = (principal: string) => {
    if (!principal) return "Unassigned";
    if (principal === me) return "You";
    if (principalKind(principal) === "agent") {
      const agent = agents.data?.find((a) => a.name === bareName(principal));
      if (agent) return displayName(agent);
    }
    return bareName(principal);
  };

  return (
    <>
      <div className="olano-filters">
        <div className="olano-chips" role="group" aria-label="Show issues">
          {FILTERS.map((f) => (
            <IonChip
              key={f.id}
              outline={filter !== f.id}
              color={filter === f.id ? "primary" : undefined}
              aria-pressed={filter === f.id}
              onClick={() => setFilter(f.id)}
            >
              <IonLabel>{f.label}</IonLabel>
            </IonChip>
          ))}
        </div>
      </div>

      {issues.error && !issues.data && <ErrorNote message={issues.error} onRetry={() => void reload()} />}

      {!issues.data && !issues.error && (
        <IonList inset>
          {[0, 1, 2].map((i) => (
            <IonItem key={i}>
              <IonLabel>
                <IonSkeletonText animated style={{ width: "65%" }} />
                <IonSkeletonText animated style={{ width: "40%" }} />
              </IonLabel>
            </IonItem>
          ))}
        </IonList>
      )}

      {issues.data && issues.data.length === 0 && <Empty icon={checkmarkCircleOutline} {...EMPTY[filter]} />}

      {issues.data && issues.data.length > 0 && (
        <IonList inset>
          {issues.data.map((issue) => (
            <IonItem key={issue.id} button detail routerLink={`/inbox/issues/${issue.id}`} className={issue.state === "closed" ? "olano-read" : undefined}>
              <IonLabel className="ion-text-wrap">
                <h3>
                  <span className="olano-issue-number">#{issue.id}</span> {issue.title}
                </h3>
                <p className="olano-issue-row-meta">
                  {issue.state === "closed" && <span className="olano-issue-closed">Closed</span>}
                  <span className="olano-issue-type-tag">{issue.type}</span>
                  {name(issue.assignee)}
                  {issue.comment_count ? ` · ${issue.comment_count} ${issue.comment_count === 1 ? "comment" : "comments"}` : ""}
                </p>
              </IonLabel>
              <IonNote slot="end" className="olano-row-time">
                {ago(issue.updated_at)}
              </IonNote>
            </IonItem>
          ))}
        </IonList>
      )}
    </>
  );
}
