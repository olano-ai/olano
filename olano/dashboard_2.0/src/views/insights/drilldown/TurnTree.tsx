import { Bot, ChevronRight, Wrench, Users } from "lucide-react";
import { useT } from "../../../core/i18n";
import { STR } from "../strings";
import { fmtUsd, prettyModel } from "../helpers";
import type { TcTreeNode } from "./drilldownTypes";

// Recursive renderer for a thread's execution tree: turn → AI call → tool /
// subagent, with the cost at each node. Clicking an AI-call node opens its
// raw prompt/response in the inspector.

const ICON: Record<string, typeof Bot> = {
  turn: ChevronRight,
  ai_call: Bot,
  tool: Wrench,
  subagent: Users,
};

function nodeLabel(n: TcTreeNode): string {
  if (n.kind === "ai_call" && n.model) return prettyModel(n.model);
  return n.label;
}

export function TreeNode({
  node,
  depth,
  onOpenCall,
}: {
  node: TcTreeNode;
  depth: number;
  onOpenCall: (ns: string, seq: number) => void;
}) {
  const t = useT(STR);
  const Icon = ICON[node.kind] ?? ChevronRight;
  const clickable = node.kind === "ai_call" && node.seq != null;
  const isStructural = node.kind === "turn";
  return (
    <div>
      <div
        className="row lc-tree-node"
        onClick={clickable ? () => onOpenCall(node.ns ?? "", node.seq as number) : undefined}
        style={{
          paddingLeft: 8 + depth * 18,
          cursor: clickable ? "pointer" : "default",
          gap: 8,
          fontWeight: isStructural ? 600 : 400,
        }}
      >
        <span style={{ display: "inline-flex", color: "var(--text-3)" }} aria-hidden>
          <Icon size={13} strokeWidth={1.75} />
        </span>
        <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {nodeLabel(node)}
          {node.kind === "tool" && <span className="muted" style={{ marginLeft: 6, fontSize: 11 }}>{t("drill.toolBadge")}</span>}
          {node.kind === "subagent" && <span className="muted" style={{ marginLeft: 6, fontSize: 11 }}>{t("drill.subagentBadge")}</span>}
        </span>
        {node.usd > 0 && (
          <span className="tabular" style={{ fontSize: 11.5, minWidth: 58, textAlign: "right" }}>
            {fmtUsd(node.usd)}
          </span>
        )}
      </div>
      {node.children.map((c, i) => (
        <TreeNode key={`${c.kind}-${c.seq ?? c.ns ?? i}`} node={c} depth={depth + 1} onOpenCall={onOpenCall} />
      ))}
    </div>
  );
}

export function TurnTree({
  nodes,
  onOpenCall,
}: {
  nodes: TcTreeNode[];
  onOpenCall: (ns: string, seq: number) => void;
}) {
  const t = useT(STR);
  if (!nodes.length) return <p className="muted">{t("drill.noExecutionTree")}</p>;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 1 }}>
      {nodes.map((n, i) => (
        <TreeNode key={`turn-${i}`} node={n} depth={0} onOpenCall={onOpenCall} />
      ))}
    </div>
  );
}
