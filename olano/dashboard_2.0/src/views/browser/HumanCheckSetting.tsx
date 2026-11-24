import { useEffect, useState } from "react";
import { api, ApiError } from "../../core/api";
import { useT } from "../../core/i18n";
import { useToast } from "../../ui/toast";
import { STR } from "./strings";

// "When a site asks for a person": this agent's override of
// config.yaml → browser.human_check (agent.json5 → browser.human_check and
// human_check_wait, olano/browser/human_check.py). Empty means the home
// setting. Readable by everyone who may chat with the agent; only an admin or
// one of its owners can change it (the server checks again and says can_edit).

interface Payload {
  human_check: string | null;
  human_check_wait: number | null;
  home: { human_check: string; human_check_wait: number };
  can_edit: boolean;
}

export function HumanCheckSetting({ agentName }: { agentName: string }) {
  const t = useT(STR);
  const toast = useToast();
  const [data, setData] = useState<Payload | null>(null);
  const [mode, setMode] = useState("");
  const [minutes, setMinutes] = useState("");
  const [busy, setBusy] = useState(false);
  const url = `/api/agents/${encodeURIComponent(agentName)}/browser/human-check`;

  const take = (p: Payload) => {
    setData(p);
    setMode(p.human_check || "");
    setMinutes(p.human_check_wait ? String(p.human_check_wait) : "");
  };

  useEffect(() => {
    setData(null);
    api
      .get<Payload>(url)
      .then(take)
      .catch(() => setData(null));
  }, [url]);

  if (!data) return null;
  const minutesOk = minutes.trim() === "" || (/^\d+$/.test(minutes.trim()) && +minutes >= 1 && +minutes <= 120);
  const dirty = mode !== (data.human_check || "") || minutes.trim() !== (data.human_check_wait ? String(data.human_check_wait) : "");
  const homeMode = t(`br.humanCheck.${data.home.human_check}`);

  async function save() {
    setBusy(true);
    try {
      take(
        await api.put<Payload>(url, {
          human_check: mode || null,
          human_check_wait: minutes.trim() ? parseInt(minutes, 10) : null,
        }),
      );
      toast.push(t("br.tab.check.saved"));
    } catch (err) {
      toast.push(err instanceof ApiError ? err.detail : String(err), "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="card browser-aa" data-testid="browser-human-check-setting">
      <h3 className="browser-aa-title">{t("br.tab.check")}</h3>
      <p className="muted browser-aa-help">{t("br.tab.check.hint")}</p>
      <div className="admin-form-row" style={{ gap: 16, marginTop: 10, alignItems: "flex-end", flexWrap: "wrap" }}>
        <label className="admin-field" style={{ flex: "1 1 240px" }}>
          <span className="admin-field-label">{t("br.tab.check")}</span>
          <select
            className="input"
            data-testid="agent-human-check"
            disabled={!data.can_edit || busy}
            value={mode}
            onChange={(e) => setMode(e.target.value)}
          >
            <option value="">{t("br.tab.check.inherit", { mode: homeMode })}</option>
            <option value="wait">{t("br.humanCheck.wait")}</option>
            <option value="hold">{t("br.humanCheck.hold")}</option>
            <option value="skip">{t("br.humanCheck.skip")}</option>
          </select>
        </label>
        <label className="admin-field" style={{ flex: "0 1 180px" }}>
          <span className="admin-field-label">
            {t("br.tab.check.minutes")}
            <span className="admin-field-hint">
              {t("br.tab.check.minutesHint", { minutes: String(data.home.human_check_wait) })}
            </span>
          </span>
          <input
            className="input"
            type="number"
            min={1}
            max={120}
            data-testid="agent-human-check-wait"
            disabled={!data.can_edit || busy || (mode || data.home.human_check) !== "wait"}
            value={minutes}
            placeholder={String(data.home.human_check_wait)}
            onChange={(e) => setMinutes(e.target.value)}
            style={!minutesOk ? { borderColor: "var(--danger)" } : undefined}
          />
        </label>
        {data.can_edit && (
          <button className="btn primary sm" disabled={!dirty || !minutesOk || busy} onClick={() => void save()}>
            {t("br.tab.check.save")}
          </button>
        )}
      </div>
    </section>
  );
}
