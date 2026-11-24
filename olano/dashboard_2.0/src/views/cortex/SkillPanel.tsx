import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../../core/api";
import { useT } from "../../core/i18n";
import { useToast } from "../../ui/toast";
import { errMessage } from "./helpers";
import { Drawer } from "./ui";
import { STR } from "./strings";

// Inline skill editor (a workspace .md). Save, Save & Approve (when backed by an
// approval-queue item), and a link to the Files view. Mirrors openSkillPanel().

function encodePath(p: string): string {
  return p
    .split("/")
    .map(encodeURIComponent)
    .join("/");
}

export function SkillPanel({
  path,
  agent,
  itemId,
  itemAgent,
  onClose,
  onApproved,
}: {
  path: string;
  agent: string;
  // When set, "Save & Approve" approves this approval-queue item after saving.
  itemId: string | null;
  itemAgent: string;
  onClose: () => void;
  onApproved: () => void;
}) {
  const toast = useToast();
  const t = useT(STR);
  const [content, setContent] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const fileName = path.split("/").pop() || path;
  const agentQs = agent ? `?agent=${encodeURIComponent(agent)}` : "";

  const load = useCallback(() => {
    api
      .get<{ content: string }>(`/api/files/${encodePath(path)}${agentQs}`)
      .then((d) => setContent(d.content || ""))
      .catch(() => setContent(t("skillPanel.notFound", { file: fileName })));
  }, [path, agentQs, fileName, t]);

  useEffect(load, [load]);

  async function save(): Promise<boolean> {
    if (content === null) return false;
    try {
      await api.put(`/api/files/${encodePath(path)}${agentQs}`, { content });
      return true;
    } catch (e) {
      toast.push(t("skillPanel.toast.saveFailed", { error: errMessage(e, t("skillPanel.toast.writeError")) }), "error");
      return false;
    }
  }

  async function saveAndClose() {
    setBusy(true);
    if (await save()) {
      toast.push(t("skillPanel.toast.saved"));
      onClose();
    }
    setBusy(false);
  }

  async function saveAndApprove() {
    if (!itemId) return;
    setBusy(true);
    if (!(await save())) {
      setBusy(false);
      return;
    }
    try {
      await api.post("/api/pulse/approve", { item_id: itemId, agent: itemAgent });
      toast.push(t("skillPanel.toast.savedApproved"));
      onApproved();
    } catch (e) {
      toast.push(errMessage(e, t("skillPanel.toast.approveFailed")), "error");
      setBusy(false);
    }
  }

  return (
    <Drawer
      title={fileName}
      sub={<span style={{ fontFamily: "var(--font-mono)" }}>{path}</span>}
      onClose={onClose}
      width={620}
      footer={
        <>
          <Link
            to={`/files?path=${encodeURIComponent(path)}&agent=${encodeURIComponent(agent)}`}
            className="btn"
            style={{ marginRight: "auto" }}
          >
            {t("skillPanel.openInFiles")}
          </Link>
          <button className="btn" onClick={onClose} disabled={busy}>
            {t("skillPanel.cancel")}
          </button>
          <button className="btn" onClick={() => void saveAndClose()} disabled={busy || content === null}>
            {t("skillPanel.save")}
          </button>
          {itemId ? (
            <button className="btn primary" onClick={() => void saveAndApprove()} disabled={busy || content === null}>
              {t("skillPanel.saveApprove")}
            </button>
          ) : null}
        </>
      }
    >
      {content === null ? (
        <div className="spinner" />
      ) : (
        <textarea
          className="input"
          spellCheck={false}
          value={content}
          onChange={(e) => setContent(e.target.value)}
          onKeyDown={(e) => {
            if ((e.metaKey || e.ctrlKey) && e.key === "s") {
              e.preventDefault();
              void saveAndClose();
            }
          }}
          style={{
            width: "100%",
            height: "100%",
            minHeight: 360,
            resize: "none",
            fontFamily: "var(--font-mono)",
            fontSize: 12.5,
            lineHeight: 1.6,
          }}
        />
      )}
    </Drawer>
  );
}
