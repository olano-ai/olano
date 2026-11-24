// The anchor contract that makes a setting linkable.
//
// Every indexable section and field in the dashboard carries
// data-setting-id="<dot.namespaced.id>". A data attribute rather than an id=
// because the ids are dot-namespaced (config.speech.ttsProvider) and dots need
// escaping in a CSS selector, while [data-setting-id="…"] reads the same from
// JS, from Playwright and from tests/e2e/route_checks.py.
//
// The same id is the SettingsEntry id in core/settingsIndex — one vocabulary,
// so a renamed field fails the drift guard instead of silently routing nowhere.

export const SETTING_ATTR = "data-setting-id";

/**
 * Spread onto the outermost element of a section or field:
 *
 *   <section className="card" {...settingAnchor(settingId)}>
 *
 * Passing undefined yields an empty object, so a call site that has no id
 * (a card rendered outside the index) stays untouched.
 */
export function settingAnchor(id?: string): Record<string, string> {
  return id ? { [SETTING_ATTR]: id } : {};
}

/** The selector for one setting, e.g. for querySelector or a Playwright locator. */
export function settingSelector(id: string): string {
  return `[${SETTING_ATTR}="${id}"]`;
}
