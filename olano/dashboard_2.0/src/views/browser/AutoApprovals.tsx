import { useCallback, useEffect, useRef, useState } from "react";
import { Lock, Pencil, Plus, ShieldCheck, Trash2 } from "lucide-react";
import { api, ApiError } from "../../core/api";
import { useT } from "../../core/i18n";
import { useToast } from "../../ui/toast";
import {
  ACTION_KINDS,
  type ActionKind,
  type AutoApproveRule,
  cleanRules,
  type DraftProblem,
  draftProblem,
  EXAMPLES,
  normalizeSite,
  ruleProblem,
  sameRules,
  siteIsValid,
} from "./autoApprove";
import { STR } from "./strings";

// "Actions that don't ask": the agent's narrow exceptions to the browser's
// always-ask approval gate (agent.json5 → browser.auto_approve, enforced by
// olano/browser/auto_approve.py). Everyone who may chat with the agent can read
// them; only an admin or one of the agent's owners can change them (the server
// checks it again on save, and reports `can_edit`).
//
// Written for someone who has never seen the feature: every rule reads back as
// a plain sentence while it is edited, an empty list offers one-click examples,
// and each choice names the buttons it covers.

interface Payload {
  rules: AutoApproveRule[];
  can_edit: boolean;
}

const MAX_RULES = 20;
type T = (key: string, vars?: Record<string, string>) => string;

function copy(rules: AutoApproveRule[]): AutoApproveRule[] {
  return rules.map((r) => ({ ...r, actions: [...r.actions] }));
}

/** "On linkedin.com, the agent may send messages and posts without asking you, only while you chat with it." */
function summarize(rule: AutoApproveRule, t: T): string {
  const site = normalizeSite(rule.site);
  if (!site || !siteIsValid(site)) return t("br.aa.summary.new");
  const acts = ACTION_KINDS.filter((k) => rule.actions.includes(k)).map((k) => t(`br.aa.act.${k}`));
  if (acts.length === 0) return t("br.aa.summary.noAction", { site });
  const actions =
    acts.length === 1
      ? acts[0]
      : acts.slice(0, -1).join(t("br.aa.sep")) + t("br.aa.sepLast") + acts[acts.length - 1];
  return t("br.aa.summary", {
    site,
    actions,
    when: t(rule.in_background ? "br.aa.summary.always" : "br.aa.summary.chat"),
  });
}

function problemText(problem: DraftProblem, t: T): string {
  return "site" in problem ? t(problem.key, { site: problem.site }) : t(problem.key);
}

export function AutoApprovals({ agentName }: { agentName: string }) {
  const t: T = useT(STR);
  const toast = useToast();
  const [saved, setSaved] = useState<AutoApproveRule[] | null>(null);
  const [draft, setDraft] = useState<AutoApproveRule[]>([]);
  const [canEdit, setCanEdit] = useState(false);
  const [busy, setBusy] = useState(false);
  const [focusIndex, setFocusIndex] = useState<number | null>(null);
  // Saved rules show as their one-line sentence; a new or edited one is open.
  const [open, setOpen] = useState<Set<number>>(new Set());
  const url = `/api/agents/${encodeURIComponent(agentName)}/browser/auto-approvals`;

  const take = useCallback((p: Payload) => {
    setSaved(p.rules);
    setDraft(copy(p.rules));
    setCanEdit(!!p.can_edit);
    setOpen(new Set());
  }, []);

  useEffect(() => {
    api
      .get<Payload>(url)
      .then(take)
      .catch((err) => toast.push(err instanceof ApiError ? err.detail : String(err), "error"));
  }, [url, take, toast]);

  if (!saved) return null;
  const dirty = !sameRules(draft, saved);
  const problem = draftProblem(draft);
  const full = draft.length >= MAX_RULES;
  const taken = new Set(draft.map((r) => normalizeSite(r.site)));
  const examples = EXAMPLES.filter((e) => !taken.has(e.rule.site));

  const update = (i: number, patch: Partial<AutoApproveRule>) =>
    setDraft((rules) => rules.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  const toggleKind = (i: number, kind: ActionKind) =>
    setDraft((rules) =>
      rules.map((r, j) =>
        j !== i
          ? r
          : { ...r, actions: r.actions.includes(kind) ? r.actions.filter((k) => k !== kind) : [...r.actions, kind] },
      ),
    );
  const add = (rule: AutoApproveRule) => {
    setFocusIndex(rule.site ? null : draft.length);
    setOpen((o) => new Set(o).add(draft.length));
    setDraft((rules) => [...rules, { ...rule, actions: [...rule.actions] }]);
  };
  const remove = (i: number) => {
    setFocusIndex(null);
    // Indices after the removed rule shift down by one.
    setOpen((o) => new Set([...o].filter((j) => j !== i).map((j) => (j > i ? j - 1 : j))));
    setDraft((rules) => rules.filter((_, j) => j !== i));
  };

  async function save() {
    setBusy(true);
    try {
      take(await api.put<Payload>(url, { rules: cleanRules(draft) }));
      toast.push(t("br.aa.saved"));
    } catch (err) {
      toast.push(err instanceof ApiError ? err.detail : String(err), "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="card browser-aa" data-testid="browser-auto-approvals">
      <h3 className="browser-aa-title">
        <ShieldCheck size={16} aria-hidden="true" />
        {t("br.aa.title")}
      </h3>
      <p className="browser-aa-lead">{t("br.aa.lead")}</p>
      <p className="browser-aa-never">
        <Lock size={13} aria-hidden="true" />
        <span>{t("br.aa.never")}</span>
      </p>
      <details className="browser-aa-how">
        <summary>{t("br.aa.how")}</summary>
        <ul>
          {[1, 2, 3, 4, 5].map((n) => (
            <li key={n}>{t(`br.aa.how.${n}`)}</li>
          ))}
        </ul>
      </details>

      {!canEdit ? (
        <>
          {saved.length === 0 ? (
            <p className="browser-aa-empty">{t("br.aa.empty")}</p>
          ) : (
            <ul className="browser-aa-readonly" data-testid="browser-aa-readonly">
              {saved.map((r) => (
                <li key={r.site}>{summarize(r, t)}</li>
              ))}
            </ul>
          )}
          <p className="muted browser-aa-help" style={{ marginTop: 10 }}>
            {t("br.aa.readOnly")}
          </p>
        </>
      ) : (
        <>
          {draft.length === 0 && <p className="browser-aa-empty">{t("br.aa.empty")}</p>}

          <div className="browser-aa-rules">
            {draft.map((rule, i) => {
              const ruleIssue = ruleProblem(rule, draft.slice(0, i));
              // A rule with a problem stays open, so the problem is in sight.
              return open.has(i) || ruleIssue ? (
                <RuleEditor
                  key={i}
                  index={i}
                  rule={rule}
                  problem={ruleIssue}
                  autoFocus={focusIndex === i}
                  t={t}
                  onSite={(site) => update(i, { site })}
                  onToggle={(kind) => toggleKind(i, kind)}
                  onBackground={(on) => update(i, { in_background: on })}
                  onDone={
                    ruleIssue
                      ? undefined
                      : () =>
                          setOpen((o) => {
                            const next = new Set(o);
                            next.delete(i);
                            return next;
                          })
                  }
                  onRemove={() => remove(i)}
                />
              ) : (
                <div className="browser-aa-rule is-folded" key={i} data-testid="browser-aa-rule">
                  <p className="browser-aa-summary">{summarize(rule, t)}</p>
                  <span className="browser-aa-rule-buttons">
                    <button
                      type="button"
                      className="btn ghost sm"
                      aria-label={t("br.aa.editFor", { site: normalizeSite(rule.site) })}
                      onClick={() => setOpen((o) => new Set(o).add(i))}
                    >
                      <Pencil size={13} aria-hidden="true" /> {t("br.aa.edit")}
                    </button>
                    <button
                      type="button"
                      className="btn ghost sm"
                      aria-label={t("br.aa.removeFor", { site: normalizeSite(rule.site) })}
                      onClick={() => remove(i)}
                    >
                      <Trash2 size={13} aria-hidden="true" /> {t("br.aa.remove")}
                    </button>
                  </span>
                </div>
              );
            })}
          </div>

          <div className="browser-aa-adders">
            {examples.length > 0 && draft.length === 0 && (
              <p className="muted browser-aa-help">{t("br.aa.examples")}</p>
            )}
            <div className="browser-aa-adder-row">
              {draft.length === 0 &&
                examples.map((e) => (
                  <button
                    key={e.id}
                    type="button"
                    className="btn sm browser-aa-example"
                    data-testid={`browser-aa-example-${e.id}`}
                    disabled={full}
                    onClick={() => add(e.rule)}
                  >
                    <Plus size={13} aria-hidden="true" /> {t(`br.aa.example.${e.id}`)}
                  </button>
                ))}
              <button
                type="button"
                className="btn sm"
                data-testid="browser-aa-add"
                disabled={full}
                onClick={() => add({ site: "", actions: ["send"], in_background: false })}
              >
                <Plus size={13} aria-hidden="true" /> {t("br.aa.add")}
              </button>
            </div>
            {full && <p className="muted browser-aa-help">{t("br.aa.limit")}</p>}
          </div>

          {dirty && (
            <div className="browser-aa-savebar" role="region" aria-label={t("br.aa.unsaved")}>
              <span className="browser-aa-unsaved">{t("br.aa.unsaved")}</span>
              {problem && (
                <span className="browser-aa-problem" role="status">
                  {problemText(problem, t)}
                </span>
              )}
              <span className="browser-aa-savebar-actions">
                <button
                  type="button"
                  className="btn ghost sm"
                  disabled={busy}
                  onClick={() => {
                    setFocusIndex(null);
                    setOpen(new Set());
                    setDraft(copy(saved));
                  }}
                >
                  {t("br.aa.discard")}
                </button>
                <button
                  type="button"
                  className="btn primary sm"
                  data-testid="browser-aa-save"
                  disabled={busy || !!problem}
                  onClick={() => void save()}
                >
                  {t("br.aa.save")}
                </button>
              </span>
            </div>
          )}
        </>
      )}
    </section>
  );
}

function RuleEditor({
  index,
  rule,
  problem,
  autoFocus,
  t,
  onSite,
  onToggle,
  onBackground,
  onDone,
  onRemove,
}: {
  index: number;
  rule: AutoApproveRule;
  problem: DraftProblem | null;
  autoFocus: boolean;
  t: T;
  onSite: (site: string) => void;
  onToggle: (kind: ActionKind) => void;
  onBackground: (on: boolean) => void;
  onDone?: () => void;
  onRemove: () => void;
}) {
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (autoFocus) input.current?.focus();
  }, [autoFocus]);

  const id = `aa-${index}`;
  const site = normalizeSite(rule.site);
  // A half-typed site is not an error yet: complain only once it is clearly wrong.
  const siteError =
    problem && (problem.key === "br.aa.badSite" || problem.key === "br.aa.dupSite") && rule.site.trim()
      ? problemText(problem, t)
      : null;
  const siteNote = siteError
    ? null
    : site
      ? site.startsWith("*.")
        ? t("br.aa.siteCoversSub", { site: site.slice(2) })
        : t("br.aa.siteCovers", { site })
      : t("br.aa.siteHelp");

  return (
    <div className="browser-aa-rule" data-testid="browser-aa-rule">
      <div className="browser-aa-rule-head">
        <p className="browser-aa-summary" aria-live="polite">
          {summarize(rule, t)}
        </p>
        <span className="browser-aa-rule-buttons">
          {onDone && (
            <button type="button" className="btn ghost sm" onClick={onDone}>
              {t("br.aa.done")}
            </button>
          )}
          <button
            type="button"
            className="btn ghost sm"
            aria-label={t("br.aa.removeFor", { site: site || "…" })}
            onClick={onRemove}
          >
            <Trash2 size={13} aria-hidden="true" /> {t("br.aa.remove")}
          </button>
        </span>
      </div>

      <div className="browser-aa-field">
        <label htmlFor={`${id}-site`}>{t("br.aa.site")}</label>
        <input
          ref={input}
          id={`${id}-site`}
          className="input"
          value={rule.site}
          placeholder={t("br.aa.sitePlaceholder")}
          autoComplete="off"
          autoCapitalize="none"
          spellCheck={false}
          inputMode="url"
          aria-invalid={siteError ? true : undefined}
          aria-describedby={`${id}-site-note`}
          onChange={(e) => onSite(e.target.value)}
          onBlur={() => {
            // Show the person what will be saved: a pasted link becomes its site.
            if (site && site !== rule.site) onSite(site);
          }}
        />
        <p id={`${id}-site-note`} className={siteError ? "browser-aa-error" : "muted browser-aa-help"}>
          {siteError ?? siteNote}
        </p>
      </div>

      <fieldset className="browser-aa-field">
        <legend>{t("br.aa.actions")}</legend>
        <div className="browser-aa-choices">
          {ACTION_KINDS.map((kind) => {
            const on = rule.actions.includes(kind);
            return (
              <label key={kind} className={`browser-aa-choice${on ? " is-on" : ""}`}>
                <input
                  type="checkbox"
                  checked={on}
                  aria-labelledby={`${id}-${kind}`}
                  aria-describedby={`${id}-${kind}-eg`}
                  onChange={() => onToggle(kind)}
                />
                <span>
                  <span id={`${id}-${kind}`} className="browser-aa-choice-title">
                    {t(`br.aa.kind.${kind}`)}
                  </span>
                  <span id={`${id}-${kind}-eg`} className="browser-aa-choice-eg">
                    {t(`br.aa.kind.${kind}.eg`)}
                  </span>
                </span>
              </label>
            );
          })}
        </div>
        <p className={rule.actions.length ? "muted browser-aa-help" : "browser-aa-error"}>
          {rule.actions.length ? t("br.aa.actionsHelp") : t("br.aa.needAction", { site: site || "…" })}
        </p>
      </fieldset>

      <fieldset className="browser-aa-field">
        <legend>{t("br.aa.when")}</legend>
        <div className="browser-aa-choices">
          {([false, true] as const).map((bg) => {
            const key = bg ? "always" : "chat";
            return (
              <label key={key} className={`browser-aa-choice${rule.in_background === bg ? " is-on" : ""}`}>
                <input
                  type="radio"
                  name={`${id}-when`}
                  checked={rule.in_background === bg}
                  aria-labelledby={`${id}-when-${key}`}
                  aria-describedby={`${id}-when-${key}-eg`}
                  onChange={() => onBackground(bg)}
                />
                <span>
                  <span id={`${id}-when-${key}`} className="browser-aa-choice-title">
                    {t(`br.aa.when.${key}`)}
                  </span>
                  <span id={`${id}-when-${key}-eg`} className="browser-aa-choice-eg">
                    {t(`br.aa.when.${key}.eg`)}
                  </span>
                </span>
              </label>
            );
          })}
        </div>
      </fieldset>
    </div>
  );
}
