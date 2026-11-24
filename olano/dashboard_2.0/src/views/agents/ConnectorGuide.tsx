import { BookOpen, CheckCircle2 } from "lucide-react";
import { useT } from "../../core/i18n";
import { STR } from "./strings";
import type { ConnectorGuide as ConnectorGuideModel } from "./connectorCatalog";

export function ConnectorGuide({ guide }: { guide: ConnectorGuideModel }) {
  const t = useT(STR);
  const chips = [t(guide.titleKey), t("catalog.guide.chipGuidedSetup"), t("catalog.guide.chipRestartRequired")];
  return (
    <aside className="connector-guide">
      <div className="connector-guide__head">
        <BookOpen size={16} strokeWidth={1.75} />
        <div>
          <strong>{t("guide.title")}</strong>
          <p>{t(guide.summaryKey)}</p>
        </div>
      </div>
      <div className="connector-guide__chips">
        {chips.map((chip) => (
          <span key={chip} className="badge">
            {chip}
          </span>
        ))}
      </div>
      <ol className="connector-guide__steps">
        {guide.stepKeys.map((stepKey, index) => (
          <li key={stepKey}>
            <CheckCircle2 size={15} strokeWidth={1.75} />
            <div>
              <strong>{t("catalog.guide.step", { n: String(index + 1) })}</strong>
              <p>{t(stepKey)}</p>
            </div>
          </li>
        ))}
      </ol>
    </aside>
  );
}
