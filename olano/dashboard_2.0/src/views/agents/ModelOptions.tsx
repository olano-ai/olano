import { useT, type LocalDict } from "../../core/i18n";
import { modelValueLabel, type ModelOption } from "./catalog";

// Option list for the NATIVE model <select>s (agent form, agent detail,
// subagent modal, Config defaults). Native selects can't style individual
// options cross-browser, so the "Olano tiers first, visually set apart" rule
// is expressed the way native selects allow: an <optgroup> for the managed
// tiers ahead of one for everything else. The chat header's custom popover
// (ChatHeader.ModelPicker) renders the same split with real styling.

const STR: LocalDict = {
  en: {
    // Deployment defaults: what the alias follows and where to retarget it —
    // the target itself is appended per option ("currently Olano Turbo").
    "modelgroup.default": "Deployment defaults — set in Config → Default models",
    "modelalias.currently": "currently {target}",
    "modelgroup.olano": "Olano — managed, billed in credits",
    "modelgroup.byok": "Your providers",
    // One line per tier: what it's for, in the words someone choosing a plan
    // uses — never which model backs it (the gateway deliberately never says,
    // and what backs a tier changes). Kept short enough to survive a native
    // <option> on a phone, and free of dashes: in a one-slot surface the hint
    // follows the name after an em-dash, and a second one reads as a stutter.
    //
    // `auto` is unreachable today (the runtime stops offering the tier until
    // the gateway routes per request) — kept so re-enabling it is a one-line
    // server change, and it must stay TRUE of the routing we actually run.
    "modeltier.auto": "Picks the best tier for each task",
    "modeltier.turbo": "Fast and cheap for everyday tasks",
    "modeltier.pro": "More power for harder, multi-step work",
    "modeltier.max": "Maximum capability, at the highest cost",
  },
  zh: {
    "modelgroup.default": "部署默认 — 在 配置 → 默认模型 中设置",
    "modelalias.currently": "当前为 {target}",
    "modelgroup.olano": "Olano — 托管，按 olano 积分计费",
    "modelgroup.byok": "你的服务商",
    "modeltier.auto": "自动为每个任务挑选最合适的档位",
    "modeltier.turbo": "快速便宜，适合日常任务",
    "modeltier.pro": "算力更强，适合复杂的多步骤任务",
    "modeltier.max": "能力最强，费用也最高",
  },
  es: {
    "modelgroup.default": "Predeterminados del despliegue — se fijan en Configuración → Modelos predeterminados",
    "modelalias.currently": "actualmente {target}",
    "modelgroup.olano": "Olano — gestionado, facturado en créditos",
    "modelgroup.byok": "Tus proveedores",
    "modeltier.auto": "Elige el mejor nivel para cada tarea",
    "modeltier.turbo": "Rápido y barato para tareas del día a día",
    "modeltier.pro": "Más potencia para tareas complejas de varios pasos",
    "modeltier.max": "Máxima capacidad, al mayor coste",
  },
};

// Translation key for a tier's one-liner, or null when we have no copy for it
// (a tier the cloud catalog added ahead of this build). Never build the key
// blind: useT falls back to the global dict and then to the key itself, so an
// unknown tier would render the literal string "modeltier.<x>" in the menu.
const HINTED_TIERS = new Set(["auto", "turbo", "pro", "max"]);

export function olanoTierHintKey(option: ModelOption): string | null {
  // `option.tier` is already normalized by catalog.ts (a renamed tier's old id
  // arrives as the tier it serves), so this set carries real tiers only.
  return option.tier && HINTED_TIERS.has(option.tier) ? `modeltier.${option.tier}` : null;
}

// "currently Olano Turbo" — an alias option's hint names its live target (the
// product name for an olano tier, the raw id otherwise), so the picker answers
// "what will this actually run" without a click.
function aliasHint(t: (k: string, vars?: Record<string, string>) => string, o: ModelOption): string | null {
  if (!o.alias) return null;
  return t("modelalias.currently", { target: o.target ? modelValueLabel(o.target) : "—" });
}

/** `(option) => "Olano Pro — more power for…"`, for surfaces with one text slot. */
export function useModelOptionText() {
  const t = useT(STR);
  return (o: ModelOption) => {
    const alias = aliasHint(t, o);
    if (alias) return `${o.label} — ${alias}`;
    const key = olanoTierHintKey(o);
    return key ? `${o.label} — ${lowerFirst(t(key))}` : o.label;
  };
}

/** `(option) => "Picks the right tier…"`, for surfaces with a second line. */
export function useModelOptionHint() {
  const t = useT(STR);
  return (o: ModelOption) => {
    const alias = aliasHint(t, o);
    if (alias) return alias;
    const key = olanoTierHintKey(o);
    return key ? t(key) : null;
  };
}

// The hints are written as standalone sentences (their own line in the chat
// picker); after an em-dash inside one option label they read better lowercase.
// Latin-only: leave a first character that has no case (CJK) untouched.
function lowerFirst(s: string): string {
  const first = s.charAt(0);
  return first.toLowerCase() === first ? s : first.toLowerCase() + s.slice(1);
}

export function ModelOptionGroups({ options }: { options: ModelOption[] }) {
  const t = useT(STR);
  const text = useModelOptionText();
  const aliases = options.filter((o) => o.alias);
  const olano = options.filter((o) => o.olano && !o.alias);
  const byok = options.filter((o) => !o.olano && !o.alias);
  if (aliases.length === 0 && olano.length === 0) {
    return (
      <>
        {byok.map((m) => (
          <option key={m.value} value={m.value}>
            {m.label}
          </option>
        ))}
      </>
    );
  }
  return (
    <>
      {aliases.length > 0 && (
        <optgroup label={t("modelgroup.default")}>
          {aliases.map((m) => (
            <option key={m.value} value={m.value}>
              {text(m)}
            </option>
          ))}
        </optgroup>
      )}
      {olano.length > 0 && (
        <optgroup label={t("modelgroup.olano")}>
          {olano.map((m) => (
            <option key={m.value} value={m.value}>
              {text(m)}
            </option>
          ))}
        </optgroup>
      )}
      <optgroup label={t("modelgroup.byok")}>
        {byok.map((m) => (
          <option key={m.value} value={m.value}>
            {m.label}
          </option>
        ))}
      </optgroup>
    </>
  );
}
